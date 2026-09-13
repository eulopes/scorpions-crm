"""Injeção Fase 1: candidatos do dump da Receita -> CRM.

Ponte DuckDB -> SQLite. Recebe a saída de ``receita_dump.candidatos_do_diff``
e: (1) grava os leads na base (reusa ``salvar_leads_no_banco``, que já
deduplica por CNPJ sem sobrescrever trabalho comercial), (2) registra o Sales
Signal correspondente, (3) recalcula o Opportunity Score. Eventos de BAIXA
desativam os sinais ativos do lead existente.

É o passo que o orquestrador mensal (``scripts/rodar_receita_dump.py``) chama
depois do diff e do matcher.
"""

from __future__ import annotations

from typing import Any

from automation import salvar_leads_no_banco
from change_detection import FONTE_RECEITA_DUMP
from company_history import conectar
from niche_sources import normalizar_cnpj
from opportunity_engine import evaluate_opportunity
from sales_signals import registrar_signal


def _lead_id_por_cnpj(conexao: Any, cnpj: str) -> int | None:
    linha = conexao.execute(
        "SELECT id FROM leads WHERE cnpj = ? ORDER BY id LIMIT 1", (cnpj,)
    ).fetchone()
    return int(linha["id"]) if linha else None


def aplicar_supressoes(cnpjs: list[str]) -> int:
    """Para cada CNPJ que a Receita baixou/inaptou: se existe lead, desativa
    seus sinais ativos (não apaga o lead -- só para de tratá-lo como quente)."""
    normalizados = [c for c in (normalizar_cnpj(x) for x in cnpjs) if c]
    if not normalizados:
        return 0
    marcados = 0
    with conectar() as conexao:
        for cnpj in normalizados:
            lead_id = _lead_id_por_cnpj(conexao, cnpj)
            if lead_id is None:
                continue
            conexao.execute(
                "UPDATE sales_signals SET active = 0 WHERE lead_id = ? AND active = 1",
                (lead_id,),
            )
            marcados += 1
    return marcados


def injetar_candidatos(resultado_matcher: dict[str, list]) -> dict[str, int]:
    """Aplica a saída de ``candidatos_do_diff``. Idempotente: re-rodar a mesma
    competência não duplica lead (dedup por CNPJ) nem sinal (dedup por
    tipo+lead+evidência ativa em ``registrar_signal``)."""
    candidatos = resultado_matcher.get("candidatos", [])
    if candidatos:
        salvar_leads_no_banco([dict(item["lead"]) for item in candidatos])

    sinais = 0
    avaliados = 0
    sem_lead = 0
    for item in candidatos:
        cnpj = normalizar_cnpj(item["lead"].get("cnpj"))
        if not cnpj:
            sem_lead += 1
            continue
        with conectar() as conexao:
            lead_id = _lead_id_por_cnpj(conexao, cnpj)
        if lead_id is None:
            sem_lead += 1
            continue
        novo = registrar_signal(lead_id, item["mudanca"], fonte=FONTE_RECEITA_DUMP)
        if novo:
            sinais += 1
        evaluate_opportunity(lead_id, houve_novo_sinal=bool(novo))
        avaliados += 1

    suprimidos = aplicar_supressoes(resultado_matcher.get("supressoes", []))
    return {
        "candidatos": len(candidatos),
        "sinais_novos": sinais,
        "avaliados": avaliados,
        "sem_lead": sem_lead,
        "suprimidos": suprimidos,
    }
