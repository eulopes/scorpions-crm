"""CRM de prospecção e mapeamento de clientes em um único arquivo.

Execução:
    pip install -r requirements.txt
    streamlit run app.py 

Fontes reais:
    Bacen: instituições financeiras em funcionamento, sem chave.
    CVM: corretoras em funcionamento, via BrasilAPI e sem chave.
    B3: empresas listadas, enriquecidas por CNPJ via BrasilAPI e sem chave.
    OpenStreetMap: descoberta local sem chave; cadastro ativo não verificado.
    Google Places: opcional; exige GOOGLE_PLACES_API_KEY.
"""

from __future__ import annotations

import os
import ipaddress
import json
import logging
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import threading

import bcrypt
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

from crm_strategy import (
    enriquecer_lead_icp,
    mapear_status_funil,
    PERFIS_ICP,
    STATUS_FUNIL,
)
from access_control import (
    NIVEIS,
    ORDEM_NIVEIS,
    niveis_administraveis_por,
    nivel_valido,
    paginas_visiveis,
    pode,
    rotulo_nivel,
)
from niche_sources import (
    resolver_fontes_reais,
    roteamento_por_nicho,
)

from automation import (
    FONTE_AUTOMATICA,
    FONTE_MOTOR_CONTINUO,
    FONTE_OSM,
    FONTES_AUTOMACAO,
    LIMIAR_QUALIFICACAO,
    alternar_campanha,
    buscar_bacen_instituicoes,
    buscar_leads_automaticamente,
    buscar_leads_por_fonte,
    criar_campanha,
    executar_campanha,
    excluir_campanha,
    FONTE_B3,
    FONTE_CVM,
    iniciar_banco_automacao,
    listar_campanhas,
    listar_execucoes,
    listar_atividades,
    registrar_atividade,
    criar_alvo_continuo,
    listar_alvos_continuos,
    alternar_alvo_continuo,
    excluir_alvo_continuo,
    salvar_leads_no_banco,
    status_worker,
    ler_config,
    salvar_config,
    telefone_suprimido,
    adicionar_supressao,
    remover_supressao,
    listar_supressao,
    registrar_mensagem_enviada,
    leads_ja_contatados_ha_dias,
    gerar_link_whatsapp,
)
from opportunity_engine import (
    NIVEIS_OPORTUNIDADE,
    get_opportunity_timeline,
    recommend_next_action,
)
from sales_signals import listar_signals_ativos


APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("CRM_DB_PATH", str(APP_DIR / "scorpions_base.db")))
GOOGLE_PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
BRASIL_API_CNPJ_URL = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
PADRAO_CNPJ = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")
LIMITE_PAGINA_BYTES = 5_000_000
STATUS = STATUS_FUNIL
FONTES_AUTOMACAO_UI = tuple(dict.fromkeys(FONTES_AUTOMACAO))
# "Motor Contínuo (todas as fontes)" só é uma fonte válida para campanhas
# agendadas (usa os alvos cadastrados) -- buscar_leads_por_fonte não sabe
# tratá-la, então ela não pode aparecer no seletor de busca manual.
FONTES_PROSPECCAO_MANUAL = tuple(f for f in FONTES_AUTOMACAO_UI if f != FONTE_MOTOR_CONTINUO)

# Rótulos compartilhados para as tabelas de resultado de prospecção (busca
# automática e extração por URL) — sem isso o st.dataframe mostra os nomes
# crus das colunas (nome_empresa, segmento_icp...) em vez de rótulos legíveis.
COLUNAS_LEAD_LABELS: dict[str, Any] = {
    "nome_empresa": "Empresa",
    "razao_social": "Razão social",
    "cnpj": "CNPJ",
    "decisor": "Decisor",
    "nicho": "Nicho",
    "endereco": "Endereço",
    "cidade": "Cidade",
    "telefone": "Telefone",
    "site": "Site",
    "email": "E-mail",
    "status": "Etapa",
    "status_receita": "Situação (Receita)",
    "origem": "Origem",
    "observacoes": "Observações",
    "pontuacao": "Score",
    "motivo_qualificacao": "Motivo da qualificação",
    "segmento_icp": "Segmento ICP",
    "servicos_recomendados": "Serviços recomendados",
}


def configurar_google_places() -> bool:
    """Carrega a chave do Google Places sem expô-la na interface ou nos logs."""
    chave = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if not chave:
        try:
            chave = str(st.secrets.get("GOOGLE_PLACES_API_KEY", "")).strip()
        except (FileNotFoundError, KeyError, AttributeError):
            chave = ""
    if chave.casefold() in {"cole_sua_chave_aqui", "sua_nova_chave", "your_api_key_here"}:
        chave = ""
    if chave:
        os.environ["GOOGLE_PLACES_API_KEY"] = chave
    return bool(chave)


MAX_TENTATIVAS_LOGIN = 5
BLOQUEIO_LOGIN_SEGUNDOS = 60
SENHA_TAMANHO_MINIMO = 8
# "123" pedido não passa no mínimo de 8 caracteres (SENHA_TAMANHO_MINIMO) --
# provisória com o mesmo espírito (fácil de digitar, óbvia que precisa trocar).
SENHA_PROVISORIA_INICIAL = "trocar123"


@st.cache_resource
def _estado_login() -> dict[str, Any]:
    """Estado de bloqueio de login por IP: uma unica instancia por processo
    (via st.cache_resource, nao por sessao/aba), guardando tentativas por
    endereco IP. Isso evita duas falhas: (1) abrir uma aba nova reiniciar a
    contagem de um atacante, e (2) um estranho travar TODO MUNDO (inclusive o
    admin de verdade) so de errar a senha propositalmente 5 vezes de qualquer
    lugar - com o bloqueio por IP, ele so tranca a propria conexao dele.

    st.context.ip_address pode ser falsificado (nao e 100% confiavel como unica
    defesa), mas ja resolve o cenario mais comum de abuso."""
    return {"por_ip": {}, "lock": threading.Lock()}


def _chave_ip() -> str:
    ip = st.context.ip_address
    return ip if ip else "local"


@st.cache_resource
def _estado_rate_limit() -> dict[str, Any]:
    """Mesmo padrão do bloqueio de login (cache_resource compartilhado entre
    sessões, não por aba): limita quantas vezes cada usuário pode disparar
    uma ação cara (consulta externa, scraping) numa janela de tempo — sem
    isso, qualquer usuário autenticado pode martelar o botão e estourar cota
    de API de terceiro ou usar o servidor como proxy de scraping."""
    return {"por_chave": {}, "lock": threading.Lock()}


def limite_de_taxa_excedido(acao: str, limite: int = 10, janela_segundos: int = 60) -> bool:
    """True se o usuário logado já bateu o limite de `acao` na janela — quem
    chamar deve mostrar um aviso e não prosseguir com a chamada externa."""
    chave = f"{st.session_state.get('usuario_id', 'anonimo')}:{acao}"
    estado = _estado_rate_limit()
    agora = time.time()
    with estado["lock"]:
        historico = [t for t in estado["por_chave"].get(chave, []) if agora - t < janela_segundos]
        excedeu = len(historico) >= limite
        if not excedeu:
            historico.append(agora)
        estado["por_chave"][chave] = historico
    return excedeu


@st.cache_resource
def _hash_fantasma() -> str:
    """Hash bcrypt calculado uma unica vez (operacao lenta de proposito) para
    ser usado quando o usuario informado nao existe, mantendo o tempo de
    resposta igual ao de uma senha errada de um usuario real."""
    return bcrypt.hashpw(b"usuario-inexistente", bcrypt.gensalt()).decode("utf-8")


def _usuarios_autenticacao() -> dict[str, str]:
    """Le os usuarios/hashes de duas formas possiveis, para funcionar tanto no
    Streamlit Community Cloud (secrets.toml) quanto em hosts que usam variavel
    de ambiente pura (Railway, Render, VPS): AUTH_USERS_JSON='{"admin": "hash"}'."""
    variavel_ambiente = os.getenv("AUTH_USERS_JSON", "").strip()
    if variavel_ambiente:
        try:
            usuarios = json.loads(variavel_ambiente)
            if isinstance(usuarios, dict):
                return {str(k): str(v) for k, v in usuarios.items()}
        except json.JSONDecodeError:
            pass
    try:
        return dict(st.secrets.get("AUTH_USERS", {}))
    except (FileNotFoundError, AttributeError):
        return {}


@st.cache_resource
def carregar_tema_css() -> str:
    """Lê theme.css uma única vez por processo. Editar esse arquivo e reiniciar
    o Streamlit (ou limpar o cache) já reflete no visual, sem tocar em app.py."""
    return (APP_DIR / "theme.css").read_text(encoding="utf-8")


@st.cache_resource
def _iniciar_worker_em_background() -> int:
    """Sobe worker.py como subprocesso, uma única vez por processo do Streamlit.

    Hospedagens como o Railway só rodam um comando por serviço e não repassam
    processos em segundo plano iniciados via '&' no Procfile — por isso o worker
    precisa ser filho do próprio processo Python do app, não do shell que o inicia.
    """
    processo = subprocess.Popen([sys.executable, str(APP_DIR / "worker.py")])
    return processo.pid


def render_page_header(
    titulo: str,
    subtitulo: str,
    *,
    badges: list[str] | None = None,
    status: tuple[str, str] | None = None,
    acoes: list[tuple[str, str, bool]] | None = None,
) -> str | None:
    """Cabeçalho compacto e padronizado por página: título/subtítulo à
    esquerda, badges de status e no máximo 1-2 ações contextuais à direita.

    `acoes` é uma lista de (rótulo, key, é_primário). Devolve o rótulo do
    botão clicado (ou None) para o chamador decidir o que fazer — navegar,
    abrir um diálogo etc. Nunca repete as mesmas ações em todas as páginas.
    """
    clicado: str | None = None
    badges_html = "".join(
        f'<span class="context-pill">{escape(badge)}</span>' for badge in (badges or [])
    )
    status_html = ""
    if status:
        rotulo_status, estado_status = status
        estado_seguro = estado_status if estado_status in {"online", "offline", "warning", "error"} else "neutral"
        status_html = (
            f'<span class="status-pill status-{estado_seguro}">{escape(rotulo_status)}</span>'
        )

    with st.container(
        key="page_header",
        horizontal=True,
        wrap=True,
        horizontal_alignment="distribute",
        vertical_alignment="center",
        gap="small",
    ):
        with st.container(key="page_header_copy", width="stretch", gap=None):
            st.markdown(
                f'<h1>{escape(titulo)}</h1><p class="page-header-sub">{escape(subtitulo)}</p>',
                unsafe_allow_html=True,
            )
        if badges_html or status_html or acoes:
            with st.container(
                key="page_header_context",
                width="content",
                horizontal=True,
                wrap=True,
                horizontal_alignment="right",
                vertical_alignment="center",
                gap="small",
            ):
                if badges_html or status_html:
                    st.markdown(
                        f'<div class="page-header-badges">{badges_html}{status_html}</div>',
                        unsafe_allow_html=True,
                    )
                for rotulo, key, primario in acoes or []:
                    if st.button(
                        rotulo,
                        key=key,
                        type="primary" if primario else "secondary",
                        width="content",
                    ):
                        clicado = rotulo
    return clicado


def render_empty_state(
    titulo: str,
    descricao: str,
    *,
    icone: str | None = None,
    acao_primaria: tuple[str, str] | None = None,
    acao_secundaria: tuple[str, str] | None = None,
    compacto: bool = False,
) -> str | None:
    """Estado vazio padronizado (ícone opcional, título, descrição, até duas
    ações). `acao_primaria`/`acao_secundaria` são (rótulo, key). Devolve o
    rótulo do botão clicado, se houver."""
    clicado: str | None = None
    classe_compacta = " empty-state--compact" if compacto else ""
    st.markdown(
        f"""
        <div class="empty-state{classe_compacta}">
          {f'<div class="empty-icon">{escape(icone)}</div>' if icone else ''}
          <div class="empty-title">{escape(titulo)}</div>
          <p class="empty-desc">{escape(descricao)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    acoes = [a for a in (acao_primaria, acao_secundaria) if a]
    if acoes:
        chave_acoes = f"empty_actions_{acoes[0][1]}"
        with st.container(
            key=chave_acoes,
            horizontal=True,
            wrap=True,
            horizontal_alignment="center",
            vertical_alignment="center",
        ):
            for indice, (rotulo, key) in enumerate(acoes):
                if st.button(
                    rotulo,
                    key=key,
                    type="primary" if indice == 0 else "secondary",
                    width="content",
                ):
                    clicado = rotulo
    return clicado


def _sugestao_icp(chave_widget: str) -> None:
    with st.expander(
        "Sugerir nicho por perfil de cliente (ICP)",
        icon=":material/lightbulb:",
        type="compact",
    ):
        perfil_escolhido = st.selectbox(
            "Selecione um perfil estratégico",
            options=list(PERFIS_ICP.keys()),
            index=None,
            placeholder="Escolha um perfil...",
            key=chave_widget,
        )
        if perfil_escolhido:
            st.info(f"**Sugestão de busca para o nicho:** `{PERFIS_ICP[perfil_escolhido]['consulta_sugerida']}`")


@st.dialog("Nova campanha")
def _dialog_nova_campanha() -> None:
    with st.form("nova_campanha_dialog", clear_on_submit=True):
        c1, c2 = st.columns(2)
        nome_campanha = c1.text_input("Nome da campanha", placeholder="Clínicas de Campinas")
        fonte_campanha = c2.selectbox("Fonte", FONTES_AUTOMACAO_UI)
        nicho_campanha = c1.text_input(
            "Nicho ou segmento", placeholder="Ex.: Todos ou cooperativa de crédito", max_chars=120
        )
        local_campanha = c2.text_input("Município, UF", placeholder="Campinas, SP", max_chars=120)
        limite_campanha = c1.number_input("Leads por execução", min_value=1, max_value=100, value=8)
        horario_campanha = c2.time_input("Horário diário", value=datetime.strptime("08:00", "%H:%M").time())
        _sugestao_icp("sugestao_campanha_dialog")
        ativa_campanha = st.checkbox("Ativar imediatamente", value=True)
        criar = st.form_submit_button("Criar campanha", type="primary", width="stretch")
    if criar:
        try:
            campanha_id = criar_campanha(
                nome_campanha, nicho_campanha, local_campanha, fonte_campanha,
                int(limite_campanha), horario_campanha.strftime("%H:%M"), ativa_campanha,
            )
            st.session_state["aviso_automacao"] = f"Campanha #{campanha_id} criada."
            st.toast("Campanha criada.", icon=":material/check_circle:")
            st.rerun()
        except ValueError as erro:
            st.error(str(erro))


@st.dialog("Novo alvo contínuo")
def _dialog_novo_alvo() -> None:
    with st.form("novo_alvo_continuo_dialog", clear_on_submit=True):
        c1, c2 = st.columns(2)
        nome_alvo = c1.text_input("Nome do alvo", placeholder="Clínicas de Campinas")
        nicho_alvo = c1.text_input("Nicho ou segmento", placeholder="Ex.: cooperativa de crédito", max_chars=120)
        local_alvo = c2.text_input("Município, UF", placeholder="Campinas, SP", max_chars=120)
        _sugestao_icp("sugestao_alvo_dialog")
        ativo_alvo = st.checkbox("Ativar imediatamente", value=True)
        adicionar_alvo = st.form_submit_button("Adicionar alvo", type="primary", width="stretch")
    if adicionar_alvo:
        try:
            alvo_id = criar_alvo_continuo(nome_alvo, nicho_alvo, local_alvo, ativo_alvo)
            st.session_state["aviso_automacao"] = f"Alvo contínuo #{alvo_id} criado."
            st.rerun()
        except ValueError as erro:
            st.error(str(erro))


@st.dialog("Novo usuário")
def _dialog_novo_usuario() -> None:
    mapa_equipes = {e["id"]: e["nome"] for e in listar_equipes()}
    with st.form("form_novo_usuario_dialog", clear_on_submit=True):
        c1, c2 = st.columns(2)
        novo_username = c1.text_input("Usuário (login)")
        novo_nome = c2.text_input("Nome completo")
        nova_senha = c1.text_input("Senha", type="password")
        novo_email = c2.text_input("E-mail (opcional)")
        novo_nivel = c1.selectbox("Nível", ORDEM_NIVEIS, format_func=rotulo_nivel)
        opcoes_equipe_criacao = {"Sem equipe": None, **{v: k for k, v in mapa_equipes.items()}}
        nova_equipe_rotulo = c2.selectbox("Equipe", list(opcoes_equipe_criacao))
        criar_usuario_clicado = st.form_submit_button("Criar usuário", type="primary", width="stretch")
    if criar_usuario_clicado:
        try:
            criar_usuario(
                novo_username, nova_senha, novo_nome, novo_nivel,
                opcoes_equipe_criacao[nova_equipe_rotulo], novo_email,
            )
            st.session_state["aviso_equipe"] = f"Usuário '{novo_username}' criado."
            st.toast(f"Usuário '{novo_username}' criado.", icon=":material/check_circle:")
            st.rerun()
        except ValueError as erro:
            st.warning(str(erro))
        except sqlite3.IntegrityError:
            st.warning("Esse nome de usuário já existe.")


@st.dialog("Nova equipe")
def _dialog_nova_equipe() -> None:
    with st.form("form_nova_equipe_dialog", clear_on_submit=True):
        nome_equipe = st.text_input("Nome da equipe", placeholder="Equipe Comercial SP")
        criar_equipe_clicado = st.form_submit_button("Criar equipe", type="primary", width="stretch")
    if criar_equipe_clicado:
        if nome_equipe.strip():
            try:
                criar_equipe(nome_equipe)
                st.rerun()
            except sqlite3.IntegrityError:
                st.warning("Já existe uma equipe com esse nome.")
        else:
            st.warning("Informe um nome para a equipe.")


def exigir_login() -> None:
    """Bloqueia o restante do app até o usuário autenticar com usuário/senha.
    O CSS (tema + estilo do card de login) já foi injetado mais acima, antes
    desta função ser chamada — ver carregar_tema_css() e theme.css."""
    if st.session_state.get("autenticado"):
        return

    agora = time.time()
    estado = _estado_login()
    chave_ip = _chave_ip()
    bloqueio_ip = estado["por_ip"].get(chave_ip, {"tentativas": 0, "bloqueado_ate": 0.0})

    with st.container(key="login_card"):
        st.markdown('<div class="login-eyebrow">Acesso restrito</div>', unsafe_allow_html=True)
        st.markdown('<div class="login-title">SCORPIONS</div>', unsafe_allow_html=True)
        st.markdown('<div class="login-sub">Soluções tecnológicas · CRM</div>', unsafe_allow_html=True)

        if agora < bloqueio_ip["bloqueado_ate"]:
            restante = int(bloqueio_ip["bloqueado_ate"] - agora)
            st.error(f"Muitas tentativas incorretas a partir desta conexão. Tente novamente em {restante}s.")
        else:
            with st.form("form_login", border=False):
                usuario = st.text_input("Usuário", key="login_usuario")
                senha = st.text_input("Senha", type="password", key="login_senha")
                enviado = st.form_submit_button("Entrar", key="login_entrar", width="stretch")

            if enviado:
                with st.spinner("Entrando..."):
                    # bcrypt.checkpw roda sempre (mesmo pra usuario inexistente) para nao
                    # vazar, por tempo de resposta, quais usuarios existem de verdade.
                    registro = buscar_usuario_por_username(usuario)
                    hash_guardado = registro["senha_hash"] if registro else _hash_fantasma()
                    senha_confere = bcrypt.checkpw(senha.encode("utf-8"), hash_guardado.encode("utf-8"))
                valido = registro is not None and senha_confere
                if valido and registro["status"] != "ativo":
                    registrar_evento_login(usuario, sucesso=False, ip=chave_ip)
                    st.error("Esta conta está desativada. Fale com seu gestor ou diretor.")
                elif valido:
                    registrar_evento_login(usuario, sucesso=True, ip=chave_ip)
                    st.session_state.autenticado = True
                    st.session_state.usuario_logado = registro["username"]
                    st.session_state.usuario_id = registro["id"]
                    st.session_state.nivel_usuario = registro["nivel"]
                    st.session_state.equipe_id_usuario = registro["equipe_id"]
                    st.session_state.nome_usuario = registro["nome"]
                    with estado["lock"]:
                        estado["por_ip"].pop(chave_ip, None)
                    st.rerun()
                else:
                    registrar_evento_login(usuario, sucesso=False, ip=chave_ip)
                    with estado["lock"]:
                        bloqueio_ip["tentativas"] += 1
                        if bloqueio_ip["tentativas"] >= MAX_TENTATIVAS_LOGIN:
                            bloqueio_ip["bloqueado_ate"] = agora + BLOQUEIO_LOGIN_SEGUNDOS
                            bloqueio_ip["tentativas"] = 0
                        estado["por_ip"][chave_ip] = bloqueio_ip
                    st.error("Usuário ou senha inválidos.")

    st.stop()


@st.cache_resource
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def conectar():
    # 1. check_same_thread=False libera o uso entre as threads do Streamlit
    # 2. timeout=30.0 evita erros de lock se o worker.py tentar gravar junto
    conexao = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
    conexao.row_factory = sqlite3.Row
    try:
        yield conexao
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()  # Fecha a conexão ao finalizar a instrução 'with'


def iniciar_banco() -> None:
    with conectar() as conexao:
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                place_id TEXT,
                cnpj TEXT,
                nome_empresa TEXT NOT NULL,
                razao_social TEXT,
                decisor TEXT,
                nicho TEXT NOT NULL,
                endereco TEXT,
                cidade TEXT,
                telefone TEXT,
                site TEXT,
                email TEXT,
                status TEXT NOT NULL DEFAULT 'Novos Leads',
                status_receita TEXT,
                origem TEXT NOT NULL,
                observacoes TEXT,
                pontuacao INTEGER,
                motivo_qualificacao TEXT,
                segmento_icp TEXT,
                servicos_recomendados TEXT,
                valor_proposta REAL,
                proximo_contato TEXT,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL
            )
            """
        )
        # Migra bancos criados por versões anteriores sem apagar dados.
        colunas_existentes = {
            linha["name"] for linha in conexao.execute("PRAGMA table_info(leads)").fetchall()
        }
        novas_colunas = {
            "cnpj": "TEXT",
            "razao_social": "TEXT",
            "decisor": "TEXT",
            "status_receita": "TEXT",
            "pontuacao": "INTEGER",
            "motivo_qualificacao": "TEXT",
            "segmento_icp": "TEXT",
            "servicos_recomendados": "TEXT",
            "valor_proposta": "REAL",
            "alerta_vencido_em": "TEXT",
            "proximo_contato": "TEXT",
            "responsavel_usuario_id": "INTEGER",
            "fit_score": "INTEGER",
            "intent_score": "INTEGER",
            "timing_score": "INTEGER",
            "data_confidence_score": "INTEGER",
            "opportunity_score": "INTEGER",
            "opportunity_level": "TEXT",
            "opportunity_reason": "TEXT",
            "why_now": "TEXT",
            "opportunity_updated_at": "TEXT",
            "opportunity_delta": "INTEGER",
            "last_signal_at": "TEXT",
            "next_intelligence_refresh_at": "TEXT",
        }
        for coluna, tipo in novas_colunas.items():
            if coluna not in colunas_existentes:
                conexao.execute(f"ALTER TABLE leads ADD COLUMN {coluna} {tipo}")

        if "status" in colunas_existentes:
            leads_com_status_antigo = conexao.execute(
                "SELECT id, status FROM leads WHERE status NOT IN ({})".format(
                    ",".join("?" for _ in STATUS_FUNIL)
                ),
                STATUS_FUNIL,
            ).fetchall()
            for linha in leads_com_status_antigo:
                novo_status = mapear_status_funil(linha["status"])
                if novo_status != linha["status"]:
                    conexao.execute(
                        "UPDATE leads SET status = ? WHERE id = ?", (novo_status, linha["id"])
                    )
        conexao.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_place_id "
            "ON leads(place_id) WHERE place_id IS NOT NULL AND place_id <> ''"
        )
        conexao.execute("DROP INDEX IF EXISTS idx_leads_nome_endereco")
        conexao.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_nome_endereco_sem_id "
            "ON leads(nome_empresa, endereco) "
            "WHERE (cnpj IS NULL OR cnpj = '') "
            "AND (place_id IS NULL OR place_id = '') "
            "AND endereco IS NOT NULL AND endereco <> ''"
        )
        conexao.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_cnpj "
            "ON leads(cnpj) WHERE cnpj IS NOT NULL AND cnpj <> ''"
        )


