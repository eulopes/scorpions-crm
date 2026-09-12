"""Orquestrador da Fase 3: baixa o CNES (Cadastro Nacional de Estabelecimentos
de Saúde), carrega no DuckDB, faz o diff contra a carga anterior e injeta os
candidatos (leads + sinais) no CRM -- específico do ICP Clínicas, Hospitais &
Laboratórios.

Diferença em relação à Receita/obras: o CNES é um snapshot NACIONAL COMPLETO
publicado a cada poucos dias (não um arquivo por mês fiscal), então aqui
"competência" é a data em que o download foi feito. Rodar este script
periodicamente (ex.: mensal, junto com a Receita) baixa a versão mais nova e
compara contra a última carga salva.

Uso:
    python scripts/rodar_cnes.py --hoje 2026-09-12 --comparar-com 2026-08-01
    python scripts/rodar_cnes.py --hoje 2026-09-12 --ufs SP,RJ --dry-run
    python scripts/rodar_cnes.py --hoje 2026-09-12 --pular-download

Env: CNES_DUMP_DB, CNES_DUMP_CACHE (store/cache), CRM_DB_PATH (base do CRM).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cnes_dump as cd  # noqa: E402
from cnes_ingest import injetar_candidatos  # noqa: E402


def _garantir_competencia(comp: str, *, pular_download: bool, forcar_carga: bool) -> int:
    with cd.conectar() as con:
        cd.migrar_esquema(con)
        ja_carregada = comp in cd.competencias_carregadas(con)
    if ja_carregada and not forcar_carga:
        print(f"  {comp}: já carregada, pulando.")
        with cd.conectar() as con:
            return int(con.execute(
                "SELECT linhas FROM competencias_carregadas WHERE competencia = ?", [comp]
            ).fetchone()[0])

    if pular_download:
        caminho = cd.CACHE_DIR / "cnes_estabelecimentos_csv.zip"
        if not caminho.exists():
            raise SystemExit(f"--pular-download mas não há arquivo em {caminho}")
    else:
        print(f"  {comp}: baixando (~56 MB)...")
        inicio = time.time()
        caminho = cd.baixar_dump()
        print(f"  {comp}: download em {time.time() - inicio:.0f}s")

    with cd.conectar() as con:
        cd.migrar_esquema(con)
        inicio = time.time()
        total = cd.carregar_estabelecimentos(con, comp, caminho)
    print(f"  {comp}: {total:,} estabelecimentos carregados em {time.time() - inicio:.0f}s")
    return total


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hoje", default=None, help="Data do download novo, AAAA-MM-DD (padrão: hoje).")
    p.add_argument("--comparar-com", required=True, help="Data da carga anterior já salva, AAAA-MM-DD.")
    p.add_argument("--ufs", default=None, help="Lista separada por vírgula, ex.: SP,RJ,MG.")
    p.add_argument("--tipos", default=None, help=f"Subconjunto de {','.join(cd.TIPOS_EVENTO)}.")
    p.add_argument("--incluir-nao-classificado", action="store_true",
                   help="Mantém candidatos sem ICP reconhecido.")
    p.add_argument("--pular-download", action="store_true", help="Usa o zip já no cache.")
    p.add_argument("--forcar-carga", action="store_true", help="Recarrega mesmo se a competência já existe.")
    p.add_argument("--dry-run", action="store_true", help="Mostra o que faria, sem tocar no CRM.")
    args = p.parse_args()

    comp_novo = cd._validar_competencia(args.hoje or date.today().isoformat())
    comp_antigo = cd._validar_competencia(args.comparar_com)
    ufs = tuple(u.strip().upper() for u in args.ufs.split(",")) if args.ufs else None
    tipos = tuple(t.strip().upper() for t in args.tipos.split(",")) if args.tipos else None

    print(f"Fase 3 (CNES) — {comp_antigo} -> {comp_novo}" + (f" | UFs {','.join(ufs)}" if ufs else ""))

    _garantir_competencia(comp_novo, pular_download=args.pular_download, forcar_carga=args.forcar_carga)
    with cd.conectar() as con:
        if comp_antigo not in cd.competencias_carregadas(con):
            raise SystemExit(
                f"Competência base {comp_antigo} não está carregada. "
                "Rode este script uma primeira vez apontando --hoje para essa data."
            )

    with cd.conectar() as con:
        eventos = cd.diff_dumps(con, comp_novo, comp_antigo, tipos=tipos, ufs=ufs)
    por_tipo: dict[str, int] = {}
    for e in eventos:
        por_tipo[e["tipo"]] = por_tipo.get(e["tipo"], 0) + 1
    print(f"\nDiff: {len(eventos)} evento(s) — " + ", ".join(f"{k}={v}" for k, v in sorted(por_tipo.items())))

    resultado = cd.candidatos_do_diff(eventos, incluir_nao_classificado=args.incluir_nao_classificado)
    candidatos = resultado["candidatos"]
    print(f"Matcher: {len(candidatos)} candidato(s) dentro do ICP, {len(resultado['supressoes'])} supressão(ões).")

    if args.dry_run:
        for c in candidatos[:20]:
            l = c["lead"]
            print(f"  [{c['evento_tipo']}] {l['nome_empresa']} — {l['nicho']} — {l['cidade']} — {l['cnpj']}")
        if len(candidatos) > 20:
            print(f"  ... e mais {len(candidatos) - 20}")
        print("\n--dry-run: nada foi gravado no CRM.")
        return 0

    relatorio = injetar_candidatos(resultado)
    print("\nInjeção:", ", ".join(f"{k}={v}" for k, v in relatorio.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
