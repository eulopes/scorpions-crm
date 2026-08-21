"""Estratégia comercial e perfis de cliente ideal (ICP) da Scorpions.

O módulo não depende da interface, do banco de dados ou de bibliotecas externas.
Ele concentra os nomes do novo funil, a compatibilidade com os status antigos e
uma classificação determinística dos leads nos quatro segmentos definidos no
planejamento estratégico da Scorpions.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


STATUS_FUNIL = [
    "Novos Leads",
    "Contato / Qualificação",
    "Vistoria Técnica / Diagnóstico",
    "Proposta Enviada",
    "Fechado / Contrato",
    "Descartado",
]

# Compatibilidade direta com os valores atualmente gravados no CRM. O estágio
# antigo "Em negociação" é preservado como uma oportunidade que já chegou à
# fase de proposta, em vez de fazê-la regredir para a qualificação inicial.
MAPEAMENTO_STATUS_LEGADO = {
    "Mapeado": "Novos Leads",
    "Tentativa de contato": "Contato / Qualificação",
    "Em negociação": "Proposta Enviada",
    "Fechado": "Fechado / Contrato",
    "Descartado": "Descartado",
}


PERFIS_ICP: dict[str, dict[str, tuple[str, ...] | str]] = {
    "Galpões Logísticos & Indústrias": {
        "consulta_sugerida": (
            "áreas industriais, transportadoras e centros de distribuição"
        ),
        "palavras_chave": (
            "galpão logístico",
            "galpões logísticos",
            "galpão industrial",
            "galpões industriais",
            "centro de distribuição",
            "centros de distribuição",
            "transportadora",
            "transportadoras",
            "operador logístico",
            "operadores logísticos",
            "armazém",
            "armazéns",
            "fábrica",
            "fábricas",
            "indústria",
            "indústrias",
            "industrial",
        ),
        "servicos_recomendados": (
            "CFTV perimetral",
            "Controle de acesso",
            "Wi-Fi industrial",
            "Cabeamento estruturado",
            "Obras técnicas",
        ),
    },
    "Escritórios Corporativos & Serviços": {
        "consulta_sugerida": (
            "advocacias, contabilidades, corretoras e holdings"
        ),
        "palavras_chave": (
            "escritório de advocacia",
            "escritórios de advocacia",
            "sociedade de advogados",
            "advocacia",
            "contabilidade",
            "escritório contábil",
            "escritórios contábeis",
            "contador",
            "contadores",
            "corretora",
            "corretoras",
            "holding",
            "holdings",
            "escritório corporativo",
            "escritórios corporativos",
        ),
        "servicos_recomendados": (
            "Organização de racks",
            "Servidores",
            "Firewall",
            "Segurança de dados",
            "Contrato mensal de suporte de TI",
        ),
    },
    "Clínicas, Hospitais & Laboratórios": {
        "consulta_sugerida": (
            "centros médicos, redes de atendimento e laboratórios"
        ),
        "palavras_chave": (
            "centro médico",
            "centros médicos",
            "rede de atendimento",
            "redes de atendimento",
            "clínica",
            "clínicas",
            "hospital",
            "hospitais",
            "laboratório de análises",
            "laboratórios de análises",
            "laboratório clínico",
            "laboratórios clínicos",
            "laboratório",
            "laboratórios",
            "consultório médico",
            "consultórios médicos",
            "odontologia",
        ),
        "servicos_recomendados": (
            "Redundância de rede",
            "Nobreaks",
            "CFTV em áreas comuns e recepção",
            "Conformidade com LGPD",
        ),
    },
    "Comércios & Redes de Varejo": {
        "consulta_sugerida": (
            "supermercados, concessionárias e lojas de departamento"
        ),
        "palavras_chave": (
            "rede de varejo",
            "redes de varejo",
            "rede de lojas",
            "redes de lojas",
            "loja de departamento",
            "lojas de departamento",
            "supermercado",
            "supermercados",
            "hipermercado",
            "hipermercados",
            "concessionária",
            "concessionárias",
            "varejo",
            "varejista",
            "varejistas",
        ),
        "servicos_recomendados": (
            "CFTV contra perdas",
            "Estabilidade de rede para PDVs e caixas",
            "Infraestrutura elétrica e de TI",
        ),
    },
}


ICP_NAO_CLASSIFICADO = "Não classificado"

# Expressões que contêm palavras válidas fora do sentido comercial desejado.
# Uma âncora física forte ainda pode superar a exclusão: por exemplo,
# "indústria de software com fábrica de hardware" continua sendo uma indústria.
_FALSOS_AMIGOS: dict[str, tuple[str, ...]] = {
    "Galpões Logísticos & Indústrias": (
        "indústria de software",
        "indústria cultural",
        "indústria criativa",
        "indústria do entretenimento",
    ),
    "Escritórios Corporativos & Serviços": (
        "escritório virtual",
    ),
    "Clínicas, Hospitais & Laboratórios": (
        "engenharia clínica",
        "laboratório de software",
        "laboratório de inovação",
    ),
    "Comércios & Redes de Varejo": (
        "comércio eletrônico",
        "loja virtual",
        "varejo online",
    ),
}

_ANCORAS_FISICAS: dict[str, tuple[str, ...]] = {
    "Galpões Logísticos & Indústrias": (
        "galpão",
        "galpões",
        "centro de distribuição",
        "transportadora",
        "armazém",
        "armazéns",
        "fábrica",
        "fábricas",
    ),
    "Escritórios Corporativos & Serviços": (
        "escritório de advocacia",
        "escritório contábil",
        "sociedade de advogados",
        "corretora",
        "holding",
    ),
    "Clínicas, Hospitais & Laboratórios": (
        "centro médico",
        "clínica",
        "clínicas",
        "hospital",
        "hospitais",
        "laboratório de análises",
        "laboratório clínico",
    ),
    "Comércios & Redes de Varejo": (
        "supermercado",
        "hipermercado",
        "concessionária",
        "loja de departamento",
        "rede de lojas",
    ),
}

_CAMPOS_CLASSIFICACAO: tuple[tuple[str, int], ...] = (
    ("segmento_icp", 5),
    ("nicho", 4),
    ("segmento", 4),
    ("atividade_principal", 4),
    ("cnae_fiscal_descricao", 4),
    ("atividade", 3),
    ("nome_empresa", 2),
    ("nome_fantasia", 2),
    ("razao_social", 2),
    ("observacoes", 1),
    ("descricao", 1),
)


def normalizar_texto(valor: Any) -> str:
    """Remove acentos e pontuação sem permitir correspondência por substring."""
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(letra for letra in texto if not unicodedata.combining(letra))
    texto = re.sub(r"[^a-z0-9]+", " ", texto.casefold())
    return " ".join(texto.split())


def _texto_valor(valor: Any) -> str:
    if valor is None:
        return ""
    if isinstance(valor, str):
        return normalizar_texto(valor)
    if isinstance(valor, Mapping):
        return normalizar_texto(" ".join(str(item) for item in valor.values()))
    if isinstance(valor, Sequence) and not isinstance(valor, (bytes, bytearray)):
        return normalizar_texto(" ".join(str(item) for item in valor))
    return normalizar_texto(valor)


def _contem_frase(texto_normalizado: str, frase: str) -> bool:
    """Compara tokens inteiros: ``hospital`` não casa com ``hospitalidade``."""
    frase_normalizada = normalizar_texto(frase)
    return bool(
        frase_normalizada
        and f" {frase_normalizada} " in f" {texto_normalizado} "
    )


def mapear_status_funil(status: Any) -> str:
    """Converte um status antigo e preserva qualquer status novo já válido."""
    texto = str(status or "").strip()
    if not texto:
        return STATUS_FUNIL[0]
    if texto in STATUS_FUNIL:
        return texto
    if texto in MAPEAMENTO_STATUS_LEGADO:
        return MAPEAMENTO_STATUS_LEGADO[texto]

    normalizado = normalizar_texto(texto)
    por_nome_normalizado = {
        normalizar_texto(origem): destino
        for origem, destino in MAPEAMENTO_STATUS_LEGADO.items()
    }
    por_nome_normalizado.update(
        {normalizar_texto(destino): destino for destino in STATUS_FUNIL}
    )
    return por_nome_normalizado.get(normalizado, STATUS_FUNIL[0])


def _pontuar_perfil(
    textos_por_campo: tuple[tuple[str, int], ...],
    segmento: str,
) -> int:
    perfil = PERFIS_ICP[segmento]
    palavras = perfil["palavras_chave"]
    assert isinstance(palavras, tuple)

    texto_completo = " ".join(texto for texto, _ in textos_por_campo if texto)
    tem_falso_amigo = any(
        _contem_frase(texto_completo, frase)
        for frase in _FALSOS_AMIGOS.get(segmento, ())
    )
    tem_ancora_fisica = any(
        _contem_frase(texto_completo, frase)
        for frase in _ANCORAS_FISICAS.get(segmento, ())
    )
    if tem_falso_amigo and not tem_ancora_fisica:
        return 0

    pontos = 0
    termos_contados: set[tuple[str, int]] = set()
    for texto, peso_campo in textos_por_campo:
        for palavra in palavras:
            termo = normalizar_texto(palavra)
            if not termo or not _contem_frase(texto, termo):
                continue
            assinatura = (termo, peso_campo)
            if assinatura in termos_contados:
                continue
            termos_contados.add(assinatura)
            especificidade = 3 if " " in termo else 2
            pontos += peso_campo * especificidade
    return pontos


def classificar_icp(lead: dict[str, Any]) -> tuple[str, list[str]]:
    """Classifica um lead no ICP mais aderente e devolve seus serviços.

    A classificação usa somente palavras ou frases completas em campos de negócio.
    Endereço e origem não entram no cálculo para que, por exemplo, um bairro
    chamado "Distrito Industrial" não transforme um escritório em indústria.
    """
    if not isinstance(lead, dict):
        raise TypeError("lead deve ser um dicionário.")

    segmento_existente = str(lead.get("segmento_icp") or "").strip()
    segmento_por_nome = {
        normalizar_texto(segmento): segmento for segmento in PERFIS_ICP
    }
    segmento_confirmado = segmento_por_nome.get(normalizar_texto(segmento_existente))
    if segmento_confirmado:
        servicos = PERFIS_ICP[segmento_confirmado]["servicos_recomendados"]
        assert isinstance(servicos, tuple)
        return segmento_confirmado, list(servicos)

    textos_por_campo = tuple(
        (_texto_valor(lead.get(campo)), peso)
        for campo, peso in _CAMPOS_CLASSIFICACAO
        if _texto_valor(lead.get(campo))
    )
    if not textos_por_campo:
        return ICP_NAO_CLASSIFICADO, []

    pontuacoes = {
        segmento: _pontuar_perfil(textos_por_campo, segmento)
        for segmento in PERFIS_ICP
    }
    melhor_segmento, melhor_pontuacao = max(
        pontuacoes.items(), key=lambda item: item[1]
    )
    if melhor_pontuacao <= 0:
        return ICP_NAO_CLASSIFICADO, []

    # Empate entre perfis sinaliza um lead ambíguo; classificá-lo automaticamente
    # produziria uma recomendação comercial arbitrária.
    if sum(pontos == melhor_pontuacao for pontos in pontuacoes.values()) > 1:
        return ICP_NAO_CLASSIFICADO, []

    servicos = PERFIS_ICP[melhor_segmento]["servicos_recomendados"]
    assert isinstance(servicos, tuple)
    return melhor_segmento, list(servicos)


def enriquecer_lead_icp(lead: dict[str, Any]) -> dict[str, Any]:
    """Retorna uma cópia do lead com segmento e serviços recomendados."""
    if not isinstance(lead, dict):
        raise TypeError("lead deve ser um dicionário.")
    resultado = dict(lead)
    segmento, servicos = classificar_icp(resultado)
    resultado["segmento_icp"] = segmento
    resultado["servicos_recomendados"] = "; ".join(servicos)
    return resultado


def _autoteste() -> None:
    assert len(STATUS_FUNIL) == 6
    assert mapear_status_funil("Mapeado") == "Novos Leads"
    assert mapear_status_funil("em NEGOCIAÇÃO") == "Proposta Enviada"
    assert mapear_status_funil("Fechado / Contrato") == "Fechado / Contrato"

    casos = (
        (
            {"nicho": "Transportadora e centro de distribuição"},
            "Galpões Logísticos & Indústrias",
        ),
        (
            {"nome_empresa": "Almeida Escritório de Advocacia"},
            "Escritórios Corporativos & Serviços",
        ),
        (
            {"cnae_fiscal_descricao": "Laboratório de análises clínicas"},
            "Clínicas, Hospitais & Laboratórios",
        ),
        (
            {"nicho": "Rede de supermercados e lojas de departamento"},
            "Comércios & Redes de Varejo",
        ),
    )
    for lead, esperado in casos:
        segmento, servicos = classificar_icp(lead)
        assert segmento == esperado, (lead, segmento, esperado)
        assert servicos

    # Falsos amigos e correspondências parciais não devem gerar ICP.
    for lead in (
        {"nicho": "Software para o setor de hospitalidade"},
        {"nicho": "Banco de dados para a indústria de software"},
        {"nicho": "Comércio eletrônico e loja virtual"},
    ):
        assert classificar_icp(lead) == (ICP_NAO_CLASSIFICADO, [])

    original = {"nicho": "Hospital", "status": "Mapeado"}
    enriquecido = enriquecer_lead_icp(original)
    assert original == {"nicho": "Hospital", "status": "Mapeado"}
    assert enriquecido["segmento_icp"] == "Clínicas, Hospitais & Laboratórios"
    assert "Redundância de rede" in enriquecido["servicos_recomendados"]


if __name__ == "__main__":
    _autoteste()
    print("crm_strategy.py: autotestes concluídos com sucesso.")
