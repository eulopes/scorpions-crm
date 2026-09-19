# Handoff: Scorpions CRM — redesign da interface (v2)

## Overview

Scorpions CRM é o CRM interno da Scorpions (soluções tecnológicas), usado diariamente por poucas pessoas do time comercial próprio. Centraliza leads capturados em fontes públicas (Bacen, CVM, B3, Google Places, OpenStreetMap), aplica scoring de ICP, organiza negócios em kanban por etapa, permite cadastro manual de empresas e agenda campanhas automáticas de captura.

O app atual é um Streamlit funcional, mas herda a aparência padrão da ferramenta. Este handoff cobre o **redesenho da interface** na identidade visual da marca Scorpions, resolvendo quatro problemas concretos relatados pelo time:

1. Tabelas de "Clientes/Empresas" densas e cortadas, exigindo scroll horizontal.
2. Dropdown de "Excluir lead" mostrando só o ID do registro → risco de exclusão errada.
3. Estados vazios que parecem quebrados em vez de "carregando".
4. Falta de identidade — o produto não parece premium apesar da marca já ter identidade definida.

## About the Design Files

Os arquivos deste pacote são **referências de design feitas em HTML** — protótipos que mostram aparência e comportamento pretendidos, **não código de produção para copiar**. A tarefa é **recriar estes designs no ambiente do codebase de destino** (Streamlit atual com CSS customizado, ou uma stack web como React/Next se houver migração), usando os padrões e bibliotecas já estabelecidos nele. Se ainda não existir ambiente definido para a nova interface, escolha a stack mais adequada e implemente lá.

`Scorpions CRM v2.dc.html` é um componente único que contém as 6 telas, controladas por estado interno (`state.screen`). Abre direto no navegador.

## Fidelity

**High-fidelity.** Cores, tipografia, espaçamentos, estados e microinterações são finais. Recrie a UI fielmente. Onde o codebase de destino já tiver componentes equivalentes (input, select, tabela, modal), use-os e aplique os tokens abaixo.

## Screens / Views

Todas as telas compartilham o mesmo shell: **sidebar fixa de 252px à esquerda + header sticky + área de conteúdo rolável**.

### Shell — Sidebar

- **Layout:** `width: 252px`, `flex: none`, `position: sticky; top: 0; height: 100vh`, fundo `#0A0A0F`, borda direita `1px solid #1E232B`, `padding: 20px 0`, `display: flex; flex-direction: column; gap: 20px`.
- **Marca (topo):** `padding: 0 18px`, flex row, `gap: 11px`. Logo 38×38px, `object-fit: cover`, `object-position: center 34%`, borda `1px solid #1E232B`, `border-radius: 8px`, `filter: brightness(1.35) contrast(1.1)`. Ao lado: "SCORPIONS" em Orbitron 700, 14px, `letter-spacing: .2em`, cor `#F5F7FA`; abaixo "SOLUÇÕES TECNOLÓGICAS" em 9.5px, `letter-spacing: .22em`, uppercase, cor `#7DD3FC`.
- **Busca global:** container `background: #111319`, borda `1px solid rgba(125,211,252,.18)`, `border-radius: 4px`, `padding: 8px 11px`. Ícone `⌕` cor `#5A6373`; input transparente sem borda, 12.5px; badge de atalho "⌘K" 9.5px, borda `1px solid #2D333D`, `border-radius: 3px`, `padding: 1px 5px`. Digitar aqui filtra a base e navega para a tela Clientes.
- **Nav:** label de grupo "WORKSPACE" 9.5px, `letter-spacing: .24em`, uppercase, `#5A6373`. Cada item: linha de 9px 10px, `font-size: 12.5px`, `border-left: 2px solid transparent`, cor `#8A94A6`. Ativo: `background: rgba(37,104,255,.12)`, `border-left-color: #7DD3FC`, cor `#F5F7FA`, `font-weight: 600`, `border-radius: 0 3px 3px 0`. Hover: `background: #111319; color: #F5F7FA`. Cada item tem ícone glifo (15px de largura, centralizado) e, quando aplicável, um badge de contagem à direita (9.5px, borda `1px solid #2D333D`, pill).
  - Itens (ícone · label · contagem): `◱` Dashboard · —; `▤` Negócios / Pipeline · 8; `⌖` Leads / Prospecção · —; `▦` Clientes / Empresas · 47; `⚡` Automação · 1; `＋` Nova empresa · —.
