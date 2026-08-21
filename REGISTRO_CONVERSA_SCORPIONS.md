# Registro da conversa — CRM Scorpions

Data do registro: 16 de agosto de 2026

> Este documento é um resumo cronológico e técnico da conversa, não uma transcrição literal palavra por palavra.

## 1. Objetivo inicial

Criar um robô de prospecção capaz de:

- localizar CNPJs em páginas públicas;
- consultar dados empresariais pela BrasilAPI;
- manter somente empresas ativas;
- transformar os resultados em leads utilizáveis no CRM da Scorpions;
- automatizar as buscas por nicho e localização.

## 2. Primeira solução discutida

O fluxo inicial recebido fazia web scraping de uma URL, procurava padrões de CNPJ com expressão regular e consultava cada CNPJ na BrasilAPI.

Foram identificados e tratados pontos como:

- correção de `if __name__ == "__main__":`;
- validação dos dígitos do CNPJ;
- remoção de duplicatas;
- filtro de situação cadastral ativa;
- tratamento de erros e limites de requisição;
- armazenamento de empresa, razão social, decisor, atividade, telefone, e-mail e endereço.

## 3. Transformação em CRM

O projeto evoluiu para uma aplicação Streamlit com banco SQLite e as seguintes áreas:

- visão geral e funil;
- busca de leads;
- consulta individual por CNPJ;
- campanhas automáticas;
- base de clientes editável;
- cadastro manual;
- histórico de execuções do worker.

Arquivos principais:

- `app.py`: interface do CRM;
- `automation.py`: campanhas, qualificação, persistência e worker;
- `niche_sources.py`: integração e roteamento das fontes públicas;
- `worker.py`: execução periódica das campanhas;
- `scorpions_base.db`: banco local SQLite;
- `openapi.json`: especificação OpenAPI da BrasilAPI.

## 4. Bases de dados utilizadas

### Banco Central do Brasil

Usado para bancos, financeiras, fintechs, cooperativas de crédito, consórcios, câmbio e instituições de pagamento.

- fonte pública;
- não exige chave;
- retorna entidades autorizadas e em atividade;
- permite filtro por município/UF ou busca nacional.

### CVM via BrasilAPI

Usada para corretoras e distribuidoras de valores mobiliários.

- não exige chave;
- mantém somente registros em funcionamento normal;
- permite filtro por UF e município;
- fornece CNPJ, endereço, telefone e e-mail quando disponíveis.

### B3 + Receita Federal via BrasilAPI

Usada para companhias abertas e empresas listadas.

- não exige chave;
- os emissores são deduplicados por CNPJ;
- cada CNPJ é enriquecido com situação cadastral, endereço, município, telefone, e-mail, CNAE e quadro societário;
- empresas inativas são descartadas.

### Google Places

Usado para nichos gerais, como clínicas, restaurantes, lojas e prestadores locais.

- exige a variável `GOOGLE_PLACES_API_KEY`;
- nunca é substituído silenciosamente por dados demonstrativos;
- a cidade é confirmada pelos componentes reais do endereço retornado pelo Google.

## 5. Roteamento automático por nicho

Ao escolher **Automático por nicho**, o CRM decide a fonte:

- banco, crédito, pagamento, fintech, consórcio ou câmbio → Bacen;
- corretora de valores, CTVM, DTVM ou mercado de capitais → CVM;
- B3, companhia aberta, empresa listada ou bolsa de valores → B3 + Receita;
- demais nichos → Google Places, quando a chave estiver configurada.

Expressões ambíguas são protegidas. Exemplos:

- “corretora imobiliária” não vai para a CVM;
- “ações trabalhistas” não vai para a B3;
- “banco de dados” não vai para o Bacen;
- “consultoria financeira” é tratada como negócio local, não instituição supervisionada.

## 6. Qualificação automática

Um candidato automático precisa:

- corresponder ao nicho escolhido;
- estar na localização solicitada, ou em busca nacional;
- estar ativo quando a fonte permite confirmar a situação;
- possuir pelo menos um canal de contato válido;
- atingir score mínimo de 70/100.

A pontuação considera:

- aderência ao nicho;
- localização;
- adequação da fonte;
- atividade confirmada;
- CNPJ válido;
- telefone, e-mail e site disponíveis.

O CRM grava `pontuacao` e `motivo_qualificacao` para auditoria. Valores como `N/A`, “Não informado” e hífens não contam como contato.

## 7. Duplicatas e preservação do trabalho comercial

Quando um CNPJ já existe, o CRM não cria outra linha. Ele pode preencher dados que estavam vazios e atualizar score/motivo, preservando:

