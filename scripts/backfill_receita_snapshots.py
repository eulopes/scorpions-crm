"""Backfill: coleta um snapshot-baseline com dados da Receita para todo lead
com CNPJ válido que ainda não tenha um.

Roda UMA vez antes de deixar o worker ligado. Sem esse baseline, o primeiro
diff do worker compararia um snapshot antigo (sem campos firmográficos) com o
primeiro enriquecido -- e, embora as guardas em change_detection evitem sinais
firmográficos falsos, um baseline explícito deixa o histórico limpo.

Uso:
    python scripts/backfill_receita_snapshots.py [--dry-run] [--limite N]

Respeita CRM_DB_PATH. Não recalcula scores nem gera sinais -- só grava
snapshots. É seguro repetir: pula quem já tem snapshot.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from company_history import conectar, create_company_snapshot, get_latest_snapshot  # noqa: E402
from niche_sources import normalizar_cnpj  # noqa: E402
from receita_snapshot import buscar_snapshot_receita  # noqa: E402

_CAMPOS = (
    "capital_social", "porte", "cnae_principal", "cnaes_secundarios_json",
    "situacao_cadastral", "data_situacao_cadastral", "natureza_juridica",
    "qsa_hash", "qtde_socios",
)


def _leads_alvo(limite: int | None) -> list[dict]:
    with conectar() as conexao:
        linhas = conexao.execute(
            "SELECT id, nome_empresa, razao_social, cnpj, endereco, cidade, nicho, "
            "telefone, email, site, status_receita FROM leads "
            "WHERE cnpj IS NOT NULL AND TRIM(cnpj) <> '' ORDER BY id"
        ).fetchall()
    leads = [dict(linha) for linha in linhas if normalizar_cnpj(linha["cnpj"])]
    return leads[:limite] if limite else leads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Só lista, não grava.")
    parser.add_argument("--limite", type=int, default=None, help="Processa no máximo N leads.")
    parser.add_argument("--pausa", type=float, default=0.4, help="Segundos entre chamadas.")
    args = parser.parse_args()

    leads = _leads_alvo(args.limite)
    print(f"{len(leads)} lead(s) com CNPJ válido.")

    criados = pulados = falhas = 0
    for lead in leads:
        if get_latest_snapshot(lead["id"]) is not None:
            pulados += 1
            continue
        cnpj = normalizar_cnpj(lead["cnpj"])
        receita = buscar_snapshot_receita(cnpj)
        time.sleep(args.pausa)
        if not receita:
            falhas += 1
            print(f"  [falha] #{lead['id']} {lead['nome_empresa']} (CNPJ {cnpj})")
            continue

        snapshot = {
            "lead_id": lead["id"],
            "company_name": lead.get("razao_social") or lead.get("nome_empresa"),
            "trade_name": lead.get("nome_empresa"),
            "cnpj": lead.get("cnpj"),
            "address": lead.get("endereco"),
            "city": lead.get("cidade"),
            "phone": lead.get("telefone"),
            "email": lead.get("email"),
            "website": lead.get("site"),
            "business_status": lead.get("status_receita"),
            "categories_json": lead.get("nicho"),
        }
        for campo in _CAMPOS:
            if receita.get(campo) is not None:
                snapshot[campo] = receita[campo]

        if args.dry_run:
            print(f"  [dry] #{lead['id']} {lead['nome_empresa']} <- {snapshot.get('porte')}, "
                  f"cap {snapshot.get('capital_social')}, {snapshot.get('situacao_cadastral')}")
            criados += 1
            continue

        novo_id = create_company_snapshot(lead["id"], "Receita Federal (BrasilAPI)", snapshot)
        if novo_id:
            criados += 1
            print(f"  [ok]  #{lead['id']} {lead['nome_empresa']}")
        else:
            pulados += 1

    print(f"\nBaseline: {criados} criado(s), {pulados} pulado(s), {falhas} falha(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
