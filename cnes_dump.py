"""Fase 3 do motor de trigger events -- ingestão do CNES (Cadastro Nacional de
Estabelecimentos de Saúde, Ministério da Saúde) e diff entre duas cargas para
detectar **nova unidade de saúde privada** e mudança de endereço, alimentando
especificamente o ICP "Clínicas, Hospitais & Laboratórios".

Fonte real, confirmada por download e inspeção do arquivo (nunca por suposição
de layout): a página do dataset em dadosabertos.saude.gov.br é uma UI CKAN que
não expõe API em /api/3/action (testado, 404) -- o link de download real do
recurso "CNES Estabelecimentos" (CSV) é um objeto fixo no S3:
    https://s3.sa-east-1.amazonaws.com/ckan.saude.gov.br/CNES/cnes_estabelecimentos_csv.zip
Zip de ~56 MB contendo um único CSV (~230 MB, 635 mil linhas em 12/09/2026),
delimitador ';', campos entre aspas duplas, UTF-8, CRLF, COM cabeçalho -- ao
contrário do dump da Receita. É um snapshot NACIONAL COMPLETO atualizado a
cada poucos dias, não um arquivo por competência mensal -- por isso aqui
"competencia" é a data em que o snapshot foi baixado (AAAA-MM-DD), e o diff
compara duas datas de download quaisquer, não dois meses fiscais.

Store: DuckDB, mesmo padrão de receita_dump.py/obras_dump.py -- arquivo
próprio (CNES_DUMP_DB), gitignored, sem servidor.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import duckdb
import requests

from change_detection import FONTE_CNES
from crm_strategy import classificar_icp
from niche_sources import _sessao_resiliente, normalizar_cnpj

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("CNES_DUMP_DB", str(APP_DIR / "cnes_dump.duckdb")))
CACHE_DIR = Path(os.getenv("CNES_DUMP_CACHE", str(APP_DIR / ".cnes_cache")))

URL_DUMP = "https://s3.sa-east-1.amazonaws.com/ckan.saude.gov.br/CNES/cnes_estabelecimentos_csv.zip"

# CNES - Tabela de Tipo de Estabelecimento (Ministério da Saúde/DATASUS,
# estável há anos -- fonte: Manual Técnico do CNES). Só os tipos com potencial
# de decisão de compra própria (exclui, por ex., 22 Consultório Isolado --
# tipicamente profissional autônomo sem orçamento de infraestrutura B2B, e
# unidades móveis/administrativas que não ocupam prédio comercial fixo).
_NOME_TP_UNIDADE = {
    "4": "Policlínica",
    "5": "Hospital geral",
    "7": "Hospital especializado",
    "15": "Unidade mista de saúde",
    "36": "Clínica especializada",
    "39": "Laboratório / unidade de apoio a diagnose e terapia",
    "62": "Hospital-dia isolado",
}
TP_UNIDADE_RELEVANTES = tuple(_NOME_TP_UNIDADE)

# Tabela CONCLA de Natureza Jurídica (IBGE/Receita): códigos 1xxx são sempre
# Administração Pública (órgão, autarquia, fundação pública). Como o CNES
# cadastra tanto o hospital municipal quanto a clínica privada sob a mesma
# "esfera de gestão" (TP_GESTAO/DS_ESFERA_ADMINISTRATIVA descrevem o SUS que
# paga, não quem é dono), é a natureza jurídica -- não a esfera -- que separa
# quem decide compra própria de quem depende de licitação pública.
_PREFIXO_NATUREZA_PUBLICA = "1"

# Subconjunto (de 36 colunas reais) que a carga usa -- lido pelo NOME do
# cabeçalho real do CSV (não por posição), então é resiliente a colunas
# adicionais ou reordenadas no arquivo do governo. Lista mantida aqui como
# documentação do layout confirmado e para as fixtures de teste.
COLUNAS_CSV = (
    "CO_CNES", "CO_UNIDADE", "CO_UF", "CO_IBGE", "NU_CNPJ_MANTENEDORA",
    "NO_RAZAO_SOCIAL", "NO_FANTASIA", "TP_UNIDADE", "CO_CEP", "NO_LOGRADOURO",
    "NU_ENDERECO", "NO_BAIRRO", "NU_TELEFONE", "NU_LATITUDE", "NU_LONGITUDE",
    "NU_CNPJ", "NO_EMAIL", "CO_NATUREZA_JUR", "DS_ESFERA_ADMINISTRATIVA",
)

# CO_UF do CNES é o código IBGE de 2 dígitos (ex.: "35" = São Paulo), não a
# sigla -- tabela do IBGE, estável há décadas. Usada só para o filtro --ufs
# do orquestrador aceitar siglas como o resto do sistema (Receita/obras).
_UF_POR_CODIGO_IBGE = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL",
    "28": "SE", "29": "BA", "31": "MG", "32": "ES", "33": "RJ", "35": "SP", "41": "PR",
    "42": "SC", "43": "RS", "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}

_RE_COMPETENCIA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validar_competencia(competencia: str) -> str:
    comp = str(competencia or "").strip()
    if not _RE_COMPETENCIA.match(comp):
        raise ValueError("Competência do CNES deve estar no formato AAAA-MM-DD (data do download).")
    return comp


@contextmanager
def conectar() -> Iterator[duckdb.DuckDBPyConnection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conexao = duckdb.connect(str(DB_PATH))
    try:
        yield conexao
    finally:
        conexao.close()


def migrar_esquema(conexao: duckdb.DuckDBPyConnection) -> None:
    conexao.execute(
        """
        CREATE TABLE IF NOT EXISTS estabelecimentos_saude (
            competencia TEXT NOT NULL,
            co_cnes TEXT NOT NULL,
            co_unidade TEXT,
            uf TEXT,
            municipio_codigo TEXT,
            cnpj TEXT,
            cnpj_mantenedora TEXT,
            razao_social TEXT,
            nome_fantasia TEXT,
            tp_unidade TEXT,
            natureza_jur TEXT,
            esfera_administrativa TEXT,
            cep TEXT,
            logradouro TEXT,
            numero TEXT,
            bairro TEXT,
            telefone TEXT,
            email TEXT,
            latitude DOUBLE,
            longitude DOUBLE,
            PRIMARY KEY (competencia, co_cnes)
        )
        """
    )
    conexao.execute(
        "CREATE TABLE IF NOT EXISTS competencias_carregadas ("
        "competencia TEXT PRIMARY KEY, linhas BIGINT, carregada_em TIMESTAMP DEFAULT now())"
    )


def competencias_carregadas(conexao: duckdb.DuckDBPyConnection) -> list[str]:
    linhas = conexao.execute(
        "SELECT competencia FROM competencias_carregadas ORDER BY competencia"
    ).fetchall()
    return [linha[0] for linha in linhas]


def baixar_dump(
    *, sessao: requests.Session | None = None, destino: Path | None = None
) -> Path:
    """Baixa o zip único do CNES (idempotente: pula se já existe)."""
    destino = destino or (CACHE_DIR / "cnes_estabelecimentos_csv.zip")
    if destino.exists() and destino.stat().st_size > 0:
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    propria = sessao is None
    cliente = sessao or _sessao_resiliente()
    parcial = destino.with_suffix(destino.suffix + ".parcial")
    try:
        with cliente.get(URL_DUMP, stream=True, timeout=(10, 120)) as resposta:
            resposta.raise_for_status()
            with parcial.open("wb") as saida:
                for pedaco in resposta.iter_content(chunk_size=1 << 20):
                    if pedaco:
                        saida.write(pedaco)
        parcial.replace(destino)
    finally:
        if propria:
            cliente.close()
    return destino


def _membro_csv(caminho_zip: Path) -> str:
    with zipfile.ZipFile(caminho_zip) as arquivo:
        nomes = [n for n in arquivo.namelist() if not n.endswith("/")]
    if not nomes:
        raise ValueError(f"{caminho_zip.name} está vazio.")
    return nomes[0]


def _sql_carga(caminho_csv: str, competencia: str) -> str:
    return f"""
        INSERT OR REPLACE INTO estabelecimentos_saude
        SELECT
            '{competencia}' AS competencia,
            CO_CNES AS co_cnes,
            CO_UNIDADE AS co_unidade,
            CO_UF AS uf,
            CO_IBGE AS municipio_codigo,
            nullif(regexp_replace(NU_CNPJ, '[^0-9]', '', 'g'), '') AS cnpj,
            nullif(regexp_replace(NU_CNPJ_MANTENEDORA, '[^0-9]', '', 'g'), '') AS cnpj_mantenedora,
            nullif(trim(NO_RAZAO_SOCIAL), '') AS razao_social,
            nullif(trim(NO_FANTASIA), '') AS nome_fantasia,
            TP_UNIDADE AS tp_unidade,
            nullif(trim(CO_NATUREZA_JUR), '') AS natureza_jur,
            nullif(trim(DS_ESFERA_ADMINISTRATIVA), '') AS esfera_administrativa,
            nullif(regexp_replace(CO_CEP, '[^0-9]', '', 'g'), '') AS cep,
            nullif(trim(NO_LOGRADOURO), '') AS logradouro,
            nullif(trim(NU_ENDERECO), '') AS numero,
            nullif(trim(NO_BAIRRO), '') AS bairro,
            nullif(regexp_replace(NU_TELEFONE, '[^0-9]', '', 'g'), '') AS telefone,
            nullif(trim(NO_EMAIL), '') AS email,
            try_cast(NU_LATITUDE AS DOUBLE) AS latitude,
            try_cast(NU_LONGITUDE AS DOUBLE) AS longitude
        FROM read_csv(
            '{caminho_csv}',
            delim=';', header=true, quote='"', escape='"',
            encoding='utf-8', ignore_errors=true, null_padding=true,
            all_varchar=true
        )
        WHERE CO_CNES IS NOT NULL AND trim(CO_CNES) <> ''
    """


def carregar_estabelecimentos(
    conexao: duckdb.DuckDBPyConnection, competencia: str, caminho: Path
) -> int:
    """Carrega o zip (ou .csv cru, em teste) do CNES para a competência
    (data de download). Devolve o total de linhas carregadas."""
    comp = _validar_competencia(competencia)
    caminho = Path(caminho)
    if caminho.suffix.lower() == ".zip":
        with zipfile.ZipFile(caminho) as zf, tempfile.TemporaryDirectory() as tmp:
            extraido = Path(zf.extract(_membro_csv(caminho), tmp))
            conexao.execute(_sql_carga(extraido.as_posix(), comp))
    else:
        conexao.execute(_sql_carga(caminho.as_posix(), comp))

    total = conexao.execute(
        "SELECT count(*) FROM estabelecimentos_saude WHERE competencia = ?", [comp]
    ).fetchone()[0]
    conexao.execute(
        "INSERT OR REPLACE INTO competencias_carregadas (competencia, linhas) VALUES (?, ?)",
        [comp, total],
    )
    return int(total)


# --- Diff entre duas cargas ----------------------------------------------------

TIPOS_EVENTO = (
    "NOVO_ESTABELECIMENTO",  # CO_CNES novo -- unidade de saúde recém-cadastrada
    "MUDANCA_ENDERECO",      # mesmo CO_CNES, endereço mudou
    "DESCADASTRADO",         # CO_CNES sumiu do snapshot novo (fechou/descredenciou)
)

_CAMPOS_EVENTO = (
    "co_cnes", "uf", "municipio_codigo", "cnpj", "cnpj_mantenedora",
    "razao_social", "nome_fantasia", "tp_unidade", "natureza_jur",
    "logradouro", "numero", "bairro", "cep", "telefone", "email",
    "logradouro_antes",
)


def _filtro_relevancia_sql(alias: str) -> str:
    """Só entra no diff quem é candidato real de venda B2B: CNPJ próprio
    preenchido (descarta quem opera só sob o CNPJ da mantenedora/prefeitura),
    natureza jurídica privada (exclui administração pública direta) e tipo de
    unidade com porte para decidir compra própria."""
    tipos = ", ".join(f"'{t}'" for t in TP_UNIDADE_RELEVANTES)
    return (
        f"{alias}.cnpj IS NOT NULL AND {alias}.cnpj <> '' "
        f"AND {alias}.natureza_jur IS NOT NULL "
        f"AND left({alias}.natureza_jur, 1) <> '{_PREFIXO_NATUREZA_PUBLICA}' "
        f"AND {alias}.tp_unidade IN ({tipos})"
    )


def _classificar_evento_sql(comp_novo: str, comp_antigo: str) -> str:
    return f"""
        WITH n AS (SELECT * FROM estabelecimentos_saude WHERE competencia = '{comp_novo}'),
             a AS (SELECT * FROM estabelecimentos_saude WHERE competencia = '{comp_antigo}'),
             classificado AS (
                SELECT
                    CASE
                        WHEN a.co_cnes IS NULL AND ({_filtro_relevancia_sql("n")})
                            THEN 'NOVO_ESTABELECIMENTO'
                        WHEN n.co_cnes IS NULL AND ({_filtro_relevancia_sql("a")})
                            THEN 'DESCADASTRADO'
                        WHEN a.co_cnes IS NOT NULL AND n.co_cnes IS NOT NULL
                            AND ({_filtro_relevancia_sql("n")}) AND (
                                coalesce(a.logradouro, '') <> coalesce(n.logradouro, '')
                                OR coalesce(a.numero, '') <> coalesce(n.numero, '')
                                OR coalesce(a.municipio_codigo, '') <> coalesce(n.municipio_codigo, '')
                            )
                            THEN 'MUDANCA_ENDERECO'
                        ELSE NULL
                    END AS tipo,
                    coalesce(n.co_cnes, a.co_cnes) AS co_cnes,
                    coalesce(n.uf, a.uf) AS uf,
                    coalesce(n.municipio_codigo, a.municipio_codigo) AS municipio_codigo,
                    coalesce(n.cnpj, a.cnpj) AS cnpj,
                    coalesce(n.cnpj_mantenedora, a.cnpj_mantenedora) AS cnpj_mantenedora,
                    coalesce(n.razao_social, a.razao_social) AS razao_social,
                    coalesce(n.nome_fantasia, a.nome_fantasia) AS nome_fantasia,
                    coalesce(n.tp_unidade, a.tp_unidade) AS tp_unidade,
                    coalesce(n.natureza_jur, a.natureza_jur) AS natureza_jur,
                    coalesce(n.logradouro, a.logradouro) AS logradouro,
                    coalesce(n.numero, a.numero) AS numero,
                    coalesce(n.bairro, a.bairro) AS bairro,
                    coalesce(n.cep, a.cep) AS cep,
                    coalesce(n.telefone, a.telefone) AS telefone,
                    coalesce(n.email, a.email) AS email,
                    a.logradouro AS logradouro_antes
                FROM n
                FULL OUTER JOIN a ON a.co_cnes = n.co_cnes
             )
        SELECT * FROM classificado WHERE tipo IS NOT NULL
    """


def diff_dumps(
    conexao: duckdb.DuckDBPyConnection,
    comp_novo: str,
    comp_antigo: str,
    *,
    tipos: tuple[str, ...] | None = None,
    ufs: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Compara duas cargas (datas de download) já carregadas e devolve os
    eventos de trigger, já restritos a quem passa no filtro de relevância
    (privado, CNPJ próprio, tipo de unidade com decisão de compra)."""
    comp_novo = _validar_competencia(comp_novo)
    comp_antigo = _validar_competencia(comp_antigo)
    carregadas = set(competencias_carregadas(conexao))
    faltando = {comp_novo, comp_antigo} - carregadas
    if faltando:
        raise ValueError(f"Competência(s) não carregada(s): {', '.join(sorted(faltando))}.")

    filtro_tipos = set(tipos or TIPOS_EVENTO)
    filtro_ufs = {u.upper() for u in ufs} if ufs else None

    colunas = ["tipo", *_CAMPOS_EVENTO]
    eventos: list[dict[str, Any]] = []
    for linha in conexao.execute(_classificar_evento_sql(comp_novo, comp_antigo)).fetchall():
        evento = dict(zip(colunas, linha))
        # CO_UF do CNES é o código IBGE (2 dígitos) -- convertido para sigla
        # aqui para o resto do pipeline (filtro --ufs, lead.cidade) trabalhar
        # com o mesmo formato usado por Receita/obras.
        evento["uf"] = _UF_POR_CODIGO_IBGE.get(str(evento.get("uf") or ""), str(evento.get("uf") or ""))
        if evento["tipo"] not in filtro_tipos:
            continue
        if filtro_ufs is not None and evento["uf"].upper() not in filtro_ufs:
            continue
        eventos.append(evento)
    return eventos