- status comercial;
- origem já cadastrada;
- observações do usuário;
- informações preenchidas manualmente.

O índice de nome/endereço é usado somente como alternativa para registros sem CNPJ e sem identificador da fonte.

## 8. Testes reais executados

Foram validados com dados públicos reais:

- Banco Agiplan — Campinas/SP — Bacen — score 100;
- ABN AMRO Clearing — São Paulo/SP — CVM — score 96;
- Banco Digimais — São Paulo/SP — B3 + Receita — score 88;
- busca nacional do Bacen;
- filtro municipal da B3;
- rejeição de empresa inativa, cidade incorreta ou contato ausente;
- migração idempotente e `integrity_check = ok` no SQLite;
- interface Streamlit sem exceções;
- worker em segundo plano e endpoint de saúde respondendo `ok`.

## 9. Teste real do nicho tecnologia

Como não havia chave do Google Places, foi usado o recorte gratuito de empresas de tecnologia listadas na B3, enriquecidas pela Receita/BrasilAPI.

O filtro tecnológico foi refinado para priorizar segmentos como software, programas e serviços, computadores, equipamentos, informática, telecomunicações e tecnologia da informação.

Cinco leads foram aprovados e salvos, todos com score 88:

1. Brasil Tecpar — São Paulo/SP — CNPJ `35.764.708/0001-01`;
2. BRQ — Barueri/SP — CNPJ `36.542.025/0001-64`;
3. Positivo Tecnologia — Curitiba/PR — CNPJ `81.243.735/0001-48`;
4. Quality Software — Rio de Janeiro/RJ — CNPJ `35.791.391/0001-94`;
5. Voke — São Paulo/SP — CNPJ `04.212.396/0001-91`.

Após o teste, a base ficou com 13 leads e permaneceu íntegra.

## 10. Como usar

1. Abra `http://localhost:8501`.
2. Entre em **Buscar leads**.
3. Escolha **Automático por nicho**.
4. Informe o nicho e `Município, UF`, ou use `Brasil`.
5. Execute a busca e confira score e motivo.
6. Salve os resultados aprovados.

Para agendar:

1. Abra **Automação**.
2. Crie uma campanha com a fonte **Automático por nicho**.
3. Escolha nicho, localização, quantidade diária e horário.
4. Mantenha o `worker.py` em execução.

## 11. Estado final

- CRM disponível em `http://localhost:8501`;
- worker online;
- Bacen, CVM e B3 funcionando sem chave;
- Google Places disponível quando uma chave própria for configurada;
- cinco leads tecnológicos reais já cadastrados;
- nenhum dado demonstrativo é usado como fallback de uma consulta real.

## 12. Fallback gratuito com OpenStreetMap

Para nichos locais que antes exigiam `GOOGLE_PLACES_API_KEY`, o modo automático
passou a usar o OpenStreetMap quando a chave do Google não estiver configurada.

O fluxo implementado é:

1. resolve `Município, UF` na API oficial de localidades do IBGE;
2. usa o código IBGE para delimitar exatamente a área municipal no Overpass;
3. consulta uma categoria OSM específica do nicho;
4. mantém apenas registros com telefone, e-mail ou site público;
5. aplica o filtro de localização e o score do CRM;
6. mantém a situação cadastral como **não verificada** até existir confirmação por CNPJ.

A integração possui cache SQLite de 24 horas, bloqueio para evitar chamadas
simultâneas entre interface e worker, priorização de empresas ainda não salvas e
atribuição visível aos colaboradores do OpenStreetMap sob a licença ODbL.

No teste real de `tecnologia` em `Campinas, SP`, o município foi resolvido para o
código IBGE `3509502`. A consulta encontrou sete candidatos contatáveis; os cinco
primeiros aprovados obtiveram scores de 81 a 89:

1. proFUSION embedded systems;
2. ReparaFácil Informática;
3. Vexus Data Recovery;
4. Dextraining;
5. Forti Informática.

Esses resultados foram usados para validar consulta, cache e qualificação, mas não
foram adicionados à base principal automaticamente. Dados do OpenStreetMap devem
ser confirmados antes da abordagem comercial.

## 13. Diagnóstico de fontes

Para dar mais transparência ao usuário sobre o porquê de uma busca automática
não retornar resultados, foi implementado um painel de diagnóstico na aba
**Buscar leads**.

Quando o modo **Automático por nicho** está ativo, o sistema agora exibe:

- a fonte de dados que será usada (Bacen, CVM, B3, Google Places ou OpenStreetMap);
- o motivo da escolha, com base nas palavras-chave do nicho;
- um aviso sobre a qualidade dos dados, especialmente quando o OpenStreetMap é
  usado como alternativa gratuita na ausência de uma chave do Google Places.

