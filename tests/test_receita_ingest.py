"""Testes da injeção Fase 1 (candidatos do dump -> CRM). Usa um SQLite
temporário via CRM_DB_PATH, setado antes de qualquer import dos módulos do CRM.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

_TMP = Path(tempfile.gettempdir()) / "scorpions_test_receita_ingest.db"
for _s in ("", "-wal", "-shm"):
    Path(str(_TMP) + _s).unlink(missing_ok=True)
os.environ["CRM_DB_PATH"] = str(_TMP)

import automation  # noqa: E402  (cria o schema no import)
from company_history import conectar  # noqa: E402
from receita_ingest import aplicar_supressoes, injetar_candidatos  # noqa: E402
from sales_signals import listar_signals_ativos  # noqa: E402


def _candidato(cnpj: str, *, tipo: str = "NOVA_FILIAL", nome: str = "CD SOROCABA") -> dict:
    lead = {
        "place_id": f"receita:{cnpj}", "cnpj": cnpj, "nome_empresa": nome,
        "razao_social": "", "decisor": "", "nicho": "Armazéns gerais",
        "cnae_fiscal_descricao": "Armazéns gerais", "endereco": "ROD RAPOSO TAVARES KM 90",
        "cidade": "Sorocaba, SP", "telefone": "1533000000", "site": "", "email": "",
        "status": "Novos Leads", "status_receita": "ATIVA", "origem": "Receita Federal (Dados Abertos)",
        "observacoes": "teste", "segmento_icp": "Galpões Logísticos & Indústrias",
        "servicos_recomendados": "CFTV perimetral",
    }
    mudanca = {
        "type": "nova_filial_receita" if tipo == "NOVA_FILIAL" else "novo_estabelecimento_receita",
        "field": "cnpj", "before": None, "after": "Sorocaba, SP", "days_between": 30,
        "source": "Receita Federal (Dados Abertos)",
    }
    return {"evento_tipo": tipo, "lead": lead, "mudanca": mudanca}


class InjecaoTest(unittest.TestCase):
    def setUp(self):
        with conectar() as con:
            con.execute("DELETE FROM sales_signals")
            con.execute("DELETE FROM leads")

    def _leads(self):
        with conectar() as con:
            return con.execute(
                "SELECT cnpj, nome_empresa, origem, opportunity_score FROM leads ORDER BY id"
            ).fetchall()

    def test_candidato_vira_lead_com_sinal_e_score(self):
        res = injetar_candidatos({"candidatos": [_candidato("33000167000101")], "supressoes": []})
        self.assertEqual(res["candidatos"], 1)
        self.assertEqual(res["sinais_novos"], 1)
        self.assertEqual(res["avaliados"], 1)

        leads = self._leads()
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["cnpj"], "33000167000101")
        self.assertEqual(leads[0]["origem"], "Receita Federal (Dados Abertos)")
        self.assertIsNotNone(leads[0]["opportunity_score"])

        with conectar() as con:
            lead_id = con.execute("SELECT id FROM leads LIMIT 1").fetchone()["id"]
        sinais = listar_signals_ativos(lead_id)
        self.assertEqual(sinais[0]["signal_type"], "NEW_BRANCH")

    def test_reexecucao_nao_duplica_lead_nem_sinal(self):
        carga = {"candidatos": [_candidato("33000167000101")], "supressoes": []}
        injetar_candidatos(carga)
        res2 = injetar_candidatos(carga)
        self.assertEqual(len(self._leads()), 1)
        self.assertEqual(res2["sinais_novos"], 0)  # dedup por evidência ativa

    def test_cnpj_ja_na_base_recebe_o_sinal_sem_criar_duplicata(self):
        agora = automation.iso_utc()
        with conectar() as con:
            con.execute(
                "INSERT INTO leads (nome_empresa, nicho, origem, status, cnpj, criado_em, atualizado_em) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("EMPRESA EXISTENTE", "Transportadora", "Bacen", "Contato / Qualificação",
                 "33000167000101", agora, agora),
            )
        injetar_candidatos({"candidatos": [_candidato("33000167000101")], "supressoes": []})
        leads = self._leads()
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["nome_empresa"], "EMPRESA EXISTENTE")  # não sobrescreve
        with conectar() as con:
            lead_id = con.execute("SELECT id FROM leads LIMIT 1").fetchone()["id"]
        self.assertEqual(listar_signals_ativos(lead_id)[0]["signal_type"], "NEW_BRANCH")

    def test_supressao_desativa_sinais_do_lead_existente(self):
        injetar_candidatos({"candidatos": [_candidato("33000167000101")], "supressoes": []})
        with conectar() as con:
            lead_id = con.execute("SELECT id FROM leads LIMIT 1").fetchone()["id"]
        self.assertTrue(listar_signals_ativos(lead_id))

        n = aplicar_supressoes(["33.000.167/0001-01"])
        self.assertEqual(n, 1)
        self.assertEqual(listar_signals_ativos(lead_id), [])

    def test_supressao_de_cnpj_inexistente_e_no_op(self):
        self.assertEqual(aplicar_supressoes(["11444777000161"]), 0)


if __name__ == "__main__":
    unittest.main()
