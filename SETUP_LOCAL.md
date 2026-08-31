# Rodando o Scorpions CRM localmente

Guia rápido para outra pessoa clonar o repositório e ter o CRM rodando na própria máquina.

## Pré-requisitos

- Python 3.11+ (o projeto foi testado com 3.13)
- Git

## 1. Clonar e instalar dependências

```bash
git clone https://github.com/eulopes/scorpions-crm.git
cd scorpions-crm
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

pip install -r requirements.txt
```

## 2. Rodar

```bash
streamlit run app.py --server.port 8501
```

Abre em `http://localhost:8501`. Na primeira execução o app cria sozinho o banco SQLite local (`scorpions_base.db`, já no `.gitignore` — nunca é versionado) com todas as tabelas.

O worker (campanhas agendadas, atualização de inteligência de oportunidade) sobe automaticamente como subprocesso do próprio app — não precisa rodar nada à parte. Para desativar (ex.: rodando vários `streamlit run` em paralelo), defina `SCORPIONS_DISABLE_WORKER=1` antes de subir.

## 3. Primeiro acesso

O banco novo já vem com 3 contas de diretor (acesso total) pré-criadas:

| Usuário | Senha provisória |
| --- | --- |
| `lopes` | `trocar123` |
| `moroni` | `trocar123` |
| `junior` | `trocar123` |

Troque a senha assim que logar, em **Trocar senha** na barra lateral (abaixo do seu nome, acima de "Sair").

Alternativa legada: `.streamlit/secrets.toml` (copie de `secrets.toml.example`) aceita uma seção `[AUTH_USERS]` com `usuario = "hash bcrypt"` — gere o hash com `python scripts/gerar_hash_senha.py`. Não é necessário para simplesmente rodar o projeto.

## 4. Variáveis de ambiente (todas opcionais)

| Variável | Para quê | Sem ela |
| --- | --- | --- |
| `GOOGLE_PLACES_API_KEY` | Prospecção via Google Places | Fonte fica indisponível; Bacen/CVM/B3/OpenStreetMap continuam funcionando normalmente (não precisam de chave) |
| `CRM_DB_PATH` | Caminho do banco SQLite | Usa `scorpions_base.db` na raiz do projeto |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_SENDER_EMAIL` / `SMTP_RECIPIENT_EMAIL` | Alerta por e-mail de follow-up vencido | Alerta simplesmente não é enviado (sem erro) |
| `SCORPIONS_DISABLE_WORKER` | Desativa o worker embutido | Worker sobe junto automaticamente |

Também dá para colocar `GOOGLE_PLACES_API_KEY` em `.streamlit/secrets.toml` em vez de variável de ambiente.

## 5. Rodar os testes

```bash
python tests/run_all.py
```

Roda os 3 arquivos de teste (smoke test de todas as páginas, ações reais de ponta a ponta via `streamlit.testing.v1.AppTest`, e o motor de Opportunity Intelligence) em processos separados, sobre bancos SQLite temporários — nunca toca no banco local de desenvolvimento nem em produção.

## 6. Zerar a base local a qualquer momento

Logado como diretor, em **Equipe → Zona de risco**, digitando `ZERAR TUDO`. Apaga leads/snapshots/sinais/histórico de oportunidade; não mexe em usuários, equipes, campanhas nem na lista de supressão de contato.

## Estrutura rápida

- `app.py` — interface Streamlit (páginas, RBAC, login)
- `automation.py` — motor de campanhas e fontes públicas (Bacen, CVM, B3, Google Places, OpenStreetMap)
- `company_history.py` / `change_detection.py` / `sales_signals.py` / `opportunity_engine.py` — Opportunity Intelligence (scores, sinais, Radar)
- `crm_strategy.py` / `niche_sources.py` — classificação de ICP e roteamento por nicho (sem dependência de Streamlit/DB)
- `access_control.py` — níveis de usuário e permissões
- `worker.py` — scheduler das campanhas (também roda como subprocesso do app)
- `tests/` — suíte automatizada
