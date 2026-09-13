"""Testes do orquestrador mensal (scripts/rodar_pipelines_mensais.py).
SQLite temporário via CRM_DB_PATH, setado antes de qualquer import dos
módulos do CRM. Nunca dispara subprocess de verdade -- só a lógica de
"está pendente este mês?" e a marcação de sucesso/tentativa.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

_TMP = Path(tempfile.gettempdir()) / "scorpions_test_rodar_pipelines_mensais.db"
for _s in ("", "-wal", "-shm"):
    Path(str(_TMP) + _s).unlink(missing_ok=True)
os.environ["CRM_DB_PATH"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import automation  # noqa: E402  (cria o schema no import)
import rodar_pipelines_mensais as rpm  # noqa: E402


class PendenteTest(unittest.TestCase):
    def setUp(self):
        with automation.conectar() as con:
            con.execute("DELETE FROM estado_automacao")

    def test_pendente_por_padrao_sem_historico(self):
        self.assertTrue(rpm.pendente("cnes", date(2026, 9, 20)))

    def test_nao_pendente_apos_sucesso_no_mesmo_mes(self):
        automation.salvar_config("pipeline_cnes_ultimo_mes_sucesso", "2026-09")
        self.assertFalse(rpm.pendente("cnes", date(2026, 9, 25)))

    def test_pendente_de_novo_em_mes_diferente_do_ultimo_sucesso(self):
        automation.salvar_config("pipeline_cnes_ultimo_mes_sucesso", "2026-08")
        self.assertTrue(rpm.pendente("cnes", date(2026, 9, 20)))

    def test_nao_pendente_se_ja_tentou_hoje_mesmo_sem_sucesso(self):
        automation.salvar_config("pipeline_receita_ultima_tentativa", "2026-09-20")
        self.assertFalse(rpm.pendente("receita", date(2026, 9, 20)))

    def test_pendente_de_novo_no_dia_seguinte_apos_falha(self):
        automation.salvar_config("pipeline_receita_ultima_tentativa", "2026-09-20")
        self.assertTrue(rpm.pendente("receita", date(2026, 9, 21)))

    def test_pipelines_sao_independentes(self):
        automation.salvar_config("pipeline_cnes_ultimo_mes_sucesso", "2026-09")
        self.assertFalse(rpm.pendente("cnes", date(2026, 9, 20)))
        self.assertTrue(rpm.pendente("obras_sp", date(2026, 9, 20)))
        self.assertTrue(rpm.pendente("receita", date(2026, 9, 20)))


class RodarPipelineTest(unittest.TestCase):
    def setUp(self):
        with automation.conectar() as con:
            con.execute("DELETE FROM estado_automacao")

    @patch("rodar_pipelines_mensais.subprocess.run")
    def test_sucesso_registra_mes_e_libera_o_pipeline(self, mock_run):
        mock_run.return_value.returncode = 0
        resultado = rpm.rodar_pipeline("cnes", ["echo", "ok"], date(2026, 9, 20))
        self.assertEqual(resultado["status"], "sucesso")
        self.assertEqual(automation.ler_config("pipeline_cnes_ultimo_mes_sucesso"), "2026-09")
        self.assertFalse(rpm.pendente("cnes", date(2026, 9, 21)))

    @patch("rodar_pipelines_mensais.subprocess.run")
    def test_falha_nao_registra_sucesso_mas_registra_tentativa(self, mock_run):
        mock_run.return_value.returncode = 1
        resultado = rpm.rodar_pipeline("receita", ["echo", "falhou"], date(2026, 9, 20))
        self.assertEqual(resultado["status"], "falha")
        self.assertEqual(automation.ler_config("pipeline_receita_ultimo_mes_sucesso"), "")
        self.assertFalse(rpm.pendente("receita", date(2026, 9, 20)))
        self.assertTrue(rpm.pendente("receita", date(2026, 9, 21)))


class MainTest(unittest.TestCase):
    def setUp(self):
        with automation.conectar() as con:
            con.execute("DELETE FROM estado_automacao")

    def test_main_antes_do_dia_minimo_via_argv(self):
        with patch("rodar_pipelines_mensais.date") as mock_date, \
             patch("rodar_pipelines_mensais.subprocess.run") as mock_run, \
             patch.object(sys, "argv", ["rodar_pipelines_mensais.py"]):
            mock_date.today.return_value = date(2026, 9, 5)
            codigo = rpm.main()
        mock_run.assert_not_called()
        self.assertEqual(codigo, 0)

    def test_main_forcar_ignora_dia_minimo_e_roda_tudo(self):
        with patch("rodar_pipelines_mensais.date") as mock_date, \
             patch("rodar_pipelines_mensais.subprocess.run") as mock_run, \
             patch.object(sys, "argv", ["rodar_pipelines_mensais.py", "--forcar"]):
            mock_date.today.return_value = date(2026, 9, 5)
            mock_run.return_value.returncode = 0
            codigo = rpm.main()
        self.assertEqual(mock_run.call_count, 3)
        self.assertEqual(codigo, 0)

    def test_main_retorna_1_se_algum_pipeline_falhar(self):
        with patch("rodar_pipelines_mensais.date") as mock_date, \
             patch("rodar_pipelines_mensais.subprocess.run") as mock_run, \
             patch.object(sys, "argv", ["rodar_pipelines_mensais.py", "--forcar"]):
            mock_date.today.return_value = date(2026, 9, 20)
            mock_run.return_value.returncode = 1
            codigo = rpm.main()
        self.assertEqual(codigo, 1)


if __name__ == "__main__":
    unittest.main()
