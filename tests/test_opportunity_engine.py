"""Testes do motor de Opportunity Intelligence (company_history, change_detection,
sales_signals, opportunity_engine) -- usa um banco SQLite temporário, isolado
do banco de desenvolvimento, definido via CRM_DB_PATH antes de qualquer import
dos módulos do motor (eles resolvem DB_PATH na hora do import).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_ARQUIVO_TEMP = Path(tempfile.gettempdir()) / "scorpions_test_opportunity.db"
for _sufixo in ("", "-wal", "-shm"):
    Path(str(_ARQUIVO_TEMP) + _sufixo).unlink(missing_ok=True)
os.environ["CRM_DB_PATH"] = str(_ARQUIVO_TEMP)

import automation  # noqa: E402  (import após setar CRM_DB_PATH, de propósito)
import change_detection  # noqa: E402
import sales_signals  # noqa: E402
from company_history import (  # noqa: E402
    calcular_data_hash,
    compare_snapshots,
    create_company_snapshot,
    get_latest_snapshot,
    get_previous_snapshot,
    should_create_snapshot,
)
from opportunity_engine import (  # noqa: E402
    calculate_data_confidence,
    calculate_fit_score,
    calculate_intent_score,
    calculate_opportunity_score,
    calculate_timing_score,
    evaluate_opportunity,
    _nivel_por_pontuacao,
)
from sales_signals import calculate_signal_decay, derive_signals_from_changes, registrar_signal


def _inserir_lead(**overrides):
    campos = {
        "nome_empresa": "Empresa Teste", "nicho": "Transportadora", "origem": "Teste",
        "status": "Novos Leads", "cidade": "São Paulo, SP", "telefone": "", "email": "",
        "site": "", "cnpj": "", "segmento_icp": "", "servicos_recomendados": "", "status_receita": "",
    }
    campos.update(overrides)
    agora = automation.iso_utc()
    with automation.conectar() as conexao:
        cursor = conexao.execute(
            """
            INSERT INTO leads (
                nome_empresa, nicho, origem, status, cidade, telefone, email, site,
                cnpj, segmento_icp, servicos_recomendados, status_receita, criado_em, atualizado_em
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campos["nome_empresa"], campos["nicho"], campos["origem"], campos["status"],
                campos["cidade"], campos["telefone"], campos["email"], campos["site"],
                campos["cnpj"], campos["segmento_icp"], campos["servicos_recomendados"],
                campos["status_receita"], agora, agora,
            ),
        )
        return int(cursor.lastrowid)


