"""Orquestrador mensal da Fase 1: baixa o dump da Receita, carrega no DuckDB,
faz o diff contra o mês anterior, classifica por ICP+região e injeta os
candidatos (leads + sinais) no CRM.

É o que um cron mensal chama (Railway cron / GitHub Actions), depois que a
Receita publica a competência nova (costuma ser na 2ª quinzena).

Uso:
    python scripts/rodar_receita_dump.py
    python scripts/rodar_receita_dump.py --competencia 2026-08
    python scripts/rodar_receita_dump.py --competencia 2026-08 --ufs SP,RJ --dry-run
    python scripts/rodar_receita_dump.py --competencia 2026-08 --comparar-com 2026-06 --pular-download

--competencia é opcional: se omitido, descobre a mais recente já publicada
testando o servidor (a Receita costuma liberar o mês fiscal só na 2ª
quinzena do mês seguinte, então "mês corrente" nem sempre existe -- o
script tenta em cascata mês corrente, anterior, etc.). --comparar-com
segue opcional (padrão: mês anterior ao escolhido).

Env: RECEITA_DUMP_DB, RECEITA_DUMP_CACHE (store/cache), CRM_DB_PATH (base do CRM).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import receita_dump as rd  # noqa: E402
from receita_ingest import injetar_candidatos  # noqa: E402


def _garantir_competencia(comp: str, *, pular_download: bool, forcar_carga: bool) -> int:
    with rd.conectar() as con:
        rd.migrar_esquema(con)
        ja_carregada = comp in rd.competencias_carregadas(con)
    if ja_carregada and not forcar_carga:
        print(f"  {comp}: já carregada, pulando.")
        with rd.conectar() as con:
            return int(con.execute(
                "SELECT linhas FROM competencias_carregadas WHERE competencia = ?", [comp]
            ).fetchone()[0])

    if pular_download:
        base = rd.CACHE_DIR / comp
        zips = sorted(base.glob("Estabelecimentos*.zip"))
        refs = [base / n for n in rd.ARQUIVOS_REFERENCIA if (base / n).exists()]
        if not zips:
            raise SystemExit(f"--pular-download mas não há zips em {base}")
    else:
        print(f"  {comp}: baixando (pode levar bastante — ~15 GB)...")
        inicio = time.time()
        baixados = rd.baixar_competencia(comp)
        zips = [p for p in baixados if p.name.startswith("Estabelecimentos")]
        refs = [p for p in baixados if p.name in rd.ARQUIVOS_REFERENCIA]
        print(f"  {comp}: download em {time.time() - inicio:.0f}s")

    with rd.conectar() as con:
        rd.migrar_esquema(con)
        for arquivo in refs:
            tabela = "municipios" if "unicip" in arquivo.name.lower() else "cnaes"
            rd.carregar_referencia(con, tabela, arquivo)
        inicio = time.time()
        total = rd.carregar_estabelecimentos(con, comp, zips)
    print(f"  {comp}: {total:,} estabelecimentos carregados em {time.time() - inicio:.0f}s")
    return total


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--competencia", default=None,
        help="Competência nova, AAAA-MM. Se omitido, descobre a mais recente publicada no servidor.",
    )
    p.add_argument("--comparar-com", default=None, help="Competência base (padrão: mês anterior).")
    p.add_argument("--ufs", default=None, help="Lista separada por vírgula, ex.: SP,RJ,MG.")
    p.add_argument("--municipios", default=None, help="Códigos de município da Receita, separados por vírgula.")
    p.add_argument("--tipos", default=None, help=f"Subconjunto de {','.join(rd.TIPOS_EVENTO)}.")
    p.add_argument("--incluir-nao-classificado", action="store_true",
                   help="Mantém candidatos sem ICP reconhecido.")
    p.add_argument("--pular-download", action="store_true", help="Usa os zips já no cache.")
    p.add_argument("--forcar-carga", action="store_true", help="Recarrega mesmo se a competência já existe.")
    p.add_argument("--dry-run", action="store_true", help="Mostra o que faria, sem tocar no CRM.")
    args = p.parse_args()

    if args.competencia:
        comp_novo = rd._validar_competencia(args.competencia)
    else:
        print("--competencia não informado: descobrindo a mais recente publicada no servidor...")
        try:
            descoberta = rd.descobrir_competencia_mais_recente()
        except ConnectionError as erro:
            raise SystemExit(
                f"Servidor da Receita inacessível: {erro}\n"
                "Tente novamente mais tarde ou informe --competencia manualmente se já souber qual usar."
            ) from erro
        if not descoberta:
            raise SystemExit(
                "O servidor respondeu, mas nenhuma competência recente parece publicada ainda "
                "(2ª quinzena do mês é o normal). Tente novamente mais tarde ou informe "
                "--competencia manualmente."
            )
        comp_novo = descoberta
        print(f"  competência mais recente encontrada: {comp_novo}")
    comp_antigo = rd._validar_competencia(args.comparar_com or rd.mes_anterior(comp_novo))
    ufs = tuple(u.strip().upper() for u in args.ufs.split(",")) if args.ufs else None
    municipios = tuple(m.strip() for m in args.municipios.split(",")) if args.municipios else None
    tipos = tuple(t.strip().upper() for t in args.tipos.split(",")) if args.tipos else None

    print(f"Fase 1 — {comp_antigo} -> {comp_novo}"
          + (f" | UFs {','.join(ufs)}" if ufs else "")
          + (f" | municípios {','.join(municipios)}" if municipios else ""))

    for comp in (comp_antigo, comp_novo):
        _garantir_competencia(comp, pular_download=args.pular_download, forcar_carga=args.forcar_carga)

    with rd.conectar() as con:
        eventos = rd.diff_competencias(
            con, comp_novo, comp_antigo, tipos=tipos, ufs=ufs, municipios=municipios
        )
    por_tipo: dict[str, int] = {}
    for e in eventos:
        por_tipo[e["tipo"]] = por_tipo.get(e["tipo"], 0) + 1
    print(f"\nDiff: {len(eventos)} evento(s) — " + ", ".join(f"{k}={v}" for k, v in sorted(por_tipo.items())))

    resultado = rd.candidatos_do_diff(eventos, incluir_nao_classificado=args.incluir_nao_classificado)
    candidatos = resultado["candidatos"]
    print(f"Matcher: {len(candidatos)} candidato(s) dentro do ICP, {len(resultado['supressoes'])} supressão(ões).")

    if args.dry_run:
        for c in candidatos[:20]:
            l = c["lead"]
            print(f"  [{c['evento_tipo']}] {l['nome_empresa']} — {l['segmento_icp']} — {l['cidade']} — {l['cnpj']}")
        if len(candidatos) > 20:
            print(f"  ... e mais {len(candidatos) - 20}")
        print("\n--dry-run: nada foi gravado no CRM.")
        return 0

    relatorio = injetar_candidatos(resultado)
    print("\nInjeção:", ", ".join(f"{k}={v}" for k, v in relatorio.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
