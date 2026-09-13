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
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import duckdb
import requests

from change_detection import FONTE_RECEITA_DUMP
from crm_strategy import classificar_icp
from niche_sources import _sessao_resiliente, normalizar_cnpj

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("RECEITA_DUMP_DB", str(APP_DIR / "receita_dump.duckdb")))
CACHE_DIR = Path(os.getenv("RECEITA_DUMP_CACHE", str(APP_DIR / ".receita_cache")))

# Domínio confirmado via captura do Wayback Machine (20241012) depois que
# arquivos.receitafederal.gov.br parou de servir a listagem (virou Nextcloud).
# Estrutura de pastas por competência (AAAA-MM) e nomes de arquivo continuam
# os mesmos -- só o host mudou.
BASE_URL = "https://dadosabertos.rfb.gov.br/CNPJ/dados_abertos_cnpj"

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


def mes_anterior(competencia: str) -> str:
    comp = _validar_competencia(competencia)
    ano, mes = int(comp[:4]), int(comp[5:7])
    return f"{ano - 1}-12" if mes == 1 else f"{ano}-{mes - 1:02d}"


def _sessao_probing() -> requests.Session:
    """Sessão SEM retry para a sondagem de "essa competência existe?" --
    _sessao_resiliente() tem retry=3 por chamada, o que é ótimo para um
    download que vale a pena insistir, mas péssimo aqui: com até
    ``tentativas`` meses testados em cascata, 3 retries cada faria uma
    descoberta com o servidor fora do ar levar minutos em vez de segundos.
    Uma falha já é sinal suficiente para essa competência."""
    sessao = requests.Session()
    sessao.headers.update({"User-Agent": "ScorpionsCRM/1.0"})
    return sessao


def _competencia_existe_no_servidor(competencia: str, sessao: requests.Session) -> bool:
    """Testa se a Receita já publicou a competência (a pasta existe e serve
    a 1ª fatia de Estabelecimentos) -- sem baixar o arquivo inteiro."""
    url = f"{url_competencia(competencia)}/{ARQUIVOS_ESTABELECIMENTOS[0]}"
    resposta = sessao.get(url, stream=True, timeout=(5, 8))
    try:
        return resposta.status_code == 200
    finally:
        resposta.close()


def descobrir_competencia_mais_recente(
    *, sessao: requests.Session | None = None, a_partir_de: str | None = None, tentativas: int = 6
) -> str | None:
    """Testa contra o servidor real, regredindo mês a mês a partir de
    ``a_partir_de`` (padrão: mês corrente), até achar uma competência
    publicada. A Receita costuma liberar o mês fiscal só na 2ª quinzena do
    mês seguinte, então o mês corrente frequentemente ainda não existe --
    daí a cascata em vez de assumir "mês atual" cegamente. Devolve None se
    nenhuma das ``tentativas`` competências mais recentes existir (ex.: 404
    em todas -- servidor no ar mas nada publicado ainda nessa janela).

    Levanta ConnectionError se o servidor estiver simplesmente inacessível
    (timeout/recusa de conexão) -- não faz sentido "tentar os próximos 5
    meses" quando o problema é a rede, não a publicação; e silenciar isso
    devolvendo None confundiria com o caso "nada publicado ainda"."""
    hoje = date.today()
    competencia = a_partir_de or f"{hoje.year:04d}-{hoje.month:02d}"
    competencia = _validar_competencia(competencia)
    propria = sessao is None
    cliente = sessao or _sessao_probing()
    try:
        for _ in range(max(1, tentativas)):
            try:
                existe = _competencia_existe_no_servidor(competencia, cliente)
            except requests.exceptions.RequestException as erro:
                raise ConnectionError(
                    f"Não foi possível conectar ao servidor da Receita para verificar "
                    f"{competencia}: {erro}"
                ) from erro
            if existe:
                return competencia
            competencia = mes_anterior(competencia)
    finally:
        if propria:
            cliente.close()
    return None


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


# --- Diff mês-a-mês -----------------------------------------------------------

TIPOS_EVENTO = (
    "NOVA_FILIAL",           # CNPJ novo, é filial de empresa que já existia
    "NOVO_ESTABELECIMENTO",  # CNPJ novo, é matriz (empresa nova)
    "REATIVACAO",            # situação voltou a ATIVA
    "MUDANCA_ENDERECO",      # endereço mudou, ambas ATIVA
    "BAIXA",                 # ATIVA -> BAIXADA/INAPTA/SUSPENSA (para suprimir lead)
)

_CAMPOS_EVENTO = (
    "cnpj", "cnpj_basico", "matriz_filial", "nome_fantasia", "situacao",
    "uf", "municipio_codigo", "municipio_nome", "cnae_principal", "cnae_descricao",
    "data_inicio_atividade", "logradouro", "numero", "bairro", "cep", "email",
    "telefone", "situacao_antes", "logradouro_antes", "municipio_antes",
)


