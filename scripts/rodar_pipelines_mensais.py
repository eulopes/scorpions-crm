"""Orquestrador mensal: dispara Receita, Obras (SP) e CNES em sequência.

Pensado para rodar como Railway Cron Job (ou cron local) uma vez por dia --
cada pipeline só executa de fato quando ainda não teve sucesso na
competência (mês) atual, então disparar isso todo dia é seguro e barato:
nos dias em que já rodou com sucesso, a checagem é só uma leitura em
estado_automacao (sem download nenhum). Uma falha (ex.: servidor da Receita
fora do ar) não trava os outros pipelines nem impede nova tentativa no dia
seguinte, dentro do mesmo mês.

Uso:
    python scripts/rodar_pipelines_mensais.py
    python scripts/rodar_pipelines_mensais.py --dia-minimo 10
    python scripts/rodar_pipelines_mensais.py --forcar

Env: CRM_DB_PATH (base do CRM, mesma usada pelos orquestradores individuais).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import ler_config, salvar_config  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent

PIPELINES = {
    "receita": [sys.executable, str(RAIZ / "scripts" / "rodar_receita_dump.py")],
    "obras_sp": [sys.executable, str(RAIZ / "scripts" / "rodar_obras.py")],
    "cnes": [sys.executable, str(RAIZ / "scripts" / "rodar_cnes.py")],
}


def _chave_sucesso(pipeline: str) -> str:
    return f"pipeline_{pipeline}_ultimo_mes_sucesso"


def _chave_tentativa(pipeline: str) -> str:
    return f"pipeline_{pipeline}_ultima_tentativa"


def pendente(pipeline: str, hoje: date) -> bool:
    """False quando o pipeline já teve sucesso neste mês ou já foi tentado hoje."""
    if ler_config(_chave_sucesso(pipeline)) == hoje.strftime("%Y-%m"):
        return False
    if ler_config(_chave_tentativa(pipeline)) == hoje.isoformat():
        return False
    return True


def rodar_pipeline(pipeline: str, comando: list[str], hoje: date) -> dict:
    salvar_config(_chave_tentativa(pipeline), hoje.isoformat())
    print(f"\n=== {pipeline} ===")
    resultado = subprocess.run(comando, cwd=str(RAIZ))
    if resultado.returncode == 0:
        salvar_config(_chave_sucesso(pipeline), hoje.strftime("%Y-%m"))
        return {"pipeline": pipeline, "status": "sucesso"}
    return {"pipeline": pipeline, "status": "falha", "codigo": resultado.returncode}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dia-minimo", type=int, default=15,
        help="Só dispara a partir deste dia do mês -- dá tempo da Receita publicar "
             "(o próprio orquestrador da Receita observa que a 2ª quinzena é o normal). Padrão: 15.",
    )
    p.add_argument(
        "--forcar", action="store_true",
        help="Ignora --dia-minimo e o controle de 'já tentado hoje'/'já teve sucesso este mês'.",
    )
    args = p.parse_args()

    hoje = date.today()
    if not args.forcar and hoje.day < args.dia_minimo:
        print(f"Hoje é dia {hoje.day}; aguardando o dia {args.dia_minimo} do mês.")
        return 0

    resultados = []
    for pipeline, comando in PIPELINES.items():
        if not args.forcar and not pendente(pipeline, hoje):
            print(f"{pipeline}: já resolvido este mês (ou tentado hoje), pulando.")
            continue
        resultados.append(rodar_pipeline(pipeline, comando, hoje))

    print("\n=== Resumo ===")
    if not resultados:
        print("  nada a fazer.")
    for r in resultados:
        detalhe = f" (código {r['codigo']})" if r["status"] == "falha" else ""
        print(f"  {r['pipeline']}: {r['status']}{detalhe}")

    return 1 if any(r["status"] == "falha" for r in resultados) else 0


if __name__ == "__main__":
    raise SystemExit(main())