def iniciar_banco_usuarios() -> None:
    """Cria as tabelas de equipes/usuários e migra o login legado (AUTH_USERS)
    para a tabela `usuarios` como o primeiro diretor, sem exigir nenhuma
    mudança em secrets.toml/variáveis de ambiente já configuradas em produção."""
    with conectar() as conexao:
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS equipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL UNIQUE,
                criado_em TEXT NOT NULL
            )
            """
        )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                senha_hash TEXT NOT NULL,
                nome TEXT NOT NULL,
                email TEXT,
                nivel TEXT NOT NULL,
                equipe_id INTEGER,
                status TEXT NOT NULL DEFAULT 'ativo',
                criado_em TEXT NOT NULL
            )
            """
        )
        # Semeia, de forma idempotente, cada usuário já configurado em
        # AUTH_USERS/secrets.toml como diretor — é o mesmo login que já dá
        # acesso hoje, então ninguém fica trancado para fora ao atualizar.
        usuarios_legados = _usuarios_autenticacao()
        agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for username, hash_bcrypt in usuarios_legados.items():
            existe = conexao.execute(
                "SELECT 1 FROM usuarios WHERE username = ?", (username,)
            ).fetchone()
            if not existe:
                conexao.execute(
                    """
                    INSERT INTO usuarios (username, senha_hash, nome, nivel, status, criado_em)
                    VALUES (?, ?, ?, 'diretor', 'ativo', ?)
                    """,
                    (username, hash_bcrypt, username.capitalize(), agora),
                )

        # Contas iniciais pedidas para o time (diretor, acesso total) --
        # senha provisória, trocada pelo próprio usuário em "Trocar senha"
        # na barra lateral assim que ele logar pela primeira vez.
        for username, nome in (
            ("lopes", "Lopes"), ("moroni", "Moroni"), ("junior", "Junior"),
        ):
            existe = conexao.execute(
                "SELECT 1 FROM usuarios WHERE username = ?", (username,)
            ).fetchone()
            if not existe:
                senha_provisoria_hash = bcrypt.hashpw(
                    SENHA_PROVISORIA_INICIAL.encode("utf-8"), bcrypt.gensalt()
                ).decode("utf-8")
                conexao.execute(
                    """
                    INSERT INTO usuarios (username, senha_hash, nome, nivel, status, criado_em)
                    VALUES (?, ?, ?, 'diretor', 'ativo', ?)
                    """,
                    (username, senha_provisoria_hash, nome, agora),
                )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS eventos_login (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                sucesso INTEGER NOT NULL,
                ip TEXT,
                criado_em TEXT NOT NULL
            )
            """
        )
        conexao.execute(
            "CREATE INDEX IF NOT EXISTS idx_eventos_login_criado_em ON eventos_login(criado_em DESC)"
        )


def registrar_evento_login(username: str, sucesso: bool, ip: str) -> None:
    """Trilha de auditoria de login — nunca grava a senha, só o resultado.
    Persiste em banco (ao contrário do contador de bloqueio, que é só em
    memória e reseta a cada deploy) para permitir investigação forense de
    tentativas de acesso depois do fato.

    Nunca deixa uma falha aqui derrubar o login em si: auditoria é
    coadjuvante, não pode virar um novo jeito de travar o acesso de todo
    mundo por causa de um bug de logging."""
    try:
        agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with conectar() as conexao:
            conexao.execute(
                "INSERT INTO eventos_login (username, sucesso, ip, criado_em) VALUES (?, ?, ?, ?)",
                (str(username).strip()[:100], int(sucesso), str(ip)[:100] if ip else None, agora),
            )
    except Exception:
        logging.getLogger(__name__).exception("Falha ao registrar evento de login (auditoria)")


def listar_eventos_login(limite: int = 50) -> list[dict[str, Any]]:
    with conectar() as conexao:
        linhas = conexao.execute(
            "SELECT * FROM eventos_login ORDER BY criado_em DESC LIMIT ?",
            (max(1, min(int(limite), 500)),),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def criar_equipe(nome: str) -> int:
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conectar() as conexao:
        cursor = conexao.execute(
            "INSERT INTO equipes (nome, criado_em) VALUES (?, ?)", (nome.strip(), agora)
        )
        return int(cursor.lastrowid)


def listar_equipes() -> list[dict[str, Any]]:
    with conectar() as conexao:
        linhas = conexao.execute("SELECT * FROM equipes ORDER BY nome").fetchall()
    return [dict(linha) for linha in linhas]


def criar_usuario(
    username: str, senha: str, nome: str, nivel: str, equipe_id: int | None, email: str = ""
) -> int:
    if not pode(st.session_state.get("nivel_usuario"), "pode_criar_usuario"):
        raise PermissionError("Sem permissão para criar usuários.")
    if not username.strip() or not senha or not nome.strip():
        raise ValueError("Usuário, senha e nome são obrigatórios.")
    if len(senha) < SENHA_TAMANHO_MINIMO:
        raise ValueError(f"A senha precisa ter pelo menos {SENHA_TAMANHO_MINIMO} caracteres.")
    if not nivel_valido(nivel):
        raise ValueError(f"Nível inválido: {nivel}")
    senha_hash = bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conectar() as conexao:
        cursor = conexao.execute(
            """
            INSERT INTO usuarios (username, senha_hash, nome, email, nivel, equipe_id, status, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, 'ativo', ?)
            """,
            (username.strip(), senha_hash, nome.strip(), email.strip(), nivel, equipe_id, agora),
        )
        return int(cursor.lastrowid)


def trocar_propria_senha(usuario_id: int, senha_atual: str, senha_nova: str) -> None:
    """Autoatendimento -- qualquer usuário logado troca a própria senha, sem
    depender do diretor. Confere a senha atual antes de aceitar a nova."""
    with conectar() as conexao:
        linha = conexao.execute(
            "SELECT senha_hash FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()
        if not linha or not bcrypt.checkpw(senha_atual.encode("utf-8"), linha["senha_hash"].encode("utf-8")):
            raise ValueError("Senha atual incorreta.")
        if len(senha_nova) < SENHA_TAMANHO_MINIMO:
            raise ValueError(f"A nova senha precisa ter pelo menos {SENHA_TAMANHO_MINIMO} caracteres.")
        novo_hash = bcrypt.hashpw(senha_nova.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        conexao.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?", (novo_hash, usuario_id))


def buscar_usuario_por_username(username: str) -> dict[str, Any] | None:
    with conectar() as conexao:
        linha = conexao.execute(
            "SELECT * FROM usuarios WHERE username = ?", (username,)
        ).fetchone()
    return dict(linha) if linha else None


def listar_usuarios(equipe_id: int | None = None, niveis: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT usuarios.*, equipes.nome AS equipe_nome FROM usuarios "
        "LEFT JOIN equipes ON equipes.id = usuarios.equipe_id WHERE 1=1"
    )
    parametros: list[Any] = []
    if equipe_id is not None:
        sql += " AND usuarios.equipe_id = ?"
        parametros.append(equipe_id)
    if niveis:
        sql += " AND usuarios.nivel IN ({})".format(",".join("?" for _ in niveis))
        parametros.extend(niveis)
    sql += " ORDER BY usuarios.nome"
    with conectar() as conexao:
        linhas = conexao.execute(sql, parametros).fetchall()
    return [dict(linha) for linha in linhas]


def contar_leads_por_responsavel() -> dict[int, int]:
    """Quantas empresas cada usuário carrega na carteira (para a tela Equipe)."""
    with conectar() as conexao:
        linhas = conexao.execute(
            "SELECT responsavel_usuario_id, COUNT(*) AS total FROM leads "
            "WHERE responsavel_usuario_id IS NOT NULL GROUP BY responsavel_usuario_id"
        ).fetchall()
    return {int(linha["responsavel_usuario_id"]): int(linha["total"]) for linha in linhas}


def contar_leads_por_equipe() -> dict[int, int]:
    """Quantas empresas a equipe carrega no total (soma da carteira dos membros)."""
    with conectar() as conexao:
        linhas = conexao.execute(
            "SELECT usuarios.equipe_id AS equipe_id, COUNT(leads.id) AS total FROM leads "
            "JOIN usuarios ON usuarios.id = leads.responsavel_usuario_id "
            "WHERE usuarios.equipe_id IS NOT NULL GROUP BY usuarios.equipe_id"
        ).fetchall()
    return {int(linha["equipe_id"]): int(linha["total"]) for linha in linhas}


def alternar_status_usuario(usuario_id: int) -> str:
    if usuario_id == st.session_state.get("usuario_id"):
        raise PermissionError("Não é possível alternar o próprio status.")
    nivel_atual = st.session_state.get("nivel_usuario")
    niveis_permitidos = niveis_administraveis_por(nivel_atual)
    with conectar() as conexao:
        linha = conexao.execute(
            "SELECT status, nivel, equipe_id FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()
        if not linha or linha["nivel"] not in niveis_permitidos:
            raise PermissionError("Sem permissão para alternar o status desse usuário.")
        if (
            pode(nivel_atual, "escopo_gestao_usuarios") == "equipe"
            and linha["equipe_id"] != st.session_state.get("equipe_id_usuario")
        ):
            raise PermissionError("Esse usuário está fora da sua equipe.")
        novo_status = "inativo" if linha["status"] == "ativo" else "ativo"
        conexao.execute("UPDATE usuarios SET status = ? WHERE id = ?", (novo_status, usuario_id))
    return novo_status


def atribuir_lead_a_usuario(lead_id: int, usuario_id: int | None) -> None:
    escopo = pode(st.session_state.get("nivel_usuario"), "escopo_atribuicao_carteira")
    if not escopo:
        raise PermissionError("Sem permissão para atribuir carteira.")
    if escopo == "equipe":
        with conectar() as conexao:
            lead_no_escopo = conexao.execute(
                """
                SELECT 1
                FROM leads
                LEFT JOIN usuarios ON usuarios.id = leads.responsavel_usuario_id
                WHERE leads.id = ?
                  AND (leads.responsavel_usuario_id IS NULL OR usuarios.equipe_id = ?)
                """,
                (lead_id, st.session_state.get("equipe_id_usuario")),
            ).fetchone()
        if not lead_no_escopo:
            raise PermissionError("Esse lead está fora da sua equipe.")
    if usuario_id is not None:
        niveis_alvo = ("vendedor", "supervisor") if escopo == "todas" else ("vendedor",)
        with conectar() as conexao:
            alvo = conexao.execute(
                "SELECT nivel, equipe_id FROM usuarios WHERE id = ?", (usuario_id,)
            ).fetchone()
        dentro_da_equipe = escopo == "todas" or (
            alvo and alvo["equipe_id"] == st.session_state.get("equipe_id_usuario")
        )
        if not alvo or alvo["nivel"] not in niveis_alvo or not dentro_da_equipe:
            raise PermissionError("Esse usuário está fora do seu escopo de atribuição.")

    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conectar() as conexao:
        conexao.execute(
            "UPDATE leads SET responsavel_usuario_id = ?, atualizado_em = ? WHERE id = ?",
            (usuario_id, agora, lead_id),
        )
    st.cache_data.clear()


def limpar_cnpj(cnpj: str) -> str:
    """Mantém somente os dígitos informados no CNPJ."""
    return "".join(filter(str.isdigit, str(cnpj)))


def cnpj_valido(cnpj: str) -> bool:
    """Valida tamanho, repetições e os dois dígitos verificadores do CNPJ."""
    numero = limpar_cnpj(cnpj)
    if len(numero) != 14 or numero == numero[0] * 14:
        return False

    def calcular_digito(base: str, pesos: list[int]) -> str:
        resto = sum(int(digito) * peso for digito, peso in zip(base, pesos)) % 11
        return "0" if resto < 2 else str(11 - resto)

    primeiro = calcular_digito(numero[:12], [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    segundo = calcular_digito(numero[:12] + primeiro, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return numero[-2:] == primeiro + segundo


def formatar_cnpj(cnpj: str) -> str:
    numero = limpar_cnpj(cnpj)
    if len(numero) != 14:
        return numero
    return f"{numero[:2]}.{numero[2:5]}.{numero[5:8]}/{numero[8:12]}-{numero[12:]}"


def _juntar_endereco(dados: dict[str, Any]) -> str:
    logradouro = str(dados.get("logradouro") or "").strip()
    numero = str(dados.get("numero") or "").strip()
    complemento = str(dados.get("complemento") or "").strip()
    bairro = str(dados.get("bairro") or "").strip()
    municipio = str(dados.get("municipio") or "").strip()
    uf = str(dados.get("uf") or "").strip()
    cep = str(dados.get("cep") or "").strip()

    rua = ", ".join(parte for parte in [logradouro, numero] if parte)
    if complemento:
        rua = f"{rua} - {complemento}" if rua else complemento
    localidade = ", ".join(parte for parte in [bairro, f"{municipio}/{uf}".strip("/")] if parte)
    endereco = " - ".join(parte for parte in [rua, localidade] if parte)
    return f"{endereco} - CEP {cep}" if cep else endereco


def _extrair_decisor(qsa: Any) -> str:
    """Seleciona um contato societário provável, sem afirmar que é contato comercial."""
    if not isinstance(qsa, list):
        return "Não identificado"

    socios = [socio for socio in qsa if isinstance(socio, dict) and socio.get("nome_socio")]
    if not socios:
        return "Não identificado"

    prioridades = ("presidente", "administrador", "diretor", "titular", "sócio-administrador")
    escolhido = next(
        (
            socio
            for prioridade in prioridades
            for socio in socios
            if prioridade in str(socio.get("qualificacao_socio") or "").lower()
        ),
        socios[0],
    )
    nome = str(escolhido.get("nome_socio") or "").strip().title()
    qualificacao = str(escolhido.get("qualificacao_socio") or "").strip()
    return f"{nome} ({qualificacao})" if qualificacao else nome


def consultar_empresa_brasilapi(cnpj: str) -> dict[str, Any]:
    """Consulta e padroniza os dados cadastrais de uma empresa na BrasilAPI."""
    cnpj_limpo = limpar_cnpj(cnpj)
    if not cnpj_valido(cnpj_limpo):
        return {"erro": "Informe um CNPJ válido com 14 dígitos."}

    try:
        resposta = requests.get(
            BRASIL_API_CNPJ_URL.format(cnpj=cnpj_limpo),
            headers={"Accept": "application/json", "User-Agent": "ScorpionsCRM/1.0"},
            timeout=15,
        )
    except requests.exceptions.RequestException as erro:
        return {"erro": f"Falha na conexão com a BrasilAPI: {erro}"}

    if resposta.status_code == 200:
        try:
            dados = resposta.json()
        except ValueError:
            return {"erro": "A BrasilAPI retornou uma resposta inválida."}

        razao_social = str(dados.get("razao_social") or "").strip()
        nome_fantasia = str(dados.get("nome_fantasia") or "").strip()
        return {
            "place_id": f"brasilapi:{cnpj_limpo}",
            "cnpj": cnpj_limpo,
            "nome_empresa": nome_fantasia or razao_social or "Empresa sem nome informado",
            "razao_social": razao_social,
            "decisor": _extrair_decisor(dados.get("qsa")),
            "nicho": str(dados.get("cnae_fiscal_descricao") or "Não informado").strip(),
            "endereco": _juntar_endereco(dados),
            "cidade": ", ".join(
                parte for parte in [str(dados.get("municipio") or "").strip(), str(dados.get("uf") or "").strip()] if parte
            ),
            "telefone": str(dados.get("ddd_telefone_1") or dados.get("ddd_telefone_2") or "").strip(),
            "site": "",
            "email": str(dados.get("email") or "").strip(),
            "status": "Novos Leads",
            "status_receita": str(dados.get("descricao_situacao_cadastral") or "Não informado").strip(),
            "origem": "BrasilAPI (Receita Federal)",
            "observacoes": "Dados públicos consultados na BrasilAPI; valide-os antes do contato.",
        }

    mensagem = ""
    try:
        mensagem = str(resposta.json().get("message") or "").strip()
    except (ValueError, AttributeError):
        pass
    if resposta.status_code == 400:
        return {"erro": mensagem or "CNPJ inválido ou mal formatado."}
    if resposta.status_code == 404:
        return {"erro": mensagem or "CNPJ não encontrado na base consultada."}
    if resposta.status_code == 429:
        return {"erro": "Limite de consultas atingido. Aguarde um momento e tente novamente."}
    return {"erro": f"Erro na BrasilAPI (status {resposta.status_code}). {mensagem}".strip()}


def _validar_url_web(url: str) -> str | None:
    """Retorna uma mensagem de erro para URLs inseguras ou malformadas."""
    try:
        partes = urlparse(url.strip())
        host_original = partes.hostname
    except ValueError:
        return "URL inválida."

    if partes.scheme not in {"http", "https"} or not host_original:
        return "Informe uma URL completa iniciada por http:// ou https://."
    if partes.username or partes.password:
        return "URLs contendo usuário ou senha não são aceitas."

    host = host_original.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return "Endereços locais não podem ser rastreados pelo robô."
    try:
        endereco_ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not endereco_ip.is_global:
        return "Endereços de rede privada ou reservada não podem ser rastreados."
    return None


def _extrair_cnpjs_da_pagina(url_alvo: str) -> list[str] | dict[str, str]:
    erro_url = _validar_url_web(url_alvo)
    if erro_url:
        return {"erro": erro_url}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36 ScorpionsCRM/1.0"
        ),
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
    }
    try:
        resposta = requests.get(url_alvo.strip(), headers=headers, timeout=15, allow_redirects=True)
    except requests.exceptions.RequestException as erro:
        return {"erro": f"Falha ao acessar o site: {erro}"}

    erro_redirecionamento = _validar_url_web(resposta.url)
    if erro_redirecionamento:
        return {"erro": f"O redirecionamento foi bloqueado: {erro_redirecionamento}"}
    if resposta.status_code != 200:
        return {"erro": f"Não foi possível acessar a página (status {resposta.status_code})."}

    tipo_conteudo = resposta.headers.get("Content-Type", "").lower()
    if tipo_conteudo and not any(tipo in tipo_conteudo for tipo in ("text/html", "application/xhtml+xml", "text/plain")):
        return {"erro": "A URL não retornou uma página HTML ou texto compatível."}
    if len(resposta.content) > LIMITE_PAGINA_BYTES:
        return {"erro": "A página excede o limite de 5 MB definido para a extração."}

    sopa = BeautifulSoup(resposta.text, "html.parser")
    for elemento in sopa(["script", "style", "noscript", "template"]):
        elemento.decompose()
    texto_da_pagina = sopa.get_text(separator=" ")

    encontrados = {
        limpar_cnpj(valor)
        for valor in PADRAO_CNPJ.findall(texto_da_pagina)
        if cnpj_valido(valor)
    }
    return sorted(encontrados)


def robo_prospeccao_scorpions(
    url_alvo: str,
    limite: int = 20,
    pausa_segundos: float = 0.5,
) -> list[dict[str, Any]] | dict[str, str]:
    """Extrai CNPJs públicos de uma página e qualifica empresas ativas na BrasilAPI."""
    resultado_extracao = _extrair_cnpjs_da_pagina(url_alvo)
    if isinstance(resultado_extracao, dict):
        return resultado_extracao
    if not resultado_extracao:
        return {"erro": "Nenhum CNPJ válido foi encontrado no texto visível da página."}

    cnpjs = resultado_extracao[: max(1, min(int(limite), 50))]
    leads_qualificados: list[dict[str, Any]] = []
    falhas = 0
    inativas = 0
    origem_web = urlparse(url_alvo.strip())._replace(query="", fragment="").geturl()

    for posicao, cnpj in enumerate(cnpjs):
        empresa = consultar_empresa_brasilapi(cnpj)
        if "erro" in empresa:
            falhas += 1
        elif str(empresa.get("status_receita") or "").strip().upper() == "ATIVA":
            empresa["origem"] = "Web scraping + BrasilAPI"
            empresa["observacoes"] = (
                f"CNPJ localizado em {origem_web}. Contato societário obtido do QSA; valide antes da abordagem."
            )
            leads_qualificados.append(empresa)
        else:
            inativas += 1

        if posicao < len(cnpjs) - 1 and pausa_segundos > 0:
            time.sleep(min(float(pausa_segundos), 2.0))

    if not leads_qualificados:
        return {
            "erro": (
                f"Foram encontrados {len(cnpjs)} CNPJ(s), mas nenhuma empresa ativa foi qualificada "
                f"({inativas} inativa(s) e {falhas} consulta(s) sem resultado)."
            )
        }
    return leads_qualificados


def buscar_google_places(nicho: str, localizacao: str, limite: int) -> list[dict[str, Any]]:
    chave = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if not chave:
        raise RuntimeError("GOOGLE_PLACES_API_KEY não configurada.")

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": chave,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.nationalPhoneNumber,places.websiteUri,places.types,nextPageToken"
        ),
    }
    limite = min(limite, 100)
    leads: list[dict[str, Any]] = []
    token: str | None = None

    # A API do Google limita pageSize a 20 por chamada; para atender pedidos
    # maiores é preciso paginar via nextPageToken (mesma lógica do worker em
    # automation.py), senão o restante dos resultados solicitados é perdido.
    while len(leads) < limite:
        corpo: dict[str, Any] = {
            "textQuery": f"{nicho} em {localizacao}",
            "pageSize": min(20, limite - len(leads)),
        }
        if token:
            corpo["pageToken"] = token
        resposta = requests.post(
            GOOGLE_PLACES_URL,
            headers=headers,
            json=corpo,
            timeout=25,
        )
        if not resposta.ok:
            try:
                detalhe = resposta.json().get("error", {}).get("message", "")
            except ValueError:
                detalhe = ""
            if resposta.status_code in {401, 403}:
                raise RuntimeError("A chave do Google Places foi rejeitada ou não tem a API habilitada.")
            if resposta.status_code == 429:
                raise RuntimeError("O limite do Google Places foi atingido. Tente novamente mais tarde.")
            raise RuntimeError(
                f"Não foi possível consultar o Google Places agora. {detalhe}".strip()
            )

        dados = resposta.json()
        for item in dados.get("places", []):
            leads.append(
                {
                    "place_id": item.get("id", ""),
                    "nome_empresa": item.get("displayName", {}).get("text", "Sem nome"),
                    "nicho": nicho,
                    "endereco": item.get("formattedAddress", ""),
                    "cidade": localizacao,
                    "telefone": item.get("nationalPhoneNumber", ""),
                    "site": item.get("websiteUri", ""),
                    "email": "",
                    "status": "Novos Leads",
                    "origem": "Google Places",
                    "observacoes": "",
                }
            )
            if len(leads) >= limite:
                break
        token = dados.get("nextPageToken")
        if not token or len(leads) >= limite:
            break
        time.sleep(1)
    return leads


def gerar_demonstracao(nicho: str, localizacao: str, limite: int) -> list[dict[str, Any]]:
    sufixos = ["Prime", "Central", "Horizonte", "Conecta", "Ideal", "Mais", "Nova", "Ponto"]
    return [
        {
            "place_id": f"demo:{nicho.lower()}:{localizacao.lower()}:{i}",
            "nome_empresa": f"{nicho} {sufixos[i % len(sufixos)]}",
            "nicho": nicho,
            "endereco": f"Endereço demonstrativo {i + 1}, {localizacao}",
            "cidade": localizacao,
            "telefone": "",
            "site": "",
            "email": "",
            "status": "Novos Leads",
            "origem": "Demonstração",
            "observacoes": "Lead fictício para teste; confirme os dados antes do contato.",
        }
        for i in range(limite)
    ]


def salvar_leads(leads: list[dict[str, Any]]) -> tuple[int, int]:
    # Quem cadastra vira o responsável até alguém reatribuir a carteira na
    # página "Equipe" — cobre os 4 pontos de entrada (manual, prospecção,
    # extração por URL, consulta de CNPJ) num lugar só.
    usuario_atual = st.session_state.get("usuario_id")
    leads_com_responsavel = [
        {**lead, "responsavel_usuario_id": lead.get("responsavel_usuario_id") or usuario_atual}
        for lead in leads
    ]
    return salvar_leads_no_banco(leads_com_responsavel)


@st.cache_data
def listar_leads(
    busca: str = "",
    nicho: str = "Todos",
    status: str = "Todos",
    usuario_id: int | None = None,
    nivel: str | None = None,
    equipe_id: int | None = None,
) -> pd.DataFrame:
    """`usuario_id`/`nivel`/`equipe_id` entram na assinatura (não são lidos de
    st.session_state aqui dentro) porque o cache do Streamlit particiona pelo
    valor dos argumentos: se o escopo viesse de session_state, dois usuários
    diferentes poderiam receber a base cacheada um do outro."""
    sql = (
        "SELECT leads.*, usuarios.nome AS responsavel_nome FROM leads "
        "LEFT JOIN usuarios ON usuarios.id = leads.responsavel_usuario_id WHERE 1=1"
    )
    parametros: list[Any] = []
    escopo = pode(nivel, "escopo_leads") if nivel else "todos"
    if escopo == "proprios":
        sql += " AND leads.responsavel_usuario_id = ?"
        parametros.append(usuario_id)
    elif escopo == "equipe":
        sql += (
            " AND (leads.responsavel_usuario_id IS NULL OR leads.responsavel_usuario_id IN "
            "(SELECT id FROM usuarios WHERE equipe_id = ?))"
        )
        parametros.append(equipe_id)
    if busca:
        sql += " AND (nome_empresa LIKE ? OR razao_social LIKE ? OR cnpj LIKE ? OR cidade LIKE ? OR endereco LIKE ?)"
        termo = f"%{busca}%"
        digitos_busca = limpar_cnpj(busca)
        termo_cnpj = f"%{digitos_busca}%" if digitos_busca else termo
        parametros.extend([termo, termo, termo_cnpj, termo, termo])
    if nicho != "Todos":
        sql += " AND nicho = ?"
        parametros.append(nicho)
    if status != "Todos":
        sql += " AND status = ?"
        parametros.append(status)
    sql += " ORDER BY atualizado_em DESC"
    with conectar() as conexao:
        return pd.read_sql_query(sql, conexao, params=parametros)


def leads_visiveis(busca: str = "", nicho: str = "Todos", status: str = "Todos") -> pd.DataFrame:
    """Wrapper fino que lê o usuário logado da sessão e delega pro `listar_leads`
    cacheado — todo ponto do app que precisa da base de leads deve chamar este,
    não `listar_leads` direto, para não vazar o escopo de outro usuário."""
    return listar_leads(
        busca,
        nicho,
        status,
        usuario_id=st.session_state.get("usuario_id"),
        nivel=st.session_state.get("nivel_usuario"),
        equipe_id=st.session_state.get("equipe_id_usuario"),
    )


def atividades_visiveis(leads: pd.DataFrame, limite: int = 8) -> list[dict[str, Any]]:
    """Mantém a atividade recente dentro do mesmo escopo aplicado à carteira."""
    escopo = pode(st.session_state.get("nivel_usuario"), "escopo_leads")
    if escopo == "todos":
        return listar_atividades(limite)
    ids_visiveis = set(leads["id"].dropna().astype(int).tolist()) if not leads.empty else set()
    atividades = listar_atividades(100)
    return [
        atividade
        for atividade in atividades
        if atividade.get("lead_id") is not None and int(atividade["lead_id"]) in ids_visiveis
    ][:limite]


def _valor_para_str_canonico(valor: Any, tipo: str = "str") -> str:
    """Converte um valor para uma string canônica para comparação."""
    if pd.isna(valor):
        return ""
    if tipo == "date":
        try:
            return pd.to_datetime(valor).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return str(valor)
    if tipo == "float":
        try:
            return f"{float(valor):.2f}"
        except (ValueError, TypeError):
            return str(valor)
    return str(valor).strip()


def atualizar_leads(editado: pd.DataFrame, original: pd.DataFrame) -> int:
    campos = [
        "nome_empresa", "razao_social", "decisor", "nicho", "endereco", "cidade",
        "telefone", "site", "email", "status", "observacoes", "proximo_contato", "valor_proposta",
    ]
    originais = original.set_index("id")
    alterados = 0
    atividades_pendentes: list[tuple[int, str]] = []
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conectar() as conexao:
        for _, linha in editado.iterrows():
            lead_id = int(linha["id"])
            if lead_id not in originais.index:
                continue

            mudou = False
            for c in campos:
                tipo = "str"
                if c == "proximo_contato":
                    tipo = "date"
                if c == "valor_proposta":
                    tipo = "float"

                str_editado = _valor_para_str_canonico(linha[c], tipo)
                str_original = _valor_para_str_canonico(originais.loc[lead_id, c], tipo)
                if str_editado != str_original:
                    mudou = True
                    break

            if mudou:
                valores = []
                for c in campos:
                    valor = linha[c]
                    if pd.isna(valor):
                        valor_final = None
                    elif c == "proximo_contato":
                        valor_final = pd.to_datetime(valor).strftime("%Y-%m-%d")
                    elif c == "valor_proposta":
                        valor_final = float(valor) if valor and float(valor) > 0 else None
                    else:
                        valor_final = str(valor)
                    valores.append(valor_final)
                conexao.execute(
                    f"UPDATE leads SET {', '.join(f'{c} = ?' for c in campos)}, atualizado_em = ? WHERE id = ?",
                    (*valores, agora, lead_id),
                )
                atividades_pendentes.append(
                    (lead_id, f"Dados comerciais de '{linha['nome_empresa']}' atualizados.")
                )
                alterados += 1
    for lead_id, descricao in atividades_pendentes:
        registrar_atividade(
            "lead_atualizado", descricao, lead_id,
            usuario=st.session_state.get("usuario_logado", "sistema"),
        )
    st.cache_data.clear()
    return alterados


def _lead_dentro_do_escopo(lead_id: int) -> bool:
    """Confere se o lead pertence ao escopo do usuário logado (própria
    carteira, equipe ou todos, conforme o nível) — nunca confia que o
    lead_id que chegou até a função já foi filtrado só pela UI. Serve de
    segunda camada de defesa para excluir_lead/atualizar_etapa_funil, que
    recebem só o id (sem um dataframe já escopado pra cruzar, como
    atualizar_leads tem)."""
    nivel = st.session_state.get("nivel_usuario")
    escopo = pode(nivel, "escopo_leads")
    with conectar() as conexao:
        if escopo == "todos":
            linha = conexao.execute("SELECT 1 FROM leads WHERE id = ?", (lead_id,)).fetchone()
        elif escopo == "proprios":
            linha = conexao.execute(
                "SELECT 1 FROM leads WHERE id = ? AND responsavel_usuario_id = ?",
                (lead_id, st.session_state.get("usuario_id")),
            ).fetchone()
        elif escopo == "equipe":
            linha = conexao.execute(
                "SELECT 1 FROM leads WHERE id = ? AND (responsavel_usuario_id IS NULL OR "
                "responsavel_usuario_id IN (SELECT id FROM usuarios WHERE equipe_id = ?))",
                (lead_id, st.session_state.get("equipe_id_usuario")),
            ).fetchone()
        else:
            linha = None
    return linha is not None


def excluir_lead(lead_id: int) -> None:
    if not pode(st.session_state.get("nivel_usuario"), "pode_excluir_leads"):
        raise PermissionError("Sem permissão para excluir leads.")
    if not _lead_dentro_do_escopo(lead_id):
        raise PermissionError("Esse lead está fora do seu escopo.")

    # A descricao da atividade NAO leva o nome da empresa: como o lead_id
    # vira NULL na atividade apos o delete (FK ON DELETE SET NULL), guardar o
    # nome aqui deixaria o dado pessoal sobrevivendo a propria exclusao —
    # achado da auditoria LGPD (direito ao esquecimento incompleto).
    registrar_atividade(
        "lead_excluido",
        "Lead excluído da base.",
        lead_id,
        usuario=st.session_state.get("usuario_logado", "sistema"),
    )

    with conectar() as conexao:
        conexao.execute("DELETE FROM leads WHERE id = ?", (lead_id,))

    st.cache_data.clear()


FRASE_CONFIRMACAO_ZERAR_BASE = "ZERAR TUDO"


def zerar_base_leads() -> int:
    """Apaga TODOS os leads e tudo que só existe em função deles (snapshots,
    sinais, histórico de score, outcomes, mensagens). Ação irreversível,
    restrita a diretor -- mais estrita que a exclusão individual (que também
    libera gerente) por causa do tamanho do estrago possível.

    Não mexe em usuários, equipes, campanhas, alvos contínuos ou na lista de
    supressão de contato (essa é um registro de compliance independente de
    qualquer lead específico)."""
    if st.session_state.get("nivel_usuario") != "diretor":
        raise PermissionError("Só o diretor pode zerar a base de leads.")
    with conectar() as conexao:
        total = int(conexao.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"])
        for tabela in (
            "company_snapshots", "sales_signals", "opportunity_score_history",
            "opportunity_outcomes", "mensagens_enviadas",
        ):
            conexao.execute(f"DELETE FROM {tabela}")
        conexao.execute("DELETE FROM atividades_comerciais WHERE lead_id IS NOT NULL")
        conexao.execute("DELETE FROM leads")
    st.cache_data.clear()
    return total


def atualizar_etapa_funil(lead_id: int, nova_etapa: str):
    """Atualiza a etapa do funil para um único lead."""
    if nova_etapa not in STATUS:
        raise ValueError(f"Etapa inválida: {nova_etapa}")
    if not _lead_dentro_do_escopo(lead_id):
        raise PermissionError("Esse lead está fora do seu escopo.")
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conectar() as conexao:
        lead = conexao.execute(
            "SELECT nome_empresa, status FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        conexao.execute(
            "UPDATE leads SET status = ?, atualizado_em = ? WHERE id = ?",
            (nova_etapa, agora, lead_id),
        )
    if lead and lead["status"] != nova_etapa:
        registrar_atividade(
            "etapa_alterada",
            f"'{lead['nome_empresa']}' movido de '{lead['status']}' para '{nova_etapa}'.",
            lead_id,
            usuario=st.session_state.get("usuario_logado", "sistema"),
        )
    st.cache_data.clear()


def _render_kanban_card(lead: pd.Series, etapa_atual: str) -> None:
    """Renderiza um card de lead no quadro Kanban."""
    contatos_disponiveis = " · ".join(
        rotulo
        for rotulo, disponivel in (
            ("Tel", lead.get("telefone")),
            ("E-mail", lead.get("email")),
            ("Site", lead.get("site")),
        )
        if disponivel
    )
    contato_atrasado = False
    rotulo_proximo_contato = "Não definido"
    proximo_contato_str = lead.get("proximo_contato")
    if proximo_contato_str and pd.notna(proximo_contato_str):
        try:
            data_contato = pd.to_datetime(proximo_contato_str).date()
            hoje_utc = datetime.now(timezone.utc).date()
            contato_atrasado = data_contato < hoje_utc
            if contato_atrasado:
                rotulo_proximo_contato = f"Atrasado · {data_contato.strftime('%d/%m/%Y')}"
            elif data_contato == hoje_utc:
                rotulo_proximo_contato = "Hoje"
            elif data_contato == hoje_utc + timedelta(days=1):
                rotulo_proximo_contato = "Amanhã"
            else:
                rotulo_proximo_contato = data_contato.strftime("%d/%m/%Y")
        except (ValueError, TypeError, pd.errors.ParserError):
            proximo_contato_str = None

    score = lead.get('pontuacao')
    score_num = int(score) if pd.notna(score) else None
    if score_num is None:
        score_classe, score_str = "score-lo", "ICP —"
    elif score_num >= 85:
        score_classe, score_str = "score-hi", f"ICP {score_num}"
    elif score_num >= 70:
        score_classe, score_str = "score-mid", f"ICP {score_num}"
    else:
        score_classe, score_str = "score-lo", f"ICP {score_num}"

    valor_bruto = lead.get('valor_proposta')
    valor_proposta = float(valor_bruto) if pd.notna(valor_bruto) else 0.0
    icp_bruto = lead.get('segmento_icp')
    icp = icp_bruto if icp_bruto and pd.notna(icp_bruto) else 'Não classificado'
    responsavel = lead.get('responsavel_nome')
    responsavel = responsavel if responsavel and pd.notna(responsavel) else None
    cidade = str(lead.get("cidade") or "Cidade não informada")
    nicho = str(lead.get("nicho") or "Nicho não informado")
    classe_contato = " kanban-fact--overdue" if contato_atrasado else ""
    valor_html = (
        f'<span class="kanban-fact"><span>Valor</span><strong>R$ {valor_proposta:,.0f}</strong></span>'
        if valor_proposta > 0 else ""
    )
    proximo_contato_html = (
        f'<span class="kanban-fact{classe_contato}">'
        f'<span>Próximo contato</span><strong>{escape(rotulo_proximo_contato)}</strong>'
        f"</span>"
    )

    with st.container(key=f"kanban_card_{int(lead['id'])}"):
        # Os fatos ficam concatenados numa linha só (sem quebra entre eles):
        # quando valor_html fica vazio (lead sem proposta), uma linha em branco
        # no meio do bloco HTML interrompe o parser de Markdown do Streamlit no
        # meio do <div>, e o restante passa a ser exibido como texto cru.
        st.markdown(
            f"""
            <div class="kanban-card">
              <div class="kanban-topline">
                <div class="kanban-company">{escape(str(lead['nome_empresa']))}</div>
                <div class="score-badge {score_classe}">{score_str}</div>
              </div>
              <div class="kanban-location">{escape(cidade)} · {escape(nicho)}</div>
              <div class="kanban-facts">{valor_html}{proximo_contato_html}</div>
              <div class="kanban-owner">Responsável · {escape(str(responsavel or 'Não atribuído'))}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.expander("Mais detalhes", type="compact"):
            st.caption(f"Segmento ICP: {icp}")
            st.caption(f"Contatos disponíveis: {contatos_disponiveis or 'nenhum informado'}")
            if lead.get("endereco"):
                st.caption(f"Endereço: {lead.get('endereco')}")

        col_mover, col_abrir = st.columns([3, 1])
        with col_mover:
            nova_etapa = st.selectbox(
                "Mover para",
                options=STATUS,
                index=STATUS.index(etapa_atual),
                key=f"move_{lead['id']}",
                label_visibility="collapsed",
            )
            if nova_etapa != etapa_atual:
                atualizar_etapa_funil(int(lead["id"]), nova_etapa)
                st.cache_data.clear()
                st.toast(f"Lead movido para \"{nova_etapa}\".", icon=":material/check_circle:")
                st.rerun()
        with col_abrir:
            if st.button("Abrir", key=f"abrir_{lead['id']}", width="stretch"):
                st.session_state["busca_empresas"] = lead["nome_empresa"]
                st.session_state["versao_busca_empresas"] = st.session_state.get("versao_busca_empresas", 0) + 1
                st.session_state["navegacao_solicitada"] = "Empresas"
                st.rerun()

