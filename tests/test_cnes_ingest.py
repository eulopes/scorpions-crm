"""Testes da injeção Fase 3 (candidatos do CNES -> CRM). Usa um SQLite
temporário via CRM_DB_PATH, setado antes de qualquer import dos módulos do CRM.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TMP = Path(tempfile.gettempdir()) / "scorpions_test_cnes_ingest.db"
for _s in ("", "-wal", "-shm"):
    Path(str(_TMP) + _s).unlink(missing_ok=True)
os.environ["CRM_DB_PATH"] = str(_TMP)

import automation  # noqa: E402  (cria o schema no import)
from change_detection import FONTE_CNES  # noqa: E402
from cnes_ingest import aplicar_supressoes, injetar_candidatos  # noqa: E402
from company_history import conectar  # noqa: E402
from sales_signals import listar_signals_ativos  # noqa: E402


def _candidato(cnpj: str, *, tipo: str = "NOVO_ESTABELECIMENTO", nome: str = "CLINICA TESTE") -> dict:
    lead = {
        "place_id": f"cnes:000{cnpj[:4]}", "cnpj": cnpj, "nome_empresa": nome,
        "razao_social": f"{nome} LTDA", "decisor": "", "nicho": "Clínica especializada",
        "cnae_fiscal_descricao": "Clínica especializada", "endereco": "AV PAULISTA 1000",
        "cidade": "SP", "telefone": "1130000000", "site": "", "email": "",
        "status": "Novos Leads", "origem": FONTE_CNES,
        "observacoes": "teste", "segmento_icp": "Clínicas, Hospitais & Laboratórios",
        "servicos_recomendados": "Redundância de rede",
    }
    mudanca = {
        "type": "novo_estabelecimento_saude" if tipo == "NOVO_ESTABELECIMENTO" else "endereco_alterado_saude",
        "field": "co_cnes", "before": None, "after": {"local": "SP", "tipo_unidade": "Clínica especializada"},
        "tipo_unidade": "Clínica especializada", "days_between": None, "source": FONTE_CNES,
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
        self.assertEqual(leads[0]["origem"], FONTE_CNES)
        self.assertIsNotNone(leads[0]["opportunity_score"])

        with conectar() as con:
            lead_id = con.execute("SELECT id FROM leads LIMIT 1").fetchone()["id"]
        sinais = listar_signals_ativos(lead_id)
        self.assertEqual(sinais[0]["signal_type"], "NEW_BRANCH")
        self.assertEqual(sinais[0]["source"], FONTE_CNES)

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
                ("EMPRESA EXISTENTE", "Clínica", "Google Places", "Contato / Qualificação",
                 "33000167000101", agora, agora),
            )
        injetar_candidatos({"candidatos": [_candidato("33000167000101")], "supressoes": []})
        leads = self._leads()
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["nome_empresa"], "EMPRESA EXISTENTE")  # não sobrescreve
        with conectar() as con:
            lead_id = con.execute("SELECT id FROM leads LIMIT 1").fetchone()["id"]
        self.assertEqual(listar_signals_ativos(lead_id)[0]["signal_type"], "NEW_BRANCH")

    def test_endereco_alterado_gera_sinal_address_change(self):
        injetar_candidatos({
            "candidatos": [_candidato("33000167000101", tipo="MUDANCA_ENDERECO")], "supressoes": []
        })
        with conectar() as con:
            lead_id = con.execute("SELECT id FROM leads LIMIT 1").fetchone()["id"]
        self.assertEqual(listar_signals_ativos(lead_id)[0]["signal_type"], "ADDRESS_CHANGE")

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
