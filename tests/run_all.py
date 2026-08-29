"""Roda cada arquivo de teste em um processo Python separado.

streamlit.testing.v1.AppTest tem uma limitacao conhecida: criar mais de uma
instancia de AppTest.from_file() para o mesmo app.py dentro do MESMO processo
Python pode reaproveitar estado indevidamente entre elas (ex.: uma tabela do
banco criada na primeira instancia nao aparece pra segunda). Isolar cada
arquivo de teste em seu proprio processo evita esse problema por completo —
e e exatamente o que este script faz.

Uso:
    python tests/run_all.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARQUIVOS_DE_TESTE = [
    "tests.test_app_smoke",
    "tests.test_app_actions",
]


def main() -> int:
    falhou = False
    for modulo in ARQUIVOS_DE_TESTE:
        print(f"\n{'=' * 70}\n{modulo}\n{'=' * 70}")
        resultado = subprocess.run(
            [sys.executable, "-m", "unittest", modulo, "-v"],
            cwd=ROOT,
        )
        if resultado.returncode != 0:
            falhou = True
    return 1 if falhou else 0


if __name__ == "__main__":
    raise SystemExit(main())
