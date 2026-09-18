# Deploy em produção de verdade

O Streamlit Community Cloud (`crmbyscorpions.streamlit.app`) não tem disco
persistente nem processo de fundo — ver `LIMITACOES_DEPLOY.md`. A produção
real hoje é um **servidor Linux local** (rede interna), com **app.py** e
**worker.py** rodando como serviços systemd, compartilhando o mesmo disco.
Há também um guia alternativo pra Railway, caso o serviço volte a ser hospedado
na nuvem no futuro.

## O que já está pronto no código

- `Procfile`: define os dois processos (`web` roda o Streamlit, `worker` roda
  o `worker.py`) — usado pelo Railway; no servidor local isso é feito via
  unidades systemd (abaixo).
- `CRM_DB_PATH` (variável de ambiente): se definida, o banco SQLite passa a
  gravar nesse caminho em vez da pasta do projeto.
- `AUTH_USERS_JSON` (variável de ambiente): alternativa ao `secrets.toml` para
  logar os usuários do CRM sem depender do mecanismo de secrets do Streamlit
  Cloud. Formato: `{"admin": "hash_bcrypt_aqui"}` (uma linha só, JSON válido).

## Servidor local (produção atual)

Assume um usuário Linux dono do checkout do repositório (ex.: `lopes`), com
`venv/` já criado dentro da pasta do projeto e `.streamlit/secrets.toml`
preenchido (`GOOGLE_PLACES_API_KEY` etc. — nunca versionado).

### Serviços systemd `--user` (sem precisar de root)

Três unidades, todas em `~/.config/systemd/user/`:

**`scorpions-crm-web.service`** — Streamlit:
```ini
[Unit]
Description=Scorpions CRM - Streamlit App
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/lopes/scorpions-crm
ExecStart=/home/lopes/scorpions-crm/venv/bin/streamlit run app.py --server.port 8501 --server.address 0.0.0.0
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

**`scorpions-crm-worker.service`** — campanhas/automação contínua:
```ini
[Unit]
Description=Scorpions CRM - Worker de automacao
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/lopes/scorpions-crm
ExecStart=/home/lopes/scorpions-crm/venv/bin/python3 worker.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

**`scorpions-crm-pipelines.service` + `.timer`** — cron mensal dos pipelines
de dados (ver seção própria abaixo).

### Ativar

```bash
mkdir -p ~/.config/systemd/user
# copiar as unidades pra ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now scorpions-crm-web.service scorpions-crm-worker.service
loginctl enable-linger $USER   # sobrevive sem sessão SSH ativa; não precisa de sudo na maioria das distros
```

Verificar: `systemctl --user status scorpions-crm-web.service scorpions-crm-worker.service`.

### Cópia do banco (migração de outra máquina)

Nunca copiar um `scorpions_base.db` que ainda está sendo escrito por outro
processo — isso corrompe o SQLite. Antes de copiar:

1. Pare **todo** processo que toque o banco de origem (`streamlit`, `worker.py`
   — cuidado especial com instâncias de `worker.py` "esquecidas" de sessões
   anteriores; `ps aux | grep worker.py` pra confirmar zero).
2. `PRAGMA wal_checkpoint(TRUNCATE)` pra consolidar o WAL num único arquivo.
3. `PRAGMA integrity_check` na origem — precisa dar `ok` antes de copiar.
4. Pare os serviços no destino, copie o arquivo, e **confira o hash SHA256**
   nos dois lados antes de religar qualquer serviço — só then rode
   `integrity_check` no destino como confirmação final.

## Alternativa: Railway (nuvem)

Cron/deploy gerenciado, com camada gratuita e depois pago por uso — confira
o preço atual antes de comprometer o cartão.

1. Crie uma conta em **railway.app** (pode entrar direto com GitHub).
2. **New Project → Deploy from GitHub repo → `eulopes/scorpions-crm`**.
3. Railway detecta o `Procfile` e sugere os dois serviços (`web` e `worker`).
   Confirme os dois.
