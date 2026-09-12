"""Detecção de mudanças entre dois snapshots de uma mesma empresa.

Compara fatos objetivos (dados vindos de company_history.py) -- nunca decide
sozinho se uma mudança representa uma oportunidade comercial; isso é
responsabilidade de sales_signals.py e opportunity_engine.py. Módulo puro,
sem banco e sem HTTP, para ficar 100% testável isoladamente.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

# Fonte das mudanças firmográficas -- usada por sales_signals para calibrar a
# confiança (registro oficial da Receita é a evidência mais forte que temos).
FONTE_RECEITA = "Receita Federal (BrasilAPI)"
# Dump mensal de Dados Abertos (Fase 1) -- mesma autoridade, cadência mensal.
FONTE_RECEITA_DUMP = "Receita Federal (Dados Abertos)"
# Alvará de obras (Fase 2) -- fonte municipal, casada por endereço/nome (fuzzy),
# por isso a confiança parte de um patamar mais baixo que a Receita.
FONTE_OBRAS = "Prefeitura (Alvará de Obras)"
# CNES (Fase 3) -- cadastro federal do Ministério da Saúde, casado direto por
# CNPJ (sem fuzzy matching), específico do ICP Clínicas/Hospitais/Laboratórios.
FONTE_CNES = "Ministério da Saúde (CNES)"

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
    # Sinais firmográficos vindos da Receita (Fase 0).
    "capital_social_aumentou",
    "cnae_principal_alterado",
    "cnae_secundario_novo",
    "situacao_cadastral_alterada",
    "quadro_societario_alterado",
)


def _lista_json(valor: Any) -> list[str]:
    if not valor:
        return []
    if isinstance(valor, list):
        return [str(item) for item in valor]
    try:
        dados = json.loads(valor)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in dados] if isinstance(dados, list) else []


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

    # --- Sinais firmográficos (Receita / BrasilAPI) ---

    try:
        capital_antes = float(anterior.get("capital_social") or 0)
        capital_depois = float(atual.get("capital_social") or 0)
    except (TypeError, ValueError):
        capital_antes = capital_depois = 0.0
    # Só o aumento é sinal comercial; redução de capital não interessa aqui.
    if capital_depois > capital_antes > 0:
        absoluta, percentual = _variacao(capital_antes, capital_depois)
        mudancas.append(
            _mudanca(
                "capital_social_aumentou", "capital_social", capital_antes, capital_depois, dias,
                absolute_change=round(absoluta, 2), percentage_change=percentual,
                source=FONTE_RECEITA,
            )
        )

    cnae_antes = str(anterior.get("cnae_principal") or "").strip()
    cnae_depois = str(atual.get("cnae_principal") or "").strip()
    if cnae_antes and cnae_depois and cnae_antes != cnae_depois:
        mudancas.append(
            _mudanca(
                "cnae_principal_alterado", "cnae_principal", cnae_antes, cnae_depois, dias,
                source=FONTE_RECEITA,
            )
        )

    secundarios_antes = set(_lista_json(anterior.get("cnaes_secundarios_json")))
    secundarios_depois = _lista_json(atual.get("cnaes_secundarios_json"))
    novos_cnaes = [item for item in secundarios_depois if item not in secundarios_antes]
    # Só reporta quando já havia baseline -- a primeira coleta não é "novidade".
    if novos_cnaes and secundarios_antes:
        mudancas.append(
            _mudanca(
                "cnae_secundario_novo", "cnaes_secundarios_json",
                sorted(secundarios_antes), sorted(secundarios_depois), dias,
                novos=novos_cnaes, source=FONTE_RECEITA,
            )
        )

    situacao_antes = str(anterior.get("situacao_cadastral") or "").strip().upper()
    situacao_depois = str(atual.get("situacao_cadastral") or "").strip().upper()
    if situacao_antes and situacao_depois and situacao_antes != situacao_depois:
        mudancas.append(
            _mudanca(
                "situacao_cadastral_alterada", "situacao_cadastral",
                situacao_antes, situacao_depois, dias,
                reativacao=(situacao_depois == "ATIVA" and situacao_antes != "ATIVA"),
                inativacao=(situacao_antes == "ATIVA" and situacao_depois != "ATIVA"),
                source=FONTE_RECEITA,
            )
        )

    qsa_antes = str(anterior.get("qsa_hash") or "").strip()
    qsa_depois = str(atual.get("qsa_hash") or "").strip()
    if qsa_antes and qsa_depois and qsa_antes != qsa_depois:
        try:
            socios_antes = int(anterior.get("qtde_socios") or 0)
            socios_depois = int(atual.get("qtde_socios") or 0)
        except (TypeError, ValueError):
            socios_antes = socios_depois = 0
        mudancas.append(
            _mudanca(
                "quadro_societario_alterado", "qsa_hash", qsa_antes, qsa_depois, dias,
                socios_antes=socios_antes, socios_depois=socios_depois,
                source=FONTE_RECEITA,
            )
        )

    return mudancas