- **Rodapé:** divisor `height: 1px; background: linear-gradient(90deg, rgba(125,211,252,.3), transparent)`. Linha de status: bolinha 6px `#7DD3FC` com animação `pulse` (2.2s infinite) + texto "Worker online · v1.1" 11.5px `#8A94A6`. Linha de usuário: avatar 28×28 quadrado (`border-radius: 4px`, borda `1px solid #2D333D`, fundo `#111319`, texto "AD" 11px `#7DD3FC`), nome "admin" 12px/500, papel "Comercial" 10.5px `#5A6373`, botão "SAIR" outline 10.5px com `letter-spacing: .06em` (hover: borda `#7DD3FC`, texto `#F5F7FA`).

### Shell — Header

- `display: flex; align-items: center; gap: 18px`, `padding: 17px 26px`, borda inferior `1px solid #1E232B`, `background: rgba(10,10,15,.86)`, `backdrop-filter: blur(10px)`, `position: sticky; top: 0; z-index: 5`.
- Título: Orbitron 600, 19px, `letter-spacing: .06em`, uppercase. Ao lado, badge de contexto: 10px, cor `#7DD3FC`, borda `1px solid rgba(125,211,252,.28)`, `border-radius: 3px`, `padding: 2px 8px`, `letter-spacing: .1em`, uppercase.
- Subtítulo: 12px `#8A94A6`, `margin-top: 3px`.
- Ações à direita: **"Prospectar"** (secundário: `background: rgba(37,104,255,.07)`, borda `1px solid rgba(125,211,252,.3)`, texto `#F5F7FA`; hover borda `#7DD3FC` e fundo `rgba(37,104,255,.16)`) e **"+ Nova empresa"** (primário: `background: linear-gradient(90deg, #0D6EFD, #2568FF)`, borda `1px solid rgba(125,211,252,.45)`, texto `#fff`, 600, `box-shadow: 0 0 22px rgba(37,104,255,.28)`; hover `filter: brightness(1.12)`). Ambos `border-radius: 4px`, `padding: 9px 14–15px`, 12.5px.
- Título/badge/subtítulo por tela:
  - Dashboard · "Operacional" · "Prospecção, pipeline e operação comercial em um só lugar."
  - Pipeline · "4 etapas" · "Negócios abertos e valor em proposta por etapa."
  - Prospecção · "Bacen · CVM · B3" · "Captura em fontes públicas com scoring de ICP."
  - Clientes · "47 registros" · "Base cadastral, consulta CNPJ e exclusão segura."
  - Automação · "Worker ativo" · "Campanhas agendadas, motor contínuo e histórico."
  - Nova empresa · "Cadastro manual" · "Campos obrigatórios validados antes de salvar."

### 1. Dashboard

**Propósito:** ler o estado da operação em 5 segundos e saber o que fazer hoje.

**Layout:** `padding: 22px 26px 48px`, coluna com `gap: 20px`. Três blocos: faixa de KPIs, grid `1.25fr 1fr` (funil + ICP), e card de atenção em largura cheia.

- **KPIs:** grid `repeat(auto-fit, minmax(168px, 1fr))`, `gap: 11px`. Cada card: `background: linear-gradient(180deg, #111319, #0C0E13)`, borda `1px solid #1E232B`, `border-radius: 4px`, `padding: 14px 15px`, `overflow: hidden`, `position: relative`. Linha de circuito no topo: `position: absolute; top:0; left:0; right:0; height:1px; background: linear-gradient(90deg, transparent, rgba(125,211,252,.45), transparent)`. Hover: borda `rgba(125,211,252,.35)`. Conteúdo: label 9.5px `letter-spacing: .16em` uppercase `#5A6373`; valor Orbitron 600 25px; nota 11px na cor semântica.
  - Leads na base · 47 · "+47 este mês" (`#7DD3FC`)
  - Novos leads · 42 · "sem primeiro contato" (`#8A94A6`)
  - Em qualificação · 2 · "2 aguardando retorno" (`#8A94A6`)
  - Propostas · 1 · "R$ 132.000 em jogo" (`#7DD3FC`)
  - Conversão · 2,1% · "meta 6%" (`#ff8f8f`)
  - Valor bruto · R$ 206k · "ciclo médio 34 dias" (`#8A94A6`)
