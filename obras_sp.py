"""Adapter de São Paulo -- traduz o "Relatório de processos aprovados"
(SISSEL/SLE) para o registro canônico de ``obras_dump.py``.

Fonte: publicada mensalmente em
https://prefeitura.sp.gov.br/licenciamento/w/servicos/3334 -- a URL de cada
mês não segue um padrão fixo (varia entre ``sissel_AAAA_MM-xls`` e
``sissel_MM_AAAA-xls`` conforme o mês/ano), por isso ``listar_arquivos_mes``
raspa a própria página de índice em vez de compor a URL.

Layout: uma linha de título de tamanho variável, depois uma linha de
cabeçalho ("Alvará", "Processo", "Descrição", ...), depois uma linha por
processo aprovado. Localizamos colunas pelo texto do cabeçalho -- não por
índice fixo -- porque a posição já variou entre exportações.
"""

from __future__ import annotations

import io
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pandas as pd
import requests

from niche_sources import _sessao_resiliente

INDICE_URL = "https://prefeitura.sp.gov.br/licenciamento/w/servicos/3334"
CIDADE = "sao-paulo"
UF = "SP"
MUNICIPIO_NOME = "São Paulo"

# rótulo canônico (sem acento) -> chave do registro intermediário
_ROTULOS_COLUNA = {
    "alvara": "id_alvara",
    "descricao": "_descricao",
    "sql_incra": "sql_iptu",
    "categoria de uso": "_categoria_uso",
    "bairro": "bairro",
    "area da construcao (m2)": "area_construida",
    "proprietario": "proprietario",
    "endereco": "_endereco_bruto",
    "aprovacao": "_data_emissao_bruta",
    "administracao regional": "subprefeitura",
}

_TIPO_POR_PALAVRA = (
    ("aprovacao e execucao", "execucao"),
    ("projeto modificativo", "aprovacao"),
    ("certificado de conclusao", "habite-se"),
    ("apostilamento do certificado de conclusao", "habite-se"),
    ("conclusao", "habite-se"),
    ("regularizacao", "regularizacao"),
    ("demolicao", "demolicao"),
    ("reforma", "reforma"),
    ("execucao", "execucao"),
    ("aprovacao", "aprovacao"),
)


def _sem_acento(valor: Any) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    return "".join(c for c in texto if not unicodedata.combining(c)).strip().lower()


def _achar_colunas(df: pd.DataFrame) -> tuple[int, dict[str, int]]:
    """Acha a linha de cabeçalho procurando 'Alvará' + 'Descrição' juntas,
    depois localiza cada coluna de interesse pelo rótulo (tolera índice
    variar entre exportações)."""
    limite = min(30, len(df))
    for i in range(limite):
        celulas = [_sem_acento(v) for v in df.iloc[i].tolist()]
        if "alvara" in celulas and "descricao" in celulas:
            mapa: dict[str, int] = {}
            for rotulo, chave in _ROTULOS_COLUNA.items():
                idx = next((j for j, c in enumerate(celulas) if c == rotulo), None)
                if idx is not None:
                    mapa[chave] = idx
            if "id_alvara" not in mapa or "_descricao" not in mapa:
                continue
            return i, mapa
    raise ValueError(
        "Cabeçalho do SISSEL não encontrado nas primeiras linhas -- "
        "o layout pode ter mudado (checar _ROTULOS_COLUNA)."
    )


def _tipo_do_texto(descricao: str) -> str:
    normalizado = _sem_acento(descricao)
    for chave, tipo in _TIPO_POR_PALAVRA:
        if chave in normalizado:
            return tipo
    return "outro"


def _uso_da_categoria(categoria: Any) -> str:
    texto = _sem_acento(categoria)
    if texto.startswith("ind"):
        return "industrial"
    if texto.startswith("nr"):
        # LPUOS não-residencial cobre comércio/serviço/institucional; sem a
        # tabela de zoneamento completa não dá pra separar com segurança --
        # "comercial" é o padrão mais provável e ainda está no vocabulário.
        return "comercial"
    if texto.startswith("r") or texto.startswith("his") or texto.startswith("hmp"):
        return "residencial"
    return "outro"


def _endereco_e_numero(bruto: Any) -> tuple[str, str]:
    """'R   PEDRALIA 00399 ' -> ('R PEDRALIA', '399'); número é o primeiro
    token puramente numérico encontrado."""
    texto = re.sub(r"\s+", " ", str(bruto or "")).strip()
    if not texto:
        return "", ""
    partes = texto.split(" ")
    for i, parte in enumerate(partes):
        if parte.isdigit():
            logradouro = " ".join(partes[:i]).strip()
            numero = parte.lstrip("0") or "0"
            return logradouro, numero
    return texto, ""


def _data(texto: Any) -> date | None:
    bruto = str(texto or "").strip()
    if not bruto:
        return None
    try:
        return datetime.strptime(bruto, "%d/%m/%Y").date()
    except ValueError:
        return None


