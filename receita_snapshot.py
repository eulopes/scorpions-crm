"""Enriquecimento firmográfico via Receita Federal (BrasilAPI) para o motor de
Opportunity Intelligence.

Duas responsabilidades, separadas de propósito para o mapper ser 100% testável
sem rede:

- ``mapear_snapshot_receita``  -- função pura: recebe o JSON da BrasilAPI e
  devolve só os campos firmográficos que entram no snapshot.
- ``buscar_snapshot_receita`` -- faz a chamada HTTP e delega ao mapper.

Não depende de Streamlit nem do banco. Reaproveita a sessão resiliente e a
validação de CNPJ já existentes em ``niche_sources`` para não duplicar retry,
timeout e dígitos verificadores.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import requests

from niche_sources import BRASILAPI_CNPJ_URL, _sessao_resiliente, normalizar_cnpj

CAMPOS_RECEITA = (
    "capital_social",
    "porte",
    "cnae_principal",
    "cnaes_secundarios_json",
    "situacao_cadastral",
    "data_situacao_cadastral",
    "natureza_juridica",
    "qsa_hash",
    "qtde_socios",
    "address",
)


def _texto(valor: Any) -> str:
    return " ".join(str(valor or "").split())


def _capital_social(valor: Any) -> float | None:
    """A BrasilAPI manda número ou string ('1000000.00'); toleramos também o
    formato brasileiro ('1.000.000,00') por segurança."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return round(float(valor), 2)
    limpo = re.sub(r"[^\d,.-]", "", str(valor))
    if not limpo:
        return None
    if "," in limpo and "." in limpo:
        limpo = limpo.replace(".", "").replace(",", ".")
    elif "," in limpo:
        limpo = limpo.replace(",", ".")
    try:
        return round(float(limpo), 2)
    except ValueError:
        return None


def _porte(dados: dict[str, Any]) -> str:
    # descricao_porte é o rótulo legível ('MICRO EMPRESA'); porte às vezes vem
    # como código ('01') e às vezes já legível.
    for chave in ("descricao_porte", "porte", "codigo_porte"):
        texto = _texto(dados.get(chave))
        if texto and not texto.isdigit():
            return texto
    return _texto(dados.get("porte"))


def _cnae_principal(dados: dict[str, Any]) -> str:
    codigo = _texto(dados.get("cnae_fiscal") or dados.get("cnae_fiscal_principal"))
    descricao = _texto(dados.get("cnae_fiscal_descricao"))
    if codigo and descricao:
        return f"{codigo} · {descricao}"
    return descricao or codigo


def _cnaes_secundarios(dados: dict[str, Any]) -> str | None:
    itens = dados.get("cnaes_secundarios") or []
    rotulos: list[str] = []
    for item in itens:
        if not isinstance(item, dict):
            continue
        codigo = _texto(item.get("codigo"))
        descricao = _texto(item.get("descricao"))
        if codigo and descricao:
            rotulos.append(f"{codigo} · {descricao}")
        elif codigo or descricao:
            rotulos.append(codigo or descricao)
    if not rotulos:
        return None
    return json.dumps(sorted(set(rotulos)), ensure_ascii=False)


def _qsa_hash(dados: dict[str, Any]) -> tuple[str | None, int]:
    """Hash estável do quadro societário -- entrar/sair um sócio muda o hash;
    reordenação da lista, não."""
    socios = dados.get("qsa") or []
    assinaturas: list[str] = []
    for socio in socios:
        if not isinstance(socio, dict):
            continue
        nome = _texto(socio.get("nome_socio") or socio.get("nome")).casefold()
        qualificacao = _texto(
            socio.get("qualificacao_socio") or socio.get("codigo_qualificacao_socio")
        ).casefold()
        if nome:
            assinaturas.append(f"{nome}|{qualificacao}")
    if not assinaturas:
        return None, 0
    bruto = "\n".join(sorted(set(assinaturas)))
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest(), len(set(assinaturas))


def _endereco(dados: dict[str, Any]) -> str:
    logradouro = _texto(dados.get("logradouro"))
    numero = _texto(dados.get("numero"))
    complemento = _texto(dados.get("complemento"))
    bairro = _texto(dados.get("bairro"))
    municipio = _texto(dados.get("municipio"))
    uf = _texto(dados.get("uf")).upper()
    cep = re.sub(r"\D", "", str(dados.get("cep") or ""))

    principal = ", ".join(parte for parte in (logradouro, numero) if parte)
    if complemento:
        principal = f"{principal} - {complemento}" if principal else complemento
    partes = [principal, bairro]
    if municipio:
        partes.append(f"{municipio}/{uf}" if uf else municipio)
    if len(cep) == 8:
        partes.append(f"CEP {cep[:5]}-{cep[5:]}")
    return " - ".join(parte for parte in partes if parte)


def mapear_snapshot_receita(dados: dict[str, Any]) -> dict[str, Any]:
    """Extrai do JSON da BrasilAPI só os campos que entram no snapshot.

    Função pura: nada de rede, nada de banco. Chaves ausentes viram ``None`` --
    nunca inventa dado.
    """
    if not isinstance(dados, dict):
        raise TypeError("dados deve ser um dicionário do JSON da BrasilAPI.")

    qsa_hash, qtde_socios = _qsa_hash(dados)
    situacao = _texto(
        dados.get("descricao_situacao_cadastral") or dados.get("situacao_cadastral")
    ).upper() or None
    endereco = _endereco(dados)

    return {
        "capital_social": _capital_social(dados.get("capital_social")),
        "porte": _porte(dados) or None,
        "cnae_principal": _cnae_principal(dados) or None,
        "cnaes_secundarios_json": _cnaes_secundarios(dados),
        "situacao_cadastral": situacao,
        "data_situacao_cadastral": _texto(dados.get("data_situacao_cadastral")) or None,
        "natureza_juridica": _texto(dados.get("natureza_juridica")) or None,
        "qsa_hash": qsa_hash,
        "qtde_socios": qtde_socios,
        "address": endereco or None,
    }


def buscar_snapshot_receita(
    cnpj: str, *, sessao: requests.Session | None = None
) -> dict[str, Any] | None:
    """Consulta a BrasilAPI e devolve os campos firmográficos já mapeados.

    Devolve ``None`` quando o CNPJ é inválido, não existe (404) ou a fonte
    falha -- o chamador (automation) cai no comportamento anterior sem quebrar.
    """
    normalizado = normalizar_cnpj(cnpj)
    if not normalizado:
        return None

    propria = sessao is None
    cliente = sessao or _sessao_resiliente()
    try:
        resposta = cliente.get(
            BRASILAPI_CNPJ_URL.format(cnpj=normalizado), timeout=(8, 25)
        )
        if resposta.status_code == 404:
            return None
        resposta.raise_for_status()
        dados = resposta.json()
    except (requests.RequestException, ValueError):
        return None
    finally:
        if propria:
            cliente.close()

    if not isinstance(dados, dict):
        return None
    mapeado = mapear_snapshot_receita(dados)
    mapeado["_raw"] = dados
    return mapeado