st.set_page_config(page_title="Scorpions CRM", page_icon=":material/monitoring:", layout="wide")
google_places_configurado = configurar_google_places()

st.markdown(f"<style>{carregar_tema_css()}</style>", unsafe_allow_html=True)

iniciar_banco()
iniciar_banco_automacao()
iniciar_banco_usuarios()
if os.getenv("SCORPIONS_DISABLE_WORKER", "").strip() != "1":
    _iniciar_worker_em_background()

exigir_login()

# Botões fora da sidebar (header, cards do kanban etc.) não podem escrever
# direto em st.session_state["navegacao_principal"]: o widget do rádio já foi
# instanciado na hora em que eles rodam, e o Streamlit proíbe alterar o estado
# de um widget depois de criado. Por isso eles gravam em "navegacao_solicitada"
# e é resolvido aqui, sempre antes do rádio ser criado.
if st.session_state.get("navegacao_solicitada"):
    st.session_state["navegacao_principal"] = st.session_state.pop("navegacao_solicitada")

# Carregado uma única vez por rerun (cache_data) e reaproveitado nos contadores
# da sidebar, no cabeçalho e no dashboard, evitando reconsultas redundantes.
_leads_para_contadores = leads_visiveis()
_total_leads = len(_leads_para_contadores)
_campanhas_para_contadores = listar_campanhas()
_campanhas_ativas = sum(1 for c in _campanhas_para_contadores if c.get("ativa"))