Isso ajuda o usuário a entender as limitações de cada fonte e a refinar os
termos de busca para obter melhores resultados.

## 14. Gerenciamento de dependências

Para resolver erros de importação (`reportMissingModuleSource`) e facilitar a
configuração do ambiente de desenvolvimento, foi criado o arquivo `requirements.txt`.

Este arquivo lista todas as bibliotecas Python necessárias para o projeto:
`streamlit`, `pandas`, `requests`, `beautifulsoup4` e `urllib3`.

Para instalar todas as dependências, o usuário pode executar o seguinte comando
no terminal, dentro da pasta do projeto: `pip install -r requirements.txt`.

## 15. Exportação de leads para CSV

Para facilitar a análise de dados e a integração com outras ferramentas, foi
adicionada a funcionalidade de exportar a base de leads para um arquivo CSV.

Na aba **Base de clientes**, um novo botão "Exportar Visão Atual para CSV" foi
implementado. Ele permite que o usuário baixe um arquivo CSV contendo todos os
leads que correspondem aos filtros de pesquisa, nicho e status aplicados no
momento. O arquivo gerado inclui todos os campos do lead e é codificado em
UTF-8 para garantir a compatibilidade.

## 16. Melhorias de usabilidade e acompanhamento

Foram implementadas duas novas funcionalidades para melhorar a visão estratégica e o acompanhamento comercial dos leads:

1.  **Gráfico de Leads por Mês:** Na aba **Visão geral**, foi adicionado um novo gráfico que mostra a quantidade de leads criados a cada mês. Isso permite visualizar a evolução da prospecção ao longo do tempo.

2.  **Campo "Próximo Contato":**
    -   Um novo campo de data, **Próximo Contato**, foi adicionado à base de dados dos leads.
    -   Este campo pode ser preenchido no formulário de **Cadastro manual** ou editado diretamente na tabela da **Base de clientes**.
    -   Na aba **Funil (Kanban)**, os cards agora exibem a data do próximo contato. Se a data estiver vencida, um ícone de alerta (⚠️) é exibido ao lado do nome da empresa, e a data aparece riscada, sinalizando a necessidade de uma ação imediata.

Essas melhorias visam fornecer mais inteligência ao dashboard e ferramentas mais eficazes para o gerenciamento do ciclo de vendas.

## 17. Correção de erro de importação

Foi corrigido um `ImportError` onde a função `resolver_fontes_reais` estava sendo
importada incorretamente do módulo `crm_strategy` em `app.py`.

A função `resolver_fontes_reais` foi movida para ser importada de `niche_sources.py`
em `app.py`, garantindo que a estrutura de módulos esteja correta e evitando
dependências circulares.

## 18. Melhorias de Prospecção e Gestão Financeira

Foram implementadas três funcionalidades principais para expandir a capacidade de prospecção, a gestão do pipeline e o acompanhamento de leads.

### 1. Fusão de Fontes de Prospecção

Para maximizar a quantidade de leads em buscas genéricas (que não se encaixam em fontes oficiais como Bacen ou CVM), o sistema agora consulta simultaneamente o **Google Places** (se a chave de API estiver configurada) e o **OpenStreetMap**. Os resultados são unificados e deduplicados, oferecendo uma lista de prospecção mais completa.

### 2. Campo "Valor da Proposta" e Totais no Kanban

Um novo campo numérico, **Valor da Proposta**, foi adicionado aos leads. Esta melhoria permite:
- Registrar o valor monetário associado a uma oportunidade.
- Visualizar no topo de cada coluna do **Funil Kanban** a soma total dos valores das propostas naquela etapa, oferecendo uma visão clara do valor do pipeline.
- O campo pode ser preenchido no cadastro manual ou editado na base de clientes.

### 3. Alertas por E-mail para Contatos Vencidos

Foi criado um sistema de notificação para follow-ups atrasados.
- O robô (`worker.py`) agora verifica diariamente os leads cujo campo **Próximo Contato** está no passado.
- Para cada lead vencido, um **alerta por e-mail** é enviado, garantindo que nenhuma oportunidade seja esquecida.

**Configuração Necessária:** Para que os alertas por e-mail funcionem, é preciso criar ou editar um arquivo `.env` na pasta raiz do projeto e adicionar as seguintes variáveis com as credenciais do seu servidor de e-mail:

```
SMTP_HOST="smtp.example.com"
SMTP_PORT="587"
SMTP_USER="seu_email@example.com"
SMTP_PASSWORD="sua_senha_de_app"
SMTP_SENDER_EMAIL="seu_email@example.com"
SMTP_RECIPIENT_EMAIL="email_destino@example.com"
```

