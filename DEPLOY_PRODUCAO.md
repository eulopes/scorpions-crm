# Deploy em produção de verdade (Railway)

O Streamlit Community Cloud (`crmbyscorpions.streamlit.app`) não tem disco
persistente nem processo de fundo — ver `LIMITACOES_DEPLOY.md`. Este guia
publica o CRM num host que resolve os dois problemas: **app.py** e
**worker.py** rodando juntos, compartilhando um disco que sobrevive a
redeploys.

Escolhido: **Railway** (tem camada gratuita com créditos de teste, depois é
pago por uso — confira o preço atual antes de comprometer o cartão).

## O que já está pronto no código

- `Procfile`: define os dois processos (`web` roda o Streamlit, `worker` roda
  o `worker.py`).
- `CRM_DB_PATH` (variável de ambiente): se definida, o banco SQLite passa a
  gravar nesse caminho em vez da pasta do projeto — aponte para o volume
  persistente.
- `AUTH_USERS_JSON` (variável de ambiente): alternativa ao `secrets.toml` para
  logar os usuários do CRM sem depender do mecanismo de secrets do Streamlit
  Cloud. Formato: `{"admin": "hash_bcrypt_aqui"}` (uma linha só, JSON válido).

## Passo a passo

1. Crie uma conta em **railway.app** (pode entrar direto com GitHub).
2. **New Project → Deploy from GitHub repo → `eulopes/scorpions-crm`**.
3. Railway vai detectar o `Procfile` e sugerir os dois serviços (`web` e
   `worker`). Confirme os dois.
4. Em **cada um dos dois serviços**, adicione as variáveis de ambiente:
   - `GOOGLE_PLACES_API_KEY` = (sua chave real)
   - `AUTH_USERS_JSON` = `{"admin":"COLE_O_HASH_BCRYPT_AQUI"}`
   - `CRM_DB_PATH` = `/data/scorpions_base.db`
   - Variáveis de SMTP, se for usar alerta por e-mail (`SMTP_HOST`,
     `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_SENDER_EMAIL`,
     `SMTP_RECIPIENT_EMAIL`, `CRM_BASE_URL`).
5. Crie um **Volume** (disco persistente) no serviço `web`, montado em
   `/data`. **Monte o mesmo volume no serviço `worker`, no mesmo caminho
   `/data`** — os dois processos precisam enxergar o mesmo arquivo de banco.
6. Deploy. Railway te dá uma URL pública (`*.up.railway.app`); dá pra
   configurar um domínio próprio depois, se quiser.
7. Teste o login com o mesmo usuário/senha de sempre.

## Diferença chave em relação ao Streamlit Community Cloud

Nesse host, os dados persistem entre deploys e o worker roda de verdade —
esta pode virar a instância "de produção" oficial, enquanto o Streamlit
Community Cloud continua servindo como vitrine pública/superfície de teste.

## Cron mensal dos pipelines de dados (Receita/Obras/CNES)

`scripts/rodar_pipelines_mensais.py` dispara os três orquestradores em
sequência (Receita Federal, Obras/SISSEL-SP, CNES) e é seguro rodar todo
dia: cada pipeline só baixa/processa de novo quando ainda não teve sucesso
na competência (mês) atual -- nos outros dias a checagem é só uma leitura
de `estado_automacao`, sem custo de rede. Uma falha isolada (ex.: servidor
da Receita fora do ar) não trava os outros dois nem impede nova tentativa
no dia seguinte, dentro do mesmo mês.

1. No projeto Railway já criado, adicione um **novo serviço → Cron Job**,
   apontando para o mesmo repositório (`eulopes/scorpions-crm`).
2. **Start command**: `python scripts/rodar_pipelines_mensais.py`
3. **Schedule**: diário (ex.: `0 9 * * *`) -- o script sozinho decide se há
   algo a fazer; não precisa acertar o dia exato do mês na config do cron.
4. Monte o **mesmo Volume** dos serviços `web`/`worker` em `/data`, e
   defina a mesma variável `CRM_DB_PATH=/data/scorpions_base.db` -- os três
   processos precisam enxergar o mesmo banco.
5. Sem variável extra além dessa: os orquestradores individuais descobrem
   sozinhos a competência mais recente publicada em cada fonte.

Pra rodar manualmente fora do dia mínimo de espera (padrão: dia 15, dando
tempo da Receita publicar) ou ignorar o controle de "já tentei hoje":
`python scripts/rodar_pipelines_mensais.py --forcar`.
