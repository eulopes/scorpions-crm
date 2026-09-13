"""Injeção Fase 2: resultado do matcher de alvarás -> CRM.

Espelha ``receita_ingest.py``: recebe a saída de ``obras_match.casar_alvaras``
e (1) registra o sinal ``OBRA_ATIVA`` em leads já existentes, (2) cria lead
novo a partir do estabelecimento da Receita quando o casamento foi só contra
o dump (sem lead ainda), (3) devolve a fila "sem ocupante" para revisão
humana -- nada é gravado para ela, não dá pra adivinhar quem é.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from automation import salvar_leads_no_banco
from change_detection import FONTE_OBRAS
from company_history import conectar
from crm_strategy import classificar_icp
from niche_sources import normalizar_cnpj
from opportunity_engine import evaluate_opportunity
from sales_signals import registrar_signal


def _lead_id_por_cnpj(conexao: Any, cnpj: str) -> int | None:
    linha = conexao.execute(
        "SELECT id FROM leads WHERE cnpj = ? ORDER BY id LIMIT 1", (cnpj,)
    ).fetchone()
    return int(linha["id"]) if linha else None


def _dias_desde(valor: Any) -> int | None:
    if not isinstance(valor, date):
        return None
    return max(0, (date.today() - valor).days)


def _endereco_estabelecimento(est: dict[str, Any]) -> str:
    principal = " ".join(
        str(p or "").strip() for p in (est.get("logradouro"), est.get("numero")) if p
    ).strip()
    mun = str(est.get("municipio_nome") or est.get("municipio_codigo") or "").strip()
    uf = str(est.get("uf") or "").strip()
    partes = [principal, str(est.get("bairro") or "").strip()]
    if mun:
        partes.append(f"{mun}/{uf}" if uf else mun)
    cep = re.sub(r"\D", "", str(est.get("cep") or ""))
    if len(cep) == 8:
        partes.append(f"CEP {cep[:5]}-{cep[5:]}")
    return " - ".join(p for p in partes if p)


def _lead_de_estabelecimento(est: dict[str, Any]) -> dict[str, Any] | None:
    cnpj = normalizar_cnpj(est.get("cnpj"))
    if not cnpj:
        return None
    nicho = str(est.get("cnae_descricao") or est.get("cnae_principal") or "").strip()
    mun = str(est.get("municipio_nome") or est.get("municipio_codigo") or "").strip()
    uf = str(est.get("uf") or "").strip()
    lead = {
        "place_id": f"receita:{cnpj}",
        "cnpj": cnpj,
        "nome_empresa": str(est.get("nome_fantasia") or "").strip() or f"CNPJ {cnpj}",
        "razao_social": "",
        "decisor": "",
        "nicho": nicho,
        "cnae_fiscal_descricao": str(est.get("cnae_descricao") or ""),
        "endereco": _endereco_estabelecimento(est),
        "cidade": f"{mun}, {uf}" if mun and uf else (mun or uf),
        "telefone": str(est.get("telefone") or ""),
        "site": "", "email": str(est.get("email") or ""),
        "status": "Novos Leads",
        "status_receita": str(est.get("situacao") or ""),
        "origem": FONTE_OBRAS,
        "observacoes": "Detectado por alvará de obras (empresa candidata via dump da Receita). "
                       "Confirme o ocupante antes do contato comercial.",
    }
    segmento, servicos = classificar_icp(lead)
    lead["segmento_icp"] = segmento
    lead["servicos_recomendados"] = "; ".join(servicos)
    return lead


def _mudanca_de_alvara(item: dict[str, Any]) -> dict[str, Any]:
    alvara = item["alvara"]
    local_partes = [alvara.get("bairro"), alvara.get("municipio_nome")]
    local = " - ".join(str(p).strip() for p in local_partes if p)
    return {
        "type": "obra_ativa", "field": "alvara", "before": None,
        "after": {"id_alvara": alvara.get("id_alvara"), "local": local},
        "days_between": _dias_desde(alvara.get("data_emissao")),
        "area": alvara.get("area_construida"),
        "tipo_obra": alvara.get("tipo"),
        "match_confianca": item.get("confianca"),
        "source": FONTE_OBRAS,
    }


def injetar_alvaras(resultado_matcher: dict[str, list]) -> dict[str, Any]:
    """Aplica a saída de ``casar_alvaras``. Idempotente por construção:
    ``registrar_signal`` dedup por evidência ativa (id_alvara + local), e
    ``salvar_leads_no_banco`` dedup por CNPJ."""
    em_lead = resultado_matcher.get("em_lead", [])
    novo_de_receita = resultado_matcher.get("novo_de_receita", [])
    sem_ocupante = resultado_matcher.get("sem_ocupante", [])

    sinais_lead_existente = 0
    for item in em_lead:
        mudanca = _mudanca_de_alvara(item)
        novo = registrar_signal(item["lead_id"], mudanca, fonte=FONTE_OBRAS)
        if novo:
            sinais_lead_existente += 1
        evaluate_opportunity(item["lead_id"], houve_novo_sinal=bool(novo))

    leads_novos: list[dict[str, Any]] = []
    itens_validos: list[dict[str, Any]] = []
    for item in novo_de_receita:
        lead = _lead_de_estabelecimento(item["estabelecimento"])
        if lead is None:
            continue
        leads_novos.append(lead)
        itens_validos.append(item)
    if leads_novos:
        salvar_leads_no_banco(leads_novos)

    sinais_lead_novo = 0
    avaliados_novos = 0
    for item, lead in zip(itens_validos, leads_novos):
        with conectar() as conexao:
            lead_id = _lead_id_por_cnpj(conexao, lead["cnpj"])
        if lead_id is None:
            continue
        mudanca = _mudanca_de_alvara(item)
        novo = registrar_signal(lead_id, mudanca, fonte=FONTE_OBRAS)
        if novo:
            sinais_lead_novo += 1
        evaluate_opportunity(lead_id, houve_novo_sinal=bool(novo))
        avaliados_novos += 1

    return {
        "em_lead": len(em_lead),
        "sinais_em_lead_existente": sinais_lead_existente,
        "novo_de_receita": len(itens_validos),
        "sinais_em_lead_novo": sinais_lead_novo,
        "avaliados_novos": avaliados_novos,
        "sem_ocupante": len(sem_ocupante),
        "pendentes": sem_ocupante,
    }
