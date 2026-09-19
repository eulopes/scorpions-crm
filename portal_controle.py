from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

# Cada serviço é uma linha da lista -- "featured": True dá destaque ao único
# item que de fato importa mais (o CRM); o resto fica denso e quieto, como
# uma tela de status de operação de verdade (não um grid de cards de vitrine).
SERVICOS = [
    {
        "nome": "CRM Streamlit",
        "descricao": "Sistema de gerenciamento de clientes",
        "host": "192.168.1.16:8501",
        "url": "http://192.168.1.16:8501",
        "status": "online",
        "featured": True,
    },
    {
        "nome": "Grafana",
        "descricao": "Métricas e dashboards",
        "host": "192.168.1.16:3000",
        "url": "http://192.168.1.16:3000",
        "status": "online",
    },
    {
        "nome": "Pi-hole",
        "descricao": "Bloqueio de anúncios / DNS",
        "host": "192.168.1.16/admin",
        "url": "http://192.168.1.16/admin",
        "status": "online",
    },
    {
        "nome": "Cockpit",
        "descricao": "Monitoramento do servidor",
        "host": "192.168.1.16:9090",
        "url": "https://192.168.1.16:9090",
        "status": "online",
    },
    {
        "nome": "Web Interface",
        "descricao": "Interface web do CRM (novo)",
        "host": "192.168.1.16:8000",
        "url": "http://192.168.1.16:8000",
        "status": "online",
    },
]


def _linha_servico(s: dict) -> str:
    destaque = " service-row--featured" if s.get("featured") else ""
    return f"""
    <div class="service-row{destaque}">
      <span class="service-status service-status--{s['status']}" title="{s['status']}"></span>
      <div class="service-info">
        <span class="service-name">{s['nome']}</span>
        <span class="service-desc">{s['descricao']}</span>
      </div>
      <span class="service-host">{s['host']}</span>
      <a class="service-action" href="{s['url']}" target="_blank" rel="noopener">Abrir</a>
    </div>"""


