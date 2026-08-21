"""Worker em segundo plano para as campanhas automáticas do Scorpions CRM.

Exemplos:
    python worker.py                 # permanece ativo e verifica a agenda
    python worker.py --once          # executa somente campanhas pendentes
    python worker.py --campaign 1    # executa uma campanha imediatamente
"""

from __future__ import annotations

import argparse
import json
import time

from automation import (
    executar_campanha,
    executar_tarefas_rotineiras,
    iniciar_banco_automacao,
)


def imprimir(resultado: object) -> None:
    print(json.dumps(resultado, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker de campanhas do Scorpions CRM")
    parser.add_argument("--once", action="store_true", help="Executa campanhas pendentes e encerra")
    parser.add_argument("--campaign", type=int, help="Executa uma campanha específica e encerra")
    parser.add_argument("--interval", type=int, default=60, help="Intervalo de verificação em segundos")
    argumentos = parser.parse_args()

    iniciar_banco_automacao()
    if argumentos.campaign:
        imprimir(executar_campanha(argumentos.campaign))
        return
    if argumentos.once:
        imprimir(executar_tarefas_rotineiras())
        return

    intervalo = max(15, min(argumentos.interval, 3600))
    print(f"Worker Scorpions ativo. Verificação a cada {intervalo} segundos.", flush=True)
    try:
        while True:
            resultados = executar_tarefas_rotineiras()
            if resultados:
                imprimir(resultados)
            time.sleep(intervalo)
    except KeyboardInterrupt:
        print("Worker encerrado.", flush=True)


if __name__ == "__main__":
    main()
