"""Testes de ponta a ponta das principais acoes do CRM (nao so navegacao).

Usa streamlit.testing.v1.AppTest com banco isolado (arquivo temporario) —
nenhum dado do banco local de desenvolvimento ou de producao e tocado.

Cada metodo de teste cria só UMA instância de AppTest e reaproveita ela pra
todos os passos: criar múltiplas instâncias de AppTest.from_file() dentro do
mesmo processo Python se mostrou pouco confiável neste ambiente (alguma
combinação de cache_resource do Streamlit com o reuso do módulo entre
chamadas — a escrita no banco às vezes "grudava" na primeira instância
criada no processo). Uma instância por teste, com vários passos sequenciais
dentro dela, é o padrão que se mostrou estável.

Observacao: os dialogos de criacao (Nova campanha, Novo alvo, Novo usuario,
Nova equipe) sao abertos chamando a funcao decorada com @st.dialog dentro do
mesmo `if` que le o clique do botao do cabecalho. Isso funciona perfeitamente
num navegador real (o Streamlit mantem o dialogo aberto entre reruns), mas o
AppTest nao repete a chamada em reruns seguintes ao interagir com widgets
dentro do dialogo — entao esses formularios especificos nao sao testaveis por
essa via e precisam de verificacao manual no navegador.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import bcrypt
from streamlit.testing.v1 import AppTest

from access_control import paginas_visiveis

ROOT = Path(__file__).resolve().parents[1]
SENHA = "teste123"


def _hash(senha: str = SENHA) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


def _login(at: AppTest, usuario: str, senha: str = SENHA) -> None:
    at.text_input(key="login_usuario").set_value(usuario)
    at.text_input(key="login_senha").set_value(senha)
    at.button(key="login_entrar").click()
    at.run(timeout=30)


def _logout(at: AppTest) -> None:
    # Não apaga "navegacao_principal": é a key de um widget (o radio da
    # sidebar) que ainda está na árvore renderizada nesse momento — apagar a
    # key de um widget ativo quebra a reconciliação interna do AppTest no
    # próximo .run(). Só precisa derrubar o estado de autenticação mesmo.
    for chave in (
        "autenticado", "usuario_logado", "usuario_id", "nivel_usuario",
        "equipe_id_usuario", "nome_usuario",
    ):
        try:
            del at.session_state[chave]
        except KeyError:
            pass
    at.run(timeout=30)


class AcoesReaisTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.database = Path(self._tmp.name) / "acoes.db"

        test_env = {
            "CRM_DB_PATH": str(self.database),
            "SCORPIONS_DISABLE_WORKER": "1",
            "AUTH_USERS_JSON": "{}",
        }
        patcher = patch.dict(os.environ, test_env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        self.at.run(timeout=30)

        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with sqlite3.connect(self.database) as conexao:
            conexao.execute(
                """INSERT INTO usuarios (username, senha_hash, nome, nivel, status, criado_em)
                   VALUES (?, ?, ?, 'diretor', 'ativo', ?)""",
                ("diretor_e2e", _hash(), "Diretor E2E", now),
            )
            equipe_id = conexao.execute(
                "INSERT INTO equipes (nome, criado_em) VALUES (?, ?)", ("Comercial", now)
            ).lastrowid
            self.vendedor_id = conexao.execute(
                """INSERT INTO usuarios
                   (username, senha_hash, nome, email, nivel, equipe_id, status, criado_em)
                   VALUES (?, ?, ?, ?, 'vendedor', ?, 'ativo', ?)""",
                ("vendedor_e2e", _hash(), "Vendedor E2E", "v@example.com", equipe_id, now),
            ).lastrowid
            self.lead_excluir_id = conexao.execute(
                """INSERT INTO leads
                   (place_id, nome_empresa, nicho, cidade, status, origem,
                    pontuacao, criado_em, atualizado_em, responsavel_usuario_id)
                   VALUES (?, ?, ?, ?, 'Novos Leads', 'Teste isolado', 70, ?, ?, ?)""",
                ("e2e:1", "Empresa Para Excluir", "Tecnologia", "Campinas / SP", now, now, self.vendedor_id),
            ).lastrowid
            self.lead_mover_id = conexao.execute(
                """INSERT INTO leads
                   (place_id, nome_empresa, nicho, cidade, status, origem,
                    pontuacao, criado_em, atualizado_em, responsavel_usuario_id)
                   VALUES (?, ?, ?, ?, 'Novos Leads', 'Teste isolado', 80, ?, ?, ?)""",
                ("e2e:2", "Empresa Para Mover", "Saude", "Sorocaba / SP", now, now, self.vendedor_id),
            ).lastrowid

    def test_acoes_principais_do_diretor(self) -> None:
        at = self.at
        _login(at, "diretor_e2e")
        self.assertEqual([], list(at.exception))

        with self.subTest(acao="criar lead manualmente"):
            at.radio(key="navegacao_principal").set_value("Nova empresa")
            at.run(timeout=30)
            self.assertEqual([], list(at.exception))

            at.text_input(key="novo_empresa_nome_0").set_value("Empresa Criada No Teste")
            at.text_input(key="novo_empresa_nicho_0").set_value("Tecnologia")
            botao_cadastrar = next(b for b in at.button if b.label == "Cadastrar empresa")
            botao_cadastrar.click()
            at.run(timeout=30)
            self.assertEqual([], list(at.exception))

            with sqlite3.connect(self.database) as conexao:
                total = conexao.execute(
                    "SELECT COUNT(*) FROM leads WHERE nome_empresa = ?", ("Empresa Criada No Teste",)
                ).fetchone()[0]
            self.assertEqual(1, total, "lead cadastrado pelo formulario nao apareceu no banco")

        with self.subTest(acao="mover lead no pipeline"):
            at.radio(key="navegacao_principal").set_value("Pipeline")
            at.run(timeout=30)
            self.assertEqual([], list(at.exception))

            at.selectbox(key=f"move_{self.lead_mover_id}").set_value("Contato / Qualificação")
            at.run(timeout=30)
            self.assertEqual([], list(at.exception))

            with sqlite3.connect(self.database) as conexao:
                status = conexao.execute(
                    "SELECT status FROM leads WHERE id = ?", (self.lead_mover_id,)
                ).fetchone()[0]
            self.assertEqual("Contato / Qualificação", status)

        with self.subTest(acao="excluir lead com confirmacao"):
            at.radio(key="navegacao_principal").set_value("Empresas")
            at.run(timeout=30)
            self.assertEqual([], list(at.exception))

            opcao_alvo = next(
                o for o in at.selectbox(key="excluir_lead_select").options
                if o.startswith("Empresa Para Excluir")
            )
            at.selectbox(key="excluir_lead_select").set_value(opcao_alvo)
            at.run(timeout=30)
            self.assertEqual([], list(at.exception))

            at.button(key="abrir_confirmacao_exclusao").click()
            at.run(timeout=30)
            self.assertEqual([], list(at.exception))

            at.text_input(key="digitado_exclusao_lead").set_value("Empresa Para Excluir")
            at.button(key="confirmar_exclusao_lead_botao").click()
            at.run(timeout=30)
            self.assertEqual([], list(at.exception))

            with sqlite3.connect(self.database) as conexao:
                total = conexao.execute(
                    "SELECT COUNT(*) FROM leads WHERE id = ?", (self.lead_excluir_id,)
                ).fetchone()[0]
            self.assertEqual(0, total, "lead nao foi removido do banco apos a confirmacao")

        with self.subTest(acao="ativar/desativar usuario"):
            at.radio(key="navegacao_principal").set_value("Equipe")
            at.run(timeout=30)
            self.assertEqual([], list(at.exception))

            at.button(key=f"toggle_usuario_{self.vendedor_id}").click()
            at.run(timeout=30)
            self.assertEqual([], list(at.exception))

            with sqlite3.connect(self.database) as conexao:
                status = conexao.execute(
                    "SELECT status FROM usuarios WHERE id = ?", (self.vendedor_id,)
                ).fetchone()[0]
            self.assertEqual("inativo", status, "botao Desativar nao mudou o status no banco")

    def test_rbac_paginas_visiveis_por_nivel(self) -> None:
        at = self.at
        niveis = ["vendedor", "supervisor", "gerente", "diretor"]
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with sqlite3.connect(self.database) as conexao:
            for nivel in niveis:
                if nivel in ("vendedor", "diretor"):
                    continue  # ja existem (criados no setUp)
                conexao.execute(
                    """INSERT INTO usuarios (username, senha_hash, nome, nivel, status, criado_em)
                       VALUES (?, ?, ?, ?, 'ativo', ?)""",
                    (f"{nivel}_e2e", _hash(), nivel.capitalize(), nivel, now),
                )

        usuarios_por_nivel = {
            "vendedor": "vendedor_e2e",
            "supervisor": "supervisor_e2e",
            "gerente": "gerente_e2e",
            "diretor": "diretor_e2e",
        }

        for nivel in niveis:
            with self.subTest(nivel=nivel):
                _login(at, usuarios_por_nivel[nivel])
                self.assertEqual([], list(at.exception))

                opcoes_menu = list(at.radio(key="navegacao_principal").options)
                paginas_esperadas = paginas_visiveis(nivel)
                self.assertEqual(len(paginas_esperadas), len(opcoes_menu))
                self.assertEqual(
                    "Automação" in paginas_esperadas,
                    any("Automação" in opcao for opcao in opcoes_menu),
                )
                self.assertEqual(
                    "Equipe" in paginas_esperadas,
                    any("Equipe" in opcao for opcao in opcoes_menu),
                )

                _logout(at)


if __name__ == "__main__":
    unittest.main()