@app.get("/", response_class=HTMLResponse)
def portal():
    linhas_html = "".join(_linha_servico(s) for s in SERVICOS)
    total = len(SERVICOS)
    online = sum(1 for s in SERVICOS if s["status"] == "online")

    return f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Central de Controle</title>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600&display=swap');

            :root {{
                --bg: #0A0A0F;
                --panel: #111319;
                --line: #1E232B;
                --line-strong: #2D333D;
                --text: #F4F7FB;
                --text-secondary: #C3CBD8;
                --muted: #7F8B9B;
                --weak: #5A6373;
                --primary: #70D3FC;
                --secondary: #256BFF;
                --success: #35D07F;
                --danger: #FF667D;
                --radius: 10px;
            }}

            * {{ margin: 0; padding: 0; box-sizing: border-box; }}

            html, body {{ background: var(--bg); }}

            body {{
                font-family: 'Inter', system-ui, sans-serif;
                color: var(--text);
                min-height: 100vh;
                padding: 2.5rem 1.5rem;
            }}

            .wrap {{ max-width: 760px; margin: 0 auto; }}

            .topbar {{
                display: flex;
                align-items: baseline;
                justify-content: space-between;
                gap: 1rem;
                margin-bottom: 1.75rem;
                flex-wrap: wrap;
            }}

            .topbar h1 {{
                font-size: 1.5rem;
                font-weight: 700;
                letter-spacing: 0.01em;
                background: linear-gradient(120deg, #ffffff 0%, var(--text) 45%, var(--primary) 100%);
                background-clip: text;
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}

            .topbar .summary {{
                font-family: 'JetBrains Mono', monospace;
                font-size: 0.8rem;
                color: var(--muted);
                white-space: nowrap;
            }}
            .topbar .summary strong {{ color: var(--success); font-weight: 600; }}

            .panel {{
                background: var(--panel);
                border: 1px solid var(--line);
                border-radius: var(--radius);
                overflow: hidden;
            }}

            .service-row {{
                display: flex;
                align-items: center;
                gap: 0.9rem;
                padding: 0.95rem 1.1rem;
                border-bottom: 1px solid var(--line);
                transition: background 0.15s ease;
            }}
            .service-row:last-child {{ border-bottom: none; }}
            .service-row:hover {{ background: rgba(112, 211, 252, 0.045); }}

            .service-row--featured {{
                background: linear-gradient(155deg, rgba(0, 110, 253, 0.12), transparent);
                border-left: 3px solid var(--primary);
                padding-left: calc(1.1rem - 3px);
            }}
            .service-row--featured .service-name {{ font-size: 0.98rem; }}
            .service-row--featured:hover {{ background: linear-gradient(155deg, rgba(0, 110, 253, 0.18), transparent); }}

            .service-status {{
                width: 8px; height: 8px; border-radius: 50%; flex: none;
            }}
            .service-status--online {{ background: var(--success); }}
            .service-status--offline {{ background: var(--danger); }}

            .service-info {{
                display: flex;
                flex-direction: column;
                min-width: 0;
                flex: 1;
            }}
            .service-name {{
                font-weight: 600;
                font-size: 0.9rem;
                color: var(--text);
            }}
            .service-desc {{
                font-size: 0.78rem;
                color: var(--muted);
                margin-top: 0.1rem;
            }}

            .service-host {{
                font-family: 'JetBrains Mono', monospace;
                font-size: 0.76rem;
                color: var(--text-secondary);
                white-space: nowrap;
            }}

            .service-action {{
                flex: none;
                font-size: 0.78rem;
                font-weight: 600;
                color: var(--primary);
                text-decoration: none;
                border: 1px solid var(--line-strong);
                border-radius: 6px;
                padding: 0.35rem 0.7rem;
                transition: border-color 0.15s ease, background 0.15s ease;
            }}
            .service-action:hover {{
                border-color: var(--primary);
                background: rgba(112, 211, 252, 0.1);
            }}

            .remote {{
                margin-top: 1.25rem;
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 1rem;
                padding: 0.85rem 1.1rem;
                background: var(--panel);
                border: 1px solid var(--line);
                border-radius: var(--radius);
                flex-wrap: wrap;
            }}
            .remote .label {{
                font-size: 0.78rem;
                color: var(--muted);
            }}
            .remote code {{
                font-family: 'JetBrains Mono', monospace;
                font-size: 0.8rem;
                color: var(--text-secondary);
                margin-left: 0.5rem;
            }}
            .remote button {{
                font-family: 'Inter', sans-serif;
                font-size: 0.76rem;
                font-weight: 600;
                color: var(--text);
                background: transparent;
                border: 1px solid var(--line-strong);
                border-radius: 6px;
                padding: 0.4rem 0.8rem;
                cursor: pointer;
                transition: border-color 0.15s ease;
            }}
            .remote button:hover {{ border-color: var(--primary); }}

            footer {{
                margin-top: 1.5rem;
                font-family: 'JetBrains Mono', monospace;
                font-size: 0.7rem;
                color: var(--weak);
                text-align: center;
            }}

            @media (max-width: 560px) {{
                body {{ padding: 1.5rem 1rem; }}
                .service-row {{ flex-wrap: wrap; }}
                .service-host {{ order: 3; width: 100%; padding-left: 1.2rem; }}
                .remote {{ flex-direction: column; align-items: flex-start; }}
            }}
        </style>
    </head>
    <body>
        <div class="wrap">
            <div class="topbar">
                <h1>Central de Controle</h1>
                <span class="summary"><strong>{online}/{total}</strong> serviços operacionais</span>
            </div>

            <div class="panel">
                {linhas_html}
            </div>

            <div class="remote">
                <span class="label">Acesso remoto ao servidor <code>ssh usuario@192.168.1.16</code></span>
                <button onclick="copiarSSH()">Copiar</button>
            </div>

            <footer>servidor local · 192.168.1.16</footer>
        </div>

        <script>
            function copiarSSH() {{
                navigator.clipboard.writeText("ssh usuario@192.168.1.16");
            }}
        </script>
    </body>
    </html>
    """


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
