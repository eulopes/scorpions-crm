"""Opportunity Engine -- calcula os scores determinísticos (Fit, Intent,
Timing, Data Confidence, Opportunity) e explica cada um deles.

Não faz chamadas HTTP: só lê o que a coleta (automation.py) e os módulos de
histórico/sinais (company_history.py, sales_signals.py) já persistiram. Toda
a lógica aqui é determinística, auditável e explicável -- nada de ML, nada de
inferência apresentada como fato.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from company_history import agora_utc, conectar, iso_utc, listar_snapshots
from crm_strategy import PERFIS_ICP
from niche_sources import normalizar_cnpj
from sales_signals import calculate_signal_decay, listar_signals_ativos

# Pesos centralizados -- nunca hardcoded dentro da função de cálculo, pra
# poder ajustar o comportamento do produto sem tocar em lógica.
PESOS_OPORTUNIDADE = {
    "fit": 0.30,
    "intent": 0.30,
    "timing": 0.25,
    "confidence": 0.15,
}

NIVEIS_OPORTUNIDADE = (
    (39, "Baixa"),
    (59, "Moderada"),
    (74, "Boa"),
    (89, "Alta"),
    (100, "Crítica"),
)


def migrar_esquema(conexao: sqlite3.Connection) -> None:
    conexao.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_score_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            fit_score INTEGER,
            intent_score INTEGER,
            timing_score INTEGER,
            data_confidence_score INTEGER,
            opportunity_score INTEGER,
            opportunity_level TEXT,
            reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
        )
        """
    )
    conexao.execute(
        "CREATE INDEX IF NOT EXISTS idx_opportunity_history_lead "
        "ON opportunity_score_history(lead_id, created_at DESC)"
    )
    conexao.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL UNIQUE,
            opportunity_score_at_entry INTEGER,
            fit_score_at_entry INTEGER,
            intent_score_at_entry INTEGER,
            timing_score_at_entry INTEGER,
            signals_json TEXT,
            outcome TEXT NOT NULL,
            value REAL,
            closed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
        )
        """
    )


def _nivel_por_pontuacao(pontuacao: int) -> str:
    for limite, nivel in NIVEIS_OPORTUNIDADE:
        if pontuacao <= limite:
            return nivel
    return NIVEIS_OPORTUNIDADE[-1][1]


def _dias_desde(momento_iso: str | None) -> int:
    if not momento_iso:
        return 9999
    try:
        instante = datetime.fromisoformat(str(momento_iso).replace("Z", "+00:00"))
    except ValueError:
        return 9999
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=timezone.utc)
    return max(0, (agora_utc() - instante).days)


def calculate_fit_score(lead: dict[str, Any], ultimo_snapshot: dict[str, Any] | None) -> int:
    """'Quanto esta empresa combina com o cliente ideal?' -- sem eventos
    temporais, só características firmográficas já conhecidas do lead."""
    pontos = 0
    segmento = str(lead.get("segmento_icp") or "").strip()
    if segmento and segmento in PERFIS_ICP:
        pontos += 55
    if str(lead.get("servicos_recomendados") or "").strip():
        pontos += 15
    if str(lead.get("cidade") or "").strip():
        pontos += 10
    unidades = (ultimo_snapshot or {}).get("units_detected") or 0
    try:
        if int(unidades) >= 2:
            pontos += 20
    except (TypeError, ValueError):
        pass
    return min(100, pontos)


def calculate_data_confidence(lead: dict[str, Any], snapshots: list[dict[str, Any]]) -> int:
    """'Quão confiáveis são os dados desta empresa?' -- existir um campo não é
    o mesmo que ele ser confiável: pesa fonte, consistência e atualidade."""
    pontos = 0
    if lead.get("cnpj") and normalizar_cnpj(lead.get("cnpj")):
        pontos += 25
    situacao = str(lead.get("status_receita") or "").upper()
    if "ATIVA" in situacao or "FUNCIONAMENTO NORMAL" in situacao:
        pontos += 20
    if str(lead.get("telefone") or "").strip():
        pontos += 15
    if str(lead.get("email") or "").strip():
        pontos += 10
    if str(lead.get("site") or "").strip():
        pontos += 10
    fontes_distintas = {s.get("source") for s in snapshots if s.get("source")}
    if len(fontes_distintas) >= 2:
        pontos += 10
    if snapshots:
        dias = _dias_desde(snapshots[0].get("captured_at"))
        if dias <= 30:
            pontos += 10
        elif dias <= 90:
            pontos += 5
    return min(100, pontos)


def calculate_intent_score(sinais_ativos: list[dict[str, Any]]) -> int:
    """'Existem sinais observáveis de necessidade ou mudança comercial?' --
    baseado só em Sales Signals reais, nunca em posse de telefone/site/CNPJ."""
    if not sinais_ativos:
        return 0
    soma_ponderada = 0.0
    peso_total = 0.0
    for sinal in sinais_ativos:
        decaimento = calculate_signal_decay(sinal["signal_type"], _dias_desde(sinal.get("detected_at")))
        peso = (float(sinal.get("confidence") or 0) / 100) * decaimento
        soma_ponderada += float(sinal.get("signal_strength") or 0) * peso
        peso_total += peso
    if peso_total <= 0:
        return 0
    media = soma_ponderada / peso_total
    fator_quantidade = min(1.15, 1 + 0.05 * (len(sinais_ativos) - 1))
    return int(round(min(100, media * fator_quantidade)))


def calculate_timing_score(sinais_ativos: list[dict[str, Any]]) -> int:
    """'Existe motivo para abordar esta empresa agora?' -- depende só de
    recência/combinação de sinais; Fit alto sozinho não gera Timing alto."""
    if not sinais_ativos:
        return 0
    decaimentos = [
        calculate_signal_decay(sinal["signal_type"], _dias_desde(sinal.get("detected_at")))
        for sinal in sinais_ativos
    ]
    recencia_media = sum(decaimentos) / len(decaimentos)
    bonus_combinacao = min(20, 5 * (len(sinais_ativos) - 1))
    return int(round(min(100, recencia_media * 100 * 0.8 + bonus_combinacao)))


def calculate_opportunity_score(fit: int, intent: int, timing: int, confidence: int) -> int:
    pesos = PESOS_OPORTUNIDADE
    bruto = (
        pesos["fit"] * fit + pesos["intent"] * intent
        + pesos["timing"] * timing + pesos["confidence"] * confidence
    )
    return int(round(min(100, max(0, bruto))))


def generate_why_company(lead: dict[str, Any], fit: int) -> str:
    segmento = str(lead.get("segmento_icp") or "").strip()
    if not segmento or segmento not in PERFIS_ICP:
        return "Perfil ainda não classificado em um ICP conhecido -- verifique manualmente antes de priorizar."
    if fit >= 75:
        return f'Esta empresa apresenta características fortemente compatíveis com o perfil "{segmento}".'
    if fit >= 50:
        return f'Esta empresa apresenta características compatíveis com o perfil "{segmento}", com dados parciais.'
    return f'Classificada como "{segmento}", mas com poucos atributos de aderência confirmados até agora.'


def generate_why_now(sinais_ativos: list[dict[str, Any]]) -> dict[str, Any]:
    """Separa FACT (o que foi observado), INFERENCE (o que isso pode indicar)
    e RECOMMENDATION -- nunca apresenta inferência como se fosse fato."""
    if not sinais_ativos:
        return {
            "facts": [],
            "inference": "Nenhum sinal comercial ativo identificado até agora.",
            "recommendation": "Monitorar -- ainda não há motivo objetivo para priorizar contato imediato.",
        }
    ordenados = sorted(sinais_ativos, key=lambda s: s.get("detected_at") or "", reverse=True)
    fatos = [sinal["description"] for sinal in ordenados[:5]]
    forca_media = sum(int(s.get("signal_strength") or 0) for s in sinais_ativos) / len(sinais_ativos)
    if forca_media >= 60:
        inferencia = "Esses eventos, em conjunto, podem indicar expansão ou crescimento recente."
        recomendacao = "Priorizar abordagem comercial nos próximos dias."
    elif forca_media >= 35:
        inferencia = "Esses eventos podem indicar mudança relevante, mas ainda com sinal moderado."
        recomendacao = "Priorizar com atenção moderada -- vale contato, sem urgência extrema."
    else:
        inferencia = "Mudanças pontuais, sem força suficiente para indicar um evento comercial claro."
        recomendacao = "Monitorar antes de investir esforço comercial forte."
    return {"facts": fatos, "inference": inferencia, "recommendation": recomendacao}


def _json_seguro(valor: Any) -> Any:
    if not valor:
        return None
    try:
        return json.loads(valor)
    except (TypeError, ValueError):
        return valor


def build_evidence(sinais_ativos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "fact",
            "field": sinal.get("signal_type"),
            "value": _json_seguro(sinal.get("evidence_after_json")),
            "previous_value": _json_seguro(sinal.get("evidence_before_json")),
            "source": sinal.get("source"),
            "collected_at": sinal.get("detected_at"),
        }
        for sinal in sinais_ativos
    ]


def recommend_next_action(
    opportunity_level: str, timing_score: int, data_confidence_score: int, lead: dict[str, Any]
) -> str:
    if data_confidence_score < 40:
        return "Enriquecer dados -- confiança insuficiente para uma recomendação comercial forte."
    if opportunity_level in ("Crítica", "Alta"):
        if str(lead.get("telefone") or "").strip():
            return "Priorizar contato por telefone/WhatsApp."
        if str(lead.get("email") or "").strip():
            return "Priorizar abordagem por e-mail."
        return "Priorizar -- buscar um canal de contato antes de abordar."
    if timing_score < 20:
        return "Monitorar -- sem motivo objetivo para abordagem imediata."
    return "Baixa prioridade -- manter na base e reavaliar no próximo ciclo."


def _pontuacao_anterior(lead_id: int) -> dict[str, Any] | None:
    with conectar() as conexao:
        linha = conexao.execute(
            "SELECT * FROM opportunity_score_history WHERE lead_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
    return dict(linha) if linha else None


def calculate_opportunity_delta(lead_id: int, opportunity_score_atual: int) -> int:
    anterior = _pontuacao_anterior(lead_id)
    if not anterior or anterior.get("opportunity_score") is None:
        return 0
    return int(opportunity_score_atual) - int(anterior["opportunity_score"])


def _salvar_score_history_se_mudou(lead_id: int, resultado: dict[str, Any], houve_novo_sinal: bool) -> None:
    anterior = _pontuacao_anterior(lead_id)
    mudou_nivel = not anterior or anterior.get("opportunity_level") != resultado["opportunity_level"]
    mudou_score = (
        not anterior
        or abs(int(anterior.get("opportunity_score") or 0) - resultado["opportunity_score"]) >= 5
    )
    if not (mudou_nivel or mudou_score or houve_novo_sinal):
        return
    motivo = "Novo sinal comercial" if houve_novo_sinal else ("Mudança de nível" if mudou_nivel else "Variação de score")
    with conectar() as conexao:
        conexao.execute(
            """
            INSERT INTO opportunity_score_history (
                lead_id, fit_score, intent_score, timing_score, data_confidence_score,
                opportunity_score, opportunity_level, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead_id, resultado["fit_score"], resultado["intent_score"], resultado["timing_score"],
                resultado["data_confidence_score"], resultado["opportunity_score"],
                resultado["opportunity_level"], motivo, iso_utc(),
            ),
        )