- **Funil comercial:** card `#111319`, borda `1px solid #1E232B`, `border-radius: 4px`, `padding: 18px`. Título Orbitron 600 13px `letter-spacing: .14em` uppercase + "últimos 30 dias" 10.5px `#5A6373`. Quatro barras (`gap: 14px`): linha de topo com nome da etapa (12px `#8A94A6`) e à direita `contagem · valor` (`#F5F7FA` com o separador "·" em `#5A6373`); trilha `height: 6px; background: #1E232B; border-radius: 999px`; preenchimento `linear-gradient(90deg, #0D6EFD, #7DD3FC)` + `box-shadow: 0 0 10px rgba(37,104,255,.5)`, largura = contagem da etapa ÷ maior contagem. **Substitui as tabelas de uma linha do app atual.**
- **ICP por segmento:** mesmo card. Cada linha: nome (elipse em overflow), "N leads" 11.5px `#8A94A6`, chip "score N". Abaixo, alerta informativo: `background: rgba(37,104,255,.06)`, borda `1px solid rgba(125,211,252,.22)`, `border-left: 2px solid #7DD3FC`, `border-radius: 3px`, `padding: 12px 14px`; título "45 de 47 leads sem ICP" 12.5px/600 `#7DD3FC`; corpo 11.5px `#8A94A6` `line-height: 1.6`; botão outline "Completar cadastros" → navega para Clientes.
- **Precisa de atenção hoje:** grid `repeat(auto-fit, minmax(230px, 1fr))`, `gap: 11px`. Cards `#0C0E13`, borda `1px solid #1E232B`, `padding: 13px 14px`. Kicker 9.5px uppercase `letter-spacing: .16em` na cor semântica, título 13.5px/600, nota 11.5px `#8A94A6`.
  - "Proposta vencendo" (`#ff8f8f`) — Empresa de Tecnologia da Informação — "R$ 132.000 enviados há 9 dias — sem retorno."
  - "Vistoria hoje" (`#7DD3FC`) — Green Tecnologia — "Diagnóstico técnico às 14h, Av. Paulista 326."
  - "Sem contato" (`#8A94A6`) — 39 leads em Novos Leads — "Nenhuma tentativa registrada desde a captura."

### 2. Negócios / Pipeline

**Propósito:** mover negócios entre etapas e ver valor em proposta por coluna.

**Layout:** `padding: 20px 26px 48px`, `overflow-x: auto`; grid `repeat(4, minmax(268px, 1fr))`, `gap: 12px`.

- **Coluna:** `background: #0C0E13`, borda `1px solid #1E232B`, `border-radius: 4px`, `padding: 13px`. Cabeçalho: bolinha 6px (`#5A6373` na 1ª etapa, `#2568FF` nas intermediárias, `#7DD3FC` em Proposta Enviada), nome 10.5px/600 `letter-spacing: .14em` uppercase, contagem Orbitron 11px `#8A94A6`. Subtítulo "R$ X em proposta" 10.5px `#5A6373`.
- **Card de negócio:** `background: #111319`, borda `1px solid #1E232B`, `border-radius: 4px`, `padding: 12px`. Hover: borda `rgba(125,211,252,.4)` + `box-shadow: 0 0 20px rgba(37,104,255,.12)`. Nome 13.5px/600 `line-height: 1.3`; badge de score à direita (ver **Score badge** em Design Tokens); linha "cidade · nicho" 11px `#8A94A6`; chip de ICP; valor da proposta Orbitron 10.5px `#7DD3FC` alinhado à direita (só quando > 0). Rodapé separado por `border-top: 1px solid #1E232B`, `padding-top: 10px`: `<select>` de etapa (fundo `#0A0A0F`, borda `1px solid #2D333D`, 11px) que move o card, e botão "Abrir" outline que navega para Clientes com a linha da empresa já expandida e a busca preenchida.
- **Coluna vazia:** caixa `border: 1px dashed #2D333D`, `border-radius: 4px`, `padding: 18px 13px`, texto 11.5px `#5A6373`: "Nada nesta etapa. Mova um lead pelo seletor do cartão." **Nunca deixar coluna vazia sem essa mensagem.**

### 3. Leads / Prospecção

**Propósito:** disparar captura em fontes públicas e triar resultados.

**Layout:** `padding: 22px 26px 48px`, `max-width: 1080px`.

