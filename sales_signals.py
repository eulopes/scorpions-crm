"""Sales Signals -- sinais comerciais derivados de mudanças objetivas
detectadas nas empresas (change_detection.py).

Um Sales Signal nunca é inventado: só existe se amparado por uma mudança real
entre dois snapshots. Reaproveita a mesma base/conexão de company_history.py
para não duplicar mais uma cópia de DB_PATH/conectar.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import timedelta
from typing import Any

from company_history import agora_utc, conectar, iso_utc

NEW_BRANCH = "NEW_BRANCH"
MULTI_UNIT = "MULTI_UNIT"
REVIEWS_GROWTH = "REVIEWS_GROWTH"
RATING_GROWTH = "RATING_GROWTH"
ADDRESS_CHANGE = "ADDRESS_CHANGE"
NEW_WEBSITE = "NEW_WEBSITE"
WEBSITE_CHANGE = "WEBSITE_CHANGE"
NEW_PHONE = "NEW_PHONE"
BUSINESS_STATUS_CHANGE = "BUSINESS_STATUS_CHANGE"
NEW_COMPANY = "NEW_COMPANY"
DIGITAL_GROWTH = "DIGITAL_GROWTH"
COMPANY_EXPANSION = "COMPANY_EXPANSION"

# Preparado para fontes futuras -- não implementados por não haver ainda uma
# fonte real capaz de sustentar a evidência (regra explícita: não inventar).
SINAIS_FUTUROS_NAO_IMPLEMENTADOS = (
    "HIRING_GROWTH", "NEW_EXECUTIVE", "FUNDING_EVENT", "TECH_CHANGE",
    "NEW_PROJECT", "FINANCIAL_EVENT", "REGULATORY_EVENT",
)

_MEIA_VIDA_DIAS = {
    NEW_BRANCH: 45, MULTI_UNIT: 45, COMPANY_EXPANSION: 45,
    REVIEWS_GROWTH: 30, RATING_GROWTH: 30, DIGITAL_GROWTH: 30,
    ADDRESS_CHANGE: 60, NEW_WEBSITE: 30, WEBSITE_CHANGE: 20,
    NEW_PHONE: 20, BUSINESS_STATUS_CHANGE: 60, NEW_COMPANY: 30,
}
_VALIDADE_DIAS_PADRAO = 90

_MAPA_TIPO_MUDANCA_PARA_SINAL = {
    "possivel_nova_unidade": MULTI_UNIT,
    "endereco_alterado": ADDRESS_CHANGE,
    "site_novo": NEW_WEBSITE,
    "site_alterado": WEBSITE_CHANGE,
    "telefone_novo": NEW_PHONE,
    "alteracao_status": BUSINESS_STATUS_CHANGE,
    "crescimento_avaliacoes": REVIEWS_GROWTH,
    "alteracao_rating": RATING_GROWTH,
    "empresa_adicionada": NEW_COMPANY,
}


def migrar_esquema(conexao: sqlite3.Connection) -> None:
    conexao.execute(
        """
        CREATE TABLE IF NOT EXISTS sales_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            signal_type TEXT NOT NULL,
            signal_strength INTEGER NOT NULL,
            confidence INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            evidence_before_json TEXT,
            evidence_after_json TEXT,
            source TEXT,
            detected_at TEXT NOT NULL,
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
        )
        """
    )
    conexao.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_signals_lead ON sales_signals(lead_id, detected_at DESC)"
    )
    conexao.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_signals_ativos ON sales_signals(active, detected_at DESC)"
    )


def _forca_por_mudanca(mudanca: dict[str, Any]) -> int:
    """'Quão relevante comercialmente é esse evento?'"""
    tipo = mudanca["type"]
    percentual = mudanca.get("percentage_change")
    absoluto = mudanca.get("absolute_change")
    if tipo == "possivel_nova_unidade":
        return 90
    if tipo == "endereco_alterado":
        return 55
    if tipo == "site_novo":
        return 40
    if tipo == "site_alterado":
        return 30
    if tipo == "telefone_novo":
        return 25
    if tipo == "alteracao_status":
        return 50
    if tipo == "crescimento_avaliacoes":
        if percentual is None:
            return 45
        if percentual >= 50:
            return 85
        if percentual >= 20:
            return 65
        if percentual > 0:
            return 40
        return 20
    if tipo == "alteracao_rating":
        return 60 if (absoluto or 0) > 0 else 30
    if tipo == "empresa_adicionada":
        return 35
    return 30


def _confianca_por_mudanca(mudanca: dict[str, Any], fonte: str) -> int:
    """'Quão confiável é a evidência de que o evento aconteceu?'"""
    base = 70
    if fonte in ("Google Places", "BrasilAPI / CNPJ", "BrasilAPI", "BrasilAPI / CVM", "BrasilAPI / B3 + Receita Federal"):
        base = 80
    dias = mudanca.get("days_between")
    if dias is not None and dias <= 1:
        base -= 15  # mudança entre duas coletas quase simultâneas é mais suspeita
    if mudanca["type"] in ("crescimento_avaliacoes", "alteracao_rating") and not mudanca.get("percentage_change"):
        base -= 10
    return max(10, min(100, base))


def calculate_signal_decay(signal_type: str, dias_desde_deteccao: int) -> float:
    """Fator de 0 a 1 -- sinais recentes pesam mais (decaimento exponencial
    por meia-vida). Não apaga o evento, só reduz seu peso no Timing Score."""
    meia_vida = _MEIA_VIDA_DIAS.get(signal_type, 30)
    dias = max(0, dias_desde_deteccao)
    return math.pow(0.5, dias / meia_vida)


def _titulo_e_descricao(mudanca: dict[str, Any]) -> tuple[str, str]:
    tipo = mudanca["type"]
    if tipo == "possivel_nova_unidade":
        return "Possível nova unidade", f"Unidades detectadas passou de {mudanca['before']} para {mudanca['after']}."
    if tipo == "endereco_alterado":
        return "Endereço alterado", f"Endereço mudou de \"{mudanca['before']}\" para \"{mudanca['after']}\"."
    if tipo == "site_novo":
        return "Novo site identificado", f"Site \"{mudanca['after']}\" identificado onde antes não havia."
    if tipo == "site_alterado":
        return "Site alterado", f"Site mudou de \"{mudanca['before']}\" para \"{mudanca['after']}\"."
    if tipo == "telefone_novo":
        return "Telefone novo identificado", f"Telefone \"{mudanca['after']}\" identificado."
    if tipo == "alteracao_status":
        return "Mudança de status cadastral", f"Status mudou de \"{mudanca['before']}\" para \"{mudanca['after']}\"."
    if tipo == "crescimento_avaliacoes":
        percentual = mudanca.get("percentage_change")
        sufixo = f" (+{percentual}%)" if percentual else ""
        return (
            "Crescimento de avaliações",
            f"Avaliações passaram de {int(mudanca['before'])} para {int(mudanca['after'])}{sufixo}.",
        )
    if tipo == "alteracao_rating":
        return "Variação de avaliação (rating)", f"Rating mudou de {mudanca['before']} para {mudanca['after']}."
    if tipo == "empresa_adicionada":
        return "Empresa adicionada à base", "Primeira coleta registrada para esta empresa."
    return "Mudança detectada", f"Campo {mudanca['field']} mudou de {mudanca['before']} para {mudanca['after']}."


def registrar_signal(
    lead_id: int, mudanca: dict[str, Any], *, fonte: str, validade_dias: int | None = None
) -> int | None:
    """Converte uma mudança objetiva em Sales Signal persistido. Não duplica
    um sinal idêntico (mesmo tipo/lead/valor-depois) ainda ativo."""
    tipo_sinal = _MAPA_TIPO_MUDANCA_PARA_SINAL.get(mudanca["type"])
    if not tipo_sinal:
        return None
    titulo, descricao = _titulo_e_descricao(mudanca)
    forca = _forca_por_mudanca(mudanca)
    confianca = _confianca_por_mudanca(mudanca, fonte)
    agora = iso_utc()
    validade = _VALIDADE_DIAS_PADRAO if validade_dias is None else validade_dias
    expira_em = iso_utc(agora_utc() + timedelta(days=validade)) if validade else None
    depois_json = json.dumps(mudanca.get("after"), ensure_ascii=False, default=str)

    with conectar() as conexao:
        duplicado = conexao.execute(
            """
            SELECT 1 FROM sales_signals
            WHERE lead_id = ? AND signal_type = ? AND active = 1 AND evidence_after_json = ?
            """,
            (lead_id, tipo_sinal, depois_json),
        ).fetchone()
        if duplicado:
            return None
        cursor = conexao.execute(
            """
            INSERT INTO sales_signals (
                lead_id, signal_type, signal_strength, confidence, title, description,
                evidence_before_json, evidence_after_json, source, detected_at, expires_at,
                active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                lead_id, tipo_sinal, forca, confianca, titulo, descricao,
                json.dumps(mudanca.get("before"), ensure_ascii=False, default=str),
                depois_json, fonte, agora, expira_em, agora,
            ),
        )
        return int(cursor.lastrowid)


def derive_signals_from_changes(lead_id: int, mudancas: list[dict[str, Any]], *, fonte: str) -> list[int]:
    ids: list[int] = []
    for mudanca in mudancas:
        novo_id = registrar_signal(lead_id, mudanca, fonte=fonte)
        if novo_id:
            ids.append(novo_id)
    return ids


def listar_signals_ativos(lead_id: int) -> list[dict[str, Any]]:
    agora = iso_utc()
    with conectar() as conexao:
        conexao.execute(
            "UPDATE sales_signals SET active = 0 "
            "WHERE lead_id = ? AND active = 1 AND expires_at IS NOT NULL AND expires_at < ?",
            (lead_id, agora),
        )
        linhas = conexao.execute(
            "SELECT * FROM sales_signals WHERE lead_id = ? AND active = 1 ORDER BY detected_at DESC",
            (lead_id,),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def listar_todos_signals_ativos(limite: int = 500) -> list[dict[str, Any]]:
    with conectar() as conexao:
        linhas = conexao.execute(
            "SELECT * FROM sales_signals WHERE active = 1 ORDER BY detected_at DESC LIMIT ?",
            (max(1, min(int(limite), 5000)),),
        ).fetchall()
    return [dict(linha) for linha in linhas]