def _primeiro_dia(competencia: str) -> date:
    comp = _validar_competencia(competencia)
    return date(int(comp[:4]), int(comp[5:7]), 1)


def _classificar_evento_sql(comp_novo: str, comp_antigo: str) -> str:
    return f"""
        WITH n AS (SELECT * FROM estabelecimentos WHERE competencia = '{comp_novo}'),
             a AS (SELECT * FROM estabelecimentos WHERE competencia = '{comp_antigo}'),
             classificado AS (
                SELECT
                    CASE
                        WHEN a.cnpj IS NULL AND n.situacao = 'ATIVA' AND n.matriz_filial = '2'
                            THEN 'NOVA_FILIAL'
                        WHEN a.cnpj IS NULL AND n.situacao = 'ATIVA'
                            THEN 'NOVO_ESTABELECIMENTO'
                        WHEN a.cnpj IS NOT NULL AND a.situacao <> 'ATIVA' AND n.situacao = 'ATIVA'
                            THEN 'REATIVACAO'
                        WHEN a.cnpj IS NOT NULL AND a.situacao = 'ATIVA' AND n.situacao <> 'ATIVA'
                            THEN 'BAIXA'
                        WHEN a.cnpj IS NOT NULL AND a.situacao = 'ATIVA' AND n.situacao = 'ATIVA' AND (
                                coalesce(a.logradouro, '') <> coalesce(n.logradouro, '')
                                OR coalesce(a.numero, '') <> coalesce(n.numero, '')
                                OR coalesce(a.municipio_codigo, '') <> coalesce(n.municipio_codigo, '')
                            )
                            THEN 'MUDANCA_ENDERECO'
                        ELSE NULL
                    END AS tipo,
                    n.cnpj, n.cnpj_basico, n.matriz_filial, n.nome_fantasia, n.situacao,
                    n.uf, n.municipio_codigo,
                    mun.nome AS municipio_nome,
                    n.cnae_principal,
                    cn.descricao AS cnae_descricao,
                    n.data_inicio_atividade,
                    n.logradouro, n.numero, n.bairro, n.cep, n.email, n.telefone,
                    a.situacao AS situacao_antes,
                    a.logradouro AS logradouro_antes,
                    a.municipio_codigo AS municipio_antes
                FROM n
                FULL OUTER JOIN a ON a.cnpj = n.cnpj
                LEFT JOIN municipios mun ON mun.codigo = n.municipio_codigo
                LEFT JOIN cnaes cn ON cn.codigo = n.cnae_principal
             )
        SELECT * FROM classificado WHERE tipo IS NOT NULL
    """


def diff_competencias(
    conexao: duckdb.DuckDBPyConnection,
    comp_novo: str,
    comp_antigo: str,
    *,
    tipos: tuple[str, ...] | None = None,
    ufs: tuple[str, ...] | None = None,
    municipios: tuple[str, ...] | None = None,
    ignorar_abertura_anterior_a: str | date | None = "auto",
) -> list[dict[str, Any]]:
    """Compara duas competências já carregadas e devolve os eventos de trigger.

    ``ignorar_abertura_anterior_a`` descarta NOVA_FILIAL/NOVO_ESTABELECIMENTO
    cuja data de início de atividade seja anterior ao limite -- se o CNPJ
    "aparece" mas abriu anos atrás, é lacuna de cobertura do dump antigo, não
    uma abertura real. ``"auto"`` usa o 1º dia da competência antiga.
    """
    comp_novo = _validar_competencia(comp_novo)
    comp_antigo = _validar_competencia(comp_antigo)
    carregadas = set(competencias_carregadas(conexao))
    faltando = {comp_novo, comp_antigo} - carregadas
    if faltando:
        raise ValueError(f"Competência(s) não carregada(s): {', '.join(sorted(faltando))}.")

    if ignorar_abertura_anterior_a == "auto":
        limite_abertura: date | None = _primeiro_dia(comp_antigo)
    elif isinstance(ignorar_abertura_anterior_a, str):
        limite_abertura = date.fromisoformat(ignorar_abertura_anterior_a)
    else:
        limite_abertura = ignorar_abertura_anterior_a

    filtro_tipos = set(tipos or TIPOS_EVENTO)
    filtro_ufs = {u.upper() for u in ufs} if ufs else None
    filtro_mun = set(municipios) if municipios else None

    colunas = ["tipo", *_CAMPOS_EVENTO]
    eventos: list[dict[str, Any]] = []
    for linha in conexao.execute(_classificar_evento_sql(comp_novo, comp_antigo)).fetchall():
        evento = dict(zip(colunas, linha))
        if evento["tipo"] not in filtro_tipos:
            continue
        if filtro_ufs is not None and str(evento.get("uf") or "").upper() not in filtro_ufs:
            continue
        if filtro_mun is not None and str(evento.get("municipio_codigo") or "") not in filtro_mun:
            continue
        if (
            evento["tipo"] in ("NOVA_FILIAL", "NOVO_ESTABELECIMENTO")
            and limite_abertura is not None
        ):
            abertura = evento.get("data_inicio_atividade")
            if abertura is not None and abertura < limite_abertura:
                continue
        eventos.append(evento)
    return eventos