def evaluate_opportunity(lead_id: int, *, houve_novo_sinal: bool = False) -> dict[str, Any]:
    """Recalcula todos os scores de um lead e persiste o resultado -- ponto
    central chamado pelo worker (refresh_company_intelligence) e pela tela de
    detalhe. Nunca deve ser chamado a cada rerun do Streamlit."""
    with conectar() as conexao:
        linha = conexao.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not linha:
        raise ValueError(f"Lead {lead_id} não encontrado.")
    lead = dict(linha)

    snapshots = listar_snapshots(lead_id, limite=10)
    ultimo_snapshot = snapshots[0] if snapshots else None
    sinais_ativos = listar_signals_ativos(lead_id)

    fit = calculate_fit_score(lead, ultimo_snapshot)
    confidence = calculate_data_confidence(lead, snapshots)
    intent = calculate_intent_score(sinais_ativos)
    timing = calculate_timing_score(sinais_ativos)
    opportunity = calculate_opportunity_score(fit, intent, timing, confidence)
    nivel = _nivel_por_pontuacao(opportunity)
    delta = calculate_opportunity_delta(lead_id, opportunity)
    why_company = generate_why_company(lead, fit)
    why_now = generate_why_now(sinais_ativos)
    evidencias = build_evidence(sinais_ativos)
    proxima_acao = recommend_next_action(nivel, timing, confidence, lead)

    resultado = {
        "lead_id": lead_id,
        "fit_score": fit,
        "intent_score": intent,
        "timing_score": timing,
        "data_confidence_score": confidence,
        "opportunity_score": opportunity,
        "opportunity_level": nivel,
        "opportunity_delta": delta,
        "why_company": why_company,
        "why_now": f"{why_now['recommendation']} {why_now['inference']}",
        "why_now_detalhado": why_now,
        "signals": sinais_ativos,
        "evidence": evidencias,
        "recommended_action": proxima_acao,
        "updated_at": iso_utc(),
    }

    agora = resultado["updated_at"]
    ultimo_sinal_em = max((s.get("detected_at") or "" for s in sinais_ativos), default=None) or None
    with conectar() as conexao:
        conexao.execute(
            """
            UPDATE leads SET
                fit_score = ?, intent_score = ?, timing_score = ?, data_confidence_score = ?,
                opportunity_score = ?, opportunity_level = ?, opportunity_reason = ?,
                why_now = ?, opportunity_updated_at = ?, opportunity_delta = ?,
                last_signal_at = COALESCE(?, last_signal_at)
            WHERE id = ?
            """,
            (
                fit, intent, timing, confidence, opportunity, nivel, why_company,
                resultado["why_now"], agora, delta, ultimo_sinal_em, lead_id,
            ),
        )
    _salvar_score_history_se_mudou(lead_id, resultado, houve_novo_sinal)
    return resultado