- **Painel de busca:** card `#111319`, borda `1px solid #1E232B`, `padding: 18px`, `position: relative; overflow: hidden`. Detalhe de marca: linha animada no topo — `width: 40%; height: 1px; background: linear-gradient(90deg, transparent, #2568FF, #7DD3FC)`, animação `trace 4s linear infinite` (`translateX(-100%)` → `translateX(300%)`).
- Duas linhas de campos, grid `2fr 1.3fr 108px`, `gap: 13px`: **Nicho ou segmento** (placeholder "Ex.: cooperativa de crédito — vazio busca todos"), **Município, UF** ("Ex.: Campinas, SP"), **Qtde** (valor 8, Orbitron); depois **Fonte** (select: Automático por nicho / Google Places / OpenStreetMap / Bacen · CVM · B3) e **Score mínimo** (70, Orbitron).
- **Chips de fonte:** "Google Places · conectado" em chip accent; "Bacen · público", "CVM · público", "B3 · público", "OpenStreetMap · fallback" em chip neutro. Substituem o parágrafo denso de regras do app atual.
- Ações: primário "Encontrar oportunidades" + outline "Sugerir nicho por ICP".
- **Estado vazio (idle):** caixa dashed, título Orbitron 13px uppercase "Nenhuma busca nesta sessão", corpo 12.5px `#8A94A6`: "A última execução (tecnologia · São Paulo, SP) trouxe 47 empresas — 12 entraram no pipeline e 4 eram duplicadas."
- **Estado de carregamento:** linha "Consultando fontes públicas…" com bolinha 7px `#7DD3FC` animada (`skel 1s infinite`) + 5–7 skeletons de 46px (`#111319`, borda `1px solid #1E232B`, `animation: skel 1.3s infinite`). **Este é o estado que hoje parece quebrado — precisa existir.**
- **Resultados:** título Orbitron 14px uppercase "8 oportunidades" + badge "6 acima do score mínimo". Lista de linhas (`gap: 7px`): card `#111319` borda `1px solid #1E232B`, `padding: 11px 14px`, hover borda `rgba(125,211,252,.35)`; nome 13px/600, "cidade · fonte" 11px `#8A94A6`, badge de score, botão outline "Enviar ao pipeline". Dados de exemplo: Nexo Datacenter 94, Vetor Redes 88, Cooperativa Credi Vale 85, Lab Diagnóstico Central 78, Galpão Sul Logística 72, TechMed Equipamentos 71, Oficina Digital ME 48, Padaria Bonfim 22.

### 4. Clientes / Empresas

**Propósito:** trabalhar a base cadastral sem sofrer com a tabela larga; completar CNPJ; excluir com segurança.

**Layout:** `padding: 20px 26px 48px`.

- **Filtros:** linha flex `gap: 11px`, `align-items: flex-end`, `flex-wrap: wrap`. Campos: **Pesquisar na base** (flex 1, min 210px), **Nicho** (155px, select: Todos / tecnologia / educação), **Etapa** (185px, select: Todas + as 4 etapas), e **segmented de densidade** (`background: #0A0A0F`, borda `1px solid #2D333D`, `border-radius: 3px`, `padding: 3px`; opção ativa `background: rgba(37,104,255,.18); color: #7DD3FC`, inativa transparente `#8A94A6`) com "Confortável" e "Compacta".
- **Barra de resultado:** contador Orbitron 12px uppercase "N de M empresas", dica 11px `#5A6373` "clique na linha para ver CNPJ, decisor, endereço e serviços", e à direita botão outline "Recarregar base" (dispara skeleton de ~1,1s).
- **Tabela:** wrapper `background: #111319`, borda `1px solid #1E232B`, `border-radius: 4px`, `overflow: auto`. Tabela `min-width: 900px`, `border-collapse: collapse`, 12.5px.
  - `th`: fundo `#0C0E13`, 9.5px, `letter-spacing: .16em`, uppercase, `#5A6373`, 600, `padding: 11px 12–14px`, `border-bottom: 1px solid #2D333D`.
  - **Coluna Empresa fixa:** `position: sticky; left: 0` no `th` (fundo `#0C0E13`) e no `td` (fundo `#111319`), `min-width: 228px`. **Correção central do problema de scroll horizontal.**
  - Colunas visíveis: Empresa · Cidade · Nicho · Segmento ICP · Score (dir.) · Etapa · Proposta (dir.) · Próximo contato. Todo o resto sai da grade e vai para a linha expansível.
  - Célula Empresa: caret `›` (13px `#5A6373`, `transform: rotate(90deg)` quando aberto, `transition: transform .15s`), nome 600 com elipse em `max-width: 205px`, e sublinha "#ID · CNPJ ou 'CNPJ pendente'" 10.5px `#5A6373`.
  - Proposta: Orbitron 11.5px, `#7DD3FC` quando > 0, `—` em `#5A6373` quando zero. Próximo contato: `#8A94A6`, "sem agenda" quando nulo.
  - `tr` clicável (`cursor: pointer`, borda inferior `1px solid #1E232B`, hover `background: #0C0E13`).
  - **Densidade:** o `padding` vertical das células vem de `var(--row-pad)` no wrapper — `11px` confortável, `5px` compacta.
