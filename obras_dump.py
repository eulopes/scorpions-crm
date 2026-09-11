"""Fase 2 -- alvarás / obras. Store canônico e independente de cidade.

Cada cidade tem um *adapter* (ex.: ``obras_sp.py``) que traduz o arquivo local
dela para o registro canônico abaixo; este módulo só guarda, filtra e serve.
Assim adicionar RJ/BH/Campinas é escrever um mapeador, não mexer no núcleo.

Store: DuckDB (``OBRAS_DUMP_DB``), mesmo padrão da Fase 1 -- dado público em
lote, atualização mensal, matching por endereço. Sem dependência de Streamlit.
"""

from __future__ import annotations

import os
import re
import unicodedata
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import duckdb

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("OBRAS_DUMP_DB", str(APP_DIR / "obras_dump.duckdb")))

# Vocabulário canônico -- os adapters normalizam para estes valores.
TIPOS_ALVARA = (
    "aprovacao", "execucao", "reforma", "demolicao", "regularizacao",
    "habite-se", "outro",
)
USOS = ("residencial", "comercial", "industrial", "misto", "institucional", "outro")

# Campos do registro canônico que um adapter deve produzir.
CAMPOS_ALVARA = (
    "id_alvara", "cidade", "data_emissao", "tipo", "uso", "area_construida",
    "endereco", "numero", "bairro", "municipio_nome", "uf", "cep",
    "sql_iptu", "subprefeitura", "lat", "lng",
)

_RE_COMPETENCIA = re.compile(r"^\d{4}-\d{2}$")


def _validar_competencia(competencia: str) -> str:
    comp = str(competencia or "").strip()
    if not _RE_COMPETENCIA.match(comp) or not 1 <= int(comp[5:7]) <= 12:
        raise ValueError("Competência deve estar no formato AAAA-MM.")
    return comp


def _sem_acento(valor: Any) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    return "".join(c for c in texto if not unicodedata.combining(c)).strip().lower()


def _para_float(valor: Any) -> float | None:
    """Aceita número, '1800.5' e o formato brasileiro '1.800,50'."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return float(valor) or None
    limpo = re.sub(r"[^\d,.-]", "", str(valor))
    if not limpo:
        return None
    if "," in limpo and "." in limpo:
        limpo = limpo.replace(".", "").replace(",", ".")
    elif "," in limpo:
        limpo = limpo.replace(",", ".")
    try:
        return float(limpo) or None
    except ValueError:
        return None


def normalizar_endereco(valor: Any) -> str:
    """Forma canônica p/ comparação: sem acento, sem pontuação, abreviações
    de logradouro expandidas, minúsculo, espaços colapsados."""
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c)).casefold()
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    tokens = texto.split()
    expandir = {
        "r": "rua", "av": "avenida", "avn": "avenida", "al": "alameda",
        "pc": "praca", "pca": "praca", "trav": "travessa", "tv": "travessa",
        "rod": "rodovia", "estr": "estrada", "est": "estrada", "lgo": "largo",
        "vd": "viaduto", "mal": "marechal", "pres": "presidente", "dr": "doutor",
        "profa": "professora", "prof": "professor", "eng": "engenheiro",
        "cel": "coronel", "sen": "senador", "gen": "general", "n": "",
        "no": "", "num": "", "numero": "", "km": "km",
    }
    saida = []
    for tok in tokens:
        novo = expandir.get(tok, tok)
        if novo:
            saida.append(novo)
    return " ".join(saida)


def chave_endereco(endereco: Any, numero: Any = "", municipio: Any = "") -> str:
    """Chave de casamento: logradouro normalizado + número + município.
    Número entra porque 'Av X, 100' e 'Av X, 2000' são obras diferentes."""
    base = normalizar_endereco(endereco)
    num = re.sub(r"\D", "", str(numero or ""))
    mun = normalizar_endereco(municipio)
    partes = [p for p in (base, num, mun) if p]
    return " | ".join(partes)


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
        CREATE TABLE IF NOT EXISTS alvaras (
            competencia TEXT NOT NULL,
            cidade TEXT NOT NULL,
            id_alvara TEXT NOT NULL,
            data_emissao DATE,
            tipo TEXT,
            uso TEXT,
            area_construida DOUBLE,
            endereco TEXT,
            numero TEXT,
            bairro TEXT,
            municipio_nome TEXT,
            uf TEXT,
            cep TEXT,
            sql_iptu TEXT,
            subprefeitura TEXT,
            lat DOUBLE,
            lng DOUBLE,
            chave_endereco TEXT,
            PRIMARY KEY (cidade, id_alvara, competencia)
        )
        """
    )
    conexao.execute(
        "CREATE INDEX IF NOT EXISTS idx_alvaras_chave ON alvaras(chave_endereco)"
    )
    conexao.execute(
        "CREATE TABLE IF NOT EXISTS obras_competencias ("
        "cidade TEXT, competencia TEXT, linhas BIGINT, carregada_em TIMESTAMP DEFAULT now(), "
        "PRIMARY KEY (cidade, competencia))"
    )