with st.sidebar:
    st.image("assets/scorpions-logo-mark.png", width=48)
    st.markdown(
        '<div class="brand-lockup"><strong>SCORPIONS</strong><span>Soluções tecnológicas</span></div>',
        unsafe_allow_html=True,
    )

    busca_global = st.text_input(
        "Buscar empresa, CNPJ…",
        key="busca_global",
        placeholder="Buscar empresa, CNPJ…",
        label_visibility="collapsed",
    )
    if busca_global and busca_global != st.session_state.get("_busca_global_aplicada"):
        st.session_state["_busca_global_aplicada"] = busca_global
        st.session_state["busca_empresas"] = busca_global
        st.session_state["versao_busca_empresas"] = st.session_state.get("versao_busca_empresas", 0) + 1
        st.session_state["navegacao_principal"] = "Empresas"
        st.rerun()

    st.markdown('<div class="sidebar-caption">Workspace</div>', unsafe_allow_html=True)
    _nivel_atual = st.session_state.get("nivel_usuario", "vendedor")
    _paginas_do_nivel = paginas_visiveis(_nivel_atual)
    _oportunidades_criticas_altas = int(
        _leads_para_contadores["opportunity_level"].isin(["Crítica", "Alta"]).sum()
    ) if "opportunity_level" in _leads_para_contadores.columns and _total_leads else 0
    contadores_nav = {
        "Visão geral": None,
        "Pipeline": None,
        "Prospecção": None,
        "Empresas": _total_leads,
        "Contato": None,
        "Radar": _oportunidades_criticas_altas or None,
        "Automação": _campanhas_ativas or None,
        "Nova empresa": None,
        "Equipe": None,
    }
    _icones_paginas = {
        "Visão geral": ":material/dashboard:  Dashboard",
        "Pipeline": ":material/view_kanban:  Negócios / Pipeline",
        "Prospecção": ":material/search:  Leads / Prospecção",
        "Empresas": ":material/business:  Clientes / Empresas",
        "Contato": ":material/chat:  Contato",
        "Radar": ":material/radar:  Radar",
        "Automação": ":material/bolt:  Automação",
        "Nova empresa": ":material/add_business:  Nova empresa",
        "Equipe": ":material/groups:  Equipe",
    }
    # Se o usuário logado mudou de nível (ou é outra sessão) e a página guardada
    # não está mais liberada pro nível atual, volta pro Dashboard antes de criar
    # o widget — o Streamlit não aceita um valor fora da lista de opções.
    if st.session_state.get("navegacao_principal") not in _paginas_do_nivel:
        st.session_state["navegacao_principal"] = _paginas_do_nivel[0]
    pagina = st.radio(
        "Navegação",
        list(_paginas_do_nivel),
        format_func=lambda item: _icones_paginas.get(item, item)
        + (f"  ·  {contadores_nav.get(item)}" if contadores_nav.get(item) else ""),
        label_visibility="collapsed",
        key="navegacao_principal",
    )
    st.markdown('<div class="sidebar-caption">Sistema</div>', unsafe_allow_html=True)
    estado_sidebar = status_worker()
    worker_online = bool(estado_sidebar["online"])
    st.markdown(
        f'<div class="sidebar-status"><span class="dot" style="{"" if worker_online else "background:var(--weak);animation:none;"}"></span>'
        f'Worker · {"Operacional" if worker_online else "Offline"}</div>',
        unsafe_allow_html=True,
    )
    st.caption("Scorpions CRM · v1.1")
    st.caption(
        f"{st.session_state.get('nome_usuario') or st.session_state.get('usuario_logado', '—')} · "
        f"{rotulo_nivel(_nivel_atual)}"
    )
    with st.expander("Trocar senha", icon=":material/lock_reset:"):
        with st.form("form_trocar_senha", border=False):
            senha_atual_form = st.text_input("Senha atual", type="password", key="trocar_senha_atual")
            senha_nova_form = st.text_input("Nova senha", type="password", key="trocar_senha_nova")
            if st.form_submit_button("Salvar nova senha", width="stretch"):
                try:
                    trocar_propria_senha(
                        st.session_state.get("usuario_id"), senha_atual_form, senha_nova_form
                    )
                    st.success("Senha alterada.")
                except ValueError as erro:
                    st.error(str(erro))

    if st.button("Sair", width="stretch"):
        for chave in (
            "autenticado", "usuario_logado", "usuario_id", "nivel_usuario",
            "equipe_id_usuario", "nome_usuario", "navegacao_principal",
        ):
            st.session_state.pop(chave, None)
        st.rerun()

aba_dashboard = pagina == "Visão geral"
aba_funil = pagina == "Pipeline"
aba_prospeccao = pagina == "Prospecção"
aba_cnpj = pagina == "Empresas"
aba_contato = pagina == "Contato"
aba_radar = pagina == "Radar"
aba_automacao = pagina == "Automação"
aba_base = pagina == "Empresas"
aba_manual = pagina == "Nova empresa"
aba_equipe = pagina == "Equipe"
_pode_gerenciar_campanhas = bool(pode(st.session_state.get("nivel_usuario"), "pode_gerenciar_campanhas"))

# Cada página define título/subtítulo/badges/ações próprios — nunca repete as
# mesmas ações (Prospectar / Nova empresa) indiscriminadamente em todas.
if aba_dashboard:
    _acao_header = render_page_header(
        "Dashboard",
        "Prospecção, pipeline e operação comercial em um só lugar.",
        status=("Operacional", "online"),
        acoes=[("Prospectar", "hdr_prospectar", True)],
    )
    if _acao_header:
        st.session_state["navegacao_solicitada"] = "Prospecção"
        st.rerun()
elif aba_funil:
    _etapas_ativas_count = len([e for e in STATUS if e not in ("Fechado / Contrato", "Descartado")])
    _acao_header = render_page_header(
        "Pipeline",
        "Negócios abertos e valor em proposta por etapa.",
        badges=[f"{_etapas_ativas_count} etapas"],
        acoes=[("Prospectar", "hdr_prospectar", True)],
    )
    if _acao_header:
        st.session_state["navegacao_solicitada"] = "Prospecção"
        st.rerun()
elif aba_prospeccao:
    render_page_header("Prospecção", "Captura em fontes públicas com scoring de ICP.")
elif aba_cnpj:
    _acao_header = render_page_header(
        "Clientes",
        "Base cadastral, consulta CNPJ e exclusão segura.",
        badges=[f"{_total_leads} registros"],
        acoes=[("+ Nova empresa", "hdr_nova_empresa", True)],
    )
    if _acao_header:
        st.session_state["navegacao_solicitada"] = "Nova empresa"
        st.rerun()
elif aba_contato:
    render_page_header(
        "Contato",
        "Mensagens de WhatsApp assistidas — você confere e envia, o sistema não dispara nada sozinho.",
    )
elif aba_radar:
    render_page_header(
        "Radar",
        "Oportunidades priorizadas por Fit, Intent, Timing e confiança dos dados.",
    )
elif aba_automacao:
    _acoes_automacao = [("+ Nova campanha", "hdr_nova_campanha", True)] if _pode_gerenciar_campanhas else None
    _acao_header = render_page_header(
        "Automação",
        "Campanhas agendadas, motor contínuo e histórico.",
        status=(f"Worker {'online' if worker_online else 'offline'}", "online" if worker_online else "offline"),
        acoes=_acoes_automacao,
    )
    if _acao_header:
        _dialog_nova_campanha()
elif aba_manual:
    render_page_header("Nova empresa", "Campos obrigatórios validados antes de salvar.")
elif aba_equipe:
    _acoes_equipe = (
        [("+ Usuário", "hdr_novo_usuario", True), ("+ Equipe", "hdr_nova_equipe", False)]
        if _nivel_atual == "diretor" else None
    )
    _acao_header = render_page_header(
        "Equipe",
        "Contas, permissões e distribuição de carteira.",
        badges=[rotulo_nivel(_nivel_atual)],
        acoes=_acoes_equipe,
    )
    if _acao_header == "+ Usuário":
        _dialog_novo_usuario()
    elif _acao_header == "+ Equipe":
        _dialog_nova_equipe()