class MigracaoTest(unittest.TestCase):
    def test_migracao_e_idempotente(self):
        automation.iniciar_banco_automacao()
        automation.iniciar_banco_automacao()
        with automation.conectar() as conexao:
            colunas = {l["name"] for l in conexao.execute("PRAGMA table_info(leads)").fetchall()}
            tabelas = {
                l["name"] for l in conexao.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for coluna in ("fit_score", "intent_score", "timing_score", "data_confidence_score",
                       "opportunity_score", "opportunity_level", "why_now", "opportunity_delta"):
            self.assertIn(coluna, colunas)
        for tabela in ("company_snapshots", "sales_signals", "opportunity_score_history", "opportunity_outcomes"):
            self.assertIn(tabela, tabelas)


class ChangeDetectionTest(unittest.TestCase):
    def test_sem_snapshot_anterior_gera_empresa_adicionada(self):
        mudancas = change_detection.comparar_snapshots(None, {"lead_id": 1})
        self.assertEqual(len(mudancas), 1)
        self.assertEqual(mudancas[0]["type"], "empresa_adicionada")

    def test_sem_mudanca_nao_gera_eventos(self):
        base = {
            "captured_at": "2026-01-01T00:00:00+00:00", "address": "Rua A", "phone": "111",
            "website": "site.com", "business_status": "ATIVA", "reviews_count": 10, "rating": 4.0,
            "units_detected": 1,
        }
        self.assertEqual(change_detection.comparar_snapshots(base, dict(base, captured_at="2026-02-01T00:00:00+00:00")), [])

    def test_crescimento_de_avaliacoes_calcula_percentual(self):
        anterior = {"captured_at": "2026-01-01T00:00:00+00:00", "reviews_count": 200}
        atual = {"captured_at": "2026-03-02T00:00:00+00:00", "reviews_count": 300}
        mudancas = change_detection.comparar_snapshots(anterior, atual)
        self.assertEqual(len(mudancas), 1)
        self.assertEqual(mudancas[0]["type"], "crescimento_avaliacoes")
        self.assertEqual(mudancas[0]["percentage_change"], 50.0)
        self.assertEqual(mudancas[0]["days_between"], 60)

    def test_nova_unidade_detectada(self):
        anterior = {"captured_at": "2026-01-01T00:00:00+00:00", "units_detected": 1}
        atual = {"captured_at": "2026-01-10T00:00:00+00:00", "units_detected": 2}
        mudancas = change_detection.comparar_snapshots(anterior, atual)
        self.assertEqual(mudancas[0]["type"], "possivel_nova_unidade")


class CompanyHistoryTest(unittest.TestCase):
    def setUp(self):
        self.lead_id = _inserir_lead(nome_empresa="Snapshot Co")

    def test_primeiro_snapshot_sempre_criado(self):
        self.assertTrue(should_create_snapshot(self.lead_id, {"company_name": "X"}))

    def test_snapshot_identico_nao_duplica(self):
        dados = {"company_name": "X", "reviews_count": 10}
        primeiro_id = create_company_snapshot(self.lead_id, "Teste", dados)
        self.assertIsNotNone(primeiro_id)
        segundo_id = create_company_snapshot(self.lead_id, "Teste", dados)
        self.assertIsNone(segundo_id)

    def test_get_previous_snapshot_com_dois_snapshots(self):
        create_company_snapshot(self.lead_id, "Teste", {"reviews_count": 10})
        create_company_snapshot(self.lead_id, "Teste", {"reviews_count": 20})
        anterior = get_previous_snapshot(self.lead_id)
        atual = get_latest_snapshot(self.lead_id)
        self.assertEqual(anterior["reviews_count"], 10)
        self.assertEqual(atual["reviews_count"], 20)

    def test_hash_estavel_para_mesmos_dados(self):
        dados = {"company_name": "X", "rating": 4.567}
        self.assertEqual(calcular_data_hash(dados), calcular_data_hash(dict(dados)))


class SalesSignalsTest(unittest.TestCase):
    def setUp(self):
        self.lead_id = _inserir_lead(nome_empresa="Signal Co")

    def test_decay_no_dia_zero_e_maximo(self):
        self.assertAlmostEqual(calculate_signal_decay(sales_signals.MULTI_UNIT, 0), 1.0)

    def test_decay_diminui_com_o_tempo(self):
        recente = calculate_signal_decay(sales_signals.REVIEWS_GROWTH, 5)
        antigo = calculate_signal_decay(sales_signals.REVIEWS_GROWTH, 200)
        self.assertGreater(recente, antigo)
        self.assertGreater(antigo, 0)

    def test_mudanca_sem_mapeamento_nao_gera_sinal(self):
        resultado = registrar_signal(self.lead_id, {"type": "mudanca_cadastral", "field": "x", "before": None, "after": None, "days_between": 1}, fonte="Teste")
        self.assertIsNone(resultado)

    def test_sinal_identico_ativo_nao_duplica(self):
        mudanca = {"type": "possivel_nova_unidade", "field": "units_detected", "before": 1, "after": 2, "days_between": 5}
        primeiro = derive_signals_from_changes(self.lead_id, [mudanca], fonte="Teste")
        segundo = derive_signals_from_changes(self.lead_id, [mudanca], fonte="Teste")
        self.assertEqual(len(primeiro), 1)
        self.assertEqual(len(segundo), 0)


class OpportunityEngineTest(unittest.TestCase):
    def test_fit_score_maximo_e_minimo(self):
        lead_classificado = {
            "segmento_icp": "Galpões Logísticos & Indústrias",
            "servicos_recomendados": "CFTV", "cidade": "São Paulo, SP",
        }
        self.assertEqual(calculate_fit_score(lead_classificado, {"units_detected": 3}), 100)
        self.assertEqual(calculate_fit_score({}, None), 0)

    def test_data_confidence_maximo_e_minimo(self):
        lead_completo = {
            "cnpj": "11222333000181", "status_receita": "ATIVA", "telefone": "119999",
            "email": "a@a.com", "site": "a.com",
        }
        snapshots = [{"source": "A", "captured_at": automation.iso_utc()}, {"source": "B", "captured_at": automation.iso_utc()}]
        self.assertEqual(calculate_data_confidence(lead_completo, snapshots), 100)
        self.assertEqual(calculate_data_confidence({}, []), 0)

    def test_intent_e_timing_zero_sem_sinais(self):
        self.assertEqual(calculate_intent_score([]), 0)
        self.assertEqual(calculate_timing_score([]), 0)

    def test_intent_cresce_com_forca_e_confianca(self):
        sinal_forte = {"signal_type": sales_signals.MULTI_UNIT, "signal_strength": 90, "confidence": 90, "detected_at": automation.iso_utc()}
        sinal_fraco = {"signal_type": sales_signals.NEW_PHONE, "signal_strength": 20, "confidence": 40, "detected_at": automation.iso_utc()}
        self.assertGreater(calculate_intent_score([sinal_forte]), calculate_intent_score([sinal_fraco]))

    def test_opportunity_score_usa_pesos_documentados(self):
        esperado = round(0.30 * 100 + 0.30 * 0 + 0.25 * 0 + 0.15 * 0)
        self.assertEqual(calculate_opportunity_score(100, 0, 0, 0), esperado)

    def test_faixas_de_nivel(self):
        self.assertEqual(_nivel_por_pontuacao(0), "Baixa")
        self.assertEqual(_nivel_por_pontuacao(39), "Baixa")
        self.assertEqual(_nivel_por_pontuacao(40), "Moderada")
        self.assertEqual(_nivel_por_pontuacao(74), "Boa")
        self.assertEqual(_nivel_por_pontuacao(75), "Alta")
        self.assertEqual(_nivel_por_pontuacao(90), "Crítica")
        self.assertEqual(_nivel_por_pontuacao(100), "Crítica")

    def test_evaluate_opportunity_persiste_e_calcula_delta(self):
        lead_id = _inserir_lead(
            nome_empresa="Delta Co", segmento_icp="Comércios & Redes de Varejo",
            servicos_recomendados="CFTV contra perdas", cidade="Curitiba, PR",
        )
        primeiro = evaluate_opportunity(lead_id)
        self.assertEqual(primeiro["opportunity_delta"], 0)

        mudanca = {"type": "possivel_nova_unidade", "field": "units_detected", "before": 1, "after": 3, "days_between": 2}
        derive_signals_from_changes(lead_id, [mudanca], fonte="Teste")
        segundo = evaluate_opportunity(lead_id, houve_novo_sinal=True)
        self.assertGreater(segundo["opportunity_score"], primeiro["opportunity_score"])
        self.assertEqual(segundo["opportunity_delta"], segundo["opportunity_score"] - primeiro["opportunity_score"])

        with automation.conectar() as conexao:
            linha = conexao.execute("SELECT opportunity_score FROM leads WHERE id = ?", (lead_id,)).fetchone()
        self.assertEqual(linha["opportunity_score"], segundo["opportunity_score"])


if __name__ == "__main__":
    unittest.main()