def mapear_linha_sissel(valores: dict[str, Any]) -> dict[str, Any]:
    """Função pura: um dict {chave_intermediária: valor} (já extraído de uma
    linha da planilha) -> registro canônico de ``obras_dump``."""
    id_alvara = str(valores.get("id_alvara") or "").strip()
    logradouro, numero = _endereco_e_numero(valores.get("_endereco_bruto"))
    return {
        "id_alvara": id_alvara,
        "cidade": CIDADE,
        "data_emissao": _data(valores.get("_data_emissao_bruta")),
        "tipo": _tipo_do_texto(valores.get("_descricao")),
        "uso": _uso_da_categoria(valores.get("_categoria_uso")),
        "area_construida": valores.get("area_construida"),  # BR '1.067,33' -- obras_dump._canonizar parseia
        "endereco": logradouro,
        "numero": numero,
        "bairro": str(valores.get("bairro") or "").strip(),
        "municipio_nome": MUNICIPIO_NOME,
        "uf": UF,
        "cep": None,
        "sql_iptu": str(valores.get("sql_iptu") or "").strip(),
        "subprefeitura": str(valores.get("subprefeitura") or "").strip(),
        "lat": None,
        "lng": None,
        "proprietario": str(valores.get("proprietario") or "").strip(),
    }


_PADRAO_ALVARA = re.compile(r"^\d{4}\.\d{2}\.\d+-\d+$")


def parsear_dataframe(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Núcleo testável de ``parsear_planilha`` -- recebe o DataFrame já lido
    (``header=None``), sem depender de ler arquivo. Linhas sem número de
    alvará no formato esperado (rodapé, linhas de página) são descartadas."""
    linha_cabecalho, colunas = _achar_colunas(df)
    registros: list[dict[str, Any]] = []
    for i in range(linha_cabecalho + 1, len(df)):
        linha = df.iloc[i]
        bruto_alvara = linha.iloc[colunas["id_alvara"]] if colunas.get("id_alvara") is not None else None
        if pd.isna(bruto_alvara) or not _PADRAO_ALVARA.match(str(bruto_alvara).strip()):
            continue
        valores = {
            chave: (linha.iloc[idx] if pd.notna(linha.iloc[idx]) else None)
            for chave, idx in colunas.items()
        }
        registros.append(mapear_linha_sissel(valores))
    return registros


def parsear_planilha(caminho_ou_bytes: str | Path | bytes) -> list[dict[str, Any]]:
    """Lê o .xls do SISSEL do disco/bytes e devolve a lista de registros
    canônicos, prontos para ``obras_dump.carregar_alvaras``."""
    origem = io.BytesIO(caminho_ou_bytes) if isinstance(caminho_ou_bytes, bytes) else caminho_ou_bytes
    df = pd.read_excel(origem, header=None, engine="xlrd")
    return parsear_dataframe(df)


def listar_arquivos_mes(*, sessao: requests.Session | None = None) -> dict[str, str]:
    """Raspa a página de índice e devolve {AAAA-MM: url_do_xls} pros meses
    listados (não compõe URL por convenção -- ela já variou entre meses).

    O índice lista o mesmo mês (ex.: "jan") várias vezes, uma por ano -- os
    anos mais recentes usam href RELATIVO (ex.: "/documents/d/licenciamento/
    sissel_2026_01-xls"), enquanto o arquivo histórico de 2024 ainda usa URL
    absoluta antiga (domínio www.prefeitura.sp.gov.br). Aceita as duas
    formas e resolve a relativa contra o domínio do índice -- aceitar só
    absolutas (como antes) faz a função "ver" apenas 2024 e nunca os meses
    correntes, que é justamente quem mais importa para o orquestrador."""
    propria = sessao is None
    cliente = sessao or _sessao_resiliente()
    try:
        resposta = cliente.get(INDICE_URL, timeout=(10, 30))
        resposta.raise_for_status()
    finally:
        if propria:
            cliente.close()

    meses = {
        "jan": "01", "fev": "02", "mar": "03", "abr": "04", "mai": "05", "jun": "06",
        "jul": "07", "ago": "08", "set": "09", "out": "10", "nov": "11", "dez": "12",
    }
    resultado: dict[str, str] = {}
    for m in re.finditer(
        r'href="((?:https://|/)[^"]*?/sissel[^"]*)"[^>]*>\s*(' + "|".join(meses) + r")\s*<",
        resposta.text, flags=re.IGNORECASE,
    ):
        href, mes_txt = m.group(1), m.group(2).lower()
        url = urljoin(INDICE_URL, href)
        ano = re.search(r"20\d{2}", url)
        if not ano:
            continue
        competencia = f"{ano.group(0)}-{meses[mes_txt]}"
        resultado[competencia] = url
    return resultado


def competencia_mais_recente(disponiveis: dict[str, str]) -> str | None:
    """A mais recente entre as competências listadas no índice -- "AAAA-MM"
    ordena cronologicamente como string, sem precisar parsear data. Usada
    pelo orquestrador quando --competencia não é informado, para sempre
    processar o mês mais novo publicado pela prefeitura em vez de exigir que
    o operador descubra isso manualmente."""
    return max(disponiveis) if disponiveis else None


def baixar_e_mapear(competencia: str, *, sessao: requests.Session | None = None) -> list[dict[str, Any]]:
    """Ponta a ponta: acha a URL do mês no índice, baixa e mapeia."""
    disponiveis = listar_arquivos_mes(sessao=sessao)
    url = disponiveis.get(competencia)
    if not url:
        raise ValueError(f"Competência {competencia} não encontrada no índice do SISSEL.")
    propria = sessao is None
    cliente = sessao or _sessao_resiliente()
    try:
        resposta = cliente.get(url, timeout=(10, 60))
        resposta.raise_for_status()
    finally:
        if propria:
            cliente.close()
    return parsear_planilha(resposta.content)
