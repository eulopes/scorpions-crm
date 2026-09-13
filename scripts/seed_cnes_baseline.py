"""Carga inicial (baseline) do CNES: injeta TODOS os estabelecimentos
relevantes de uma UF/região como leads novos, sem depender de um diff real
contra uma carga anterior -- útil na primeira vez que se usa o CNES numa
região, quando ainda não existe um "antes" real para comparar (rodar_cnes.py
normal exige uma carga anterior já salva).

Funciona registrando uma competência vazia como base de comparação: no FULL
OUTER JOIN de diff_dumps(), todo estabelecimento relevante da carga real
aparece como NOVO_ESTABELECIMENTO (o "antigo" não tem nada para casar).
Depois desta carga inicial, o cron mensal normal (scripts/rodar_cnes.py) já
funciona sozinho, comparando cada nova carga contra a mais recente salva.

Uso:
    python scripts/rodar_cnes.py --hoje 2026-09-12 --pular-download  # garante a carga real
    python scripts/seed_cnes_baseline.py --competencia 2026-09-12 --ufs SP --dry-run
    python scripts/seed_cnes_baseline.py --competencia 2026-09-12 --ufs SP

Env: CNES_DUMP_DB, CRM_DB_PATH (base do CRM).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cnes_dump as cd  # noqa: E402
from cnes_ingest import injetar_candidatos  # noqa: E402

COMPETENCIA_BASELINE_VAZIA = "2000-01-01"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--competencia", required=True, help="Competência real já carregada, AAAA-MM-DD.")
    p.add_argument("--ufs", default=None, help="Lista separada por vírgula, ex.: SP,RJ. Sem isso, carga nacional.")
    p.add_argument("--dry-run", action="store_true", help="Mostra o que faria, sem tocar no CRM.")
    args = p.parse_args()

    comp = cd._validar_competencia(args.competencia)
    ufs = tuple(u.strip().upper() for u in args.ufs.split(",")) if args.ufs else None

    with cd.conectar() as con:
        cd.migrar_esquema(con)
        carregadas = cd.competencias_carregadas(con)
        if comp not in carregadas:
            raise SystemExit(
                f"Competência {comp} não está carregada em {cd.DB_PATH}. "
                "Rode scripts/rodar_cnes.py primeiro (ou --pular-download se o zip já está em cache)."
            )
        if COMPETENCIA_BASELINE_VAZIA not in carregadas:
            con.execute(
                "INSERT INTO competencias_carregadas (competencia, linhas) VALUES (?, 0)",
                [COMPETENCIA_BASELINE_VAZIA],
            )
            print(f"  competência baseline vazia {COMPETENCIA_BASELINE_VAZIA} registrada (0 linhas).")

    print(f"Baseline CNES — {COMPETENCIA_BASELINE_VAZIA} (vazia) -> {comp}"
          + (f" | UFs {','.join(ufs)}" if ufs else " | Brasil inteiro"))

    with cd.conectar() as con:
        eventos = cd.diff_dumps(
            con, comp, COMPETENCIA_BASELINE_VAZIA, tipos=("NOVO_ESTABELECIMENTO",), ufs=ufs
        )
    print(f"Diff: {len(eventos)} estabelecimento(s) relevante(s) encontrados.")

    resultado = cd.candidatos_do_diff(eventos)
    candidatos = resultado["candidatos"]
    print(f"Matcher: {len(candidatos)} candidato(s) classificados no ICP "
          f"(descartados: sem CNPJ válido ou não classificado).")

    if args.dry_run:
        for c in candidatos[:20]:
            l = c["lead"]
            print(f"  {l['nome_empresa']} — {l['nicho']} — {l['cidade']} — {l['cnpj']}")
        if len(candidatos) > 20:
            print(f"  ... e mais {len(candidatos) - 20}")
        print("\n--dry-run: nada foi gravado no CRM.")
        return 0

    inicio = time.time()
    relatorio = injetar_candidatos(resultado)
    print(f"\nInjeção em {time.time() - inicio:.0f}s:", ", ".join(f"{k}={v}" for k, v in relatorio.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
