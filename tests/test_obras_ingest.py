"""Testes da injeção Fase 2 (alvarás casados -> CRM). SQLite temporário via
CRM_DB_PATH, setado antes de qualquer import dos módulos do CRM.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

_TMP = Path(tempfile.gettempdir()) / "scorpions_test_obras_ingest.db"
for _s in ("", "-wal", "-shm"):
    Path(str(_TMP) + _s).unlink(missing_ok=True)
os.environ["CRM_DB_PATH"] = str(_TMP)

import automation  # noqa: E402  (cria o schema no import)
from company_history import conectar  # noqa: E402
from obras_ingest import injetar_alvaras  # noqa: E402
from sales_signals import listar_signals_ativos  # noqa: E402


def _alvara(**over):
    base = {
        "id_alvara": "2026/EXEC/001", "tipo": "execucao", "area_construida": 1800.0,
        "bairro": "Itaim Bibi", "municipio_nome": "São Paulo",
        "data_emissao": date(2026, 8, 1),
    }
    base.update(over)
    return base


class InjecaoAlvaraTest(unittest.TestCase):
    def setUp(self):
        with conectar() as con:
            con.execute("DELETE FROM sales_signals")
            con.execute("DELETE FROM leads")

    def _cria_lead(self, **over):
        agora = automation.iso_utc()
        campos = {"nome_empresa": "Empresa Existente", "nicho": "Transportadora",
                  "origem": "Bacen", "status": "Contato / Qualificação",
                  "cnpj": None, "criado_em": agora, "atualizado_em": agora}
        campos.update(over)
        with conectar() as con:
            cur = con.execute(
                "INSERT INTO leads (nome_empresa, nicho, origem, status, cnpj, criado_em, atualizado_em) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (campos["nome_empresa"], campos["nicho"], campos["origem"], campos["status"],
                 campos["cnpj"], campos["criado_em"], campos["atualizado_em"]),
            )
            return int(cur.lastrowid)

    def test_em_lead_gera_sinal_obra_ativa_e_score(self):
        lead_id = self._cria_lead()
        resultado = {
            "em_lead": [{"alvara": _alvara(), "lead_id": lead_id, "confianca": "alta", "por": "nome"}],
            "novo_de_receita": [], "sem_ocupante": [],
        }
        relatorio = injetar_alvaras(resultado)
        self.assertEqual(relatorio["sinais_em_lead_existente"], 1)
        sinais = listar_signals_ativos(lead_id)
        self.assertEqual(sinais[0]["signal_type"], "OBRA_ATIVA")
        self.assertGreaterEqual(sinais[0]["signal_strength"], 80)  # 1800m2
        self.assertGreaterEqual(sinais[0]["confidence"], 70)  # confianca alta
        with conectar() as con:
            score = con.execute("SELECT opportunity_score FROM leads WHERE id=?", (lead_id,)).fetchone()[0]
        self.assertIsNotNone(score)

    def test_confianca_media_reduz_a_confianca_do_sinal(self):
        lead_id = self._cria_lead()
        alta = {"em_lead": [{"alvara": _alvara(id_alvara="A1"), "lead_id": lead_id, "confianca": "alta"}],
                "novo_de_receita": [], "sem_ocupante": []}
        injetar_alvaras(alta)
        conf_alta = listar_signals_ativos(lead_id)[0]["confidence"]

        with conectar() as con:
            con.execute("DELETE FROM sales_signals")
        media = {"em_lead": [{"alvara": _alvara(id_alvara="A2"), "lead_id": lead_id, "confianca": "media"}],
                 "novo_de_receita": [], "sem_ocupante": []}
        injetar_alvaras(media)
        conf_media = listar_signals_ativos(lead_id)[0]["confidence"]
        self.assertGreater(conf_alta, conf_media)

    def test_novo_de_receita_cria_lead_e_sinal(self):
        est = {
            "cnpj": "33000167000101", "nome_fantasia": "CD CAMPINAS",
            "logradouro": "ROD RAPOSO TAVARES", "numero": "90", "uf": "SP",
            "municipio_nome": "CAMPINAS", "cnae_descricao": "Armazéns gerais",
        }
        resultado = {"em_lead": [],
                     "novo_de_receita": [{"alvara": _alvara(), "estabelecimento": est, "confianca": "media"}],
                     "sem_ocupante": []}
        relatorio = injetar_alvaras(resultado)
        self.assertEqual(relatorio["novo_de_receita"], 1)
        self.assertEqual(relatorio["sinais_em_lead_novo"], 1)
        with conectar() as con:
            lead = con.execute(
                "SELECT nome_empresa, segmento_icp, opportunity_score FROM leads WHERE cnpj=?",
                ("33000167000101",),
            ).fetchone()
        self.assertEqual(lead["nome_empresa"], "CD CAMPINAS")
        self.assertEqual(lead["segmento_icp"], "Galpões Logísticos & Indústrias")
        self.assertIsNotNone(lead["opportunity_score"])

    def test_estabelecimento_com_cnpj_invalido_e_ignorado_sem_quebrar(self):
        est = {"cnpj": "00000000000000", "nome_fantasia": "X"}
        resultado = {"em_lead": [],
                     "novo_de_receita": [{"alvara": _alvara(), "estabelecimento": est, "confianca": "media"}],
                     "sem_ocupante": []}
        relatorio = injetar_alvaras(resultado)
        self.assertEqual(relatorio["novo_de_receita"], 0)

    def test_sem_ocupante_nao_grava_nada_e_volta_como_pendente(self):
        resultado = {"em_lead": [], "novo_de_receita": [],
                     "sem_ocupante": [{"alvara": _alvara(), "motivo": "sem empresa no endereço"}]}
        relatorio = injetar_alvaras(resultado)
        self.assertEqual(relatorio["sem_ocupante"], 1)
        self.assertEqual(len(relatorio["pendentes"]), 1)
        with conectar() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM leads").fetchone()[0], 0)

    def test_reexecucao_do_mesmo_alvara_nao_duplica_sinal(self):
        lead_id = self._cria_lead()
        resultado = {"em_lead": [{"alvara": _alvara(), "lead_id": lead_id, "confianca": "alta"}],
                     "novo_de_receita": [], "sem_ocupante": []}
        injetar_alvaras(resultado)
        relatorio2 = injetar_alvaras(resultado)
        self.assertEqual(relatorio2["sinais_em_lead_existente"], 0)
        self.assertEqual(len(listar_signals_ativos(lead_id)), 1)


if __name__ == "__main__":
    unittest.main()
