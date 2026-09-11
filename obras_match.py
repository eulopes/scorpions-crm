"""Fase 2 -- casamento alvará -> empresa.

O alvará é indexado por imóvel (endereço + SQL do IPTU), nunca por CNPJ. Este
módulo resolve "quem opera neste endereço?" em ordem de confiança:

  1. nome do proprietário (SISSEL/SP costuma trazer a razão social de quem
     pediu o alvará) bate com um lead que já temos       -> em_lead, alta
  2. endereço bate com um lead que já temos               -> em_lead, alta
  3. nome do proprietário bate com um estabelecimento do
     dump da Receita                                      -> novo_de_receita, media
  4. endereço bate com um estabelecimento do dump          -> novo_de_receita, media
  5. nada casou                                            -> sem_ocupante (revisão humana)

Função pura: recebe listas/índices já montados pelo chamador, não toca banco.
Endereço e razão social brasileiros são sujos, então os dois casamentos são
por *assinatura* (conjuntos normalizados), nunca por string exata.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

_STOPWORDS = {
    # logradouros por extenso e abreviados (após tirar acento/pontuação)
    "rua", "avenida", "alameda", "praca", "travessa", "rodovia", "estrada",
    "largo", "viaduto", "via", "km",
    "av", "avn", "r", "al", "pc", "pca", "trav", "tv", "rod", "estr", "est",
    "lgo", "vd", "rd",
    # conectivos e ruído comum de endereço
    "de", "da", "do", "das", "dos", "e",
    "lote", "quadra", "bloco", "conj", "conjunto", "jardim", "vila", "parque",
    "sitio", "chacara", "loteamento", "cep", "andar", "sala", "loja", "galpao",
}


def _norm(valor: Any) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def _numero_do_endereco(texto: str) -> str:
    """Pega o número predial: preferência para o que vem logo após vírgula,
    senão a primeira sequência de 1-6 dígitos que não pareça CEP."""
    bruto = str(texto or "")
    apos_virgula = re.search(r",\s*(\d{1,6})(?!\d)", bruto)
    if apos_virgula:
        return apos_virgula.group(1).lstrip("0") or "0"
    for candidato in re.findall(r"(?<!\d)(\d{1,6})(?!\d)", bruto):
        if len(candidato) != 8:  # não é CEP
            return candidato.lstrip("0") or "0"
    return ""


_UF = {
    "ac", "al", "ap", "am", "ba", "ce", "df", "es", "go", "ma", "mt", "ms",
    "mg", "pa", "pb", "pr", "pe", "pi", "rj", "rn", "rs", "ro", "rr", "sc",
    "sp", "se", "to",
}


def assinatura_endereco(
    endereco: Any, numero: Any = "", municipio: Any = ""
) -> tuple[frozenset[str], str, frozenset[str]]:
    """(-> tokens do logradouro, número predial, tokens do município)."""
    texto = _norm(endereco)
    mun_tokens = {t for t in _norm(municipio).split() if t not in _UF}
    num = re.sub(r"\D", "", str(numero or "")).lstrip("0")
    if not num:
        num = _numero_do_endereco(endereco)

    tokens = set(texto.split()) - _STOPWORDS - mun_tokens
    tokens = {t for t in tokens if not t.isdigit() and len(t) >= 3}
    return frozenset(tokens), num, frozenset(mun_tokens)


def _combina(
    a: tuple[frozenset[str], str, frozenset[str]],
    b: tuple[frozenset[str], str, frozenset[str]],
) -> bool:
    tokens_a, num_a, mun_a = a
    tokens_b, num_b, mun_b = b
    if num_a and num_b and num_a != num_b:
        return False
    # município casa se um conjunto de tokens contém o núcleo do outro
    if mun_a and mun_b and not (mun_a <= mun_b or mun_b <= mun_a):
        return False
    if not tokens_a or not tokens_b:
        return False
    intersecao = tokens_a & tokens_b
    if tokens_a <= tokens_b or tokens_b <= tokens_a:
        return len(intersecao) >= 1
    uniao = tokens_a | tokens_b
    return len(intersecao) / len(uniao) >= 0.6


_SUFIXOS_SOCIETARIOS = {
    "ltda", "me", "epp", "eireli", "sa", "s a", "sociedade", "empresarial",
    "individual", "limitada", "cia", "companhia",
}


def normalizar_nome_empresa(nome: Any) -> frozenset[str]:
    """Tokens do nome fantasia/razão social, sem sufixo societário nem
    pontuação -- 'Aspect Midia Ind Eletr Comercio e Serv LTDA' e 'ASPECT
    MIDIA IND. ELETR. COMERCIO E SERVICOS LTDA' geram o mesmo núcleo."""
    tokens = set(_norm(nome).split()) - _STOPWORDS - _SUFIXOS_SOCIETARIOS
    return frozenset(t for t in tokens if len(t) >= 3)