# --- Matcher: evento do diff -> lead candidato + payload de sinal -------------

_EVENTO_PARA_MUDANCA = {
    "NOVO_ESTABELECIMENTO": "novo_estabelecimento_saude",
    "MUDANCA_ENDERECO": "endereco_alterado_saude",
}


def _local_evento(evento: dict[str, Any]) -> str:
    uf = str(evento.get("uf") or "").strip().upper()
    return uf


def _endereco_evento(evento: dict[str, Any]) -> str:
    principal = " ".join(
        parte for parte in (str(evento.get("logradouro") or ""), str(evento.get("numero") or "")) if parte
    ).strip()
    partes = [principal, str(evento.get("bairro") or "").strip(), _local_evento(evento)]
    cep = re.sub(r"\D", "", str(evento.get("cep") or ""))
    if len(cep) == 8:
        partes.append(f"CEP {cep[:5]}-{cep[5:]}")
    return " - ".join(parte for parte in partes if parte)


def candidatos_do_diff(
    eventos: list[dict[str, Any]], *, incluir_nao_classificado: bool = False
) -> dict[str, list]:
    """Traduz eventos do diff do CNES em leads candidatos (ICP já forçado a
    'Clínicas, Hospitais & Laboratórios' via classificar_icp com o tipo de
    unidade como nicho) + payload de mudança para sales_signals.

    DESCADASTRADO não vira candidato -- entra em ``supressoes`` (mesmo
    contrato de receita_dump/obras_dump)."""
    candidatos: list[dict[str, Any]] = []
    supressoes: list[str] = []

    for evento in eventos:
        tipo = evento["tipo"]
        cnpj_valido = normalizar_cnpj(evento.get("cnpj"))
        if not cnpj_valido:
            continue
        evento = {**evento, "cnpj": cnpj_valido}

        if tipo == "DESCADASTRADO":
            supressoes.append(evento["cnpj"])
            continue

        nome_tp = _NOME_TP_UNIDADE.get(str(evento.get("tp_unidade") or ""), "Estabelecimento de saúde")
        nome = (
            str(evento.get("nome_fantasia") or "").strip()
            or str(evento.get("razao_social") or "").strip()
            or f"CNPJ {evento['cnpj']}"
        )
        lead = {
            "place_id": f"cnes:{evento['co_cnes']}",
            "cnpj": evento["cnpj"],
            "nome_empresa": nome,
            "razao_social": str(evento.get("razao_social") or ""),
            "decisor": "",
            "nicho": nome_tp,
            "cnae_fiscal_descricao": nome_tp,
            "endereco": _endereco_evento(evento),
            "cidade": _local_evento(evento),
            "telefone": str(evento.get("telefone") or ""),
            "site": "",
            "email": str(evento.get("email") or ""),
            "status": "Novos Leads",
            "origem": FONTE_CNES,
            "observacoes": (
                f"Detectado pelo CNES (Ministério da Saúde) -- {nome_tp} ({tipo}). "
                "Confirme os dados antes do contato comercial."
            ),
        }
        segmento, servicos = classificar_icp(lead)
        lead["segmento_icp"] = segmento
        lead["servicos_recomendados"] = "; ".join(servicos)
        if segmento == "Não classificado" and not incluir_nao_classificado:
            continue

        mtipo = _EVENTO_PARA_MUDANCA[tipo]
        if tipo == "NOVO_ESTABELECIMENTO":
            mudanca = {
                "type": mtipo, "field": "co_cnes", "before": None,
                "after": {"local": _local_evento(evento), "tipo_unidade": nome_tp},
                "tipo_unidade": nome_tp,
                "days_between": None,
                "source": FONTE_CNES,
            }
        else:  # MUDANCA_ENDERECO
            mudanca = {
                "type": mtipo, "field": "address",
                "before": evento.get("logradouro_antes"),
                "after": _endereco_evento(evento),
                "tipo_unidade": nome_tp,
                "days_between": None,
                "source": FONTE_CNES,
            }
        candidatos.append({"evento_tipo": tipo, "lead": lead, "mudanca": mudanca})

    return {"candidatos": candidatos, "supressoes": supressoes}