def explain_opportunity(lead_id: int) -> dict[str, Any]:
    """Recalcula e devolve a explicação completa -- usado pela tela de detalhe."""
    return evaluate_opportunity(lead_id)


def get_opportunity_timeline(lead_id: int) -> list[dict[str, Any]]:
    eventos: list[dict[str, Any]] = [
        {
            "data": snapshot["captured_at"],
            "tipo": "snapshot",
            "descricao": f"Snapshot coletado via {snapshot.get('source') or 'fonte desconhecida'}.",
        }
        for snapshot in listar_snapshots(lead_id, limite=100)
    ]
    with conectar() as conexao:
        sinais = conexao.execute(
            "SELECT * FROM sales_signals WHERE lead_id = ? ORDER BY detected_at", (lead_id,)
        ).fetchall()
        historico = conexao.execute(
            "SELECT * FROM opportunity_score_history WHERE lead_id = ? ORDER BY created_at",
            (lead_id,),
        ).fetchall()
    for sinal in sinais:
        eventos.append({"data": sinal["detected_at"], "tipo": "sinal", "descricao": sinal["title"]})
    anterior_score = None
    for ponto in historico:
        descricao = f"Opportunity Score {ponto['opportunity_score']}"
        if anterior_score is not None and anterior_score != ponto["opportunity_score"]:
            seta = "↑" if ponto["opportunity_score"] > anterior_score else "↓"
            descricao = f"Opportunity Score {anterior_score} {seta} {ponto['opportunity_score']}"
        eventos.append({"data": ponto["created_at"], "tipo": "score", "descricao": descricao})
        anterior_score = ponto["opportunity_score"]
    eventos.sort(key=lambda evento: evento["data"] or "")
    return eventos