- **Linha expandida:** `td` com `colspan="8"`, fundo `#0C0E13`, `padding: 16px 14px`. Grid `repeat(auto-fit, minmax(190px, 1fr))`, `gap: 14px`, com 6 pares label/valor (label 9.5px uppercase `letter-spacing: .16em` `#5A6373`; valor 12.5px `#C3CBD8`): Razão social, CNPJ, Decisor, Endereço, Contato (tel · site), Serviços recomendados. Fallbacks textuais: "não informada", "pendente de consulta", "não mapeado", "sem contato". Abaixo, ações: "Consultar CNPJ" (accent suave), "Registrar contato" (outline) e, alinhado à direita, "Excluir" em vermelho — este handler precisa de `event.stopPropagation()` para não colapsar a linha.
- **Estado vazio de filtro:** caixa dashed, título Orbitron 13px uppercase "Nenhuma empresa com esses filtros", corpo "A base tem 47 registros. Limpe os filtros ou cadastre a empresa manualmente.", botão "Limpar filtros".
- **Estado de carregamento:** 7 skeletons de 44px.
- **Dois cards no rodapé** (grid `1fr 1fr`, `gap: 14px`):
  - *Consulta cadastral · BrasilAPI* — título Orbitron 12px uppercase, nota "Dados públicos, sem chave. Preenche razão social, endereço e CNAE.", input de CNPJ + botão primário "Consultar".
  - *Excluir registro* — card com borda `1px solid rgba(255,107,107,.2)`, título em `#ff8f8f`, nota "O seletor mostra nome, cidade e etapa — nunca só o ID.", `<select>` cujas opções são **`Nome — #ID · Cidade · Etapa`** (ex.: "Green Tecnologia — #6 · São Paulo, SP · Vistoria Técnica"), e botão de exclusão vermelho. **Correção do risco de exclusão errada.**

### 5. Automação

**Propósito:** operar campanhas agendadas e auditar execuções.

**Layout:** `padding: 20px 26px 48px`.

- **Faixa de status do worker:** `background: rgba(37,104,255,.06)`, borda `1px solid rgba(125,211,252,.22)`, `border-left: 2px solid #7DD3FC`, `border-radius: 3px`, `padding: 12px 15px`, 12.5px `#C3CBD8`, com bolinha pulsante: "Worker online — 1 campanha ativa, próxima execução às 19:17".
- **Campanhas como cards** (grid `repeat(auto-fit, minmax(250px, 1fr))`, `gap: 11px`) — substituem o dump de tabela SQL do app atual. Card `#111319`, borda `1px solid #1E232B`, `padding: 15px`. Nome 13.5px/600 + chip de status (Ativa = accent, Pausada = neutro). Linha "escopo · fonte" 11.5px `#8A94A6`. Rodapé separado por `border-top: 1px solid #1E232B`, `padding-top: 12px`, `gap: 16px`: três métricas com label 9.5px uppercase e valor Orbitron 14px — Limite/dia, Horário, Última.
  - #1 "caçando cliente" · todos · são paulo, sp · Automático por nicho · 8 · 19:17 · 21/08 22:16 · Ativa
  - #2 "clínicas SP" · saúde · são paulo, sp · Google Places · 12 · 07:30 · 20/08 07:31 · Pausada
- **Barra de ações:** select "Gerenciar campanha" (320px) com opções `#N — nome · escopo`, + "Executar agora" (primário), "Ativar / pausar" (outline), "Excluir campanha" (vermelho outline → confirmação).
- **Histórico de execuções:** tabela nos mesmos tokens; colunas Início · Campanha · Status (chip) · Encontrados · Inseridos · Duplicados (as três numéricas em Orbitron 11.5px, alinhadas à direita; Inseridos em `#7DD3FC`, Duplicados em `#8A94A6`). Status: Concluída = chip accent, Sem resultados = neutro, Erro de chave = chip vermelho.