if aba_dashboard:
    dados = _leads_para_contadores
    escopo_dashboard = pode(_nivel_atual, "escopo_leads")
    if escopo_dashboard in {"todos", "equipe"}:
        if escopo_dashboard == "equipe":
            usuarios_carteira_dashboard = listar_usuarios(equipe_id=st.session_state.get("equipe_id_usuario"))
        else:
            usuarios_carteira_dashboard = listar_usuarios()
        opcoes_carteira_dashboard = {
            "Todos": None,
            "Minha carteira": st.session_state.get("usuario_id"),
        }
        opcoes_carteira_dashboard.update({
            f"{usuario['nome']} (@{usuario['username']})": usuario["id"]
            for usuario in usuarios_carteira_dashboard
            if usuario["id"] != st.session_state.get("usuario_id")
        })
        with st.container(horizontal=True, horizontal_alignment="right"):
            carteira_dashboard = st.selectbox(
                "Carteira",
                list(opcoes_carteira_dashboard),
                key="filtro_carteira_dashboard",
                width=260,
            )
        responsavel_dashboard = opcoes_carteira_dashboard[carteira_dashboard]
        if responsavel_dashboard is not None:
            dados = dados[dados["responsavel_usuario_id"] == responsavel_dashboard].copy()
    total = len(dados)
    fechados = int((dados["status"] == "Fechado / Contrato").sum()) if total else 0
    em_andamento = int(
        dados["status"].isin(["Contato / Qualificação", "Vistoria Técnica / Diagnóstico", "Proposta Enviada"]).sum()
    ) if total else 0
    conversao = (fechados / total * 100) if total else 0
    etapas_ativas_dashboard = [
        etapa for etapa in STATUS if etapa not in ("Fechado / Contrato", "Descartado")
    ]
    valor_pipeline = float(
        dados.loc[
            dados["status"].isin(etapas_ativas_dashboard), "valor_proposta"
        ].fillna(0).sum()
    ) if total else 0.0
    propostas = int((dados["status"] == "Proposta Enviada").sum()) if total else 0
    novos = int((dados["status"] == "Novos Leads").sum()) if total else 0
    valor_propostas = float(
        dados.loc[dados["status"] == "Proposta Enviada", "valor_proposta"].fillna(0).sum()
    ) if total else 0.0

    hoje = datetime.now(timezone.utc).date()
    if total and "criado_em" in dados.columns:
        criado_em_dt = pd.to_datetime(dados["criado_em"], errors="coerce", utc=True)
        novos_no_mes = int(((criado_em_dt.dt.year == hoje.year) & (criado_em_dt.dt.month == hoje.month)).sum())
    else:
        novos_no_mes = 0

    st.markdown(
        """
        <div class="sales-summary">
          <div class="sales-summary__item accent">
            <span class="label">Leads</span>
            <strong>{}</strong>
            <span class="note" style="color:#7DD3FC">+{} este mês</span>
          </div>
          <div class="sales-summary__item">
            <span class="label">Novos</span>
            <strong>{}</strong>
            <span class="note">em Novos Leads</span>
          </div>
          <div class="sales-summary__item">
            <span class="label">Em andamento</span>
            <strong>{}</strong>
            <span class="note">{} em proposta enviada</span>
          </div>
          <div class="sales-summary__item">
            <span class="label">Propostas</span>
            <strong>{}</strong>
            <span class="note" style="color:#7DD3FC">R$ {:,.0f} em jogo</span>
          </div>
          <div class="sales-summary__item">
            <span class="label">Conversão</span>
            <strong>{:.1f}%</strong>
            <span class="note">{} fechado(s)</span>
          </div>
          <div class="sales-summary__item accent">
            <span class="label">Valor em pipeline</span>
            <strong>R$ {:,.0f}</strong>
            <span class="note">negócios em etapas ativas</span>
          </div>
        </div>
        """.format(
            total, novos_no_mes, novos, em_andamento, propostas, propostas, valor_propostas,
            conversao, fechados, valor_pipeline,
        ),
        unsafe_allow_html=True,
    )

    if total:
        etapas_funil_dash = [etapa for etapa in STATUS if etapa not in ("Fechado / Contrato", "Descartado")]
        contagens_funil = [int((dados["status"] == etapa).sum()) for etapa in etapas_funil_dash]
        valores_funil = [
            float(dados.loc[dados["status"] == etapa, "valor_proposta"].fillna(0).sum())
            for etapa in etapas_funil_dash
        ]
        max_contagem_funil = max(1, max(contagens_funil))

        icp_base = dados["segmento_icp"].fillna("").astype(str)
        classificados_mask = ~icp_base.isin(["", "Não classificado"])
        nao_classificados = total - int(classificados_mask.sum())
        icp_agrupado = (
            dados.loc[classificados_mask]
            .groupby(icp_base[classificados_mask])["pontuacao"]
            .agg(["count", "mean"])
            .sort_values("count", ascending=False)
            .head(5)
        ) if classificados_mask.any() else pd.DataFrame(columns=["count", "mean"])

        col_funil, col_icp = st.columns([1.25, 1])
        with col_funil:
            with st.container(key="dash_funil_card"):
                st.markdown(
                    '<h3 style="margin:0;font-family:Orbitron,sans-serif;font-size:0.85rem;font-weight:600;'
                    'letter-spacing:0.12em;text-transform:uppercase;color:#F5F7FA;">Funil comercial</h3>'
                    '<div style="font-size:0.7rem;color:#5A6373;margin-top:0.2rem;">visão geral do pipeline ativo</div>',
                    unsafe_allow_html=True,
                )
                linhas_funil = "".join(
                    '<div class="funnel-row">'
                    '<div class="funnel-top">'
                    f'<span>{etapa}</span>'
                    f'<span class="val">{contagem} <span class="sep">·</span> R$ {valor:,.0f}</span>'
                    '</div>'
                    '<div class="funnel-track">'
                    f'<div class="funnel-fill" style="width:{max(4, round(contagem / max_contagem_funil * 100))}%"></div>'
                    '</div>'
                    '</div>'
                    for etapa, contagem, valor in zip(etapas_funil_dash, contagens_funil, valores_funil)
                )
                st.markdown(f'<div style="margin-top:1rem;">{linhas_funil}</div>', unsafe_allow_html=True)

        with col_icp:
            with st.container(key="dash_icp_card"):
                st.markdown(
                    '<h3 style="margin:0;font-family:Orbitron,sans-serif;font-size:0.85rem;font-weight:600;'
                    'letter-spacing:0.12em;text-transform:uppercase;color:#F5F7FA;">ICP por segmento</h3>',
                    unsafe_allow_html=True,
                )
                if not icp_agrupado.empty:
                    linhas_icp = "".join(
                        '<div class="icp-row">'
                        f'<span class="name">{nome}</span>'
                        f'<span class="leads">{int(linha["count"])} leads</span>'
                        f'<span class="chip chip-accent">score {round(linha["mean"]) if pd.notna(linha["mean"]) else "—"}</span>'
                        '</div>'
                        for nome, linha in icp_agrupado.iterrows()
                    )
                    st.markdown(f'<div style="margin-top:0.7rem;">{linhas_icp}</div>', unsafe_allow_html=True)
                else:
                    st.markdown(
                        '<div style="margin-top:0.7rem;font-size:0.78rem;color:#8A94A6;">'
                        'Nenhum lead classificado por ICP ainda.</div>',
                        unsafe_allow_html=True,
                    )
                if nao_classificados > 0:
                    st.markdown(
                        f"""
                        <div class="icp-alert">
                          <div class="icp-alert-title">{nao_classificados} de {total} leads sem ICP</div>
                          <p>O scoring não segmenta sem CNPJ. Complete o cadastro para destravar o funil.</p>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    if st.button("Completar cadastros", key="dash_completar_cadastros"):
                        st.session_state["navegacao_solicitada"] = "Empresas"
                        st.rerun()

        st.markdown('<div class="section-title" style="margin-top:0.3rem;">Prioridades de hoje</div>', unsafe_allow_html=True)

        cartoes_atencao: list[tuple[str, str, str, str]] = []
        # Fica em espaço Timestamp (sem `.dt.date`) de propósito: quando a coluna
        # inteira é nula, `.dt.date` no pandas 3.x devolve dtype datetime64 (não
        # object) e comparar isso com um `date` puro explode com TypeError.
        # Comparando Timestamp com Timestamp o resultado é robusto nos dois casos.
        datas_proximo_contato = pd.to_datetime(dados["proximo_contato"], errors="coerce")
        hoje_ts = pd.Timestamp(hoje)

        retornos_atrasados = dados[
            dados["status"].isin(etapas_funil_dash)
            & datas_proximo_contato.notna()
            & (datas_proximo_contato < hoje_ts)
        ]
        if not retornos_atrasados.empty:
            indice_mais_antigo = datas_proximo_contato[retornos_atrasados.index].idxmin()
            retorno_mais_antigo = retornos_atrasados.loc[indice_mais_antigo]
            data_mais_antiga = datas_proximo_contato.loc[indice_mais_antigo]
            cartoes_atencao.append((
                "Retornos atrasados",
                f"{len(retornos_atrasados)} contato(s) vencido(s)",
                f"Mais antigo: {retorno_mais_antigo['nome_empresa']} · {data_mais_antiga.strftime('%d/%m/%Y')}.",
                "#ff8f8f",
            ))

        sem_proximo_contato = dados[
            (dados["status"] == "Novos Leads") & datas_proximo_contato.isna()
        ]
        if not sem_proximo_contato.empty:
            cartoes_atencao.append((
                "Leads sem próximo contato",
                f"{len(sem_proximo_contato)} lead(s) em Novos Leads",
                "Defina a próxima ação comercial para manter a carteira em movimento.",
                "#F3C75F",
            ))

        propostas_df = dados[dados["status"] == "Proposta Enviada"]
        if not propostas_df.empty:
            valor_em_propostas = float(propostas_df["valor_proposta"].fillna(0).sum())
            cartoes_atencao.append((
                "Propostas em aberto",
                f"{len(propostas_df)} proposta(s) enviada(s)",
                f"R$ {valor_em_propostas:,.0f} registrados nesta etapa."
                if valor_em_propostas > 0 else "Há propostas sem valor informado nesta etapa.",
                "#7DD3FC",
            ))

        pontuacao_numerica = pd.to_numeric(dados["pontuacao"], errors="coerce")
        leads_icp_alto = dados[pontuacao_numerica >= 85]
        if not leads_icp_alto.empty:
            maior_score = int(pontuacao_numerica[leads_icp_alto.index].max())
            cartoes_atencao.append((
                "ICP alto",
                f"{len(leads_icp_alto)} oportunidade(s) com score ≥ 85",
                f"Maior score disponível na carteira: {maior_score}.",
                "#35D07F",
            ))

        if cartoes_atencao:
            colunas_atencao = st.columns(len(cartoes_atencao))
            for coluna, (kicker, titulo, nota, cor) in zip(colunas_atencao, cartoes_atencao):
                with coluna:
                    st.markdown(
                        f"""
                        <div class="attn-card">
                          <div class="attn-kicker" style="color:{cor}">{escape(kicker)}</div>
                          <div class="attn-title">{escape(titulo)}</div>
                          <div class="attn-note">{escape(nota)}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
        else:
            render_empty_state(
                "Tudo em dia",
                "Nenhuma pendência operacional identificada com os dados disponíveis.",
                compacto=True,
            )

    if total:
        with st.expander("Mais indicadores"):
            c1, c2 = st.columns(2)
            with c1:
                st.subheader("Pipeline por etapa")
                pipeline_por_etapa = (
                    dados["status"]
                    .value_counts()
                    .rename_axis("Etapa")
                    .rename("Quantidade")
                    .to_frame()
                )
                if not pipeline_por_etapa.empty:
                    st.dataframe(
                        pipeline_por_etapa.reset_index(),
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "Etapa": st.column_config.TextColumn("Etapa"),
                            "Quantidade": st.column_config.NumberColumn("Leads", format="%d"),
                        },
                    )
                st.subheader("Geração por mês")
                if 'criado_em' in dados.columns and not dados['criado_em'].isnull().all():
                    dados_grafico = dados.copy()
                    dados_grafico["criado_em_dt"] = pd.to_datetime(dados_grafico["criado_em"], errors="coerce")
                    dados_grafico.dropna(subset=["criado_em_dt"], inplace=True)
                    if not dados_grafico.empty:
                        leads_por_mes = (
                            dados_grafico.set_index("criado_em_dt")
                            .resample("ME")
                            .size()
                            .rename("Novos Leads")
                            .to_frame()
                        )
                        leads_por_mes.index = leads_por_mes.index.strftime("%Y-%m")
                        if not leads_por_mes.empty:
                            leads_por_mes = leads_por_mes.rename_axis("Mês").reset_index()
                            st.dataframe(
                                leads_por_mes,
                                width="stretch",
                                hide_index=True,
                                column_config={
                                    "Mês": st.column_config.TextColumn("Mês"),
                                    "Novos Leads": st.column_config.NumberColumn("Leads", format="%d"),
                                },
                            )
                    else:
                        st.caption("Ainda não há datas válidas para exibir evolução.")
            with c2:
                st.subheader("Nicho principal")
                nichos_principais = (
                    dados["nicho"]
                    .fillna("Não informado")
                    .replace("", "Não informado")
                    .value_counts()
                    .head(10)
                    .rename_axis("Nicho")
                    .rename("Quantidade")
                    .to_frame()
                )
                if not nichos_principais.empty:
                    st.dataframe(
                        nichos_principais.reset_index(),
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "Nicho": st.column_config.TextColumn("Nicho"),
                            "Quantidade": st.column_config.NumberColumn("Leads", format="%d"),
                        },
                    )
                st.subheader("Valor em proposta")
                etapas_ativas_para_grafico = [
                    "Novos Leads", "Contato / Qualificação", "Vistoria Técnica / Diagnóstico", "Proposta Enviada"
                ]
                valores_por_etapa = dados[
                    dados["status"].isin(etapas_ativas_para_grafico)
                ].groupby("status")["valor_proposta"].sum()
                valores_por_etapa = pd.to_numeric(valores_por_etapa, errors="coerce").fillna(0)
                if not valores_por_etapa.empty and valores_por_etapa.sum() > 0:
                    dados_valor = valores_por_etapa.rename("Valor").rename_axis("Etapa").reset_index()
                    st.dataframe(
                        dados_valor,
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "Etapa": st.column_config.TextColumn("Etapa"),
                            "Valor": st.column_config.NumberColumn("Valor", format="R$ %.2f"),
                        },
                    )
                else:
                    st.info("Nenhuma proposta com valor registrado nas etapas ativas do funil.")

            atividades_recentes = atividades_visiveis(dados, 8)
            if atividades_recentes:
                st.subheader("Atividade recente")
                tabela_atividades = pd.DataFrame(atividades_recentes)
                tabela_atividades["criado_em"] = pd.to_datetime(
                    tabela_atividades["criado_em"], errors="coerce", utc=True
                ).dt.strftime("%d/%m/%Y %H:%M")
                tabela_atividades["empresa"] = tabela_atividades["nome_empresa"].fillna("Lead removido")
                st.dataframe(
                    tabela_atividades[["criado_em", "tipo", "empresa", "descricao"]],
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "criado_em": st.column_config.TextColumn("Quando"),
                        "tipo": st.column_config.TextColumn("Evento"),
                        "empresa": st.column_config.TextColumn("Empresa"),
                        "descricao": st.column_config.TextColumn("Detalhes"),
                    },
                )
    else:
        _clique_vazio_dash = render_empty_state(
            "Vamos começar?",
            "Você ainda não tem leads na base. Escolha um caminho para começar a preencher o pipeline.",
            icone="🔍",
            acao_primaria=("Prospectar empresas", "vazio_prospectar"),
            acao_secundaria=("Cadastrar manualmente", "vazio_manual"),
        )
        if _clique_vazio_dash == "Prospectar empresas":
            st.session_state["navegacao_solicitada"] = "Prospecção"
            st.rerun()
        elif _clique_vazio_dash == "Cadastrar manualmente":
            st.session_state["navegacao_solicitada"] = "Nova empresa"
            st.rerun()

if aba_funil:
    dados_funil = leads_visiveis()
    etapas_kanban = [etapa for etapa in STATUS if etapa not in ("Fechado / Contrato", "Descartado")]
    cols = st.columns(len(etapas_kanban))

    for i, etapa in enumerate(etapas_kanban):
        with cols[i]:
            with st.container(key=f"kanban_stage_{i}"):
                leads_na_etapa = dados_funil[dados_funil["status"] == etapa]
                total_valor_etapa = float(leads_na_etapa["valor_proposta"].fillna(0).sum())
                _dot_cor = "#5A6373" if i == 0 else (
                    "#7DD3FC" if i == len(etapas_kanban) - 1 else "#2568FF"
                )
                st.markdown(
                    f"""
                    <div class="kanban-stage-head">
                      <span class="kanban-stage-dot" style="background:{_dot_cor};"></span>
                      <span class="kanban-stage-name">{escape(etapa)}</span>
                      <span class="kanban-stage-count">{len(leads_na_etapa)}</span>
                    </div>
                    <div class="kanban-stage-value">R$ {total_valor_etapa:,.2f} em propostas</div>
                    """,
                    unsafe_allow_html=True,
                )
                if leads_na_etapa.empty:
                    _empty_desc = (
                        "Prospecte empresas ou cadastre manualmente para começar o funil."
                        if i == 0 else
                        f"As empresas aparecerão aqui quando avançarem de {etapas_kanban[i - 1]}."
                    )
                    render_empty_state(
                        f"Nenhuma oportunidade em {etapa}",
                        _empty_desc,
                        compacto=True,
                    )
                for _, lead in leads_na_etapa.sort_values(
                    "pontuacao", ascending=False, na_position="last"
                ).iterrows():
                    _render_kanban_card(lead, etapa)

    _fechados_df = dados_funil[dados_funil["status"] == "Fechado / Contrato"]
    _descartados_df = dados_funil[dados_funil["status"] == "Descartado"]
    if not _fechados_df.empty or not _descartados_df.empty:
        with st.expander(
            f"Fechados ({len(_fechados_df)}) · Descartados ({len(_descartados_df)})"
        ):
            _valor_fechado = float(_fechados_df["valor_proposta"].fillna(0).sum())
            st.caption(f"R$ {_valor_fechado:,.0f} em contratos fechados.")
            _tabela_encerrados = pd.concat([_fechados_df, _descartados_df])[
                ["nome_empresa", "cidade", "status", "valor_proposta", "atualizado_em"]
            ]
            st.dataframe(
                _tabela_encerrados,
                width="stretch",
                hide_index=True,
                column_config={
                    "nome_empresa": st.column_config.TextColumn("Empresa"),
                    "cidade": st.column_config.TextColumn("Cidade"),
                    "status": st.column_config.TextColumn("Etapa"),
                    "valor_proposta": st.column_config.NumberColumn("Valor", format="R$ %.2f"),
                    "atualizado_em": st.column_config.TextColumn("Atualizado em"),
                },
            )

def exibir_diagnostico_fonte(nicho: str):
    """Explica o roteamento somente quando a pessoa decide abrir o detalhe."""
    with st.expander(
        "Como escolhemos a fonte?",
        icon=":material/info:",
        type="compact",
    ):
        st.caption(
            "Bancos, crédito e pagamentos usam Bacen; corretoras usam CVM; "
            "companhias abertas usam B3. Outros nichos usam Google Places quando "
            f"há chave e OpenStreetMap como fallback. O corte automático atual é {LIMIAR_QUALIFICACAO}/100."
        )
        if not nicho:
            st.caption("Informe um nicho para ver qual fonte o roteador prevê.")
            return

        fontes_ideais = roteamento_por_nicho(nicho)
        fontes_reais = resolver_fontes_reais(nicho)
        fonte_principal = fontes_reais[0]
        st.markdown(f"**Fonte prevista para este nicho:** {fonte_principal}")

        explicacoes = {
            "Bacen": "Cadastro público de instituições autorizadas pelo Banco Central.",
            FONTE_CVM: "Cadastro público de corretoras e distribuidoras de valores.",
            FONTE_B3: "Companhias abertas, com enriquecimento cadastral disponível.",
            "Google Places": "Cobertura de negócios locais quando a integração está conectada.",
            FONTE_OSM: "Fonte pública colaborativa, com cobertura variável e sem validação de CNPJ.",
        }
        st.caption(explicacoes.get(fonte_principal, "Fonte disponível para o modo selecionado."))
        if fonte_principal == FONTE_OSM and "Google Places" in fontes_ideais:
            st.caption("O Google Places seria prioritário, mas não está conectado neste ambiente.")

if aba_prospeccao:
    def _sugerir_nicho():
        with st.expander(
            "Sugerir nicho por perfil de cliente (ICP)",
            icon=":material/lightbulb:",
            type="compact",
        ):
            perfil_escolhido = st.selectbox(
                "Selecione um perfil estratégico",
                options=list(PERFIS_ICP.keys()),
                index=None,
                placeholder="Escolha um perfil...",
            )
            if perfil_escolhido:
                sugestao = PERFIS_ICP[perfil_escolhido]["consulta_sugerida"]
                st.info(f"**Sugestão de busca para o nicho:** `{sugestao}`")

    aviso_prospeccao = st.session_state.pop("aviso_prospeccao", None)
    if aviso_prospeccao:
        st.success(aviso_prospeccao)
    c1, c2, c3 = st.columns([2, 2, 1])
    nicho_busca = c1.text_input(
        "Nicho ou segmento",
        placeholder="Ex.: cooperativa de crédito (obrigatório, exceto na fonte Bacen)",
        max_chars=120,
    )
    local_busca = c2.text_input(
        "Município, UF",
        placeholder="Ex.: Campinas, SP",
        max_chars=120,
    )
    limite = c3.number_input("Quantidade", min_value=1, max_value=100, value=8)

    fonte_busca = st.selectbox(
        "Fonte",
        FONTES_PROSPECCAO_MANUAL,
        help=(
            "No modo automático, o nicho define a fonte e apenas candidatos com "
            "localização, algum contato e situação ativa quando a fonte permite confirmá-la "
            "são aprovados."
        ),
    )

    _chips_fonte = [("Google Places", "conectado" if google_places_configurado else "sem chave")]
    _chips_fonte += [(f, "público") for f in ("Bacen", FONTE_CVM, FONTE_B3)]
    _chips_fonte.append((FONTE_OSM, "fallback"))
    st.markdown(
        '<div class="source-chip-row">'
        + "".join(
            f'<span class="chip {"chip-accent" if rotulo == "conectado" else "chip-neutral"}">{nome} · {rotulo}</span>'
            for nome, rotulo in _chips_fonte
        )
        + "</div>",
        unsafe_allow_html=True,
    )
    if not google_places_configurado and fonte_busca == "Google Places":
        st.warning("Preencha `GOOGLE_PLACES_API_KEY` em `.streamlit/secrets.toml` e reinicie o Streamlit.")

    if fonte_busca == FONTE_AUTOMATICA:
        exibir_diagnostico_fonte(nicho_busca.strip())

    _sugerir_nicho()
    if fonte_busca == "Bacen":
        st.caption(
            "Fonte pública sem chave: instituições autorizadas pelo Banco Central. "
            "Não inclui empresas de setores como saúde, varejo ou alimentação."
        )
    if fonte_busca == FONTE_OSM:
        st.caption(
            "Fonte pública sem chave e com cobertura variável. Exige Município, UF e algum "
            "contato público; não comprova CNPJ nem situação ativa na Receita."
        )
    if st.button(
        "Encontrar oportunidades",
        type="primary",
        icon=":material/search:",
        width="content",
    ):
        st.session_state.pop("resultados_busca", None)
        st.session_state.pop("resumo_busca_salva", None)
        st.session_state["busca_prospeccao_executada"] = False
        nicho_obrigatorio = fonte_busca != "Bacen"
        if not local_busca.strip() or (nicho_obrigatorio and not nicho_busca.strip()):
            st.warning("Informe a localização e, para esta fonte, também o nicho.")
        elif limite_de_taxa_excedido("prospeccao_busca", limite=15, janela_segundos=60):
            st.warning("Muitas buscas em pouco tempo. Aguarde um minuto e tente de novo.")
        else:
            with st.spinner("Mapeando possíveis clientes..."):
                try:
                    if fonte_busca == FONTE_AUTOMATICA:
                        encontrados = buscar_leads_automaticamente(
                            nicho_busca.strip(),
                            local_busca.strip(),
                            int(limite),
                            "Busca pela interface",
                        )
                    elif fonte_busca == "Demonstração":
                        encontrados = gerar_demonstracao(nicho_busca.strip(), local_busca.strip(), int(limite))
                    else:
                        encontrados = buscar_leads_por_fonte(
                            fonte_busca,
                            nicho_busca.strip(),
                            local_busca.strip(),
                            int(limite),
                            "Busca pela interface",
                        )
                    st.session_state["resultados_busca"] = encontrados
                    st.session_state["busca_prospeccao_executada"] = True
                except (requests.RequestException, RuntimeError) as erro:
                    st.error(str(erro))
                except ValueError as erro:
                    st.error(str(erro))

    resultados = st.session_state.get("resultados_busca", [])
    resumo_busca_salva = st.session_state.get("resumo_busca_salva")
    if resumo_busca_salva:
        st.caption(
            f"{resumo_busca_salva['encontradas']} encontradas · "
            f"{resumo_busca_salva['novas']} novas · "
            f"{resumo_busca_salva['ja_cadastradas']} já cadastradas"
        )
    if resultados:
        st.markdown(
            f'<div class="section-title" style="margin-bottom:0.2rem;">{len(resultados)} encontrada(s)</div>',
            unsafe_allow_html=True,
        )
        if any(resultado.get("pontuacao") is not None for resultado in resultados):
            origens = sorted({str(resultado.get("origem") or "") for resultado in resultados})
            st.success(
                "Filtro automático concluído. Fonte(s): "
                + ", ".join(origem for origem in origens if origem)
                + "."
            )
        if any("openstreetmap" in str(resultado.get("origem") or "").casefold() for resultado in resultados):
            st.caption(
                "Dados © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), "
                "licença ODbL. Confirme CNPJ, atividade e contatos antes da abordagem."
            )
        tabela_resultados = pd.DataFrame(resultados)
        colunas_exibicao = ["nome_empresa", "nicho", "cidade", "telefone", "email", "site", "pontuacao", "segmento_icp", "servicos_recomendados", "origem"]
        colunas_disponiveis = [col for col in colunas_exibicao if col in tabela_resultados.columns]
        colunas_internas = [
            coluna for coluna in tabela_resultados.columns if str(coluna).startswith("_")
        ]
        tabela_resultados = tabela_resultados.drop(
            columns=["place_id", *colunas_internas], errors="ignore"
        )

        st.dataframe(
            tabela_resultados[colunas_disponiveis],
            width="stretch",
            hide_index=True,
            column_config=COLUNAS_LEAD_LABELS,
        )
        if st.button(
            "Adicionar resultados à base",
            type="primary",
            icon=":material/add_business:",
            width="content",
        ):
            inseridos, duplicados = salvar_leads(resultados)
            st.toast(f"{inseridos} lead(s) adicionado(s) à base.", icon=":material/check_circle:")
            st.session_state["aviso_prospeccao"] = f"{inseridos} lead(s) adicionado(s). {duplicados} duplicado(s) ignorado(s)."
            st.session_state["resumo_busca_salva"] = {
                "encontradas": len(resultados),
                "novas": inseridos,
                "ja_cadastradas": duplicados,
            }
            st.rerun()
    elif st.session_state.get("busca_prospeccao_executada"):
        render_empty_state(
            "Nenhuma empresa encontrada",
            "Tente outro nicho, ajuste a localização ou consulte como a fonte foi escolhida.",
            compacto=True,
        )

