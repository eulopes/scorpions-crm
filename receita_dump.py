"""Fase 1 do motor de trigger events -- ingestão do dump mensal de Dados
Abertos CNPJ da Receita Federal e diff mês-a-mês para detectar **nova filial**,
reativação e mudança de endereço no universo inteiro de CNPJs (não só na base).

Store: DuckDB (embutido, colunar, lê CSV direto, aguenta ~60M linhas e o
ANTI JOIN mensal num arquivo só, sem servidor). Trocável por Postgres na
Fase 4, quando houver escrita concorrente multi-tenant.

Este módulo cobre: schema, download da competência e carga de
``Estabelecimentos`` no DuckDB. O diff e o matcher para ICP ficam em passos
seguintes. Nada aqui depende de Streamlit.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import duckdb
import requests

from niche_sources import _sessao_resiliente

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("RECEITA_DUMP_DB", str(APP_DIR / "receita_dump.duckdb")))
CACHE_DIR = Path(os.getenv("RECEITA_DUMP_CACHE", str(APP_DIR / ".receita_cache")))

BASE_URL = "https://arquivos.receitafederal.gov.br/dados/cnpj/dados_abertos_cnpj"

# 10 fatias de Estabelecimentos + referências que interessam ao matcher.
ARQUIVOS_ESTABELECIMENTOS = tuple(f"Estabelecimentos{i}.zip" for i in range(10))
ARQUIVOS_REFERENCIA = ("Municipios.zip", "Cnaes.zip")

# Colunas do CSV de Estabelecimentos, na ordem do layout oficial (sem cabeçalho).
COLUNAS_ESTAB = (
    "cnpj_basico", "cnpj_ordem", "cnpj_dv", "matriz_filial", "nome_fantasia",
    "situacao_cadastral", "data_situacao_cadastral", "motivo_situacao",
    "nome_cidade_exterior", "pais", "data_inicio_atividade", "cnae_principal",
    "cnae_secundaria", "tipo_logradouro", "logradouro", "numero", "complemento",
    "bairro", "cep", "uf", "municipio", "ddd1", "telefone1", "ddd2", "telefone2",
    "ddd_fax", "fax", "email", "situacao_especial", "data_situacao_especial",
)

_SITUACAO = {
    "01": "NULA", "02": "ATIVA", "03": "SUSPENSA", "04": "INAPTA", "08": "BAIXADA",
}

_RE_COMPETENCIA = re.compile(r"^\d{4}-\d{2}$")


def _validar_competencia(competencia: str) -> str:
    comp = str(competencia or "").strip()
    if not _RE_COMPETENCIA.match(comp):
        raise ValueError("Competência deve estar no formato AAAA-MM (ex.: 2026-08).")
    mes = int(comp[5:7])
    if not 1 <= mes <= 12:
        raise ValueError("Mês inválido na competência.")
    return comp


def url_competencia(competencia: str) -> str:
    return f"{BASE_URL}/{_validar_competencia(competencia)}"


def arquivos_da_competencia(*, com_referencia: bool = True) -> tuple[str, ...]:
    """Lista fixa e conhecida -- não depende de HTTP."""
    if com_referencia:
        return ARQUIVOS_ESTABELECIMENTOS + ARQUIVOS_REFERENCIA
    return ARQUIVOS_ESTABELECIMENTOS


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
        CREATE TABLE IF NOT EXISTS estabelecimentos (
            competencia TEXT NOT NULL,
            cnpj TEXT NOT NULL,
            cnpj_basico TEXT,
            matriz_filial TEXT,
            nome_fantasia TEXT,
            situacao TEXT,
            data_situacao DATE,
            data_inicio_atividade DATE,
            cnae_principal TEXT,
            uf TEXT,
            municipio_codigo TEXT,
            bairro TEXT,
            cep TEXT,
            logradouro TEXT,
            numero TEXT,
            email TEXT,
            telefone TEXT,
            PRIMARY KEY (competencia, cnpj)
        )
        """
    )
    conexao.execute(
        "CREATE TABLE IF NOT EXISTS municipios (codigo TEXT PRIMARY KEY, nome TEXT)"
    )
    conexao.execute(
        "CREATE TABLE IF NOT EXISTS cnaes (codigo TEXT PRIMARY KEY, descricao TEXT)"
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


def _baixar_arquivo(
    sessao: requests.Session, url: str, destino: Path
) -> Path:
    """Baixa em streaming; pula se já existe com tamanho > 0."""
    if destino.exists() and destino.stat().st_size > 0:
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_suffix(destino.suffix + ".parcial")
    with sessao.get(url, stream=True, timeout=(10, 120)) as resposta:
        resposta.raise_for_status()
        with parcial.open("wb") as saida:
            for pedaco in resposta.iter_content(chunk_size=1 << 20):
                if pedaco:
                    saida.write(pedaco)
    parcial.replace(destino)
    return destino


def baixar_competencia(
    competencia: str,
    *,
    apenas: tuple[str, ...] | None = None,
    sessao: requests.Session | None = None,
    destino_base: Path | None = None,
) -> list[Path]:
    """Baixa os .zip da competência para o cache local. Devolve os caminhos.

    ``apenas`` restringe a lista (ex.: só uma fatia, em teste). Idempotente:
    arquivo já baixado é reaproveitado.
    """
    comp = _validar_competencia(competencia)
    base = (destino_base or CACHE_DIR) / comp
    alvos = apenas or arquivos_da_competencia()
    propria = sessao is None
    cliente = sessao or _sessao_resiliente()
    baixados: list[Path] = []
    try:
        for nome in alvos:
            caminho = _baixar_arquivo(cliente, f"{url_competencia(comp)}/{nome}", base / nome)
            baixados.append(caminho)
    finally:
        if propria:
            cliente.close()
    return baixados


def _membro_csv(caminho_zip: Path) -> str:
    with zipfile.ZipFile(caminho_zip) as arquivo:
        nomes = [n for n in arquivo.namelist() if not n.endswith("/")]
    if not nomes:
        raise ValueError(f"{caminho_zip.name} está vazio.")
    return nomes[0]


def _sql_carga_estabelecimentos(caminho_csv: str, competencia: str) -> str:
    colunas = ", ".join(f"'{nome}': 'VARCHAR'" for nome in COLUNAS_ESTAB)
    casos_situacao = " ".join(
        f"WHEN '{codigo}' THEN '{rotulo}'" for codigo, rotulo in _SITUACAO.items()
    )
    return f"""
        INSERT OR REPLACE INTO estabelecimentos
        SELECT
            '{competencia}' AS competencia,
            cnpj_basico || lpad(cnpj_ordem, 4, '0') || lpad(cnpj_dv, 2, '0') AS cnpj,
            cnpj_basico,
            matriz_filial,
            nullif(trim(nome_fantasia), '') AS nome_fantasia,
            CASE situacao_cadastral {casos_situacao} ELSE situacao_cadastral END AS situacao,
            try_strptime(data_situacao_cadastral, '%Y%m%d')::DATE AS data_situacao,
            try_strptime(data_inicio_atividade, '%Y%m%d')::DATE AS data_inicio_atividade,
            nullif(trim(cnae_principal), '') AS cnae_principal,
            uf,
            municipio AS municipio_codigo,
            nullif(trim(bairro), '') AS bairro,
            nullif(regexp_replace(cep, '[^0-9]', '', 'g'), '') AS cep,
            nullif(trim(concat_ws(' ', tipo_logradouro, logradouro)), '') AS logradouro,
            nullif(trim(numero), '') AS numero,
            nullif(trim(email), '') AS email,
            nullif(trim(coalesce(ddd1, '') || coalesce(telefone1, '')), '') AS telefone
        FROM read_csv(
            '{caminho_csv}',
            delim=';', header=false, quote='"', escape='"',
            encoding='latin-1', ignore_errors=true, null_padding=true,
            columns={{{colunas}}}
        )
    """


def carregar_estabelecimentos(
    conexao: duckdb.DuckDBPyConnection,
    competencia: str,
    arquivos: list[Path],
) -> int:
    """Carrega uma ou mais fatias ``Estabelecimentos*.zip`` (ou .csv cru) na
    tabela ``estabelecimentos`` para a competência. Devolve o total de linhas
    da competência após a carga."""
    comp = _validar_competencia(competencia)
    for caminho in arquivos:
        caminho = Path(caminho)
        if caminho.suffix.lower() == ".zip":
            with zipfile.ZipFile(caminho) as zf, tempfile.TemporaryDirectory() as tmp:
                membro = _membro_csv(caminho)
                extraido = Path(zf.extract(membro, tmp))
                conexao.execute(_sql_carga_estabelecimentos(extraido.as_posix(), comp))
        else:
            conexao.execute(_sql_carga_estabelecimentos(caminho.as_posix(), comp))

    total = conexao.execute(
        "SELECT count(*) FROM estabelecimentos WHERE competencia = ?", [comp]
    ).fetchone()[0]
    conexao.execute(
        "INSERT OR REPLACE INTO competencias_carregadas (competencia, linhas) VALUES (?, ?)",
        [comp, total],
    )
    return int(total)


def carregar_referencia(
    conexao: duckdb.DuckDBPyConnection, tabela: str, arquivo: Path
) -> int:
    """``Municipios`` e ``Cnaes``: CSV de 2 colunas (codigo;descricao)."""
    if tabela not in ("municipios", "cnaes"):
        raise ValueError("tabela deve ser 'municipios' ou 'cnaes'.")
    coluna_valor = "nome" if tabela == "municipios" else "descricao"
    arquivo = Path(arquivo)

    def _carregar(csv_path: str) -> None:
        conexao.execute(
            f"""
            INSERT OR REPLACE INTO {tabela}
            SELECT trim(codigo), nullif(trim(valor), '') FROM read_csv(
                '{csv_path}', delim=';', header=false, quote='"', escape='"',
                encoding='latin-1', ignore_errors=true,
                columns={{'codigo': 'VARCHAR', 'valor': 'VARCHAR'}}
            )
            """
        )

    if arquivo.suffix.lower() == ".zip":
        with zipfile.ZipFile(arquivo) as zf, tempfile.TemporaryDirectory() as tmp:
            extraido = Path(zf.extract(_membro_csv(arquivo), tmp))
            _carregar(extraido.as_posix())
    else:
        _carregar(arquivo.as_posix())
    _ = coluna_valor  # documenta o mapeamento; a query usa alias fixo 'valor'
    return int(conexao.execute(f"SELECT count(*) FROM {tabela}").fetchone()[0])
