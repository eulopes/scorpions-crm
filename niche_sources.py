"""Fontes públicas complementares para prospecção por nicho.

O módulo é deliberadamente independente de ``app.py`` e ``automation.py``.
Ele encapsula os detalhes e limitações dos endpoints BrasilAPI/CVM/B3 para que
possa ser importado pelo motor de automação sem criar dependência circular.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BRASILAPI_CVM_URL = "https://brasilapi.com.br/api/cvm/corretoras/v1"
BRASILAPI_B3_URL = "https://brasilapi.com.br/api/tickers/b3/acoes/v1"
BRASILAPI_CNPJ_URL = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
FONTE_OSM = "OpenStreetMap"
LIMITE_MAXIMO = 50

UFS = {
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT",
    "MS", "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO",
    "RR", "SC", "SP", "SE", "TO",
}

TERMOS_CVM = {
    "corretora de valores", "corretoras de valores", "corretora de acoes",
    "corretoras de acoes", "distribuidora de valores", "distribuidoras de valores",
    "ctvm", "dtvm", "valores mobiliarios", "mercado de capitais",
}
TERMOS_BACEN = {
    "banco", "bancos", "fintech", "fintechs", "financeira", "financeiras",
    "credito", "meio de pagamento", "meios de pagamento", "instituicao financeira",
    "instituicoes financeiras", "cooperativa de credito", "cooperativas de credito",
    "consorcio", "consorcios", "pagamento", "pagamentos", "instituicao de pagamento",
    "instituicoes de pagamento", "cambio",
}
TERMOS_B3 = {
    "b3", "bolsa de valores", "empresa listada", "empresas listadas",
    "companhia aberta", "companhias abertas", "mercado acionario",
}
TERMOS_FINANCEIROS_GERAIS = {
    "financeiro", "financeiros", "setor financeiro", "mercado financeiro",
    "servico financeiro", "servicos financeiros",
}
TERMOS_CORRETORAS_NAO_CVM = {
    "corretora de imoveis", "corretoras de imoveis", "corretagem de imoveis",
    "corretora de seguros", "corretoras de seguros", "corretagem de seguros",
    "corretora imobiliaria", "corretoras imobiliarias", "corretora de plano de saude",
    "corretora de criptomoedas",
}
TERMOS_NAO_BACEN = {
    "assessoria financeira", "consultoria financeira", "educacao financeira",
    "planejamento financeiro",
}
LOCALIZACOES_NACIONAIS = {"", "brasil", "nacional", "todo o brasil"}
PALAVRAS_GENERICAS_B3 = {
    "acao", "acoes", "aberta", "abertas", "aberto", "abertos", "acionario",
    "b3", "bolsa", "brasil", "companhia", "companhias", "empresa", "empresas",
    "listada", "listadas", "listado", "listados", "mercado", "na", "no", "valor",
    "valores",
}
PALAVRAS_GENERICAS_CVM = {
    "acao", "acoes", "capital", "capitais", "corretora", "corretoras", "ctvm", "de",
    "distribuidora", "distribuidoras", "dtvm", "mercado", "mobiliario", "mobiliarios",
    "financeiro", "financeiros", "servico", "servicos", "setor", "titulo", "titulos",
    "valor", "valores",
}
PALAVRAS_CONEXAO = {
    "a", "as", "com", "da", "das", "de", "do", "dos", "e", "em", "na", "nas",
    "no", "nos", "o", "os", "para", "por", "segmento", "setor",
}
EQUIVALENTES_SETORIAIS = {
    "tecnologia": (
        "tecnologia", "technology", "software", "programas e servicos",
        "programs and services", "computadores e equipamentos", "hardware and equipments",
        "tecnologia da informacao", "informatica", "servicos de tecnologia",
        "tratamento de dados", "telecomunicacao", "eletronico",
    ),
    "tecnologias": (
        "tecnologia", "technology", "software", "programas e servicos",
        "programs and services", "computadores e equipamentos", "hardware and equipments",
        "tecnologia da informacao", "informatica", "servicos de tecnologia",
        "tratamento de dados", "telecomunicacao", "eletronico",
    ),
}
_CACHE_CNPJ: dict[str, dict[str, Any] | None] = {}


def normalizar_texto(valor: Any) -> str:
    """Normaliza acentos, espaços e caixa para comparações determinísticas."""
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(letra for letra in texto if not unicodedata.combining(letra))
    return " ".join(texto.casefold().split())


def normalizar_cnpj(valor: Any) -> str:
    """Restaura zeros à esquerda e rejeita CNPJs com dígitos verificadores inválidos."""
    digitos = re.sub(r"\D", "", str(valor or ""))
    if not digitos or len(digitos) > 14:
        return ""
    digitos = digitos.zfill(14)
    if len(set(digitos)) == 1:
        return ""

    def calcular(base: str, pesos: tuple[int, ...]) -> str:
        resto = sum(int(numero) * peso for numero, peso in zip(base, pesos)) % 11
        return str(0 if resto < 2 else 11 - resto)

    primeiro = calcular(digitos[:12], (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    segundo = calcular(digitos[:12] + primeiro, (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    return digitos if digitos[-2:] == primeiro + segundo else ""


def _texto_cadastral(valor: Any) -> str:
    texto = str(valor or "").strip()
    marcador = normalizar_texto(texto)
    if marcador in {"n/a", "na", "nao informado", "sem nome"}:
        return ""
    return texto if any(caractere.isalnum() for caractere in texto) else ""


def separar_cidade_uf(localizacao: str, *, permite_brasil: bool = False) -> tuple[str, str]:
    """Aceita ``Cidade, UF``, ``Cidade/UF`` ou ``Cidade - UF``."""
    texto = " ".join(str(localizacao or "").strip().split())
    if permite_brasil and normalizar_texto(texto) in LOCALIZACOES_NACIONAIS:
        return "", ""
    if not texto:
        raise ValueError("Informe a localização como Cidade, UF.")

    correspondencia = re.match(r"^(.*?)\s*(?:,|/|-)\s*([A-Za-z]{2})$", texto)
    if correspondencia:
        cidade = correspondencia.group(1).strip()
        uf = correspondencia.group(2).upper()
    else:
        cidade, uf = texto, ""
    if not cidade:
        raise ValueError("Informe a cidade antes da UF, por exemplo: São Paulo, SP.")
    if uf and uf not in UFS:
        raise ValueError("Informe uma UF brasileira válida, por exemplo: SP.")
    return cidade, uf


def _tem_termo(texto: str, termos: set[str]) -> bool:
    normalizado = normalizar_texto(texto)
    return any(
        re.search(rf"(?<!\w){re.escape(termo)}(?!\w)", normalizado)
        for termo in termos
    )


def roteamento_por_nicho(nicho: str) -> list[str]:
    """Escolhe fontes em ordem de preferência; nunca usa demonstração como fallback."""
    normalizado = normalizar_texto(nicho)
    if "banco de dados" in normalizado:
        return ["Google Places", FONTE_OSM]
    if _tem_termo(nicho, TERMOS_CORRETORAS_NAO_CVM):
        return ["Google Places", FONTE_OSM]
    if _tem_termo(nicho, TERMOS_CVM):
        return ["CVM - Corretoras"]
    if re.search(r"(?<!\w)corretoras?(?!\w)", normalizado):
        return ["Google Places", FONTE_OSM]
    if _tem_termo(nicho, TERMOS_B3):
        return ["B3 - Empresas listadas"]
    if _tem_termo(nicho, TERMOS_NAO_BACEN):
        return ["Google Places", FONTE_OSM]
    if _tem_termo(nicho, TERMOS_BACEN):
        return ["Bacen"]
    if _tem_termo(nicho, TERMOS_FINANCEIROS_GERAIS):
        return ["Bacen", "CVM - Corretoras"]
    return ["Google Places", FONTE_OSM]


def resolver_fontes_reais(nicho: str) -> list[str]:
    """Resolve as fontes ideais em fontes executáveis, usando OSM como fallback."""
    fontes = roteamento_por_nicho(nicho)
    if os.getenv("GOOGLE_PLACES_API_KEY", "").strip():
        return fontes
    return [
        FONTE_OSM if fonte == "Google Places" else fonte for fonte in fontes
    ]


def _sessao_resiliente() -> requests.Session:
    sessao = requests.Session()
    repeticao = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    sessao.mount("https://", HTTPAdapter(max_retries=repeticao))
    sessao.headers.update({"Accept": "application/json", "User-Agent": "ScorpionsCRM/1.0"})
    return sessao


def _obter_lista(
    url: str,
    *,
    parametros: dict[str, str] | None = None,
    sessao: requests.Session | None = None,
) -> list[dict[str, Any]]:
    propria = sessao is None
    cliente = sessao or _sessao_resiliente()
    try:
        resposta = cliente.get(url, params=parametros, timeout=(8, 40))
        resposta.raise_for_status()
        dados = resposta.json()
    except requests.RequestException as erro:
        raise RuntimeError(f"A fonte pública respondeu com erro: {erro}.") from erro
    except ValueError as erro:
        raise RuntimeError("A fonte pública retornou JSON inválido.") from erro
    finally:
        if propria:
            cliente.close()
    if not isinstance(dados, list):
        raise RuntimeError("A fonte pública retornou um formato inesperado.")
    return [item for item in dados if isinstance(item, dict)]


def _endereco_cvm(item: dict[str, Any]) -> str:
    principal = ", ".join(
        parte for parte in (str(item.get("logradouro") or "").strip(), str(item.get("complemento") or "").strip())
        if parte
    )
    partes = [principal, str(item.get("bairro") or "").strip()]
    cep = re.sub(r"\D", "", str(item.get("cep") or ""))
    if len(cep) == 8:
        partes.append(f"CEP {cep[:5]}-{cep[5:]}")
    return " - ".join(parte for parte in partes if parte)


def _endereco_cnpj(item: dict[str, Any]) -> str:
    logradouro = str(item.get("logradouro") or "").strip()
    numero = str(item.get("numero") or "").strip()
    complemento = str(item.get("complemento") or "").strip()
    bairro = str(item.get("bairro") or "").strip()
    cep = re.sub(r"\D", "", str(item.get("cep") or ""))
    principal = ", ".join(parte for parte in (logradouro, numero) if parte)
    if complemento:
        principal = f"{principal} - {complemento}" if principal else complemento
    partes = [principal, bairro]
    if len(cep) == 8:
        partes.append(f"CEP {cep[:5]}-{cep[5:]}")
    return " - ".join(parte for parte in partes if parte)


def _decisor_cnpj(qsa: Any) -> str:
    if not isinstance(qsa, list):
        return ""
    socios = [socio for socio in qsa if isinstance(socio, dict) and socio.get("nome_socio")]
    if not socios:
        return ""
    prioridades = ("presidente", "administrador", "diretor", "titular", "socio-administrador")
    escolhido = next(
        (
            socio
            for prioridade in prioridades
            for socio in socios
            if prioridade in normalizar_texto(socio.get("qualificacao_socio"))
        ),
        socios[0],
    )
    nome = str(escolhido.get("nome_socio") or "").strip().title()
    qualificacao = str(escolhido.get("qualificacao_socio") or "").strip()
    return f"{nome} ({qualificacao})" if qualificacao else nome


def _consultar_cnpj(cnpj: str, sessao: requests.Session) -> dict[str, Any] | None:
    if cnpj in _CACHE_CNPJ:
        return _CACHE_CNPJ[cnpj]
    resposta = sessao.get(BRASILAPI_CNPJ_URL.format(cnpj=cnpj), timeout=(8, 25))
    if resposta.status_code == 404:
        _CACHE_CNPJ[cnpj] = None
        return None
    resposta.raise_for_status()
    dados = resposta.json()
    resultado = dados if isinstance(dados, dict) else None
    _CACHE_CNPJ[cnpj] = resultado
    return resultado


def _termos_especificos_b3(nicho: str) -> list[str]:
    palavras = re.findall(r"[a-z0-9]+", normalizar_texto(nicho))
    return [
        palavra
        for palavra in palavras
        if len(palavra) >= 2
        and palavra not in PALAVRAS_GENERICAS_B3
        and palavra not in PALAVRAS_CONEXAO
    ]


def _termos_especificos_cvm(nicho: str) -> list[str]:
    palavras = re.findall(r"[a-z0-9]+", normalizar_texto(nicho))
    return [
        palavra
        for palavra in palavras
        if len(palavra) >= 2
        and palavra not in PALAVRAS_GENERICAS_CVM
        and palavra not in PALAVRAS_CONEXAO
    ]


def _texto_corresponde_aos_termos(texto: str, termos: list[str]) -> bool:
    if not termos:
        return True
    texto_normalizado = normalizar_texto(texto)
    palavras_texto = re.findall(r"[a-z0-9]+", texto_normalizado)
    for termo in termos:
        equivalentes = EQUIVALENTES_SETORIAIS.get(termo, (termo,))
        encontrou = False
        for equivalente in equivalentes:
            if " " in equivalente:
                encontrou = bool(
                    re.search(
                        rf"(?<!\w){re.escape(equivalente)}(?!\w)",
                        texto_normalizado,
                    )
                )
            else:
                raiz = equivalente.rstrip("s")
                encontrou = any(
                    palavra == equivalente
                    or palavra.rstrip("s") == raiz
                    or palavra.startswith(raiz)
                    for palavra in palavras_texto
                )
            if encontrou:
                break
        if not encontrou:
            return False
    return True


def _pontuacao_aderencia_b3(item: dict[str, Any], termos: list[str]) -> int:
    if not termos:
        return 0
    segmento = f"{item.get('segment', '')} {item.get('segment_eng', '')}"
    nomes = (
        f"{item.get('company_name', '')} {item.get('trading_name', '')} "
        f"{item.get('issuing_company', '')}"
    )
    pontos = 0
    if _texto_corresponde_aos_termos(segmento, termos):
        pontos += 20
    if _texto_corresponde_aos_termos(nomes, termos):
        pontos += 5
    return pontos


def buscar_corretoras_cvm(
    nicho: str,
    localizacao: str,
    limite: int,
    *,
    sessao: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Retorna apenas registros CVM ativos e na cidade/UF solicitada."""
    cidade, uf = separar_cidade_uf(localizacao, permite_brasil=True)
    limite = max(1, min(int(limite), LIMITE_MAXIMO))
    itens = _obter_lista(BRASILAPI_CVM_URL, parametros={"uf": uf} if uf else None, sessao=sessao)
    cidade_normalizada = normalizar_texto(cidade)
    nicho_normalizado = normalizar_texto(nicho)
    nicho_classificado = _tem_termo(nicho, TERMOS_CVM | TERMOS_FINANCEIROS_GERAIS)
    termos_especificos = _termos_especificos_cvm(nicho)

    filtrados: list[dict[str, Any]] = []
    for item in itens:
        if normalizar_texto(item.get("status")) != "em funcionamento normal":
            continue
        if uf and str(item.get("uf") or "").upper() != uf:
            continue
        if cidade_normalizada and normalizar_texto(item.get("municipio")) != cidade_normalizada:
            continue
        pesquisavel = normalizar_texto(
            f"{item.get('type', '')} {item.get('nome_social', '')} {item.get('nome_comercial', '')}"
        )
        if termos_especificos and not _texto_corresponde_aos_termos(
            pesquisavel, termos_especificos
        ):
            continue
        if (
            not nicho_classificado
            and not termos_especificos
            and nicho_normalizado
            and nicho_normalizado not in pesquisavel
        ):
            continue
        if not normalizar_cnpj(item.get("cnpj")):
            continue
        filtrados.append(item)

    filtrados.sort(
        key=lambda item: normalizar_texto(
            _texto_cadastral(item.get("nome_comercial"))
            or _texto_cadastral(item.get("nome_social"))
        )
    )
    leads: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for item in filtrados:
        cnpj = normalizar_cnpj(item.get("cnpj"))
        if cnpj in vistos:
            continue
        vistos.add(cnpj)
        razao_social = _texto_cadastral(item.get("nome_social")) or "Corretora sem nome"
        nome = _texto_cadastral(item.get("nome_comercial")) or razao_social
        municipio = str(item.get("municipio") or cidade).strip()
        item_uf = str(item.get("uf") or uf).strip().upper()
        codigo_cvm = str(item.get("codigo_cvm") or "").strip()
        leads.append(
            {
                "place_id": f"cvm:{cnpj}",
                "cnpj": cnpj,
                "nome_empresa": nome,
                "razao_social": razao_social,
                "decisor": "",
                "nicho": "Corretora / distribuidora de valores",
                "endereco": _endereco_cvm(item),
                "cidade": ", ".join(parte for parte in (municipio, item_uf) if parte),
                "telefone": _texto_cadastral(item.get("telefone")),
                "site": "",
                "email": _texto_cadastral(item.get("email")),
                "status": "Novos Leads",
                "status_receita": "EM FUNCIONAMENTO NORMAL (CVM)",
                "origem": "BrasilAPI / CVM",
                "observacoes": (
                    "Registro em funcionamento normal na listagem pública da CVM"
                    + (f" (código {codigo_cvm})" if codigo_cvm else "")
                    + ". Confirme os dados antes do contato comercial."
                ),
            }
        )
        if len(leads) >= limite:
            break
    return leads