@st.dialog("Excluir campanha?")
def _confirmar_exclusao_campanha(campanha_id: int, nome: str) -> None:
    st.markdown(
        f'<p style="font-size:0.82rem;color:#C3CBD8;line-height:1.65;margin:0 0 0.2rem;">'
        f'A campanha "{nome}" e seu histórico de execuções serão removidos. '
        f'Os leads já capturados permanecem na base.</p>',
        unsafe_allow_html=True,
    )
    digitado = st.text_input("Digite o nome da campanha para confirmar", key="digitado_exclusao_campanha")
    confirma_habilitado = digitado.strip().lower() == nome.strip().lower()
    col_cancelar, col_excluir = st.columns(2)
    with col_cancelar:
        if st.button("Cancelar", width="stretch", key="cancelar_exclusao_campanha"):
            st.session_state["confirmar_exclusao_campanha_id"] = None
            st.rerun()
    with col_excluir:
        if st.button(
            "Excluir definitivamente", type="primary", width="stretch",
            disabled=not confirma_habilitado, key="confirmar_exclusao_campanha_botao",
        ):
            excluir_campanha(campanha_id)
            st.session_state["confirmar_exclusao_campanha_id"] = None
            st.session_state["aviso_automacao"] = f"Campanha '{nome}' excluída; o histórico foi preservado."
            st.toast(f"Campanha '{nome}' excluída.", icon=":material/delete:")
            st.rerun()