4. Em **cada um dos dois serviços**, adicione as variáveis de ambiente:
   - `GOOGLE_PLACES_API_KEY` = (sua chave real)
   - `AUTH_USERS_JSON` = `{"admin":"COLE_O_HASH_BCRYPT_AQUI"}`
   - `CRM_DB_PATH` = `/data/scorpions_base.db`
   - Variáveis de SMTP, se for usar alerta por e-mail (`SMTP_HOST`,
     `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_SENDER_EMAIL`,
     `SMTP_RECIPIENT_EMAIL`, `CRM_BASE_URL`).
5. Crie um **Volume** (disco persistente) no serviço `web`, montado em
   `/data`. **Monte o mesmo volume no serviço `worker`, no mesmo caminho
   `/data`**.
6. Deploy. Railway dá uma URL pública (`*.up.railway.app`).
7. Teste o login com o mesmo usuário/senha de sempre.

## Cron mensal dos pipelines de dados (Receita/Obras/CNES)

`scripts/rodar_pipelines_mensais.py` dispara os três orquestradores em
sequência (Receita Federal, Obras/SISSEL-SP, CNES) e é seguro rodar todo
dia: cada pipeline só baixa/processa de novo quando ainda não teve sucesso
na competência (mês) atual -- nos outros dias a checagem é só uma leitura
de `estado_automacao`, sem custo de rede. Uma falha isolada (ex.: servidor
da Receita fora do ar) não trava os outros dois nem impede nova tentativa
no dia seguinte, dentro do mesmo mês.

### No servidor local: systemd timer

```ini
# ~/.config/systemd/user/scorpions-crm-pipelines.service
[Unit]
Description=Scorpions CRM - Pipelines mensais (Receita/Obras/CNES)

[Service]
Type=oneshot
WorkingDirectory=/home/lopes/scorpions-crm
ExecStartPre=/usr/bin/git pull origin master
ExecStart=/home/lopes/scorpions-crm/venv/bin/python3 scripts/rodar_pipelines_mensais.py
StandardOutput=append:/home/lopes/scorpions-crm/pipelines_mensais.log
StandardError=append:/home/lopes/scorpions-crm/pipelines_mensais.log
```

`ExecStartPre` garante que o checkout do servidor está sempre atualizado com
`origin/master` antes de cada disparo -- sem isso, um script novo (como este
próprio orquestrador) pode nunca chegar ao servidor se ninguém lembrar de dar
`git pull` manualmente. Só o serviço do cron faz pull automático; os serviços
`web`/`worker` continuam exigindo deploy manual (`git pull` + restart), para
não derrubar sessões de usuário sem aviso.

```ini
# ~/.config/systemd/user/scorpions-crm-pipelines.timer
[Unit]
Description=Dispara os pipelines mensais do Scorpions CRM uma vez por dia

[Timer]
OnCalendar=daily
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now scorpions-crm-pipelines.timer
systemctl --user list-timers scorpions-crm-pipelines.timer   # confirma o próximo disparo
```

`Persistent=true` garante que, se o servidor estiver desligado no horário
programado, o timer roda assim que a máquina voltar. Log de cada execução em
`pipelines_mensais.log`, na pasta do projeto.

### No Railway: Cron Job

1. No projeto Railway, adicione um **novo serviço → Cron Job**, apontando
   para o mesmo repositório.
2. **Start command**: `python scripts/rodar_pipelines_mensais.py`
3. **Schedule**: diário (ex.: `0 9 * * *`).
4. Monte o **mesmo Volume** dos serviços `web`/`worker` em `/data`, com
   `CRM_DB_PATH=/data/scorpions_base.db`.

### Rodar manualmente

Fora do dia mínimo de espera (padrão: dia 15, dando tempo da Receita
publicar) ou ignorando o controle de "já tentei hoje":
`python scripts/rodar_pipelines_mensais.py --forcar`.