def _nomes_combinam(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return False
    intersecao = a & b
    if a <= b or b <= a:
        return len(intersecao) >= 1
    uniao = a | b
    return len(intersecao) / len(uniao) >= 0.7


def indexar_leads_por_nome(leads: Iterable[dict[str, Any]]) -> list[tuple[frozenset[str], int]]:
    """[(nome normalizado, lead_id)] a partir de nome_empresa/razao_social."""
    indice = []
    for lead in leads:
        for campo in ("nome_empresa", "razao_social"):
            assinatura = normalizar_nome_empresa(lead.get(campo))
            if assinatura:
                indice.append((assinatura, int(lead["id"])))
    return indice


def indexar_estabelecimentos_por_nome(
    estabelecimentos: Iterable[dict[str, Any]],
) -> list[tuple[frozenset[str], dict[str, Any]]]:
    """[(nome normalizado, estabelecimento)] a partir do nome_fantasia do
    dump da Receita -- a Fase 1 não carrega razão social (arquivo Empresas
    não é lido ainda), então essa camada é mais fraca que a de leads."""
    indice = []
    for est in estabelecimentos:
        assinatura = normalizar_nome_empresa(est.get("nome_fantasia"))
        if assinatura:
            indice.append((assinatura, est))
    return indice


def indexar_leads(leads: Iterable[dict[str, Any]]) -> list[tuple[tuple, int]]:
    """[(assinatura, lead_id)] a partir de linhas de ``leads`` (id, endereco, cidade)."""
    indice = []
    for lead in leads:
        assinatura = assinatura_endereco(lead.get("endereco"), "", lead.get("cidade"))
        if assinatura[0]:
            indice.append((assinatura, int(lead["id"])))
    return indice


def indexar_estabelecimentos(estabelecimentos: Iterable[dict[str, Any]]) -> list[tuple[tuple, dict[str, Any]]]:
    """[(assinatura, estabelecimento)] a partir de linhas do dump da Receita."""
    indice = []
    for est in estabelecimentos:
        assinatura = assinatura_endereco(
            est.get("logradouro"), est.get("numero"), est.get("municipio_nome") or est.get("municipio_codigo")
        )
        if assinatura[0]:
            indice.append((assinatura, est))
    return indice


def casar_alvaras(
    alvaras: list[dict[str, Any]],
    *,
    indice_leads: list[tuple[tuple, int]],
    indice_estabelecimentos: list[tuple[tuple, dict[str, Any]]] | None = None,
    indice_leads_nome: list[tuple[frozenset[str], int]] | None = None,
    indice_estabelecimentos_nome: list[tuple[frozenset[str], dict[str, Any]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Classifica cada alvará em em_lead / novo_de_receita / sem_ocupante,
    tentando nome do proprietário antes de endereço (mais confiável quando
    o alvará traz a razão social de quem pediu)."""
    indice_estabelecimentos = indice_estabelecimentos or []
    indice_leads_nome = indice_leads_nome or []
    indice_estabelecimentos_nome = indice_estabelecimentos_nome or []
    em_lead: list[dict[str, Any]] = []
    novo_de_receita: list[dict[str, Any]] = []
    sem_ocupante: list[dict[str, Any]] = []

    for alvara in alvaras:
        nome_alvara = normalizar_nome_empresa(alvara.get("proprietario"))
        assinatura = assinatura_endereco(
            alvara.get("endereco"), alvara.get("numero"), alvara.get("municipio_nome")
        )

        lead_id = next((lid for nome, lid in indice_leads_nome if _nomes_combinam(nome_alvara, nome)), None)
        if lead_id is not None:
            em_lead.append({"alvara": alvara, "lead_id": lead_id, "confianca": "alta", "por": "nome"})
            continue

        if assinatura[0]:
            lead_id = next((lid for assin, lid in indice_leads if _combina(assinatura, assin)), None)
            if lead_id is not None:
                em_lead.append({"alvara": alvara, "lead_id": lead_id, "confianca": "alta", "por": "endereco"})
                continue

        est = next((e for nome, e in indice_estabelecimentos_nome if _nomes_combinam(nome_alvara, nome)), None)
        if est is not None:
            novo_de_receita.append({"alvara": alvara, "estabelecimento": est, "confianca": "media", "por": "nome"})
            continue

        if assinatura[0]:
            est = next((e for assin, e in indice_estabelecimentos if _combina(assinatura, assin)), None)
            if est is not None:
                novo_de_receita.append(
                    {"alvara": alvara, "estabelecimento": est, "confianca": "media", "por": "endereco"}
                )
                continue

        motivo = "sem empresa no endereço" if assinatura[0] else "endereço insuficiente"
        sem_ocupante.append({"alvara": alvara, "motivo": motivo})

    return {
        "em_lead": em_lead,
        "novo_de_receita": novo_de_receita,
        "sem_ocupante": sem_ocupante,
    }