## 20. Melhorias no Dashboard e Personalização de Alertas

Foram adicionadas funcionalidades para aprimorar a visualização estratégica e a flexibilidade dos alertas por e-mail.

### 1. Gráfico de Pizza de Valor por Etapa do Funil

Na aba **Visão Geral**, foi implementado um novo gráfico de pizza que exibe a distribuição do valor total das propostas pelas etapas ativas do funil (Novos Leads, Contato / Qualificação, Vistoria Técnica / Diagnóstico, Proposta Enviada). Isso proporciona uma visão rápida e estratégica da concentração de valor no pipeline de vendas.

### 2. Corpo do E-mail de Alerta Customizável

O e-mail de alerta para "Próximo Contato" vencido foi aprimorado para ser mais informativo e personalizável:
- O corpo do e-mail agora inclui mais detalhes do lead, como observações, e um link direto para o lead no CRM.
- O conteúdo do e-mail pode ser editado diretamente na string `body` dentro da função `_enviar_alerta_vencimento` no arquivo `automation.py`.
- Para que o link do CRM no e-mail funcione corretamente, é necessário adicionar a variável de ambiente `CRM_BASE_URL` ao arquivo `.env`, apontando para o endereço base da sua aplicação Streamlit (ex: `http://localhost:8501`).

**Configuração Adicional Necessária:**

```
CRM_BASE_URL="http://localhost:8501"
```
```

## 19. Correção de Bugs e Refatoração (Ciclo de Testes)

Com base em um roteiro de testes sistemático, foram identificados e corrigidos bugs críticos e realizadas melhorias de robustez no código.

1.  **Correção Crítica do Worker (`worker.py`):** O robô de automação estava inoperante devido a uma chamada de função incorreta (`executar_campanhas_pendentes` em vez de `executar_tarefas_rotineiras`). A correção reativou a execução de campanhas e o envio de alertas de e-mail.

2.  **Correção de Bugs na Interface (`app.py`):**
    -   **Atualização de Leads:** A lógica na aba "Base de clientes" foi aprimorada para evitar atualizações desnecessárias de leads não modificados e para garantir que o campo "Próximo Contato" seja salvo em um formato de data consistente, corrigindo sua exibição no Kanban.
    -   **Robustez do Dashboard:** O gráfico "Leads Criados por Mês" agora trata corretamente dados de criação inválidos, evitando que a aba "Visão Geral" quebre.

3.  **Refatoração de Código (`automation.py`):** Foi eliminada a duplicação da função `_separar_cidade_uf`, centralizando a lógica no módulo `niche_sources.py` para melhorar a manutenibilidade do código.

4.  **Melhoria de Dependências (`requirements.txt`):** O pacote `tzdata` foi adicionado para garantir a compatibilidade do tratamento de fusos horários em diferentes sistemas operacionais, prevenindo potenciais erros.

Essas alterações restauram a estabilidade da aplicação e aprimoram a qualidade geral do código.
```

## 19. Correção de Bugs e Refatoração (Ciclo de Testes)

Com base em um roteiro de testes sistemático, foram identificados e corrigidos bugs críticos e realizadas melhorias de robustez no código.

1.  **Correção Crítica do Worker (`worker.py`):** O robô de automação estava inoperante devido a uma chamada de função incorreta (`executar_campanhas_pendentes` em vez de `executar_tarefas_rotineiras`). A correção reativou a execução de campanhas e o envio de alertas de e-mail.

2.  **Correção de Bugs na Interface (`app.py`):**
    -   **Atualização de Leads:** A lógica na aba "Base de clientes" foi aprimorada para evitar atualizações desnecessárias de leads não modificados e para garantir que o campo "Próximo Contato" seja salvo em um formato de data consistente, corrigindo sua exibição no Kanban.
    -   **Robustez do Dashboard:** O gráfico "Leads Criados por Mês" agora trata corretamente dados de criação inválidos, evitando que a aba "Visão Geral" quebre.

3.  **Refatoração de Código (`automation.py`):** Foi eliminada a duplicação da função `_separar_cidade_uf`, centralizando a lógica no módulo `niche_sources.py` para melhorar a manutenibilidade do código.

4.  **Melhoria de Dependências (`requirements.txt`):** O pacote `tzdata` foi adicionado para garantir a compatibilidade do tratamento de fusos horários em diferentes sistemas operacionais, prevenindo potenciais erros.

Essas alterações restauram a estabilidade da aplicação e aprimoram a qualidade geral do código.
```