### 6. Nova empresa

**Propósito:** cadastro manual com validação clara.

**Layout:** `padding: 22px 26px 48px`, `max-width: 920px`. Card `#111319`, borda `1px solid #1E232B`, `padding: 20px`. Grid `1fr 1fr`, `gap: 15px 20px`.

- Campos, na ordem: **Empresa \*** (placeholder "Nome fantasia"), Razão social, **Nicho \*** ("Ex.: tecnologia"), CNPJ ("00.000.000/0001-91"), Contato / decisor, Cidade UF, E-mail, Telefone, Endereço (`grid-column: 1/-1`), Observações (textarea `min-height: 88px`, `grid-column: 1/-1`).
- Labels: 10.5px `#8A94A6`, `letter-spacing: .06em`, uppercase, `margin-bottom: 6px`; o asterisco de obrigatório em `#7DD3FC`.
- **Validação:** ao salvar sem Empresa ou Nicho, a borda do campo vira `1px solid rgba(255,107,107,.55)` e aparece mensagem 11px `#ff8f8f` abaixo — "Obrigatório para salvar o registro." / "Obrigatório — define a fonte de prospecção." Nada é salvo.
- **Rodapé de ações:** separado por `border-top: 1px solid #1E232B`, `padding-top: 16px`. "Salvar empresa" (primário), "Cancelar" (outline, volta para Clientes) e, após sucesso, badge "Cadastrada e enviada a Novos Leads" (11.5px `#7DD3FC`, borda `1px solid rgba(125,211,252,.3)`). O registro entra na base com etapa `Novos Leads`, score 50 e ICP "Não classificado".

### Modal de confirmação de exclusão

- **Backdrop:** `position: fixed; inset: 0; display: grid; place-items: center; padding: 24px; background: rgba(5,6,8,.78); backdrop-filter: blur(3px); z-index: 20`.
- **Diálogo:** `width: min(460px, 100%)`, `background: #111319`, borda `1px solid rgba(255,107,107,.3)`, `border-radius: 4px`, `padding: 22px`, `box-shadow: 0 24px 60px rgba(0,0,0,.6)`, `animation: fade .18s ease`.
- Título Orbitron 14px uppercase `letter-spacing: .1em` em `#ff8f8f`: "Excluir empresa?" / "Excluir campanha?".
- Corpo 12.5px `#C3CBD8` `line-height: 1.65` **nomeando o registro**: "Você vai excluir {nome} (#{id} · {cidade}), hoje em {etapa}. Esta ação não pode ser desfeita." Para campanha: "A campanha “caçando cliente” e seu histórico de execuções serão removidos. Os leads já capturados permanecem na base."
- **Confirmação digitada:** campo "Digite o nome para confirmar"; o botão "Excluir definitivamente" só habilita quando o texto (trim + lowercase) casa com o nome do registro. Desabilitado: `opacity: .45; cursor: not-allowed`. Habilitado: `background: rgba(255,107,107,.16)`, borda `1px solid rgba(255,107,107,.5)`, texto `#ffb3b3`, 600.
- Ações alinhadas à direita, `gap: 9px`: "Cancelar" (outline) e "Excluir definitivamente".

## Interactions & Behavior

