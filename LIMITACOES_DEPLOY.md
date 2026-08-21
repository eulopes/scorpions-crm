# Limitações do deploy no Streamlit Community Cloud

O app publicado em `https://crmbyscorpions.streamlit.app/` roda no **Streamlit
Community Cloud**, que é gratuito mas tem duas limitações importantes para
este projeto:

## 1. Disco não é persistente

O arquivo `scorpions_base.db` (SQLite) vive dentro do container da aplicação.
Toda vez que o app é reiniciado (redeploy por push no Git, "sleep" por
inatividade, manutenção da plataforma), **o banco volta ao estado inicial
vazio**. Nenhum dado gravado nessa instância sobrevive a um restart.

## 2. Não roda processo em segundo plano

O `worker.py` (que executa campanhas agendadas e envia alertas de e-mail de
"Próximo Contato" vencido) **não roda no Streamlit Community Cloud**. A
plataforma só mantém viva a aplicação web em si; não há como manter um
processo separado de longa duração.

## O que isso significa na prática

- A URL pública serve bem como **vitrine e superfície de teste de segurança**
  (foi assim que usamos até agora, inclusive para os testes de red team com o
  Deep Hat).
- Para uso comercial de verdade, com dados reais e automação funcionando, a
  operação principal deve continuar **local** (`streamlit run app.py` +
  `python worker.py` rodando ao mesmo tempo), ou migrar para um host que
  ofereça disco persistente e processo de fundo (ex: Render, Railway, um VPS).

## Caminho recomendado para produção real

Ver `DEPLOY_PRODUCAO.md` para o passo a passo de deploy num host com essas
capacidades.
