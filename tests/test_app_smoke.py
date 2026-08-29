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
                "SCORPIONS_DISABLE_WORKER": "1",
                "AUTH_USERS_JSON": json.dumps({"teste": password_hash}),
            }
            with patch.dict(os.environ, test_env, clear=False):
                app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
                app.run(timeout=30)
                self.assertEqual([], list(app.exception))

                self._seed_business_data(database, password_hash)

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
