"""Orquestrador mensal da Fase 2 (alvarás de obras) -- hoje só São Paulo.

Baixa/mapeia a competência (adapter da cidade), carrega no store canônico,
filtra o recorte relevante, casa contra a base de leads e (opcionalmente)
contra o dump da Receita, e injeta no CRM.

Uso:
    python scripts/rodar_obras.py --competencia 2026-08
    python scripts/rodar_obras.py --competencia 2026-08 --dry-run
    python scripts/rodar_obras.py --competencia 2026-08 --arquivo local.xls

Env: OBRAS_DUMP_DB (store), RECEITA_DUMP_DB (opcional, camada de match extra),
CRM_DB_PATH (base do CRM).
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import obras_dump as od  # noqa: E402
import obras_match as om  # noqa: E402
import obras_sp  # noqa: E402
import receita_dump as rd  # noqa: E402
from company_history import conectar as conectar_crm  # noqa: E402
from obras_ingest import injetar_alvaras  # noqa: E402

ADAPTERS = {"sao-paulo": obras_sp}


def _leads_da_base() -> list[dict]:
    with conectar_crm() as con:
        linhas = con.execute(
            "SELECT id, nome_empresa, razao_social, endereco, cidade FROM leads"
        ).fetchall()
    return [dict(l) for l in linhas]


def _estabelecimentos_da_receita(uf: str, competencia: str | None) -> list[dict]:
    """Camada extra de match: estabelecimentos ATIVA da Receita na UF, já com
    nome de município/CNAE resolvidos. Devolve [] se a Fase 1 nunca rodou."""
    try:
        with rd.conectar() as con:
            rd.migrar_esquema(con)
            disponiveis = rd.competencias_carregadas(con)
            comp = competencia or (disponiveis[-1] if disponiveis else None)
            if not comp:
                return []
            cursor = con.execute(
                """
                SELECT e.*, m.nome AS municipio_nome, c.descricao AS cnae_descricao
                FROM estabelecimentos e
                LEFT JOIN municipios m ON m.codigo = e.municipio_codigo
                LEFT JOIN cnaes c ON c.codigo = e.cnae_principal
                WHERE e.competencia = ? AND e.uf = ? AND e.situacao = 'ATIVA'
                """,
                [comp, uf],
            )
            colunas = [d[0] for d in cursor.description]
            linhas = cursor.fetchall()
    except Exception:
        return []
    return [dict(zip(colunas, linha)) for linha in linhas]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cidade", default="sao-paulo", choices=list(ADAPTERS))
    p.add_argument("--competencia", required=True, help="AAAA-MM.")
    p.add_argument("--arquivo", default=None, help="Usa um .xls local em vez de baixar.")
    p.add_argument("--area-minima", type=float, default=500.0)
    p.add_argument("--sem-receita", action="store_true", help="Não tenta casar contra o dump da Receita.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--saida-pendentes", default=None, help="CSV com a fila 'sem ocupante'.")
    args = p.parse_args()

    adapter = ADAPTERS[args.cidade]
    comp = od._validar_competencia(args.competencia)

    print(f"Fase 2 — {args.cidade} — {comp}")
    if args.arquivo:
        registros = adapter.parsear_planilha(args.arquivo)
    else:
        print("  baixando planilha do mês...")
        registros = adapter.baixar_e_mapear(comp)
    print(f"  {len(registros)} processo(s) na planilha.")

    with od.conectar() as con:
        od.migrar_esquema(con)
        od.carregar_alvaras(con, args.cidade, comp, registros)
        relevantes = od.alvaras_relevantes(con, args.cidade, comp, area_minima=args.area_minima)
    print(f"  {len(relevantes)} relevante(s) (área ≥ {args.area_minima:.0f} m², uso não-residencial, "
          f"fase aprovação/execução/reforma).")

    leads = _leads_da_base()
    indice_leads = om.indexar_leads(leads)
    indice_leads_nome = om.indexar_leads_por_nome(leads)

    indice_est: list = []
    indice_est_nome: list = []
    if not args.sem_receita:
        estabelecimentos = _estabelecimentos_da_receita("SP", None)
        indice_est = om.indexar_estabelecimentos(estabelecimentos)
        indice_est_nome = om.indexar_estabelecimentos_por_nome(estabelecimentos)
        print(f"  {len(estabelecimentos)} estabelecimento(s) da Receita disponível(is) para casar (UF SP).")

    resultado = om.casar_alvaras(
        relevantes, indice_leads=indice_leads, indice_estabelecimentos=indice_est,
        indice_leads_nome=indice_leads_nome, indice_estabelecimentos_nome=indice_est_nome,
    )
    print(f"\nCasamento: {len(resultado['em_lead'])} em lead existente, "
          f"{len(resultado['novo_de_receita'])} candidato via Receita, "
          f"{len(resultado['sem_ocupante'])} sem ocupante identificado.")

    if args.dry_run:
        for item in resultado["em_lead"][:15]:
            print(f"  [em_lead:{item['por']}] lead #{item['lead_id']} <- alvará "
                  f"{item['alvara'].get('id_alvara')} ({item['alvara'].get('tipo')}, "
                  f"{item['alvara'].get('area_construida')} m²)")
        print("\n--dry-run: nada foi gravado no CRM.")
    else:
        relatorio = injetar_alvaras(resultado)
        print("\nInjeção:", ", ".join(f"{k}={v}" for k, v in relatorio.items() if k != "pendentes"))

    if args.saida_pendentes and resultado["sem_ocupante"]:
        caminho = Path(args.saida_pendentes)
        with caminho.open("w", newline="", encoding="utf-8") as arquivo:
            escritor = csv.writer(arquivo)
            escritor.writerow(["id_alvara", "tipo", "area_construida", "endereco", "numero",
                                "bairro", "proprietario", "motivo"])
            for item in resultado["sem_ocupante"]:
                a = item["alvara"]
                escritor.writerow([a.get("id_alvara"), a.get("tipo"), a.get("area_construida"),
                                    a.get("endereco"), a.get("numero"), a.get("bairro"),
                                    a.get("proprietario"), item.get("motivo")])
        print(f"Fila 'sem ocupante' salva em {caminho} ({len(resultado['sem_ocupante'])} linha(s)).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