- **Navegação:** clique na sidebar troca `screen`; ao trocar, limpa `expanded` e `saved`. Header "Prospectar" → Prospecção; "+ Nova empresa" → Nova empresa. Alerta de ICP → Clientes. "Abrir" no card do pipeline → Clientes com `expanded = id` e `query = nome`.
- **Busca global da sidebar:** digitar atualiza `query` **e** navega para Clientes (busca em nome + cidade + CNPJ, case-insensitive, substring).
- **Filtros:** combinados por AND (query ∧ nicho ∧ etapa). Zero resultados → estado vazio com "Limpar filtros".
- **Expandir linha:** clique na `tr` alterna `expanded` (só uma linha aberta por vez). Ações internas param a propagação.
- **Mover negócio:** `<select>` do card altera `stage` do registro; KPIs, barras do funil e contagens das colunas recalculam na hora.
- **Prospecção:** clique em "Encontrar oportunidades" → `prosp = 'loading'` → após 1400ms → `prosp = 'done'`. Em produção, trocar o timeout pela chamada real e manter o mesmo estado de skeleton (inclusive para erro: prever `prosp = 'error'` com mensagem acionável).
- **Recarregar base:** `loading = true` por 1100ms com skeletons; em produção, ligar ao fetch real.
- **Exclusão:** abrir confirmação (do card de exclusão ou da linha expandida) → digitar o nome → confirmar remove o registro e fecha; cancelar não altera nada.
- **Cadastro:** valida no submit; sucesso limpa os campos, mostra badge e insere na base.
- **Animações:** `fade` (.25s ease, `translateY(5px)` → 0) na entrada de cada tela; `skel` (opacidade .25 → .6 → .25, 1.2–1.3s infinite) nos skeletons; `pulse` (`box-shadow` 0 → 7px, 2.2s infinite) nos indicadores de worker; `trace` (4s linear infinite) na linha de circuito do painel de prospecção; `transition: transform .15s` no caret.
- **Estados de foco:** `:focus-visible { outline: 1px solid #7DD3FC; outline-offset: 2px }` — nunca o anel azul padrão do navegador.
- **Scrollbars:** thumb `#2D333D` arredondado, track `#0A0A0F`, 10px.
- **Responsivo:** desktop-first (uso interno). As grids de KPI/cards já usam `auto-fit`; a tabela rola horizontalmente com a coluna Empresa fixa. Abaixo de ~1100px, empilhar os grids `1.25fr 1fr` e `1fr 1fr` em coluna única e considerar colapsar a sidebar em ícones.

## State Management

| Estado | Tipo | Papel |
| --- | --- | --- |
| `screen` | enum | dashboard · pipeline · prospeccao · clientes · automacao · nova |
| `rowsData` | array | base de empresas (fonte da verdade de KPIs, funil, kanban e tabela) |
| `expanded` | id \| null | linha aberta na tabela |
| `query`, `nicho`, `status` | string | filtros da base |
| `dense` | boolean | densidade da tabela (`--row-pad`) |
| `loading` | boolean | skeleton da tabela |
| `prosp` | enum | idle · loading · done (adicionar `error` em produção) |
| `deleteId`, `campaignId` | id | seleção nos cards de gestão |
| `confirmKind` | null \| 'lead' \| 'campanha' | modal aberto e seu tipo |
| `typed` | string | texto da confirmação digitada |
| `empresa`, `nichoNovo`, `touched`, `saved` | — | formulário de cadastro |

**Data fetching (produção):** listar empresas com paginação server-side (a base real tem 47+ e cresce), agregações de KPI/funil/ICP idealmente calculadas no backend, consulta CNPJ via BrasilAPI, execução de campanha e histórico via worker. Todo endpoint precisa de estado de carregamento e de erro — o protótipo cobre carregando e vazio; **erro é o que falta especificar e deve ser implementado no mesmo padrão visual** (borda e texto em `#ff8f8f`, mensagem acionável).

## Design Tokens

**Cores (manual da marca Scorpions):**

| Token | Hex | Uso |
| --- | --- | --- |
| Fundo base | `#0A0A0F` | body, sidebar, inputs |
| Fundo painel | `#111319` | cards, tabelas, modal |
| Fundo painel escuro | `#0C0E13` | cabeçalho de tabela, linha expandida, cards internos |
| Linha / borda | `#1E232B` | bordas de card e divisores |
| Linha forte | `#2D333D` | bordas de input, régua sob o header da tabela |
| Azul primário | `#0D6EFD` → `#2568FF` | gradiente dos botões primários e barras |
| Ciano accent | `#7DD3FC` | destaques, valores, estados ativos, chips |
| Texto | `#F5F7FA` | texto principal |
| Texto secundário | `#C3CBD8` | células de tabela, corpo do modal |
| Texto muted | `#8A94A6` | labels, notas |
| Texto fraco | `#5A6373` | metadados, placeholders |
| Alerta | `#ff8f8f` / `#ffb3b3` | erros, exclusão, conversão abaixo da meta |

Tintas derivadas: `rgba(37,104,255,.06 / .07 / .10 / .12 / .16 / .18)` para fundos accent; `rgba(125,211,252,.18 / .22 / .28 / .30 / .35 / .45)` para bordas accent; `rgba(255,107,107,.08 / .12 / .16 / .20 / .35 / .40 / .50 / .55)` para o vermelho.

