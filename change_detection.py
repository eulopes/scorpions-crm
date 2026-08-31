"""Detecção de mudanças entre dois snapshots de uma mesma empresa.

Compara fatos objetivos (dados vindos de company_history.py) -- nunca decide
sozinho se uma mudança representa uma oportunidade comercial; isso é
responsabilidade de sales_signals.py e opportunity_engine.py. Módulo puro,
sem banco e sem HTTP, para ficar 100% testável isoladamente.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

TIPOS_MUDANCA = (
    "empresa_adicionada",
    "endereco_alterado",
    "telefone_novo",
    "site_novo",
    "site_alterado",
    "categoria_nova",
    "mudanca_cadastral",
    "crescimento_avaliacoes",
    "alteracao_rating",
    "possivel_nova_unidade",
    "mudanca_nome",
    "alteracao_status",
)


def _parse_data(valor: Any) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None


def _dias_entre(anterior: dict[str, Any], atual: dict[str, Any]) -> int | None:
    data_anterior = _parse_data(anterior.get("captured_at"))
    data_atual = _parse_data(atual.get("captured_at"))
    if not data_anterior or not data_atual:
        return None
    return max(0, (data_atual - data_anterior).days)


def _mudanca(tipo: str, campo: str, antes: Any, depois: Any, dias: int | None, **extra: Any) -> dict[str, Any]:
    evento: dict[str, Any] = {
        "type": tipo,
        "field": campo,
        "before": antes,
        "after": depois,
        "days_between": dias,
    }
    evento.update(extra)
    return evento


def _variacao(antes: float, depois: float) -> tuple[float, float | None]:
    absoluta = depois - antes
    percentual = (absoluta / antes * 100) if antes else None
    return absoluta, (round(percentual, 1) if percentual is not None else None)


def comparar_snapshots(anterior: dict[str, Any] | None, atual: dict[str, Any]) -> list[dict[str, Any]]:
    """Devolve a lista de mudanças objetivas entre dois snapshots.

    Sem snapshot anterior, a única "mudança" relatada é a criação da empresa
    (útil pra timeline; não gera Sales Signal comercial por si só).
    """
    if anterior is None:
        return [_mudanca("empresa_adicionada", "lead_id", None, atual.get("lead_id"), None)]

    mudancas: list[dict[str, Any]] = []
    dias = _dias_entre(anterior, atual)

    endereco_antes = str(anterior.get("address") or "").strip()
    endereco_depois = str(atual.get("address") or "").strip()
    if endereco_antes and endereco_depois and endereco_antes != endereco_depois:
        mudancas.append(_mudanca("endereco_alterado", "address", endereco_antes, endereco_depois, dias))

    telefone_antes = str(anterior.get("phone") or "").strip()
    telefone_depois = str(atual.get("phone") or "").strip()
    if telefone_depois and telefone_depois != telefone_antes:
        tipo = "telefone_novo" if not telefone_antes else "mudanca_cadastral"
        mudancas.append(_mudanca(tipo, "phone", telefone_antes or None, telefone_depois, dias))

    site_antes = str(anterior.get("website") or "").strip()
    site_depois = str(atual.get("website") or "").strip()
    if site_depois and site_depois != site_antes:
        tipo = "site_novo" if not site_antes else "site_alterado"
        mudancas.append(_mudanca(tipo, "website", site_antes or None, site_depois, dias))

    nome_antes = str(anterior.get("trade_name") or anterior.get("company_name") or "").strip()
    nome_depois = str(atual.get("trade_name") or atual.get("company_name") or "").strip()
    if nome_antes and nome_depois and nome_antes != nome_depois:
        mudancas.append(_mudanca("mudanca_nome", "trade_name", nome_antes, nome_depois, dias))

    status_antes = str(anterior.get("business_status") or "").strip()
    status_depois = str(atual.get("business_status") or "").strip()
    if status_antes and status_depois and status_antes != status_depois:
        mudancas.append(_mudanca("alteracao_status", "business_status", status_antes, status_depois, dias))

    categorias_antes = anterior.get("categories_json")
    categorias_depois = atual.get("categories_json")
    if categorias_depois and categorias_depois != categorias_antes:
        mudancas.append(_mudanca("categoria_nova", "categories_json", categorias_antes, categorias_depois, dias))

    try:
        reviews_antes = float(anterior.get("reviews_count") or 0)
        reviews_depois = float(atual.get("reviews_count") or 0)
    except (TypeError, ValueError):
        reviews_antes = reviews_depois = 0.0
    if reviews_depois > reviews_antes > 0:
        absoluta, percentual = _variacao(reviews_antes, reviews_depois)
        mudancas.append(
            _mudanca(
                "crescimento_avaliacoes", "reviews_count", reviews_antes, reviews_depois, dias,
                absolute_change=absoluta, percentage_change=percentual,
            )
        )
    elif reviews_depois > 0 and reviews_antes == 0:
        mudancas.append(
            _mudanca(
                "crescimento_avaliacoes", "reviews_count", 0, reviews_depois, dias,
                absolute_change=reviews_depois, percentage_change=None,
            )
        )

    try:
        rating_antes = float(anterior.get("rating") or 0)
        rating_depois = float(atual.get("rating") or 0)
    except (TypeError, ValueError):
        rating_antes = rating_depois = 0.0
    if rating_antes and rating_depois and abs(rating_depois - rating_antes) >= 0.1:
        absoluta, percentual = _variacao(rating_antes, rating_depois)
        mudancas.append(
            _mudanca(
                "alteracao_rating", "rating", rating_antes, rating_depois, dias,
                absolute_change=round(absoluta, 2), percentage_change=percentual,
            )
        )

    try:
        unidades_antes = int(anterior.get("units_detected") or 0)
        unidades_depois = int(atual.get("units_detected") or 0)
    except (TypeError, ValueError):
        unidades_antes = unidades_depois = 0
    if unidades_depois > unidades_antes:
        mudancas.append(
            _mudanca(
                "possivel_nova_unidade", "units_detected", unidades_antes, unidades_depois, dias,
                absolute_change=unidades_depois - unidades_antes,
            )
        )

    return mudancas
