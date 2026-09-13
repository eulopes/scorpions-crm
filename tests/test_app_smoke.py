"""Smoke test das rotas principais do CRM com banco isolado.

O teste usa ``streamlit.testing`` e desativa somente o subprocesso do worker.
Nenhum dado do banco local de desenvolvimento ou de producao e acessado.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bcrypt
from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]


class AppSmokeTest(unittest.TestCase):
    def test_login_and_every_director_page_render(self) -> None:
        password = "teste123"
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "smoke.db"
            test_env = {
                "CRM_DB_PATH": str(database),
                "OBRAS_DUMP_DB": str(Path(temp_dir) / "obras_smoke.duckdb"),
                "SCORPIONS_DISABLE_WORKER": "1",
                "AUTH_USERS_JSON": json.dumps({"teste": password_hash}),
                # Só para configurar_google_places() retornar True e o botão
                # "Consultar avaliações no Google" aparecer no perfil da
                # empresa -- o teste nunca clica nele, então nenhuma chamada
                # de rede de verdade acontece com esta chave falsa.
                "GOOGLE_PLACES_API_KEY": "chave-de-teste-fake",
            }
            with patch.dict(os.environ, test_env, clear=False):
                app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
                app.run(timeout=30)
                self.assertEqual([], list(app.exception))

                self._seed_business_data(database, password_hash)
                self._seed_scored_lead(database)

                app.text_input(key="login_usuario").set_value("teste")
                app.text_input(key="login_senha").set_value(password)
                app.button(key="login_entrar").click()
                app.run(timeout=30)
                self.assertEqual([], list(app.exception))

                routes = {
                    "Vis\u00e3o geral": "Dashboard",
                    "Pipeline": "Pipeline",
                    "Prospec\u00e7\u00e3o": "Prospec\u00e7\u00e3o",
                    "Empresas": "Clientes",
                    "Radar": "Radar",
                    "Nova empresa": "Nova empresa",
                    "Automa\u00e7\u00e3o": "Automa\u00e7\u00e3o",
                    "Equipe": "Equipe",
                }
                for route, expected_title in routes.items():
                    with self.subTest(route=route):
                        app.radio(key="navegacao_principal").set_value(route)
                        app.run(timeout=30)
                        self.assertEqual([], list(app.exception))
                        rendered_markdown = "\n".join(
                            str(element.value) for element in app.markdown
                        )
                        self.assertIn(expected_title, rendered_markdown)

                        if route == "Radar":
                            # Cobre o caminho "com dados" (tabela, filtros e
                            # detalhe de uma oportunidade) -- o único lead
                            # seedado com opportunity_score é o "Empresa Radar
                            # Teste" inserido por _seed_scored_lead.
                            self.assertGreaterEqual(len(app.dataframe), 1)
                            detalhe = app.selectbox(key="radar_detalhe_selecionado")
                            self.assertGreaterEqual(len(detalhe.options), 1)
                            self.assertIn("Por que essa empresa", rendered_markdown)
                            self.assertIn("Por que agora", rendered_markdown)
                            self.assertIn("Próxima melhor ação", rendered_markdown)
                            # Perfil consolidado da empresa (Receita) -- seedado
                            # com um company_snapshot em _seed_scored_lead. O
                            # título do expander não é capturado por
                            # app.markdown (é o rótulo do próprio widget), por
                            # isso a verificação é sobre o conteúdo dentro dele.
                            self.assertIn("MICRO EMPRESA", rendered_markdown)
                            self.assertIn("R$ 50.000,00", rendered_markdown)
                            self.assertIn("FULANA DE TAL", rendered_markdown)
                            # Enriquecimento sob demanda (Google Places): o
                            # lead tem um place_id "de verdade" (sem prefixo)
                            # e a chave está configurada -- o botão deve
                            # aparecer. O teste nunca clica nele (sem chamada
                            # de rede real).
                            botoes_google = [
                                b for b in app.button
                                if "Consultar avaliações no Google" in (b.label or "")
                            ]
                            self.assertEqual(len(botoes_google), 1)

    @staticmethod
    def _seed_scored_lead(database: Path) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with sqlite3.connect(database) as connection:
            lead_id = connection.execute(
                """
                INSERT INTO leads (
                    place_id, nome_empresa, nicho, cidade, telefone, email, site, cnpj,
                    status, origem, segmento_icp, servicos_recomendados, status_receita,
                    fit_score, intent_score, timing_score, data_confidence_score,
                    opportunity_score, opportunity_level, opportunity_reason, why_now,
                    opportunity_updated_at, opportunity_delta, last_signal_at,
                    criado_em, atualizado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    # Sem prefixo (":") de propósito -- simula um place_id
                    # real do Google Places, para exercitar o botão
                    # "Consultar avaliações no Google" no perfil da empresa.
                    "ChIJRadarTesteSemPrefixo123", "Empresa Radar Teste", "Transportadora", "Campinas, SP",
                    "(11) 90000-0000", "radar@example.com", "https://radar.example.com", "11222333000181",
                    "Novos Leads", "Teste isolado", "Galpões Logísticos & Indústrias", "CFTV perimetral",
                    "ATIVA",
                    90, 85, 80, 95,
                    88, "Alta", "Esta empresa apresenta características fortemente compatíveis com o perfil.",
                    "Priorizar abordagem comercial nos próximos dias.",
                    now, 12, now, now, now,
                ),
            ).lastrowid
            # Cobre o painel "Perfil da empresa (Receita)" -- dados que
            # company_history.create_company_snapshot já grava, mas que só
            # aparecem em tela a partir de montar_perfil_empresa().
            connection.execute(
                """
                INSERT INTO company_snapshots (
                    lead_id, source, captured_at, data_hash, porte, capital_social,
                    cnae_principal, situacao_cadastral, data_situacao_cadastral,
                    natureza_juridica, qsa_hash, qtde_socios, socios_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lead_id, "Receita Federal (BrasilAPI)", now, "hash-teste",
                    "MICRO EMPRESA", 50000.0,
                    "5211-7/01 · Transporte rodoviário de carga", "ATIVA", "2020-01-01",
                    "206-2 - Sociedade Empresária Limitada", "hash-qsa", 1,
                    json.dumps([{"nome": "FULANA DE TAL", "qualificacao": "49 - Sócio-Administrador"}]),
                ),
            )

    @staticmethod
    def _seed_business_data(database: Path, password_hash: str) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        yesterday = (now - timedelta(days=1)).date().isoformat()
        tomorrow = (now + timedelta(days=1)).date().isoformat()

        with sqlite3.connect(database) as connection:
            team_id = connection.execute(
                "INSERT INTO equipes (nome, criado_em) VALUES (?, ?)",
                ("Comercial", now.isoformat()),
            ).lastrowid
            seller_id = connection.execute(
                """
                INSERT INTO usuarios
                    (username, senha_hash, nome, email, nivel, equipe_id, status, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, 'ativo', ?)
                """,
                (
                    "vendedor",
                    password_hash,
                    "Pessoa Vendedora",
                    "vendedor@example.com",
                    "vendedor",
                    team_id,
                    now.isoformat(),
                ),
            ).lastrowid

            leads = (
                ("Empresa Alfa", "Tecnologia", "Campinas / SP", "Novos Leads", 91, 0, yesterday),
                ("Empresa Beta", "Saude", "Sao Paulo / SP", "Contato / Qualifica\u00e7\u00e3o", 78, 0, tomorrow),
                ("Empresa Gama", "Industria", "Sorocaba / SP", "Vistoria T\u00e9cnica / Diagn\u00f3stico", 72, 18000, None),
                ("Empresa Delta", "Servicos", "Jundiai / SP", "Proposta Enviada", 88, 42000, yesterday),
                ("Empresa Epsilon", "Varejo", "Santos / SP", "Fechado / Contrato", 86, 30000, None),
                ("Empresa Zeta", "Logistica", "Guarulhos / SP", "Descartado", 64, 0, None),
            )
            for index, (name, niche, city, status, score, value, next_contact) in enumerate(leads):
                connection.execute(
                    """
                    INSERT INTO leads (
                        place_id, nome_empresa, nicho, cidade, telefone, email,
                        status, origem, pontuacao, segmento_icp, valor_proposta,
                        proximo_contato, criado_em, atualizado_em,
                        responsavel_usuario_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"smoke:{index}",
                        name,
                        niche,
                        city,
                        "(11) 4000-0000",
                        f"contato{index}@example.com",
                        status,
                        "Teste isolado",
                        score,
                        "ICP de teste",
                        value,
                        next_contact,
                        now.isoformat(),
                        now.isoformat(),
                        seller_id,
                    ),
                )

            connection.execute(
                """
                INSERT INTO campanhas (
                    nome, nicho, localizacao, fonte, limite_diario, horario,
                    ativa, executando, criada_em, atualizada_em
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                """,
                (
                    "Campanha smoke",
                    "Tecnologia",
                    "Campinas, SP",
                    "Automatica (recomendada)",
                    20,
                    "08:00",
                    now.isoformat(),
                    now.isoformat(),
                ),
            )


if __name__ == "__main__":
    unittest.main()