def competencias_carregadas(conexao: duckdb.DuckDBPyConnection, cidade: str | None = None) -> list[tuple[str, str]]:
    sql = "SELECT cidade, competencia FROM obras_competencias"
    params: list[Any] = []
    if cidade:
        sql += " WHERE cidade = ?"
        params.append(cidade)
    sql += " ORDER BY cidade, competencia"
    return [(r[0], r[1]) for r in conexao.execute(sql, params).fetchall()]


def _canonizar(registro: dict[str, Any], competencia: str) -> dict[str, Any]:
    saida = {campo: registro.get(campo) for campo in CAMPOS_ALVARA}
    saida["competencia"] = competencia
    saida["cidade"] = str(saida.get("cidade") or "").strip().lower()
    saida["tipo"] = _sem_acento(saida.get("tipo") or "outro")
    if saida["tipo"] not in TIPOS_ALVARA:
        saida["tipo"] = "outro"
    saida["uso"] = _sem_acento(saida.get("uso") or "outro")
    if saida["uso"] not in USOS:
        saida["uso"] = "outro"
    saida["area_construida"] = _para_float(saida.get("area_construida"))
    for campo in ("endereco", "numero", "bairro", "municipio_nome", "uf", "sql_iptu", "subprefeitura"):
        valor = saida.get(campo)
        saida[campo] = str(valor).strip() if valor not in (None, "") else None
    cep = re.sub(r"\D", "", str(saida.get("cep") or ""))
    saida["cep"] = cep if len(cep) == 8 else None
    saida["chave_endereco"] = chave_endereco(
        saida.get("endereco"), saida.get("numero"), saida.get("municipio_nome")
    ) or None
    return saida


def carregar_alvaras(
    conexao: duckdb.DuckDBPyConnection,
    cidade: str,
    competencia: str,
    registros: list[dict[str, Any]],
) -> int:
    """Grava registros já canônicos (produzidos por um adapter de cidade).
    ``INSERT OR REPLACE`` -> recarregar a mesma competência não duplica."""
    comp = _validar_competencia(competencia)
    cidade = str(cidade or "").strip().lower()
    if not cidade:
        raise ValueError("cidade é obrigatória.")

    colunas = list(CAMPOS_ALVARA) + ["competencia", "chave_endereco"]
    placeholders = ", ".join(["?"] * len(colunas))
    linhas = 0
    for registro in registros:
        dados = _canonizar({**registro, "cidade": registro.get("cidade") or cidade}, comp)
        if not dados.get("id_alvara"):
            continue
        conexao.execute(
            f"INSERT OR REPLACE INTO alvaras ({', '.join(colunas)}) VALUES ({placeholders})",
            [dados[c] for c in colunas],
        )
        linhas += 1

    total = conexao.execute(
        "SELECT count(*) FROM alvaras WHERE cidade = ? AND competencia = ?", [cidade, comp]
    ).fetchone()[0]
    conexao.execute(
        "INSERT OR REPLACE INTO obras_competencias (cidade, competencia, linhas) VALUES (?, ?, ?)",
        [cidade, comp, total],
    )
    return linhas


def alvaras_relevantes(
    conexao: duckdb.DuckDBPyConnection,
    cidade: str,
    competencia: str,
    *,
    area_minima: float = 500.0,
    usos: tuple[str, ...] = ("comercial", "industrial", "misto"),
    tipos: tuple[str, ...] = ("aprovacao", "execucao", "reforma"),
) -> list[dict[str, Any]]:
    """O recorte que interessa comercialmente: obra grande, uso não-residencial,
    fase de aprovação/execução/reforma. Corta ~95% do volume (o grosso é
    reforma residencial pequena)."""
    comp = _validar_competencia(competencia)
    cidade = str(cidade or "").strip().lower()
    filtro_uso = " OR ".join(["uso = ?"] * len(usos))
    filtro_tipo = " OR ".join(["tipo = ?"] * len(tipos))
    sql = f"""
        SELECT * FROM alvaras
        WHERE cidade = ? AND competencia = ?
          AND (area_construida IS NULL OR area_construida >= ?)
          AND ({filtro_uso})
          AND ({filtro_tipo})
        ORDER BY area_construida DESC NULLS LAST, data_emissao DESC
    """
    params = [cidade, comp, area_minima, *usos, *tipos]
    cursor = conexao.execute(sql, params)
    colunas = [d[0] for d in cursor.description]
    return [dict(zip(colunas, linha)) for linha in cursor.fetchall()]