def sincronizar_outcomes_pendentes() -> int:
    """Registra o resultado comercial (ganho/perdido) de leads já fechados ou
    descartados que ainda não têm outcome salvo -- sem ML, só preserva
    histórico para conversion_rate_by_score_range() analisar depois."""
    with conectar() as conexao:
        pendentes = conexao.execute(
            """
            SELECT l.* FROM leads l
            LEFT JOIN opportunity_outcomes o ON o.lead_id = l.id
            WHERE l.status IN ('Fechado / Contrato', 'Descartado') AND o.id IS NULL
            """
        ).fetchall()
    registrados = 0
    for linha in pendentes:
        lead = dict(linha)
        sinais = listar_signals_ativos(lead["id"])
        outcome = "won" if lead["status"] == "Fechado / Contrato" else "lost"
        with conectar() as conexao:
            conexao.execute(
                """
                INSERT INTO opportunity_outcomes (
                    lead_id, opportunity_score_at_entry, fit_score_at_entry,
                    intent_score_at_entry, timing_score_at_entry, signals_json,
                    outcome, value, closed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lead["id"], lead.get("opportunity_score"), lead.get("fit_score"),
                    lead.get("intent_score"), lead.get("timing_score"),
                    json.dumps([s["signal_type"] for s in sinais], ensure_ascii=False),
                    outcome, lead.get("valor_proposta"), lead.get("atualizado_em"), iso_utc(),
                ),
            )
        registrados += 1
    return registrados


def conversion_rate_by_score_range() -> list[dict[str, Any]]:
    """Só mostra correlação observada -- nunca afirma causalidade."""
    faixas = ((0, 49), (50, 69), (70, 84), (85, 100))
    with conectar() as conexao:
        outcomes = [dict(o) for o in conexao.execute("SELECT * FROM opportunity_outcomes").fetchall()]
    resultado = []
    for minimo, maximo in faixas:
        do_intervalo = [
            o for o in outcomes
            if o["opportunity_score_at_entry"] is not None and minimo <= o["opportunity_score_at_entry"] <= maximo
        ]
        total = len(do_intervalo)
        ganhos = [o for o in do_intervalo if o["outcome"] == "won"]
        taxa = (len(ganhos) / total * 100) if total else None
        ticket_medio = (sum(float(o.get("value") or 0) for o in ganhos) / len(ganhos)) if ganhos else None
        resultado.append(
            {
                "faixa": f"{minimo}-{maximo}",
                "total": total,
                "fechados": len(ganhos),
                "taxa_conversao": round(taxa, 1) if taxa is not None else None,
                "ticket_medio": round(ticket_medio, 2) if ticket_medio is not None else None,
            }
        )
    return resultado


def build_company_features(lead_id: int) -> dict[str, Any]:
    """Extrai atributos determinísticos -- preparação para um futuro motor de
    lookalike; não implementa nenhum ML nesta fase."""
    with conectar() as conexao:
        linha = conexao.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not linha:
        raise ValueError(f"Lead {lead_id} não encontrado.")
    lead = dict(linha)
    snapshots = listar_snapshots(lead_id, limite=1)
    ultimo = snapshots[0] if snapshots else {}
    return {
        "lead_id": lead_id,
        "nicho": lead.get("nicho"),
        "segmento_icp": lead.get("segmento_icp"),
        "cidade": lead.get("cidade"),
        "units_detected": ultimo.get("units_detected"),
        "fit_score": lead.get("fit_score"),
        "intent_score": lead.get("intent_score"),
        "timing_score": lead.get("timing_score"),
        "data_confidence_score": lead.get("data_confidence_score"),
        "possui_telefone": bool(str(lead.get("telefone") or "").strip()),
        "possui_email": bool(str(lead.get("email") or "").strip()),
        "possui_site": bool(str(lead.get("site") or "").strip()),
    }