if aba_automacao:
    estado_worker = status_worker()
    if not estado_worker["online"]:
        render_empty_state(
            "Motor de automação indisponível",
            "Campanhas automáticas não serão executadas enquanto o serviço estiver offline.",
            compacto=True,
        )
        if _pode_gerenciar_campanhas:
            with st.expander(
                "Ver detalhes técnicos",
                icon=":material/terminal:",
                type="compact",
            ):
                st.caption(
                    "O worker roda como subprocesso do próprio app. Se ele não subir sozinho, "
                    "inicie manualmente com `python worker.py` no ambiente do serviço."
                )

    aviso_automacao = st.session_state.pop("aviso_automacao", None)
    if aviso_automacao:
        st.success(aviso_automacao)

    tab_campanhas, tab_motor_continuo, tab_extracao_url = st.tabs(
        ["Campanhas agendadas", "Motor contínuo", "Extração por URL"]
    )

    with tab_campanhas:
        st.markdown(
            '<div class="section-title">Campanhas automáticas</div>'
            '<div class="section-sub">Buscam leads em horários agendados.</div>',
            unsafe_allow_html=True,
        )
        if not _pode_gerenciar_campanhas:
            st.caption("Seu nível tem acesso de leitura às campanhas e ao histórico abaixo.")

        campanhas_atuais = listar_campanhas() # type: ignore
        if campanhas_atuais:
            colunas_campanhas = st.columns(min(3, len(campanhas_atuais)) or 1)
            for indice_campanha, campanha in enumerate(campanhas_atuais):
                ativa = bool(campanha.get("ativa"))
                status_classe = "status-active" if ativa else "status-paused"
                _ultima_execucao_raw = campanha.get("ultima_execucao")
                if _ultima_execucao_raw:
                    try:
                        _ultima_execucao_fmt = pd.to_datetime(_ultima_execucao_raw, utc=True).strftime("%d/%m %H:%M")
                    except (ValueError, TypeError):
                        _ultima_execucao_fmt = str(_ultima_execucao_raw)
                else:
                    _ultima_execucao_fmt = "nunca"
                with colunas_campanhas[indice_campanha % len(colunas_campanhas)]:
                    st.markdown(
                        f"""
                        <div class="campaign-card">
                          <div class="head">
                            <div class="name">{escape(str(campanha['nome']))}</div>
                            <span class="status-badge {status_classe}">{"Ativa" if ativa else "Pausada"}</span>
                          </div>
                          <div class="scope">{escape(str(campanha.get('nicho') or 'Todos'))} · {escape(str(campanha.get('localizacao') or '—'))} · {escape(str(campanha.get('fonte') or '—'))}</div>
                          <div class="metrics">
                            <div><div class="m-label">Limite/dia</div><div class="m-val">{campanha.get('limite_diario')}</div></div>
                            <div><div class="m-label">Horário</div><div class="m-val">{campanha.get('horario')}</div></div>
                            <div><div class="m-label">Última</div><div class="m-val" style="font-family:Inter,sans-serif;font-size:0.78rem;">{_ultima_execucao_fmt}</div></div>
                          </div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    if _pode_gerenciar_campanhas:
                        _cid = campanha["id"]
                        exec_col, pausa_col, exc_col = st.columns(3)
                        if exec_col.button("Executar agora", key=f"exec_camp_{_cid}", width="stretch"):
                            with st.spinner("Executando a campanha..."):
                                resultado_execucao = executar_campanha(_cid)
                            if resultado_execucao["status"] == "Sucesso":
                                st.success(resultado_execucao["mensagem"])
                            elif resultado_execucao["status"] == "Ignorada":
                                st.info(resultado_execucao["mensagem"])
                            else:
                                st.error(resultado_execucao["mensagem"])
                        if pausa_col.button(
                            "Ativar" if not ativa else "Pausar", key=f"toggle_camp_{_cid}", width="stretch"
                        ):
                            ativa_nova = alternar_campanha(_cid)
                            st.session_state["aviso_automacao"] = "Campanha ativada." if ativa_nova else "Campanha pausada."
                            st.rerun()
                        if exc_col.button("Excluir", key=f"del_camp_{_cid}", width="stretch"):
                            st.session_state["confirmar_exclusao_campanha_id"] = _cid
                            st.session_state["confirmar_exclusao_campanha_nome"] = campanha["nome"]
                            st.rerun()

            _campanha_id_para_excluir = st.session_state.get("confirmar_exclusao_campanha_id")
            if _campanha_id_para_excluir:
                _confirmar_exclusao_campanha(
                    int(_campanha_id_para_excluir), st.session_state.get("confirmar_exclusao_campanha_nome", "")
                )
        else:
            _clique_vazio_campanha = render_empty_state(
                "Nenhuma campanha configurada",
                "Crie uma campanha pra buscar leads automaticamente em horários agendados."
                if _pode_gerenciar_campanhas else "Peça a um gerente ou diretor pra configurar a primeira campanha.",
                icone="⚡",
                acao_primaria=("+ Nova campanha", "empty_nova_campanha") if _pode_gerenciar_campanhas else None,
            )
            if _clique_vazio_campanha:
                _dialog_nova_campanha()

        execucoes = listar_execucoes(20)
        if execucoes:
            st.markdown('<div class="section-title">Histórico de execuções</div>', unsafe_allow_html=True)
            tabela_execucoes = pd.DataFrame(execucoes)
            tabela_execucoes["inicio_em"] = pd.to_datetime(
                tabela_execucoes["inicio_em"], errors="coerce", utc=True
            ).dt.strftime("%d/%m/%Y %H:%M")
            tabela_execucoes["status"] = tabela_execucoes["status"].replace(
                {"Sucesso": "Concluída", "Falha": "Erro"}
            )
            st.dataframe(
                tabela_execucoes[
                    ["inicio_em", "campanha_nome", "status", "encontrados", "inseridos", "duplicados", "mensagem"]
                ],
                width="stretch",
                hide_index=True,
                column_config={
                    "inicio_em": st.column_config.TextColumn("Início"),
                    "campanha_nome": st.column_config.TextColumn("Campanha"),
                    "status": st.column_config.TextColumn("Status"),
                    "encontrados": st.column_config.NumberColumn("Encontrados", format="%d"),
                    "inseridos": st.column_config.NumberColumn("Inseridos", format="%d"),
                    "duplicados": st.column_config.NumberColumn("Duplicados", format="%d"),
                    "mensagem": st.column_config.TextColumn("Mensagem"),
                },
            )
        else:
            render_empty_state(
                "Nenhuma execução registrada",
                "O histórico aparecerá aqui depois que uma campanha for executada.",
                compacto=True,
            )

    with tab_motor_continuo:
        col_titulo_alvo, col_acao_alvo = st.columns([3, 1])
        with col_titulo_alvo:
            st.markdown(
                '<div class="section-title">Alvos do motor contínuo</div>'
                '<div class="section-sub">Nichos e localizações buscados automaticamente com as melhores fontes disponíveis.</div>',
                unsafe_allow_html=True,
            )
        if _pode_gerenciar_campanhas:
            with col_acao_alvo:
                if st.button("+ Novo alvo", key="tab_novo_alvo", type="primary", width="content"):
                    _dialog_novo_alvo()

        alvos_atuais = listar_alvos_continuos()
        if alvos_atuais:
            tabela_alvos = pd.DataFrame(alvos_atuais)
            tabela_alvos["ativa"] = tabela_alvos["ativa"].map({1: "Ativo", 0: "Pausado"})
            st.dataframe(
                tabela_alvos[
                    ["id", "nome", "nicho", "localizacao", "ativa", "criado_em"]
                ],
                width="stretch",
                hide_index=True,
                column_config={
                    "id": st.column_config.NumberColumn("ID", width="small"),
                    "nome": st.column_config.TextColumn("Nome"),
                    "nicho": st.column_config.TextColumn("Nicho"),
                    "localizacao": st.column_config.TextColumn("Município, UF"),
                    "ativa": st.column_config.TextColumn("Ativo"),
                    "criado_em": st.column_config.TextColumn("Criado em"),
                },
            )

            if _pode_gerenciar_campanhas:
                opcoes_alvo = {
                    f"#{alvo['id']} — {alvo['nome']}": alvo["id"] for alvo in alvos_atuais
                }
                alvo_escolhido = st.selectbox("Gerenciar alvo", list(opcoes_alvo))
                alvo_escolhido_id = opcoes_alvo[alvo_escolhido]
                alternar_alvo_col, excluir_alvo_col = st.columns(2)
                if alternar_alvo_col.button("Ativar/pausar alvo", width="stretch", key="alternar_alvo"):
                    ativa = alternar_alvo_continuo(alvo_escolhido_id)
                    st.session_state["aviso_automacao"] = "Alvo ativado." if ativa else "Alvo pausado."
                    st.rerun()
                if excluir_alvo_col.button("Excluir alvo", width="stretch", key="excluir_alvo"):
                    excluir_alvo_continuo(alvo_escolhido_id)
                    st.session_state["aviso_automacao"] = "Alvo excluído."
                    st.rerun()
        else:
            _clique_vazio_alvo = render_empty_state(
                "Nenhum alvo contínuo configurado",
                "Adicione um alvo pra o motor buscar leads sozinho, sem precisar agendar horário."
                if _pode_gerenciar_campanhas else "Peça a um gerente ou diretor pra configurar o primeiro alvo.",
                icone="🎯",
                acao_primaria=("+ Novo alvo", "empty_novo_alvo") if _pode_gerenciar_campanhas else None,
            )
            if _clique_vazio_alvo:
                _dialog_novo_alvo()

    with tab_extracao_url:
        st.markdown(
            '<div class="section-title">Extração complementar por URL</div>'
            '<div class="section-sub">Extrai CNPJs do texto visível de uma página e mantém somente empresas ativas na BrasilAPI.</div>',
            unsafe_allow_html=True,
        )
        st.info("Use apenas páginas públicas que você tenha autorização para consultar e respeite os termos e regras do site.")
        url_col, limite_col = st.columns([4, 1])
        url_rastreamento = url_col.text_input(
            "URL da página",
            placeholder="https://exemplo.com.br/diretorio-de-empresas",
        )
        limite_rastreamento = limite_col.number_input(
            "Máximo de CNPJs",
            min_value=1, 
            max_value=100,
            value=20,
            help="Limita o número de consultas feitas à API pública por execução.",
        )
        if st.button("Encontrar empresas na página", type="primary", width="content"):
            if not url_rastreamento.strip():
                st.warning("Informe a URL que será analisada.")
            elif limite_de_taxa_excedido("extracao_url", limite=5, janela_segundos=60):
                st.warning("Muitas extrações em pouco tempo. Aguarde um minuto e tente de novo.")
            else:
                with st.spinner("Extraindo CNPJs e qualificando empresas ativas..."):
                    resultado_robo = robo_prospeccao_scorpions(
                        url_rastreamento,
                        limite=int(limite_rastreamento),
                    )
                if isinstance(resultado_robo, dict):
                    st.session_state.pop("resultados_robo", None)
                    st.error(resultado_robo["erro"])
                else:
                    st.session_state["resultados_robo"] = resultado_robo
                    st.success(f"{len(resultado_robo)} empresa(s) ativa(s) qualificada(s).")

        resultados_robo = st.session_state.get("resultados_robo", [])
        if resultados_robo:
            tabela_robo = pd.DataFrame(resultados_robo).drop(columns=["place_id"], errors="ignore")
            if "cnpj" in tabela_robo:
                tabela_robo["cnpj"] = tabela_robo["cnpj"].map(formatar_cnpj)
            st.dataframe(
                tabela_robo, width="stretch", hide_index=True, column_config=COLUNAS_LEAD_LABELS
            )
            if st.button("Salvar leads ativos na base", key="salvar_resultados_robo"):
                inseridos, duplicados = salvar_leads(resultados_robo)
                st.success(f"{inseridos} lead(s) salvo(s). {duplicados} duplicado(s) ignorado(s).")

@st.dialog("Excluir empresa?")
def _confirmar_exclusao_lead(lead_id: int, nome: str, cidade: str, etapa: str) -> None:
    st.markdown(
        f'<p style="font-size:0.82rem;color:#C3CBD8;line-height:1.65;margin:0 0 0.2rem;">'
        f'Você vai excluir <strong>{nome}</strong> (#{lead_id} · {cidade or "—"}), hoje em {etapa}. '
        f'Esta ação não pode ser desfeita.</p>',
        unsafe_allow_html=True,
    )
    digitado = st.text_input("Digite o nome da empresa para confirmar", key="digitado_exclusao_lead")
    confirma_habilitado = digitado.strip().lower() == nome.strip().lower()
    col_cancelar, col_excluir = st.columns(2)
    with col_cancelar:
        if st.button("Cancelar", width="stretch", key="cancelar_exclusao_lead"):
            st.session_state["confirmar_exclusao_lead_id"] = None
            st.rerun()
    with col_excluir:
        if st.button(
            "Excluir definitivamente", type="primary", width="stretch",
            disabled=not confirma_habilitado, key="confirmar_exclusao_lead_botao",
        ):
            excluir_lead(lead_id)
            st.session_state["confirmar_exclusao_lead_id"] = None
            st.session_state["aviso_empresa"] = f"'{nome}' excluído da base."
            st.rerun()


if aba_base:
    aviso_empresa = st.session_state.pop("aviso_empresa", None)
    if aviso_empresa:
        st.success(aviso_empresa)

    todos = leads_visiveis()
    nichos = ["Todos"] + (sorted(todos["nicho"].dropna().unique().tolist()) if not todos.empty else [])
    responsaveis = ["Todos", "Sem responsável"] + (
        sorted(todos["responsavel_nome"].dropna().astype(str).unique().tolist())
        if not todos.empty else []
    )
    f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
    # Chave versionada: popar "busca_empresas" sozinho (ex.: botão "Limpar
    # filtros") não garante, na prática, que o widget já renderizado volte a
    # ficar vazio no mesmo rerun — mesma ressalva já documentada no formulário
    # de Nova Empresa. Trocar a chave força o widget a nascer de novo. O nome
    # estável "busca_empresas" continua funcionando como valor inicial pra
    # quem escreve nele de fora (busca global da sidebar, card do Pipeline).
    _versao_busca_empresas = st.session_state.get("versao_busca_empresas", 0)
    termo = f1.text_input(
        "Pesquisar na base",
        placeholder="Empresa, cidade ou CNPJ",
        value=st.session_state.get("busca_empresas", ""),
        key=f"busca_empresas_{_versao_busca_empresas}",
    )
    filtro_nicho = f2.selectbox("Filtrar nicho", nichos, key="filtro_nicho_empresas")
    filtro_status = f3.selectbox("Filtrar status", ["Todos"] + STATUS, key="filtro_status_empresas")
    filtro_responsavel = f4.selectbox(
        "Filtrar responsável", responsaveis, key="filtro_responsavel_empresas"
    )
    base = leads_visiveis(termo, filtro_nicho, filtro_status)
    if filtro_responsavel == "Sem responsável":
        base = base[base["responsavel_nome"].isna()].copy()
    elif filtro_responsavel != "Todos":
        base = base[base["responsavel_nome"] == filtro_responsavel].copy()
    if "proximo_contato" in base.columns:
        # Sem isso, células sem data mostram o texto literal "None" em vez de
        # ficarem em branco — DateColumn só reconhece NaT como vazio.
        base["proximo_contato"] = pd.to_datetime(base["proximo_contato"], errors="coerce")

    if todos.empty:
        _clique_vazio_clientes = render_empty_state(
            "Sua base ainda está vazia",
            "Prospecte empresas automaticamente ou faça seu primeiro cadastro.",
            acao_primaria=("Prospectar empresas", "clientes_vazio_prospectar"),
            acao_secundaria=("+ Nova empresa", "clientes_vazio_nova"),
        )
        if _clique_vazio_clientes == "Prospectar empresas":
            st.session_state["navegacao_solicitada"] = "Prospecção"
            st.rerun()
        elif _clique_vazio_clientes == "+ Nova empresa":
            st.session_state["navegacao_solicitada"] = "Nova empresa"
            st.rerun()
    elif base.empty:
        _clique_sem_resultado = render_empty_state(
            "Nenhuma empresa encontrada",
            "Tente alterar os filtros ou pesquisar por outro termo.",
            acao_primaria=("Limpar filtros", "clientes_limpar_filtros"),
        )
        if _clique_sem_resultado:
            for _chave_filtro in (
                "busca_empresas",
                "filtro_nicho_empresas",
                "filtro_status_empresas",
                "filtro_responsavel_empresas",
            ):
                st.session_state.pop(_chave_filtro, None)
            st.session_state["versao_busca_empresas"] = st.session_state.get("versao_busca_empresas", 0) + 1
            st.rerun()
    else:
        st.caption(f"{len(base)} de {len(todos)} empresas no recorte atual.")
        if base["origem"].astype(str).str.contains("OpenStreetMap", case=False, na=False).any():
            st.caption(
                "Parte desta base contém dados © "
                "[OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL."
            )
        colunas = [
            "id", "cnpj", "nome_empresa", "razao_social", "decisor", "nicho",
            "segmento_icp", "servicos_recomendados", "valor_proposta", "proximo_contato", "endereco", "cidade",
            "telefone", "site", "email", "status", "responsavel_nome",
            "status_receita", "origem", "pontuacao", "motivo_qualificacao", "observacoes",
        ]
        # A grade mostra só os campos essenciais (o resto fica em "Detalhes completos"
        # abaixo) para evitar a rolagem horizontal densa de antes; os campos ocultos
        # continuam presentes nos dados e participam normalmente da atualização.
        colunas_visiveis = [
            "nome_empresa", "cidade", "nicho", "segmento_icp",
            "pontuacao", "status", "valor_proposta", "proximo_contato", "responsavel_nome",
        ]
        # Cópia só para exibição: campos vazios chegam do banco como None (não
        # NaN/NaT), e Number/DateColumn mostram esse None cru como texto "None"
        # em vez de célula vazia. `atualizar_leads` compara via
        # `_valor_para_str_canonico`, que trata None e NaN da mesma forma
        # (ambos viram ""), então isso não afeta o que é salvo -- só a exibição.
        base_editor = base[colunas].copy()
        base_editor["valor_proposta"] = pd.to_numeric(base_editor["valor_proposta"], errors="coerce")
        base_editor["pontuacao"] = pd.to_numeric(base_editor["pontuacao"], errors="coerce")
        base_editor["proximo_contato"] = pd.to_datetime(base_editor["proximo_contato"], errors="coerce")
        for _coluna_texto_vazia in ("segmento_icp", "responsavel_nome"):
            base_editor[_coluna_texto_vazia] = base_editor[_coluna_texto_vazia].fillna("")

        editado = st.data_editor(
            base_editor,
            width="stretch",
            hide_index=True,
            column_order=colunas_visiveis,
            disabled=[
                "id", "cnpj", "status_receita", "origem", "pontuacao",
                "motivo_qualificacao", "segmento_icp", "servicos_recomendados", "responsavel_nome",
            ],
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "cnpj": st.column_config.TextColumn("CNPJ"),
                "nome_empresa": st.column_config.TextColumn("Empresa"),
                "responsavel_nome": st.column_config.TextColumn("Responsável"),
                "pontuacao": st.column_config.NumberColumn(
                    "Score", min_value=0, max_value=100, format="%d"
                ),
                "motivo_qualificacao": st.column_config.TextColumn("Motivo da qualificação"),
                "segmento_icp": st.column_config.TextColumn("Segmento ICP"),
                "servicos_recomendados": st.column_config.TextColumn("Serviços Recomendados"),
                "valor_proposta": st.column_config.NumberColumn(
                    "Proposta",
                    format="R$ %.2f",
                    min_value=0.0,
                ),
                "status": st.column_config.SelectboxColumn("Etapa", options=STATUS, required=True),
                "proximo_contato": st.column_config.DateColumn(
                    "Próximo contato",
                    format="DD/MM/YYYY",
                    help="Data para o próximo follow-up com o lead.",
                ),
                "site": st.column_config.LinkColumn("Site"),
            },
            key="editor_leads",
        )
        if st.button(
            "Salvar alterações",
            type="primary",
            icon=":material/save:",
            width="content",
        ):
            quantidade = atualizar_leads(editado, base[colunas])
            st.success(f"{quantidade} lead(s) atualizado(s).")
            st.rerun()

        opcoes_empresas = {
            f"{linha['nome_empresa']} — #{linha['id']} · {linha['cidade'] or '—'} · {linha['status']}": int(linha["id"])
            for _, linha in base.iterrows()
        }

        with st.expander(
            "Detalhes completos de uma empresa",
            icon=":material/business:",
            type="compact",
        ):
            rotulo_detalhe = st.selectbox(
                "Selecione a empresa", ["Selecione..."] + list(opcoes_empresas.keys()), key="detalhe_empresa_select"
            )
            if rotulo_detalhe != "Selecione...":
                linha_detalhe = base[base["id"] == opcoes_empresas[rotulo_detalhe]].iloc[0]
                campos_detalhe = [
                    ("Responsável", linha_detalhe.get("responsavel_nome") or "sem responsável"),
                    ("Razão social", linha_detalhe.get("razao_social") or "não informada"),
                    ("CNPJ", formatar_cnpj(linha_detalhe["cnpj"]) if linha_detalhe.get("cnpj") else "pendente de consulta"),
                    ("Decisor", linha_detalhe.get("decisor") or "não mapeado"),
                    ("Endereço", linha_detalhe.get("endereco") or "não informado"),
                    (
                        "Contato",
                        " · ".join(filter(None, [linha_detalhe.get("telefone"), linha_detalhe.get("site")])) or "sem contato",
                    ),
                    ("Serviços recomendados", linha_detalhe.get("servicos_recomendados") or "—"),
                    ("E-mail", linha_detalhe.get("email") or "não informado"),
                    ("Observações", linha_detalhe.get("observacoes") or "—"),
                ]
                cols_detalhe = st.columns(2)
                for indice, (rotulo, valor) in enumerate(campos_detalhe):
                    with cols_detalhe[indice % 2]:
                        st.markdown(
                            f'<div class="detail-item"><div class="detail-label">{escape(str(rotulo))}</div>'
                            f'<div class="detail-value">{escape(str(valor))}</div></div>',
                            unsafe_allow_html=True,
                        )

        csv_data = base.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Exportar visão atual para CSV",
            data=csv_data,
            file_name=f"leads_scorpions_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            icon=":material/download:",
            width="content",
        )

        st.markdown('<div class="section-title">Ferramentas da base</div>', unsafe_allow_html=True)
        _pode_excluir = bool(pode(st.session_state.get("nivel_usuario"), "pode_excluir_leads"))
        col_cnpj_card, col_delete_card = st.columns(2) if _pode_excluir else (st.container(), None)
        with col_cnpj_card:
            with st.container(key="cnpj_card"):
                st.markdown(
                    '<div class="card-title">Consulta cadastral · BrasilAPI</div>'
                    '<p class="card-note">Dados públicos, sem chave. Preenche razão social, endereço e CNAE.</p>',
                    unsafe_allow_html=True,
                )
                col_cnpj_input, col_cnpj_botao = st.columns([2, 1])
                cnpj_informado = col_cnpj_input.text_input(
                    "CNPJ", placeholder="00.000.000/0001-91", max_chars=18, label_visibility="collapsed",
                )
                consultar_clicado = col_cnpj_botao.button("Consultar", type="primary", width="stretch")
                if consultar_clicado and limite_de_taxa_excedido("consulta_cnpj", limite=20, janela_segundos=60):
                    st.warning("Muitas consultas em pouco tempo. Aguarde um minuto e tente de novo.")
                elif consultar_clicado:
                    with st.spinner("Consultando a BrasilAPI..."):
                        resultado_cnpj = consultar_empresa_brasilapi(cnpj_informado)
                    if "erro" in resultado_cnpj:
                        st.session_state.pop("resultado_cnpj", None)
                        st.error(resultado_cnpj["erro"])
                    else:
                        st.session_state["resultado_cnpj"] = enriquecer_lead_icp(resultado_cnpj)
                        st.success("Empresa localizada.")

                empresa_consultada = st.session_state.get("resultado_cnpj")
                if empresa_consultada:
                    st.markdown(f"**{empresa_consultada.get('nome_empresa', '')}**")
                    st.caption(
                        f"CNPJ: {formatar_cnpj(empresa_consultada.get('cnpj', ''))} · "
                        f"Situação: {empresa_consultada.get('status_receita', 'N/A')} · "
                        f"ICP: {empresa_consultada.get('segmento_icp', 'Não classificado')}"
                    )
                    with st.expander("Dados cadastrais completos"):
                        st.markdown(f"**Razão social:** {empresa_consultada.get('razao_social', 'N/A')}")
                        st.markdown(f"**Atividade (CNAE):** {empresa_consultada.get('nicho', 'N/A')}")
                        st.markdown(f"**Contato societário (QSA):** {empresa_consultada.get('decisor', 'N/A')}")
                        st.markdown(f"**Endereço:** {empresa_consultada.get('endereco', 'N/A')}")
                        st.markdown(f"**Telefone:** {empresa_consultada.get('telefone') or 'N/A'}")
                        st.markdown(f"**E-mail:** {empresa_consultada.get('email') or 'N/A'}")
                    if st.button("Adicionar empresa à base", key="adicionar_empresa_cnpj"):
                        inseridos, duplicados = salvar_leads([empresa_consultada])
                        if inseridos:
                            st.cache_data.clear()
                            st.session_state["aviso_empresa"] = "Empresa adicionada ao pipeline."
                            st.rerun()
                        elif duplicados:
                            st.info("Essa empresa já está cadastrada na base.")

        if _pode_excluir:
            with col_delete_card:
                with st.container(key="delete_card"):
                    st.markdown(
                        '<div class="card-title danger">Excluir registro</div>'
                        '<p class="card-note">O seletor mostra nome, cidade e etapa — nunca só o ID.</p>',
                        unsafe_allow_html=True,
                    )
                    rotulo_excluir = st.selectbox(
                        "Excluir lead", ["Selecione..."] + list(opcoes_empresas.keys()),
                        key="excluir_lead_select", label_visibility="collapsed",
                    )
                    if rotulo_excluir != "Selecione..." and st.button(
                        "Excluir", key="abrir_confirmacao_exclusao", width="stretch"
                    ):
                        st.session_state["confirmar_exclusao_lead_id"] = opcoes_empresas[rotulo_excluir]
                        st.rerun()

        _lead_id_para_excluir = st.session_state.get("confirmar_exclusao_lead_id") if _pode_excluir else None
        if _lead_id_para_excluir:
            _linha_alvo = base[base["id"] == _lead_id_para_excluir]
            if _linha_alvo.empty:
                st.session_state["confirmar_exclusao_lead_id"] = None
            else:
                _linha_alvo = _linha_alvo.iloc[0]
                _confirmar_exclusao_lead(
                    int(_lead_id_para_excluir), _linha_alvo["nome_empresa"], _linha_alvo.get("cidade"), _linha_alvo["status"]
                )

if aba_contato:
    st.info(
        "Isso só gera o link do WhatsApp já com a mensagem preenchida — "
        "você confere e clica pra enviar. Nada é disparado automaticamente. "
        "Nem todo telefone comercial tem WhatsApp (muitos são fixos); confirme antes de mandar."
    )

    _modelo_padrao = (
        "Olá! Sou da Scorpions e ajudamos empresas como a {empresa} a crescer. "
        "Podemos conversar rapidinho sobre como podemos ajudar? "
        "Se preferir não receber mais contatos, é só responder SAIR."
    )
    _modelo_atual = ler_config("modelo_mensagem_whatsapp", _modelo_padrao)
    with st.expander("Modelo de mensagem", icon=":material/edit_note:"):
        st.caption("Use {empresa}, {cidade}, {nicho} e {decisor} — são preenchidos por lead.")
        _novo_modelo = st.text_area("Mensagem", value=_modelo_atual, height=120, key="modelo_mensagem_input")
        if st.button("Salvar modelo", key="salvar_modelo_mensagem"):
            salvar_config("modelo_mensagem_whatsapp", _novo_modelo)
            st.toast("Modelo salvo.", icon=":material/check_circle:")
            st.rerun()

    _base_contato = leads_visiveis()
    _contatados_recentes = leads_ja_contatados_ha_dias(14)
    _elegiveis = []
    for _, _linha in _base_contato.iterrows():
        _telefone = str(_linha.get("telefone") or "").strip()
        if not _telefone:
            continue
        if int(_linha["id"]) in _contatados_recentes:
            continue
        if telefone_suprimido(_telefone):
            continue
        _elegiveis.append(_linha)

    st.markdown(
        f'<div class="section-title" style="margin-top:0.6rem;">'
        f'Próximos contatos ({min(10, len(_elegiveis))} de {len(_elegiveis)} elegíveis)</div>',
        unsafe_allow_html=True,
    )

    if not _elegiveis:
        render_empty_state(
            "Nenhum lead elegível pra contato agora",
            "Isso acontece quando não há telefone cadastrado, o lead já foi "
            "contatado nos últimos 14 dias, ou o número está na lista de supressão.",
            icone="💬",
            compacto=True,
        )
    else:
        for _lead_contato in _elegiveis[:10]:
            _mensagem = _modelo_atual.format(
                empresa=_lead_contato.get("nome_empresa") or "",
                cidade=_lead_contato.get("cidade") or "",
                nicho=_lead_contato.get("nicho") or "",
                decisor=_lead_contato.get("decisor") or "",
            )
            _link = gerar_link_whatsapp(_lead_contato["telefone"], _mensagem)
            with st.container(key=f"contato_card_{int(_lead_contato['id'])}"):
                st.markdown(
                    f"""
                    <div class="campaign-card">
                      <div class="head"><div class="name">{escape(str(_lead_contato['nome_empresa']))}</div></div>
                      <div class="scope">{escape(str(_lead_contato.get('cidade') or '—'))} · {escape(str(_lead_contato.get('telefone') or '—'))}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                st.caption(_mensagem)
                _col_link, _col_marcar, _col_suprimir = st.columns([1.3, 1, 1])
                if _link:
                    _col_link.link_button("Abrir no WhatsApp", _link, width="stretch")
                else:
                    _col_link.caption("Telefone inválido para link.")
                if _col_marcar.button("Marcar como enviado", key=f"marcar_enviado_{_lead_contato['id']}", width="stretch"):
                    registrar_mensagem_enviada(
                        int(_lead_contato["id"]), _lead_contato["telefone"], _mensagem,
                        st.session_state.get("usuario_logado", "sistema"),
                    )
                    st.rerun()
                if _col_suprimir.button("Não contatar mais", key=f"suprimir_{_lead_contato['id']}", width="stretch"):
                    adicionar_supressao(
                        _lead_contato["telefone"], "Solicitado pelo lead",
                        st.session_state.get("usuario_logado", "sistema"),
                    )
                    st.rerun()

    with st.expander("Lista de supressão (não contatar)", icon=":material/block:"):
        _supressoes = listar_supressao()
        if not _supressoes:
            st.caption("Nenhum número na lista de supressão ainda.")
        else:
            st.dataframe(
                pd.DataFrame(_supressoes)[["telefone", "motivo", "usuario", "criado_em"]],
                width="stretch",
                hide_index=True,
                column_config={
                    "telefone": st.column_config.TextColumn("Telefone"),
                    "motivo": st.column_config.TextColumn("Motivo"),
                    "usuario": st.column_config.TextColumn("Registrado por"),
                    "criado_em": st.column_config.TextColumn("Quando"),
                },
            )
        _col_add_tel, _col_add_motivo, _col_add_btn = st.columns([1, 2, 1])
        _tel_supressao = _col_add_tel.text_input("Telefone", key="supressao_tel_input", label_visibility="collapsed", placeholder="Telefone")
        _motivo_supressao = _col_add_motivo.text_input("Motivo", key="supressao_motivo_input", label_visibility="collapsed", placeholder="Motivo (opcional)")
        if _col_add_btn.button("Adicionar", key="supressao_add_btn", width="stretch"):
            if _tel_supressao.strip():
                adicionar_supressao(_tel_supressao, _motivo_supressao, st.session_state.get("usuario_logado", "sistema"))
                st.rerun()
            else:
                st.warning("Informe um telefone.")

def _fmt_data_radar(valor: Any) -> str:
    if not valor:
        return "—"
    convertido = pd.to_datetime(valor, errors="coerce", utc=True)
    return convertido.strftime("%d/%m/%Y %H:%M") if pd.notna(convertido) else str(valor)


if aba_radar:
    _base_radar = leads_visiveis()
    _com_score = _base_radar[_base_radar["opportunity_score"].notna()].copy() if not _base_radar.empty else _base_radar

    if _com_score.empty:
        render_empty_state(
            "Ainda sem oportunidades calculadas",
            "O worker calcula o Opportunity Score em segundo plano, em lotes — "
            "assim que ele processar as empresas da base, elas aparecem aqui.",
            icone="📡",
        )
    else:
        _col_f1, _col_f2, _col_f3, _col_f4 = st.columns([1.2, 1.2, 1, 1])
        _nichos_radar = ["Todos"] + sorted(_com_score["nicho"].dropna().unique().tolist())
        _nicho_radar = _col_f1.selectbox("Nicho", _nichos_radar, key="radar_filtro_nicho")
        _cidade_radar = _col_f2.text_input("Cidade/região", key="radar_filtro_cidade", placeholder="Filtrar por cidade")
        _score_minimo_radar = _col_f3.slider("Score mínimo", 0, 100, 0, key="radar_filtro_score")
        _niveis_radar = ["Todos"] + [nivel for _, nivel in NIVEIS_OPORTUNIDADE]
        _nivel_radar = _col_f4.selectbox("Nível", _niveis_radar, key="radar_filtro_nivel")

        _filtrado = _com_score
        if _nicho_radar != "Todos":
            _filtrado = _filtrado[_filtrado["nicho"] == _nicho_radar]
        if _cidade_radar.strip():
            _filtrado = _filtrado[_filtrado["cidade"].fillna("").str.contains(_cidade_radar.strip(), case=False)]
        if _score_minimo_radar:
            _filtrado = _filtrado[_filtrado["opportunity_score"] >= _score_minimo_radar]
        if _nivel_radar != "Todos":
            _filtrado = _filtrado[_filtrado["opportunity_level"] == _nivel_radar]
        _filtrado = _filtrado.sort_values(
            by=["opportunity_score", "opportunity_delta"], ascending=[False, False]
        )

        st.markdown(
            f'<div class="section-title">{len(_filtrado)} oportunidade(s) '
            f'de {len(_com_score)} já avaliadas</div>',
            unsafe_allow_html=True,
        )

        if _filtrado.empty:
            render_empty_state(
                "Nenhuma oportunidade com esses filtros",
                "Ajuste o nicho, a cidade, o score mínimo ou o nível para ver mais resultados.",
                icone="🔍",
                compacto=True,
            )
        else:
            # "Why Now" e "Atualizado" ficam só no painel de detalhe abaixo --
            # numa tabela de várias linhas, a frase inteira repetida empurra
            # Fit/Intent/Timing pra fora da tela sem agregar nada de novo.
            _tabela_radar = _filtrado[[
                "id", "nome_empresa", "opportunity_score", "opportunity_delta", "opportunity_level",
                "fit_score", "intent_score", "timing_score", "last_signal_at", "responsavel_nome",
            ]].copy()
            _tabela_radar["last_signal_at"] = pd.to_datetime(
                _tabela_radar["last_signal_at"], errors="coerce", utc=True
            ).dt.strftime("%d/%m/%Y %H:%M").fillna("—")
            _tabela_radar["responsavel_nome"] = _tabela_radar["responsavel_nome"].fillna("—")
            _tabela_radar = _tabela_radar.rename(columns={
                "nome_empresa": "Empresa",
                "opportunity_score": "Opportunity Score",
                "opportunity_delta": "Delta",
                "opportunity_level": "Nível",
                "fit_score": "Fit",
                "intent_score": "Intent",
                "timing_score": "Timing",
                "last_signal_at": "Último sinal",
                "responsavel_nome": "Responsável",
            })
            st.dataframe(
                _tabela_radar.drop(columns=["id"]),
                width="stretch",
                hide_index=True,
            )

            _opcoes_detalhe = {
                f"{linha['nome_empresa']} · Score {int(linha['opportunity_score'])}": int(linha["id"])
                for _, linha in _filtrado.iterrows()
            }
            _rotulo_detalhe = st.selectbox(
                "Ver detalhe da oportunidade", list(_opcoes_detalhe), key="radar_detalhe_selecionado"
            )
            if _rotulo_detalhe:
                _lead_id_detalhe = _opcoes_detalhe[_rotulo_detalhe]
                _linha_detalhe = _filtrado[_filtrado["id"] == _lead_id_detalhe].iloc[0]
                _sinais_detalhe = listar_signals_ativos(_lead_id_detalhe)

                st.markdown(
                    f"""
                    <div class="campaign-card">
                      <div class="head"><div class="name">{escape(str(_linha_detalhe['nome_empresa']))}</div></div>
                      <div class="scope">Opportunity Score {int(_linha_detalhe['opportunity_score'])} · {escape(str(_linha_detalhe['opportunity_level'] or ''))}</div>
                      <div class="metrics">
                        <div><div class="m-label">Fit</div><div class="m-val">{int(_linha_detalhe['fit_score'] or 0)}</div></div>
                        <div><div class="m-label">Intent</div><div class="m-val">{int(_linha_detalhe['intent_score'] or 0)}</div></div>
                        <div><div class="m-label">Timing</div><div class="m-val">{int(_linha_detalhe['timing_score'] or 0)}</div></div>
                        <div><div class="m-label">Confiança</div><div class="m-val">{int(_linha_detalhe['data_confidence_score'] or 0)}</div></div>
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                st.markdown(f"**Why this company:** {escape(str(_linha_detalhe.get('opportunity_reason') or '—'))}")
                st.markdown(f"**Why now:** {escape(str(_linha_detalhe.get('why_now') or '—'))}")

                with st.expander("Evidências", icon=":material/fact_check:"):
                    if not _sinais_detalhe:
                        st.caption("Nenhum sinal ativo com evidência registrada ainda.")
                    for _sinal in _sinais_detalhe:
                        st.caption(
                            f"[{escape(str(_sinal.get('source') or '—'))} · {escape(_fmt_data_radar(_sinal.get('detected_at')))}] "
                            f"{escape(str(_sinal.get('description') or _sinal.get('title') or ''))}"
                        )

                with st.expander("Linha do tempo", icon=":material/timeline:"):
                    _timeline = get_opportunity_timeline(_lead_id_detalhe)
                    if not _timeline:
                        st.caption("Sem histórico registrado ainda.")
                    for _evento in _timeline:
                        st.caption(f"{escape(_fmt_data_radar(_evento['data']))} — {escape(_evento['descricao'])}")

                _proxima_acao = recommend_next_action(
                    str(_linha_detalhe.get("opportunity_level") or "Baixa"),
                    int(_linha_detalhe.get("timing_score") or 0),
                    int(_linha_detalhe.get("data_confidence_score") or 0),
                    _linha_detalhe.to_dict(),
                )
                st.markdown(f"**Próxima melhor ação:** {escape(_proxima_acao)}")

if aba_manual:
    aviso_nova_empresa = st.session_state.pop("aviso_nova_empresa", None)
    if aviso_nova_empresa:
        st.markdown(
            f'<span class="chip chip-accent" style="font-size:0.78rem;padding:0.35rem 0.7rem;">{aviso_nova_empresa}</span>',
            unsafe_allow_html=True,
        )

    # As chaves dos campos levam um sufixo de "versão" que só muda após um
    # cadastro bem-sucedido (ou uma consulta de CNPJ com sucesso): popar
    # st.session_state de um text_input não garante, na prática, que o widget
    # volte a ficar vazio/pré-preenchido. Trocar a chave força o widget a
    # nascer de novo, sem estado anterior.
    _versao_form_nova = st.session_state.get("versao_form_nova_empresa", 0)

    def _campo_nova(nome_campo: str) -> str:
        return f"novo_empresa_{nome_campo}_{_versao_form_nova}"

    _prefill = st.session_state.get("nova_empresa_prefill") or {}
    st.markdown('<div class="section-title">1. Identificação</div>', unsafe_allow_html=True)
    col_cnpj_busca, col_cnpj_consultar = st.columns([3, 1])
    cnpj_busca_manual = col_cnpj_busca.text_input(
        "CNPJ",
        value=str(_prefill.get("cnpj") or ""),
        placeholder="00.000.000/0001-00",
        key=_campo_nova("cnpj_busca"),
        label_visibility="collapsed",
    )
    with col_cnpj_consultar:
        _consultar_cnpj_clicado = st.button(
            "Consultar CNPJ", key="nova_empresa_consultar_cnpj", type="primary", width="stretch"
        )
    if _consultar_cnpj_clicado:
        if not cnpj_busca_manual.strip():
            st.warning("Informe um CNPJ para consultar.")
        elif limite_de_taxa_excedido("consulta_cnpj", limite=20, janela_segundos=60):
            st.warning("Muitas consultas em pouco tempo. Aguarde um minuto e tente de novo.")
        else:
            with st.spinner("Consultando a BrasilAPI..."):
                _resultado_cnpj_manual = consultar_empresa_brasilapi(cnpj_busca_manual)
            if "erro" in _resultado_cnpj_manual:
                st.error(_resultado_cnpj_manual["erro"])
            else:
                st.session_state["nova_empresa_prefill"] = enriquecer_lead_icp(_resultado_cnpj_manual)
                st.session_state["versao_form_nova_empresa"] = _versao_form_nova + 1
                st.rerun()

    if _prefill:
        st.success(
            f"Empresa localizada: {_prefill.get('nome_empresa', '')}",
            icon=":material/check_circle:",
        )

    _erro_nome = st.session_state.get("erro_novo_empresa_nome", False)
    _erro_nicho = st.session_state.get("erro_novo_empresa_nicho", False)
    if _erro_nome or _erro_nicho:
        _seletores_erro = []
        if _erro_nome:
            _seletores_erro.append(f".st-key-{_campo_nova('nome')} input")
        if _erro_nicho:
            _seletores_erro.append(f".st-key-{_campo_nova('nicho')} input")
        st.markdown(
            f"<style>{', '.join(_seletores_erro)} {{ border-color: rgba(255,107,107,.55) !important; }}</style>",
            unsafe_allow_html=True,
        )

    with st.form(f"cadastro_manual_{_versao_form_nova}", clear_on_submit=False):
        st.markdown('<div class="section-title">2. Dados da empresa</div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        nome = c1.text_input("Nome fantasia *", value=_prefill.get("nome_empresa", ""), key=_campo_nova("nome"))
        if _erro_nome:
            c1.markdown('<div style="font-size:0.72rem;color:#ff8f8f;margin-top:-0.6rem;">Obrigatório para salvar o registro.</div>', unsafe_allow_html=True)
        razao_social = c2.text_input("Razão social", value=_prefill.get("razao_social", ""), key=_campo_nova("razao"))
        cidade = c1.text_input("Cidade / UF", value=_prefill.get("cidade", ""), key=_campo_nova("cidade"))
        site = c2.text_input("Site", value=_prefill.get("site", ""), key=_campo_nova("site"))
        endereco = st.text_input("Endereço", value=_prefill.get("endereco", ""), key=_campo_nova("endereco"))
        cnpj_manual = str(_prefill.get("cnpj") or cnpj_busca_manual)

        st.markdown('<div class="section-title" style="margin-top:0.5rem;">3. Dados comerciais</div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        nicho = c1.text_input("Nicho *", value=_prefill.get("nicho", ""), key=_campo_nova("nicho"))
        if _erro_nicho:
            c1.markdown('<div style="font-size:0.72rem;color:#ff8f8f;margin-top:-0.6rem;">Obrigatório — define a fonte de prospecção.</div>', unsafe_allow_html=True)
        decisor = c2.text_input("Contato/decisor", value=_prefill.get("decisor", ""), key=_campo_nova("decisor"))
        email = c1.text_input("E-mail", value=_prefill.get("email", ""), key=_campo_nova("email"))
        telefone = c2.text_input("Telefone", value=_prefill.get("telefone", ""), key=_campo_nova("telefone"))
        observacoes = st.text_area("Observações", key=_campo_nova("obs"))

        with st.expander(
            "Dados do pipeline (opcional)",
            icon=":material/tune:",
            type="compact",
        ):
            c1, c2 = st.columns(2)
            status_manual = c1.selectbox("Etapa", STATUS, key=_campo_nova("status"))
            valor_proposta_manual = c2.number_input(
                "Valor da proposta (R$)",
                min_value=0.0,
                value=0.0,
                format="%.2f",
                key=_campo_nova("valor"),
            )
            proximo_contato_manual = c1.date_input(
                "Próximo contato", value=None, key=_campo_nova("proximo")
            )
            segmento_icp_manual = c2.selectbox(
                "Segmento ICP (opcional)",
                options=[""] + list(PERFIS_ICP.keys()),
                help="Se deixado em branco, será classificado automaticamente.",
                key=_campo_nova("icp"),
            )
        enviar = st.form_submit_button(
            "Cadastrar empresa",
            type="primary",
            icon=":material/add_business:",
            width="content",
        )

    if enviar:
        if not nome.strip() or not nicho.strip():
            st.session_state["erro_novo_empresa_nome"] = not nome.strip()
            st.session_state["erro_novo_empresa_nicho"] = not nicho.strip()
            st.rerun()
        elif cnpj_manual.strip() and not cnpj_valido(cnpj_manual):
            st.warning("O CNPJ informado é inválido.")
        else:
            st.session_state.pop("erro_novo_empresa_nome", None)
            st.session_state.pop("erro_novo_empresa_nicho", None)
            lead = {
                "place_id": None, "cnpj": limpar_cnpj(cnpj_manual), "segmento_icp": segmento_icp_manual,
                "nome_empresa": nome.strip(), "razao_social": razao_social.strip(),
                "decisor": decisor.strip(), "nicho": nicho.strip(), "valor_proposta": valor_proposta_manual,
                "endereco": endereco.strip(), "cidade": cidade.strip(), "telefone": telefone.strip(), "proximo_contato": proximo_contato_manual,
                "site": site.strip(), "email": email.strip(), "status": status_manual,
                "status_receita": "", "origem": "Cadastro manual", "observacoes": observacoes.strip(),
            }
            inseridos, _ = salvar_leads([enriquecer_lead_icp(lead)])
            if inseridos:
                st.cache_data.clear()
                st.session_state.pop("nova_empresa_prefill", None)
                st.session_state["versao_form_nova_empresa"] = _versao_form_nova + 1
                st.session_state["aviso_nova_empresa"] = f"Cadastrada e enviada a {status_manual}"
                st.rerun()
            else:
                st.warning("Esse lead já existe na base.")


if aba_equipe:
    _nivel_eq = st.session_state.get("nivel_usuario")
    _meu_id = st.session_state.get("usuario_id")
    _minha_equipe = st.session_state.get("equipe_id_usuario")

    aviso_equipe = st.session_state.pop("aviso_equipe", None)
    if aviso_equipe:
        st.success(aviso_equipe)

    todas_as_equipes = listar_equipes()
    equipes_existentes = (
        [equipe for equipe in todas_as_equipes if equipe["id"] == _minha_equipe]
        if _nivel_eq == "supervisor" else todas_as_equipes
    )
    _carteira_por_usuario = contar_leads_por_responsavel()

    st.markdown('<div class="section-title">Usuários</div>', unsafe_allow_html=True)
    if _nivel_eq == "diretor":
        usuarios_visiveis = listar_usuarios()
    elif _nivel_eq == "gerente":
        usuarios_visiveis = listar_usuarios(niveis=("vendedor", "supervisor"))
    elif _nivel_eq == "supervisor":
        usuarios_visiveis = listar_usuarios(equipe_id=_minha_equipe, niveis=("vendedor",))
    else:
        usuarios_visiveis = []

    niveis_que_posso_alternar = niveis_administraveis_por(_nivel_eq)
    if not usuarios_visiveis:
        render_empty_state(
            "Nenhuma conta por aqui ainda",
            "Assim que houver usuários no seu escopo, eles aparecerão nesta lista.",
            compacto=True,
        )
    else:
        cabecalho_usuario = st.columns([1.6, 1.1, 1.35, 1.2, 0.9, 0.9, 1])
        for coluna, rotulo in zip(
            cabecalho_usuario,
            ("Nome", "Usuário", "Perfil", "Equipe", "Carteira", "Status", "Ação"),
        ):
            coluna.caption(rotulo)
        for usuario_linha in usuarios_visiveis:
            ativo = usuario_linha["status"] == "ativo"
            _carteira = _carteira_por_usuario.get(usuario_linha["id"], 0)
            with st.container(key=f"user_row_{usuario_linha['id']}"):
                (
                    col_nome,
                    col_usuario,
                    col_nivel,
                    col_equipe,
                    col_carteira,
                    col_status,
                    col_acao,
                ) = st.columns([1.6, 1.1, 1.35, 1.2, 0.9, 0.9, 1])
                col_nome.markdown(f"**{usuario_linha['nome']}**")
                col_usuario.caption(f"@{usuario_linha['username']}")
                col_nivel.markdown(
                    f'<span class="chip chip-neutral">{escape(rotulo_nivel(usuario_linha["nivel"]))}</span>',
                    unsafe_allow_html=True,
                )
                col_equipe.caption(usuario_linha.get("equipe_nome") or "Sem equipe")
                col_carteira.caption(f"{_carteira} empresa(s)")
                col_status.markdown(
                    f'<span class="status-badge {"status-active" if ativo else "status-paused"}">'
                    f'{"Ativo" if ativo else "Inativo"}</span>',
                    unsafe_allow_html=True,
                )
                pode_alternar_esta_linha = (
                    usuario_linha["nivel"] in niveis_que_posso_alternar
                    and usuario_linha["id"] != _meu_id
                )
                if pode_alternar_esta_linha:
                    rotulo_botao = "Desativar" if ativo else "Ativar"
                    if col_acao.button(
                        rotulo_botao,
                        key=f"toggle_usuario_{usuario_linha['id']}",
                        width="stretch",
                    ):
                        alternar_status_usuario(usuario_linha["id"])
                        st.rerun()
                else:
                    col_acao.caption("—")

    if equipes_existentes:
        st.markdown('<div class="section-title" style="margin-top:1.1rem;">Equipes</div>', unsafe_allow_html=True)
        _membros_por_equipe: dict[int, int] = {}
        usuarios_para_totais = (
            listar_usuarios(equipe_id=_minha_equipe)
            if _nivel_eq == "supervisor" else listar_usuarios()
        )
        for _u in usuarios_para_totais:
            if _u.get("equipe_id") is not None:
                _membros_por_equipe[_u["equipe_id"]] = _membros_por_equipe.get(_u["equipe_id"], 0) + 1
        _carteira_por_equipe = contar_leads_por_equipe()
        colunas_equipes = st.columns(min(3, len(equipes_existentes)) or 1)
        for indice_equipe, equipe in enumerate(equipes_existentes):
            with colunas_equipes[indice_equipe % len(colunas_equipes)]:
                st.markdown(
                    f"""
                    <div class="team-card">
                      <div class="t-name">{escape(str(equipe['nome']))}</div>
                      <div class="t-meta">{_membros_por_equipe.get(equipe['id'], 0)} membro(s) · {_carteira_por_equipe.get(equipe['id'], 0)} empresa(s)</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
    else:
        render_empty_state(
            "Nenhuma equipe disponível",
            "Crie uma equipe para organizar usuários e distribuição de carteira.",
            compacto=True,
        )

    escopo_atribuicao = pode(_nivel_eq, "escopo_atribuicao_carteira")
    if escopo_atribuicao:
        st.markdown(
            '<div class="section-title" style="margin-bottom:0.2rem;">Atribuir carteira</div>'
            '<div class="section-sub">Escolha um lead e o responsável que vai cuidar dele daqui pra frente.</div>',
            unsafe_allow_html=True,
        )
        leads_para_atribuir = leads_visiveis()
        if escopo_atribuicao == "todas":
            candidatos_carteira = listar_usuarios(niveis=("vendedor", "supervisor"))
        else:
            candidatos_carteira = listar_usuarios(equipe_id=_minha_equipe, niveis=("vendedor",))

        if leads_para_atribuir.empty or not candidatos_carteira:
            st.caption("Sem leads ou sem contas no seu escopo para reatribuir agora.")
        else:
            opcoes_leads_carteira = {
                f"{linha['nome_empresa']} — #{linha['id']}": int(linha["id"])
                for _, linha in leads_para_atribuir.iterrows()
            }
            opcoes_usuarios_carteira = {"Sem responsável": None, **{u["nome"]: u["id"] for u in candidatos_carteira}}
            col_lead_carteira, col_user_carteira, col_btn_carteira = st.columns([2, 2, 1])
            lead_escolhido_rotulo = col_lead_carteira.selectbox("Lead", list(opcoes_leads_carteira))
            usuario_escolhido_rotulo = col_user_carteira.selectbox("Novo responsável", list(opcoes_usuarios_carteira))
            if col_btn_carteira.button("Atribuir", width="stretch", key="btn_atribuir_carteira"):
                atribuir_lead_a_usuario(
                    opcoes_leads_carteira[lead_escolhido_rotulo],
                    opcoes_usuarios_carteira[usuario_escolhido_rotulo],
                )
                st.session_state["aviso_equipe"] = "Carteira atualizada."
                st.rerun()

    if _nivel_eq == "diretor":
        with st.expander("Histórico de login (auditoria)", icon=":material/security:"):
            eventos = listar_eventos_login(50)
            if not eventos:
                st.caption("Nenhum evento de login registrado ainda.")
            else:
                tabela_eventos = pd.DataFrame(eventos)
                tabela_eventos["criado_em"] = pd.to_datetime(
                    tabela_eventos["criado_em"], errors="coerce", utc=True
                ).dt.strftime("%d/%m/%Y %H:%M")
                tabela_eventos["sucesso"] = tabela_eventos["sucesso"].map({1: "Sucesso", 0: "Falha"})
                st.dataframe(
                    tabela_eventos[["criado_em", "username", "sucesso", "ip"]],
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "criado_em": st.column_config.TextColumn("Quando"),
                        "username": st.column_config.TextColumn("Usuário"),
                        "sucesso": st.column_config.TextColumn("Resultado"),
                        "ip": st.column_config.TextColumn("IP"),
                    },
                )

        with st.expander("Zona de risco", icon=":material/warning:"):
            st.caption(
                "Apaga todos os leads, snapshots, sinais e histórico de oportunidade da base. "
                "Não afeta usuários, equipes, campanhas nem a lista de supressão de contato. "
                "Ação irreversível."
            )
            st.text_input(
                f'Digite "{FRASE_CONFIRMACAO_ZERAR_BASE}" para habilitar',
                key="confirmacao_zerar_base",
            )
            if st.button(
                "Zerar base de leads",
                type="secondary",
                icon=":material/delete_forever:",
                disabled=st.session_state.get("confirmacao_zerar_base", "") != FRASE_CONFIRMACAO_ZERAR_BASE,
            ):
                quantidade_zerada = zerar_base_leads()
                st.session_state.pop("confirmacao_zerar_base", None)
                st.session_state["aviso_equipe"] = f"{quantidade_zerada} lead(s) apagado(s). Base zerada."
                st.rerun()

st.markdown(
    """
    <div class="app-footer">
    <span>Scorpions CRM • Pipeline comercial e prospecção</span>
      <span>Atualizado em 2026</span>
    </div>
    """,
    unsafe_allow_html=True,
)