def buscar_empresas_b3(
    nicho: str,
    localizacao: str,
    limite: int,
    *,
    sessao: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Busca emissores ativos na B3 e enriquece o CNPJ para obter cidade e contato."""
    cidade, uf = separar_cidade_uf(localizacao, permite_brasil=True)
    limite = max(1, min(int(limite), LIMITE_MAXIMO))
    propria = sessao is None
    cliente = sessao or _sessao_resiliente()
    try:
        itens = _obter_lista(BRASILAPI_B3_URL, sessao=cliente)
        termos_nicho = _termos_especificos_b3(nicho)
        agrupados: dict[str, dict[str, Any]] = {}
        emissores: dict[str, set[str]] = {}
        aderencia_por_cnpj: dict[str, int] = {}
        for item in itens:
            if normalizar_texto(item.get("status")) != "a":
                continue
            cnpj = normalizar_cnpj(item.get("cnpj"))
            if not cnpj:
                continue
            aderencia = _pontuacao_aderencia_b3(item, termos_nicho)
            if termos_nicho and aderencia == 0:
                continue
            if cnpj not in agrupados or aderencia > aderencia_por_cnpj.get(cnpj, -1):
                agrupados[cnpj] = item
                aderencia_por_cnpj[cnpj] = aderencia
            emissor = str(item.get("issuing_company") or "").strip()
            if emissor:
                emissores.setdefault(cnpj, set()).add(emissor)

        ordenados = sorted(
            agrupados.items(),
            key=lambda par: (
                -aderencia_por_cnpj.get(par[0], 0),
                normalizar_texto(par[1].get("trading_name") or par[1].get("company_name")),
            ),
        )
        if ordenados and (cidade or uf):
            chave_local = normalizar_texto(f"{cidade}|{uf}")
            deslocamento = sum(
                (indice + 1) * ord(caractere)
                for indice, caractere in enumerate(chave_local)
            ) % len(ordenados)
            ordenados = ordenados[deslocamento:] + ordenados[:deslocamento]
        maximo_consultas = min(
            len(ordenados),
            limite if not cidade and not uf else min(100, max(30, limite * 5)),
        )
        cidade_normalizada = normalizar_texto(cidade)
        leads: list[dict[str, Any]] = []
        falhas = 0
        for posicao, (cnpj, item) in enumerate(ordenados[:maximo_consultas]):
            empresa: dict[str, Any] | None = None
            try:
                empresa = _consultar_cnpj(cnpj, cliente)
            except (requests.RequestException, ValueError):
                falhas += 1
            finally:
                if posicao < maximo_consultas - 1:
                    time.sleep(0.12)
            if not empresa or normalizar_texto(empresa.get("descricao_situacao_cadastral")) != "ativa":
                continue
            municipio = str(empresa.get("municipio") or "").strip()
            empresa_uf = str(empresa.get("uf") or "").strip().upper()
            if cidade_normalizada and normalizar_texto(municipio) != cidade_normalizada:
                continue
            if uf and empresa_uf != uf:
                continue

            razao_social = str(
                empresa.get("razao_social") or item.get("company_name") or "Empresa listada sem nome"
            ).strip()
            nome = str(
                empresa.get("nome_fantasia") or item.get("trading_name") or razao_social
            ).strip()
            segmento = str(item.get("segment") or "").strip()
            atividade = str(empresa.get("cnae_fiscal_descricao") or "").strip()
            nicho_lead = atividade or segmento or "Empresa listada na B3"
            telefone = _texto_cadastral(
                empresa.get("ddd_telefone_1") or empresa.get("ddd_telefone_2") or ""
            )
            email = _texto_cadastral(empresa.get("email"))
            codigos = ", ".join(sorted(emissores.get(cnpj, set())))
            mercado = str(item.get("market") or "").strip()
            detalhes = [
                "Emissor ativo na listagem pública da B3 e empresa ativa na Receita Federal."
            ]
            if codigos:
                detalhes.append(f"Código(s) do emissor: {codigos}.")
            if segmento:
                detalhes.append(f"Segmento B3: {segmento}.")
            if mercado:
                detalhes.append(f"Mercado: {mercado}.")
            detalhes.append("Confirme os dados antes do contato comercial.")
            leads.append(
                {
                    "place_id": f"b3:{cnpj}",
                    "cnpj": cnpj,
                    "nome_empresa": nome,
                    "razao_social": razao_social,
                    "decisor": _decisor_cnpj(empresa.get("qsa")),
                    "nicho": nicho_lead,
                    "endereco": _endereco_cnpj(empresa),
                    "cidade": ", ".join(parte for parte in (municipio, empresa_uf) if parte),
                    "telefone": telefone,
                    "site": "",
                    "email": email,
                    "status": "Novos Leads",
                    "status_receita": "ATIVA (Receita Federal e listagem B3)",
                    "origem": "BrasilAPI / B3 + Receita Federal",
                    "observacoes": " ".join(detalhes),
                }
            )
            if len(leads) >= limite:
                break
        if not leads and maximo_consultas and falhas == maximo_consultas:
            raise RuntimeError("Não foi possível enriquecer os CNPJs listados pela B3.")
        return leads
    finally:
        if propria:
            cliente.close()