# --- Matcher: evento do diff -> lead candidato + payload de sinal -------------

_EVENTO_PARA_MUDANCA = {
    "NOVA_FILIAL": "nova_filial_receita",
    "NOVO_ESTABELECIMENTO": "novo_estabelecimento_receita",
    "REATIVACAO": "situacao_cadastral_alterada",
    "MUDANCA_ENDERECO": "endereco_alterado",
}


def _dias_desde(valor: date | None) -> int | None:
    if not isinstance(valor, date):
        return None
    return max(0, (date.today() - valor).days)


def _local_evento(evento: dict[str, Any]) -> str:
    municipio = str(evento.get("municipio_nome") or evento.get("municipio_codigo") or "").strip()
    uf = str(evento.get("uf") or "").strip().upper()
    if municipio and uf:
        return f"{municipio}, {uf}"
    return municipio or uf


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
    """Traduz eventos do diff em leads candidatos (já classificados por ICP) +
    o payload de mudança que ``sales_signals.registrar_signal`` consome.

    ``BAIXA`` não vira candidato -- entra em ``supressoes`` para o injetor
    desativar sinais / marcar o lead existente. Função pura: só depende dos
    dicts de evento (já enriquecidos com município/CNAE pelo diff).
    """
    candidatos: list[dict[str, Any]] = []
    supressoes: list[str] = []

    for evento in eventos:
        tipo = evento["tipo"]
        # CNPJ do dump é sempre estruturalmente válido; um que não passa na
        # verificação de dígitos indica parsing errado ou linha corrompida --
        # não dá pra casar nem contatar, então é descartado.
        cnpj_valido = normalizar_cnpj(evento.get("cnpj"))
        if not cnpj_valido:
            continue
        evento = {**evento, "cnpj": cnpj_valido}

        if tipo == "BAIXA":
            supressoes.append(evento["cnpj"])
            continue

        nicho = str(evento.get("cnae_descricao") or evento.get("cnae_principal") or "").strip()
        nome = str(evento.get("nome_fantasia") or "").strip() or f"CNPJ {evento['cnpj']}"
        lead = {
            "place_id": f"receita:{evento['cnpj']}",
            "cnpj": evento["cnpj"],
            "nome_empresa": nome,
            "razao_social": "",
            "decisor": "",
            "nicho": nicho,
            "cnae_fiscal_descricao": str(evento.get("cnae_descricao") or ""),
            "endereco": _endereco_evento(evento),
            "cidade": _local_evento(evento),
            "telefone": str(evento.get("telefone") or ""),
            "site": "",
            "email": str(evento.get("email") or ""),
            "status": "Novos Leads",
            "status_receita": str(evento.get("situacao") or ""),
            "origem": FONTE_RECEITA_DUMP,
            "observacoes": (
                f"Detectado pelo dump da Receita ({tipo}). "
                "Confirme os dados antes do contato comercial."
            ),
        }
        segmento, servicos = classificar_icp(lead)
        lead["segmento_icp"] = segmento
        lead["servicos_recomendados"] = "; ".join(servicos)
        if segmento == "Não classificado" and not incluir_nao_classificado:
            continue

        mtipo = _EVENTO_PARA_MUDANCA[tipo]
        if tipo in ("NOVA_FILIAL", "NOVO_ESTABELECIMENTO"):
            mudanca = {
                "type": mtipo, "field": "cnpj", "before": None,
                "after": _local_evento(evento),
                "days_between": _dias_desde(evento.get("data_inicio_atividade")),
                "source": FONTE_RECEITA_DUMP,
            }
        elif tipo == "REATIVACAO":
            mudanca = {
                "type": mtipo, "field": "situacao_cadastral",
                "before": evento.get("situacao_antes"), "after": evento.get("situacao"),
                "days_between": None, "reativacao": True, "inativacao": False,
                "source": FONTE_RECEITA_DUMP,
            }
        else:  # MUDANCA_ENDERECO
            mudanca = {
                "type": mtipo, "field": "address",
                "before": evento.get("logradouro_antes"),
                "after": _endereco_evento(evento),
                "days_between": None, "source": FONTE_RECEITA_DUMP,
            }
        candidatos.append({"evento_tipo": tipo, "lead": lead, "mudanca": mudanca})

    return {"candidatos": candidatos, "supressoes": supressoes}