**Tipografia:** Orbitron (500–800) em títulos, números-destaque e valores tabulares; Inter (300–700) no corpo. Escala usada: 25px Orbitron (KPI) · 19px Orbitron (título de tela) · 14px Orbitron (título de modal/seção) · 13px/12px Orbitron uppercase (títulos de card) · 13.5px (nome de item) · 12.5px (corpo, células, inputs) · 11.5px (notas, botões pequenos) · 10.5px (labels uppercase) · 9.5px (labels de tabela, kickers). Letter-spacing: `.06em` a `.24em` em tudo que é uppercase; títulos Orbitron entre `.06em` e `.14em`.

**Espaçamento:** 3 · 5 · 6 · 8 · 9 · 11 · 12 · 13 · 14 · 16 · 18 · 20 · 22 · 26 · 48px. Padding padrão de card 15–18px; gutter de página 26px; gap de grid 11–14px.

**Border radius:** 3px (inputs, chips, cards internos) · 4px (cards, botões, painéis) · 8px (só o logo) · 999px (pills e trilhas de barra). Nada mais arredondado que isso.

**Sombras / glow:** `0 0 22px rgba(37,104,255,.25–.28)` no botão primário; `0 0 20px rgba(37,104,255,.12)` no hover do card do kanban; `0 0 10px rgba(37,104,255,.5)` na barra do funil; `0 24px 60px rgba(0,0,0,.6)` no modal.

**Score badge:** Orbitron 11px, `padding: 2px 8px`, `border-radius: 3px`. ≥ 85: `color: #7DD3FC`, borda `rgba(125,211,252,.35)`, fundo `rgba(37,104,255,.12)`. 70–84: `color: #C3CBD8`, borda `#2D333D`, fundo `#0A0A0F`. < 70: `color: #8A94A6`, borda `#2D333D`, fundo `#0A0A0F`.

**Chip:** 10px, `letter-spacing: .06em`, `padding: 2px 8px`, `border-radius: 3px`, `white-space: nowrap`. Accent: texto `#7DD3FC`, borda `rgba(125,211,252,.28)`, fundo `rgba(37,104,255,.1)`. Neutro: texto `#8A94A6`, borda `#2D333D`, fundo transparente. Alerta: texto `#ff8f8f`, borda `rgba(255,107,107,.35)`, fundo `rgba(255,107,107,.08)`.

**Input:** `background: #0A0A0F`, borda `1px solid #2D333D`, `border-radius: 3px`, `padding: 9px 11px`, `font-size: 12.5px`, `outline: none`, placeholder `#5A6373`. Estado de erro: borda `rgba(255,107,107,.55)`.

## Assets

- **Logo:** `uploads/logo 1.jpeg` (marca "S" em circuito) é referenciado na sidebar como `uploads/logo%201.jpeg`, recortado com `object-fit: cover; object-position: center 34%` e clareado com `filter: brightness(1.35) contrast(1.1)`. **Em produção, substituir por SVG do manual da marca** — o JPEG é placeholder de layout. O manual também traz a versão símbolo, horizontal e monocromática, além do escorpião completo.
- **Fontes:** Orbitron e Inter via Google Fonts (`Orbitron:wght@500;600;700;800`, `Inter:wght@300;400;500;600;700`). No codebase, preferir self-host.
- **Ícones:** o protótipo usa glifos Unicode (`◱ ▤ ⌖ ▦ ⚡ ＋ ⌕ ›`) como placeholder. **Substituir pelo set de ícones de linha do manual da marca** (ou Lucide, que combina com o traço técnico da identidade).
- **Dados:** 8 empresas de exemplo derivadas dos prints do app atual (Ecta, IDEA, Green, Penso, Empresa de Tecnologia da Informação, São Paulo Tech School, Cardoso, ITM). Valores de proposta, decisores, CNPJs e datas são fictícios, criados para exercitar os estados da UI.

## Files

- `Scorpions CRM v2.dc.html` — protótipo completo das 6 telas, interativo (troca de tela, filtros, densidade, expandir linha, mover negócio, skeletons, validação, confirmação de exclusão). Abre direto no navegador; a marcação de cada tela está em blocos condicionais e o comportamento na classe de lógica ao final do arquivo.
- Existe também no projeto uma **v1** em direção visual alternativa (clara, tipográfica) — não faz parte deste handoff.
