"""Histórico de snapshots das empresas -- base para detecção de mudanças e
sinais comerciais (Opportunity Intelligence).

Módulo independente de Streamlit e de chamadas HTTP: só grava e compara o que
a coleta (automation.py) já buscou. Mantém sua própria conexão/DB_PATH, no
mesmo padrão já usado em app.py e automation.py, para não criar dependência
circular entre os módulos novos e o motor existente.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from change_detection import comparar_snapshots

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("CRM_DB_PATH", str(APP_DIR / "scorpions_base.db")))

CAMPOS_SNAPSHOT = (
    "company_name", "trade_name", "cnpj", "address", "city", "state",
    "phone", "email", "website", "business_status", "rating", "reviews_count",
    "categories_json", "units_detected",
)


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(instante: datetime | None = None) -> str:
    return (instante or agora_utc()).astimezone(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conectar() -> Iterator[sqlite3.Connection]:
    conexao = sqlite3.connect(DB_PATH, timeout=30)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA busy_timeout = 30000")
    conexao.execute("PRAGMA foreign_keys = ON")
    try:
        yield conexao
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


def migrar_esquema(conexao: sqlite3.Connection) -> None:
    """Cria `company_snapshots` -- chamada a partir de
    `automation.iniciar_banco_automacao()`, dentro da mesma transação."""
    conexao.execute(
        """
        CREATE TABLE IF NOT EXISTS company_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            data_hash TEXT NOT NULL,
            company_name TEXT,
            trade_name TEXT,
            cnpj TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            phone TEXT,
            email TEXT,
            website TEXT,
            business_status TEXT,
            rating REAL,
            reviews_count INTEGER,
            categories_json TEXT,
            units_detected INTEGER,
            raw_data_json TEXT,
            FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
        )
        """
    )
    conexao.execute(
        "CREATE INDEX IF NOT EXISTS idx_company_snapshots_lead "
        "ON company_snapshots(lead_id, captured_at DESC)"
    )


def _valor_normalizado(dados: dict[str, Any], campo: str) -> Any:
    valor = dados.get(campo)
    if isinstance(valor, float):
        return round(valor, 2)
    return valor


def calcular_data_hash(dados: dict[str, Any]) -> str:
    """Hash estável dos campos observáveis -- ignora id/timestamps, então dois
    snapshots com os mesmos dados nunca geram hashes diferentes."""
    relevante = {campo: _valor_normalizado(dados, campo) for campo in CAMPOS_SNAPSHOT}
    bruto = json.dumps(relevante, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def should_create_snapshot(lead_id: int, dados: dict[str, Any]) -> bool:
    """Evita snapshots idênticos consecutivos -- só grava quando algo mudou."""
    ultimo = get_latest_snapshot(lead_id)
    if ultimo is None:
        return True
    return ultimo["data_hash"] != calcular_data_hash(dados)


def _serializar_categorias(valor: Any) -> Any:
    if isinstance(valor, (list, dict)):
        return json.dumps(valor, ensure_ascii=False)
    return valor


def create_company_snapshot(
    lead_id: int, source: str, dados: dict[str, Any], *, raw_data: dict[str, Any] | None = None
) -> int | None:
    """Grava um snapshot se os dados observáveis mudaram desde o último.
    Devolve o id do snapshot criado, ou None se nada mudou."""
    if not should_create_snapshot(lead_id, dados):
        return None
    agora = iso_utc()
    data_hash = calcular_data_hash(dados)
    with conectar() as conexao:
        cursor = conexao.execute(
            """
            INSERT INTO company_snapshots (
                lead_id, source, captured_at, data_hash, company_name, trade_name, cnpj,
                address, city, state, phone, email, website, business_status,
                rating, reviews_count, categories_json, units_detected, raw_data_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead_id, source, agora, data_hash,
                dados.get("company_name"), dados.get("trade_name"), dados.get("cnpj"),
                dados.get("address"), dados.get("city"), dados.get("state"),
                dados.get("phone"), dados.get("email"), dados.get("website"),
                dados.get("business_status"),
                dados.get("rating"), dados.get("reviews_count"),
                _serializar_categorias(dados.get("categories_json")),
                dados.get("units_detected"),
                json.dumps(raw_data, ensure_ascii=False, default=str) if raw_data is not None else None,
            ),
        )
        return int(cursor.lastrowid)


def get_latest_snapshot(lead_id: int) -> dict[str, Any] | None:
    with conectar() as conexao:
        linha = conexao.execute(
            "SELECT * FROM company_snapshots WHERE lead_id = ? ORDER BY captured_at DESC, id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
    return dict(linha) if linha else None


def get_previous_snapshot(lead_id: int) -> dict[str, Any] | None:
    """O penúltimo snapshot -- base de comparação para o mais recente."""
    with conectar() as conexao:
        linhas = conexao.execute(
            "SELECT * FROM company_snapshots WHERE lead_id = ? ORDER BY captured_at DESC, id DESC LIMIT 2",
            (lead_id,),
        ).fetchall()
    return dict(linhas[1]) if len(linhas) > 1 else None


def listar_snapshots(lead_id: int, limite: int = 50) -> list[dict[str, Any]]:
    with conectar() as conexao:
        linhas = conexao.execute(
            "SELECT * FROM company_snapshots WHERE lead_id = ? ORDER BY captured_at DESC, id DESC LIMIT ?",
            (lead_id, max(1, min(int(limite), 500))),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def compare_snapshots(anterior: dict[str, Any] | None, atual: dict[str, Any]) -> list[dict[str, Any]]:
    """Delegado ao change_detection.py -- devolve a lista de mudanças objetivas."""
    return comparar_snapshots(anterior, atual)
