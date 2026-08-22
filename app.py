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
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
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
from niche_sources import (
    resolver_fontes_reais,
    roteamento_por_nicho,
)

from automation import (
    FONTE_AUTOMATICA,
    FONTE_OSM,
    FONTES_AUTOMACAO,
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
)


APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("CRM_DB_PATH", str(APP_DIR / "scorpions_base.db")))
GOOGLE_PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
BRASIL_API_CNPJ_URL = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
PADRAO_CNPJ = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")
LIMITE_PAGINA_BYTES = 5_000_000
STATUS = STATUS_FUNIL


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


def exigir_login() -> None:
    """Bloqueia o restante do app até o usuário autenticar com usuário/senha."""
    if st.session_state.get("autenticado"):
        return

    st.markdown(
        """
        <style>
          .st-key-login_card {
            max-width: 380px;
            margin: 10vh auto 0;
            background: rgba(17, 23, 33, 0.92);
            border: 1px solid var(--line);
            border-radius: 6px;
            padding: 2.2rem 2rem 1.8rem;
            box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4);
          }
          .login-eyebrow {
            color: var(--primary);
            font-size: 0.7rem;
            font-weight: 600;
            letter-spacing: 0.32em;
            text-transform: uppercase;
            text-align: center;
          }
          .login-title {
            font-family: Orbitron, Inter, sans-serif;
            font-size: 1.6rem;
            letter-spacing: 0.18em;
            text-align: center;
            margin: 0.5rem 0 0.15rem;
          }
          .login-sub {
            color: var(--muted);
            font-size: 0.8rem;
            text-align: center;
            margin-bottom: 1.6rem;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )

    agora = time.time()
    usuarios = _usuarios_autenticacao()
    estado = _estado_login()
    chave_ip = _chave_ip()
    bloqueio_ip = estado["por_ip"].get(chave_ip, {"tentativas": 0, "bloqueado_ate": 0.0})

    with st.container(key="login_card"):
        st.markdown('<div class="login-eyebrow">Acesso restrito</div>', unsafe_allow_html=True)
        st.markdown('<div class="login-title">SCORPIONS</div>', unsafe_allow_html=True)
        st.markdown('<div class="login-sub">Soluções tecnológicas · CRM</div>', unsafe_allow_html=True)

        if not usuarios:
            st.warning(
                "Nenhum usuário configurado ainda. Adicione a seção `[AUTH_USERS]` em "
                "`.streamlit/secrets.toml` com `usuario = \"hash_bcrypt\"` para liberar o acesso."
            )
        elif agora < bloqueio_ip["bloqueado_ate"]:
            restante = int(bloqueio_ip["bloqueado_ate"] - agora)
            st.error(f"Muitas tentativas incorretas a partir desta conexão. Tente novamente em {restante}s.")
        else:
            with st.form("form_login", border=False):
                usuario = st.text_input("Usuário")
                senha = st.text_input("Senha", type="password")
                enviado = st.form_submit_button("Entrar", use_container_width=True)

            if enviado:
                # bcrypt.checkpw roda sempre (mesmo pra usuario inexistente) para nao
                # vazar, por tempo de resposta, quais usuarios existem de verdade.
                hash_guardado = usuarios.get(usuario, _hash_fantasma())
                senha_confere = bcrypt.checkpw(senha.encode("utf-8"), hash_guardado.encode("utf-8"))
                valido = usuario in usuarios and senha_confere
                if valido:
                    st.session_state.autenticado = True
                    st.session_state.usuario_logado = usuario
                    with estado["lock"]:
                        estado["por_ip"].pop(chave_ip, None)
                    st.rerun()
                else:
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
            "places.nationalPhoneNumber,places.websiteUri,places.types"
        ),
    }
    resposta = requests.post(
        GOOGLE_PLACES_URL,
        headers=headers,
        json={"textQuery": f"{nicho} em {localizacao}", "pageSize": min(limite, 100)},
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

    leads = []
    for item in resposta.json().get("places", []):
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
    return salvar_leads_no_banco(leads)


@st.cache_data
def listar_leads(busca: str = "", nicho: str = "Todos", status: str = "Todos") -> pd.DataFrame:
    sql = "SELECT * FROM leads WHERE 1=1"
    parametros: list[str] = []
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
        registrar_atividade("lead_atualizado", descricao, lead_id)
    st.cache_data.clear()
    return alterados


def excluir_lead(lead_id: int) -> None:
    with conectar() as conexao:
        nome_empresa = conexao.execute(
            "SELECT nome_empresa FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()

    # Registra a atividade ANTES de apagar o lead: a tabela de atividades tem
    # FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE SET NULL, entao inserir
    # depois de deletar violaria a constraint (o lead_id ja nao existiria mais).
    registrar_atividade(
        "lead_excluido",
        f"Lead '{nome_empresa['nome_empresa'] if nome_empresa else lead_id}' excluído da base.",
        lead_id,
    )

    with conectar() as conexao:
        conexao.execute("DELETE FROM leads WHERE id = ?", (lead_id,))

    st.cache_data.clear()


def atualizar_etapa_funil(lead_id: int, nova_etapa: str):
    """Atualiza a etapa do funil para um único lead."""
    if nova_etapa not in STATUS:
        raise ValueError(f"Etapa inválida: {nova_etapa}")
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
    alerta_contato = ""
    proximo_contato_str = lead.get("proximo_contato")
    if proximo_contato_str:
        try:
            data_contato = datetime.strptime(proximo_contato_str, '%Y-%m-%d').date()
            if data_contato < datetime.now(timezone.utc).date():
                alerta_contato = " ⚠️"
        except (ValueError, TypeError):
            proximo_contato_str = None

    score = lead.get('pontuacao')
    score_num = int(score) if score and pd.notna(score) else None
    if score_num is None:
        score_classe, score_str = "score-lo", "N/A"
    elif score_num >= 85:
        score_classe, score_str = "score-hi", str(score_num)
    elif score_num >= 70:
        score_classe, score_str = "score-mid", str(score_num)
    else:
        score_classe, score_str = "score-lo", str(score_num)

    valor_proposta = lead.get('valor_proposta') or 0.0
    icp_bruto = lead.get('segmento_icp')
    icp = icp_bruto if icp_bruto and pd.notna(icp_bruto) else 'Não classificado'
    icp_classe = "chip-neutral" if icp == "Não classificado" else "chip-accent"

    with st.container():
        st.markdown(
            f"""
            <div class="kanban-card">
              <div class="kanban-topline">
                <div class="kanban-company">{lead['nome_empresa']}</div>
                <div class="score-badge {score_classe}">{score_str}</div>
              </div>
              <div class="kanban-meta">{lead.get('cidade') or 'N/A'}</div>
              <div class="kanban-meta kanban-contatos">{contatos_disponiveis}{alerta_contato}</div>
              <div style="margin-top:0.4rem; display:flex; align-items:center; gap:0.5rem;">
                <span class="chip {icp_classe}">{icp}</span>
                {f'<span style="margin-left:auto;font-family:Orbitron,sans-serif;font-size:0.72rem;color:#7DD3FC;">R$ {valor_proposta:,.0f}</span>' if valor_proposta > 0 else ''}
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.expander("Mais detalhes"):
            st.caption(f"**Nicho:** {lead.get('nicho') or 'N/A'}")
            st.caption(f"**Cidade:** {lead.get('cidade') or 'N/A'}")
            st.caption(f"**ICP:** {lead.get('segmento_icp') or 'N/A'}")
            if valor_proposta > 0:
                st.caption(f"**Valor Proposta:** R$ {valor_proposta:,.2f}")
            if proximo_contato_str:
                data_fmt = datetime.strptime(proximo_contato_str, '%Y-%m-%d').strftime('%d/%m/%Y')
                texto_contato = f"~~{data_fmt}~~ (Atrasado)" if alerta_contato else data_fmt
                st.caption(f"**Próximo Contato:** {texto_contato}")

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
        with col_abrir:
            if st.button("Abrir", key=f"abrir_{lead['id']}", use_container_width=True):
                st.session_state["busca_empresas"] = lead["nome_empresa"]
                st.session_state["navegacao_solicitada"] = "Empresas"
                st.rerun()

st.set_page_config(page_title="Scorpions CRM", page_icon=":material/monitoring:", layout="wide")
google_places_configurado = configurar_google_places()

st.markdown(
    """
    <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Orbitron:wght@500;600;700;800&display=swap');

      :root {
                --bg: #0A0A0F;
                --panel: #111319;
                --panel-strong: #0C0E13;
                --line: #1E232B;
                --line-strong: #2D333D;
                --text: #F5F7FA;
                --text-secondary: #C3CBD8;
                --muted: #8A94A6;
                --weak: #5A6373;
                --primary: #7DD3FC;
                --secondary: #2568FF;
                --accent: #0D6EFD;
                --danger: #ff8f8f;
                --danger-strong: #ffb3b3;
                --success: #7DD3FC;
                --warning: #ff8f8f;
      }

      html, body, .stApp, [class*="css"] { font-family: Inter, system-ui, sans-serif; }

      .stApp {
                background:
                    radial-gradient(circle at 78% 0%, rgba(37, 104, 255, 0.10), transparent 42%),
                    var(--bg);
        color: var(--text);
      }

      :focus-visible { outline: 1px solid var(--primary) !important; outline-offset: 2px !important; }

            .stApp::before {
                content: "";
                position: fixed;
                inset: 0;
                background-image:
                    linear-gradient(rgba(112, 211, 252, 0.025) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(112, 211, 252, 0.025) 1px, transparent 1px);
                background-size: 60px 60px;
                mask-image: linear-gradient(to bottom, black, transparent 78%);
                pointer-events: none;
                z-index: 0;
            }

      [data-testid="stHeader"] {
        background: rgba(6, 17, 28, 0.7);
        backdrop-filter: blur(10px);
      }

      .block-container {
                padding-top: 2rem;
                padding-bottom: 3rem;
                max-width: 1500px;
      }

            [data-testid="stSidebar"] {
                background: var(--bg);
                border-right: 1px solid var(--line);
            }

            [data-testid="stSidebar"] .block-container {
                padding: 1.25rem 1rem 1.5rem;
            }

            [data-testid="stSidebar"] img {
                border: 1px solid var(--line);
                border-radius: 8px;
                filter: brightness(1.35) contrast(1.1);
            }

            .brand-lockup {
                padding: 0.35rem 0.5rem 1.1rem;
                border-bottom: 1px solid var(--line);
                margin-bottom: 0.9rem;
            }

            .brand-lockup strong {
                display: block;
                color: var(--text);
                font-family: Orbitron, Inter, sans-serif;
                font-weight: 700;
                font-size: 0.95rem;
                letter-spacing: 0.22em;
            }

            .brand-lockup span {
                color: var(--primary);
                font-size: 0.6rem;
                letter-spacing: 0.18em;
                text-transform: uppercase;
            }

            .sidebar-caption {
                color: var(--weak);
                font-size: 0.65rem;
                letter-spacing: 0.2em;
                text-transform: uppercase;
                margin: 1.1rem 0 0.4rem;
                padding: 0 0.5rem;
            }

            .sidebar-status {
                display: flex;
                align-items: center;
                gap: 0.5rem;
                color: var(--muted);
                font-size: 0.72rem;
                padding: 0 0.5rem;
                margin-bottom: 0.6rem;
            }

            .sidebar-status .dot {
                width: 6px;
                height: 6px;
                border-radius: 50%;
                background: var(--primary);
                flex: none;
                animation: scorp-pulse 2.2s infinite;
            }

            @keyframes scorp-pulse {
                0% { box-shadow: 0 0 0 0 rgba(125, 211, 252, .5); }
                100% { box-shadow: 0 0 0 7px rgba(125, 211, 252, 0); }
            }
            @keyframes scorp-trace {
                0% { transform: translateX(-100%); }
                100% { transform: translateX(300%); }
            }
            .trace-line {
                position: relative;
                height: 1px;
                overflow: hidden;
                background: var(--line);
                margin-bottom: 0.9rem;
            }
            .trace-line::after {
                content: "";
                position: absolute;
                top: 0; left: 0;
                width: 40%; height: 1px;
                background: linear-gradient(90deg, transparent, var(--secondary), var(--primary));
                animation: scorp-trace 4s linear infinite;
            }

            [data-testid="stSidebar"] [data-testid="stTextInput"] input {
                background: var(--panel);
                border-color: rgba(125, 211, 252, .18);
                font-size: 0.8rem;
            }

      .header-shell, .st-key-header_shell {
                position: relative;
                z-index: 1;
                background: rgba(17, 19, 25, 0.86);
                backdrop-filter: blur(10px);
        border: 1px solid var(--line);
                border-radius: 4px;
        padding: 1.25rem 1.35rem 1rem 1.35rem;
        margin-bottom: 1.25rem;
        box-shadow: 0 16px 38px rgba(0, 0, 0, 0.24);
      }

      .st-key-header_shell .stButton > button { padding: 0.5rem 0.9rem; font-size: 0.8rem; margin-top: 0.6rem; }

      .sales-summary {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 0.7rem;
        margin: 0 0 1.2rem 0;
      }

      .sales-summary__item {
                position: relative;
                overflow: hidden;
                background: linear-gradient(180deg, var(--panel), var(--panel-strong));
        border: 1px solid var(--line);
                border-radius: 4px;
        padding: 0.9rem 1rem;
        min-height: 94px;
                transition: border-color 0.2s ease;
      }

      .sales-summary__item::before {
                content: "";
                position: absolute;
                top: 0; left: 0; right: 0;
                height: 1px;
                background: linear-gradient(90deg, transparent, rgba(125, 211, 252, .45), transparent);
      }

      .sales-summary__item.accent {
                border-color: rgba(125, 211, 252, .3);
      }

      .sales-summary__item .label {
        display: block;
        color: var(--weak);
        font-size: 0.68rem;
        text-transform: uppercase;
        letter-spacing: 0.14em;
      }

      .sales-summary__item strong {
        display: block;
        margin-top: 0.5rem;
        font-family: Orbitron, Inter, sans-serif;
        font-weight: 600;
        font-size: 1.55rem;
        line-height: 1.2;
        color: var(--text);
        letter-spacing: 0.01em;
      }

      .sales-summary__item .note {
        display: block;
        margin-top: 0.2rem;
        font-size: 0.72rem;
        color: var(--muted);
      }

      .title-badge {
        display: inline-block;
                background: transparent;
                color: var(--primary);
        border: 1px solid rgba(125, 211, 252, .28);
                border-radius: 3px;
        font-size: 0.65rem;
        font-weight: 700;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        padding: 0.2rem 0.55rem;
      }

      .header-shell h1, .st-key-header_shell h1 {
        margin: 0.7rem 0 0.2rem 0;
        font-family: Orbitron, Inter, sans-serif;
        font-weight: 600;
        font-size: 1.7rem;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        line-height: 1.1;
      }

      .header-shell p, .st-key-header_shell p {
        margin: 0;
        color: var(--muted);
        font-size: 0.92rem;
      }

      .status-pill {
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        background: transparent;
        border: 1px solid rgba(125, 211, 252, .28);
        color: var(--primary);
        border-radius: 999px;
        padding: 0.32rem 0.7rem;
        font-weight: 600;
        font-size: 0.72rem;
        letter-spacing: 0.04em;
      }

      .status-pill::before {
        content: "";
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--primary);
        display: inline-block;
        animation: scorp-pulse 2.2s infinite;
      }

      div[data-testid="stMetricContainer"] {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 4px;
        padding: 0.85rem 1rem;
      }

      .stDataFrame, .stTable {
        border: 1px solid var(--line);
        border-radius: 4px;
        overflow: hidden;
      }

      .stButton > button {
                border-radius: 4px;
                border: 1px solid var(--line-strong);
                background: transparent;
                color: var(--text);
        font-weight: 600;
        font-size: 0.82rem;
        transition: border-color 0.15s ease, background 0.15s ease;
      }

      .stButton > button:hover {
                border-color: var(--primary);
                background: rgba(37, 104, 255, 0.14);
                color: var(--text);
      }

      .stButton > button[kind="primary"],
      .stButton > button[data-testid="stBaseButton-primary"] {
                border: 1px solid rgba(125, 211, 252, .45);
                background: linear-gradient(90deg, var(--accent), var(--secondary));
                color: #fff;
                box-shadow: 0 0 22px rgba(37, 104, 255, .28);
      }
      .stButton > button[kind="primary"]:hover,
      .stButton > button[data-testid="stBaseButton-primary"]:hover {
                filter: brightness(1.12);
                box-shadow: 0 0 28px rgba(37, 104, 255, .38);
      }

      .stTextInput > div > div > input,
      .stNumberInput > div > div > input,
      .stDateInput > div > div > input,
      .stTimeInput > div > div > input,
      .stSelectbox > div > div > div,
      .stSelectbox > div > div > div > div,
      .stTextArea > div > div > textarea {
        background: var(--bg);
        color: var(--text);
        border: 1px solid var(--line-strong);
        border-radius: 3px;
        font-size: 0.82rem;
      }

      .stAlert {
        border-radius: 4px;
      }

      .soft-badge {
                font-family: Orbitron, Inter, sans-serif;
                background: rgba(37, 104, 255, 0.12);
                border: 1px solid rgba(125, 211, 252, 0.35);
                color: var(--primary);
                border-radius: 3px;
        padding: 0.15rem 0.5rem;
        font-size: 0.68rem;
        font-weight: 600;
      }

      .kanban-card {
                background: var(--panel);
                border: 1px solid var(--line);
                border-radius: 4px;
        padding: 0.9rem 0.9rem 0.7rem;
        margin-bottom: 0.8rem;
                transition: border-color .15s ease, box-shadow .15s ease;
      }
      .kanban-card:hover {
                border-color: rgba(125, 211, 252, .4);
                box-shadow: 0 0 20px rgba(37, 104, 255, .12);
      }

      .kanban-topline {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.5rem;
        margin-bottom: 0.45rem;
      }

      .kanban-company {
        font-weight: 600;
        font-size: 0.92rem;
        line-height: 1.3;
        color: var(--text);
      }

      .kanban-meta {
        color: var(--muted);
        font-size: 0.76rem;
        margin-top: 0.25rem;
      }

      .kanban-contatos {
        color: var(--primary);
        font-weight: 600;
        letter-spacing: 0.02em;
      }

      .css-1d391kg {
        color: var(--text) !important;
      }

      .app-footer {
        margin-top: 2rem;
        padding-top: 1rem;
        border-top: 1px solid var(--line);
        color: var(--weak);
        font-size: 0.76rem;
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        flex-wrap: wrap;
      }

      ::-webkit-scrollbar { width: 10px; height: 10px; }
      ::-webkit-scrollbar-track { background: var(--bg); }
      ::-webkit-scrollbar-thumb {
        background: var(--line-strong);
        border-radius: 999px;
      }
      ::-webkit-scrollbar-thumb:hover { background: rgba(125, 211, 252, 0.45); }

      [data-testid="stSidebar"] [role="radiogroup"] {
        gap: 1px;
      }
      [data-testid="stSidebar"] [role="radiogroup"] label {
        border-radius: 0 3px 3px 0;
        border-left: 2px solid transparent;
        padding: 0.5rem 0.6rem;
        color: var(--muted);
        font-size: 0.82rem;
        transition: background 0.15s ease, color 0.15s ease;
      }
      [data-testid="stSidebar"] [role="radiogroup"] label:hover {
        background: var(--panel);
        color: var(--text);
      }
      [data-testid="stSidebar"] [role="radiogroup"] label[data-checked="true"],
      [data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {
        background: rgba(37, 104, 255, 0.12);
        border-left-color: var(--primary);
        color: var(--text);
        font-weight: 600;
      }
      [data-testid="stSidebar"] [role="radiogroup"] label > div:first-child {
        display: none;
      }

      .stTabs [data-baseweb="tab-list"] {
        gap: 0.4rem;
        border-bottom: 1px solid var(--line);
      }
      .stTabs [data-baseweb="tab"] {
        color: var(--muted);
        font-weight: 600;
        font-size: 0.85rem;
      }
      .stTabs [aria-selected="true"] {
        color: var(--text) !important;
      }
      .stTabs [data-baseweb="tab-highlight"] {
        background: linear-gradient(90deg, var(--accent), var(--primary)) !important;
        height: 2px;
      }

      /* --- Chips --- */
      .chip {
        display: inline-block;
        font-size: 0.62rem;
        letter-spacing: 0.06em;
        padding: 0.12rem 0.5rem;
        border-radius: 3px;
        white-space: nowrap;
      }
      .chip-accent { color: var(--primary); border: 1px solid rgba(125,211,252,.28); background: rgba(37,104,255,.1); }
      .chip-neutral { color: var(--muted); border: 1px solid var(--line-strong); background: transparent; }
      .chip-alert { color: var(--danger); border: 1px solid rgba(255,107,107,.35); background: rgba(255,107,107,.08); }

      /* --- Score badge --- */
      .score-badge {
        font-family: Orbitron, Inter, sans-serif;
        font-size: 0.68rem;
        padding: 0.12rem 0.5rem;
        border-radius: 3px;
        flex: none;
      }
      .score-hi { color: var(--primary); border: 1px solid rgba(125,211,252,.35); background: rgba(37,104,255,.12); }
      .score-mid { color: var(--text-secondary); border: 1px solid var(--line-strong); background: var(--bg); }
      .score-lo { color: var(--muted); border: 1px solid var(--line-strong); background: var(--bg); }

      /* --- Funil / stage bars (dashboard) --- */
      .funnel-row { margin-bottom: 0.9rem; }
      .funnel-row:last-child { margin-bottom: 0; }
      .funnel-row .funnel-top {
        display: flex; justify-content: space-between; font-size: 0.78rem; color: var(--muted);
      }
      .funnel-row .funnel-top .val { color: var(--text); }
      .funnel-row .funnel-top .sep { color: var(--weak); }
      .funnel-track {
        height: 6px; background: var(--line); border-radius: 999px; margin-top: 0.45rem; overflow: hidden;
      }
      .funnel-fill {
        height: 6px; border-radius: 999px;
        background: linear-gradient(90deg, var(--accent), var(--primary));
        box-shadow: 0 0 10px rgba(37, 104, 255, .5);
      }

      /* --- ICP rows --- */
      .icp-row {
        display: flex; align-items: center; gap: 0.6rem; font-size: 0.8rem;
        padding: 0.3rem 0; color: var(--text);
      }
      .icp-row .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .icp-row .leads { font-size: 0.72rem; color: var(--muted); }

      .icp-alert {
        margin-top: 1rem;
        background: rgba(37, 104, 255, .06);
        border: 1px solid rgba(125, 211, 252, .22);
        border-left: 2px solid var(--primary);
        border-radius: 3px;
        padding: 0.75rem 0.9rem;
      }
      .icp-alert .icp-alert-title { font-size: 0.8rem; font-weight: 600; color: var(--primary); }
      .icp-alert p { font-size: 0.74rem; color: var(--muted); margin: 0.3rem 0 0.6rem; line-height: 1.6; }

      /* --- Attention cards --- */
      .attn-card {
        background: var(--panel-strong); border: 1px solid var(--line); border-radius: 4px;
        padding: 0.8rem 0.9rem; height: 100%; transition: border-color .15s ease;
      }
      .attn-card:hover { border-color: rgba(125,211,252,.3); }
      .attn-kicker { font-size: 0.62rem; letter-spacing: 0.14em; text-transform: uppercase; }
      .attn-title { font-size: 0.86rem; font-weight: 600; margin-top: 0.35rem; color: var(--text); }
      .attn-note { font-size: 0.74rem; color: var(--muted); margin-top: 0.2rem; line-height: 1.5; }

      /* --- Dashed empty state --- */
      .dashed-empty {
        border: 1px dashed var(--line-strong); border-radius: 4px; padding: 1.6rem 1.2rem;
      }
      .dashed-empty .dashed-title {
        font-family: Orbitron, Inter, sans-serif; font-size: 0.78rem; font-weight: 600;
        letter-spacing: 0.1em; text-transform: uppercase; color: var(--text);
      }
      .dashed-empty p { font-size: 0.78rem; color: var(--muted); margin: 0.4rem 0 0; line-height: 1.6; }

      /* --- Source chips row (prospecção) --- */
      .source-chip-row { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0.6rem 0 0.2rem; }

      /* --- Campaign cards (automação) --- */
      .campaign-card {
        background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 0.9rem 1rem;
        transition: border-color .15s ease;
      }
      .campaign-card:hover { border-color: rgba(125,211,252,.3); }
      .campaign-card .head { display: flex; align-items: center; gap: 0.5rem; }
      .campaign-card .head .name { font-size: 0.88rem; font-weight: 600; flex: 1; min-width: 0; }
      .campaign-card .scope { font-size: 0.74rem; color: var(--muted); margin-top: 0.3rem; }
      .campaign-card .metrics {
        display: flex; gap: 1.1rem; margin-top: 0.8rem; padding-top: 0.7rem; border-top: 1px solid var(--line);
      }
      .campaign-card .metrics .m-label { font-size: 0.6rem; letter-spacing: 0.1em; text-transform: uppercase; color: var(--weak); }
      .campaign-card .metrics .m-val { font-family: Orbitron, Inter, sans-serif; font-size: 0.9rem; margin-top: 0.15rem; color: var(--text); }

      /* --- Detail cards grid (empresas) --- */
      .detail-card, .st-key-cnpj_card, .st-key-delete_card, .st-key-detalhe_empresa_card {
        background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 1rem 1.1rem; height: 100%;
      }
      .detail-card.danger-card, .st-key-delete_card { border-color: rgba(255,107,107,.2); }
      .detail-card h4, .card-title {
        margin: 0; font-family: Orbitron, Inter, sans-serif; font-size: 0.76rem; font-weight: 600;
        letter-spacing: 0.1em; text-transform: uppercase; color: var(--text);
      }
      .detail-card.danger-card h4, .card-title.danger { color: var(--danger); }
      .detail-card p, .card-note { font-size: 0.74rem; color: var(--muted); margin: 0.4rem 0 0.7rem; }

      .detail-label { font-size: 0.62rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--weak); }
      .detail-value { font-size: 0.82rem; margin-top: 0.15rem; color: var(--text-secondary); }

      /* --- Streamlit dialog (delete confirmation) --- */
      div[data-testid="stDialog"] div[role="dialog"] {
        background: var(--panel);
        border: 1px solid rgba(255, 107, 107, .3);
        border-radius: 4px;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

iniciar_banco()
iniciar_banco_automacao()

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
_leads_para_contadores = listar_leads()
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
        st.session_state["navegacao_principal"] = "Empresas"
        st.rerun()

    st.markdown('<div class="sidebar-caption">Workspace</div>', unsafe_allow_html=True)
    contadores_nav = {
        "Visão geral": None,
        "Pipeline": None,
        "Prospecção": None,
        "Empresas": _total_leads,
        "Automação": _campanhas_ativas or None,
        "Nova empresa": None,
    }
    pagina = st.radio(
        "Navegação",
        ["Visão geral", "Pipeline", "Prospecção", "Empresas", "Automação", "Nova empresa"],
        format_func=lambda item: {
            "Visão geral": ":material/dashboard:  Dashboard",
            "Pipeline": ":material/view_kanban:  Negócios / Pipeline",
            "Prospecção": ":material/search:  Leads / Prospecção",
            "Empresas": ":material/business:  Clientes / Empresas",
            "Automação": ":material/bolt:  Automação",
            "Nova empresa": ":material/add_business:  Nova empresa",
        }[item]
        + (f"  ·  {contadores_nav[item]}" if contadores_nav[item] else ""),
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
    st.caption(f"Logado como {st.session_state.get('usuario_logado', '—')}")
    if st.button("Sair", use_container_width=True):
        st.session_state.pop("autenticado", None)
        st.session_state.pop("usuario_logado", None)
        st.rerun()

_TITULOS_PAGINA = {
    "Visão geral": ("Scorpions CRM", "Operacional", "Prospecção, pipeline e operação comercial em um só lugar."),
    "Pipeline": ("Pipeline", "4 etapas", "Negócios abertos e valor em proposta por etapa."),
    "Prospecção": ("Prospecção", "Bacen · CVM · B3", "Captura em fontes públicas com scoring de ICP."),
    "Empresas": (f"Clientes", f"{_total_leads} registros", "Base cadastral, consulta CNPJ e exclusão segura."),
    "Automação": ("Automação", "Worker ativo" if worker_online else "Worker offline", "Campanhas agendadas, motor contínuo e histórico."),
    "Nova empresa": ("Nova empresa", "Cadastro manual", "Campos obrigatórios validados antes de salvar."),
}
_titulo_pagina, _badge_pagina, _subtitulo_pagina = _TITULOS_PAGINA[pagina]

with st.container(key="header_shell"):
    st.markdown(
        f"""
        <div class="title-badge">SCORPIONS / soluções tecnológicas</div>
        <div style="display:flex; align-items:center; justify-content:space-between; gap:1rem; margin-top:0.7rem;">
            <h1>{_titulo_pagina}</h1>
            <div class="status-pill">{_badge_pagina}</div>
        </div>
        <p>{_subtitulo_pagina}</p>
        """,
        unsafe_allow_html=True,
    )
    col_prosp, col_nova, _col_spacer = st.columns([1.1, 1.4, 3])
    with col_prosp:
        if st.button("Prospectar", key="header_ir_prospectar", use_container_width=True):
            st.session_state["navegacao_solicitada"] = "Prospecção"
            st.rerun()
    with col_nova:
        if st.button("+ Nova empresa", key="header_ir_nova", type="primary", use_container_width=True):
            st.session_state["navegacao_solicitada"] = "Nova empresa"
            st.rerun()

aba_dashboard = pagina == "Visão geral"
aba_funil = pagina == "Pipeline"
aba_prospeccao = pagina == "Prospecção"
aba_cnpj = pagina == "Empresas"
aba_automacao = pagina == "Automação"
aba_base = pagina == "Empresas"
aba_manual = pagina == "Nova empresa"

if aba_dashboard:
    dados = _leads_para_contadores
    total = len(dados)
    fechados = int((dados["status"] == "Fechado / Contrato").sum()) if total else 0
    em_andamento = int(
        dados["status"].isin(["Contato / Qualificação", "Vistoria Técnica / Diagnóstico", "Proposta Enviada"]).sum()
    ) if total else 0
    conversao = (fechados / total * 100) if total else 0
    valor_total = float(dados["valor_proposta"].fillna(0).sum()) if total else 0.0
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

    nota_conversao_cor = "#7DD3FC" if conversao >= 6 else "#ff8f8f"

    st.markdown(
        """
        <div class="sales-summary">
          <div class="sales-summary__item">
            <span class="label">Leads na base</span>
            <strong>{}</strong>
            <span class="note" style="color:#7DD3FC">+{} este mês</span>
          </div>
          <div class="sales-summary__item">
            <span class="label">Novo pipeline</span>
            <strong>{}</strong>
            <span class="note">sem primeiro contato</span>
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
            <span class="label">Fechados</span>
            <strong>{}</strong>
            <span class="note">no total da base</span>
          </div>
          <div class="sales-summary__item">
            <span class="label">Conversão</span>
            <strong>{:.1f}%</strong>
            <span class="note" style="color:{}">meta 6%</span>
          </div>
          <div class="sales-summary__item accent">
            <span class="label">Valor bruto</span>
            <strong>R$ {:,.0f}</strong>
            <span class="note">soma de propostas ativas</span>
          </div>
        </div>
        """.format(
            total, novos_no_mes, novos, em_andamento, propostas, propostas, valor_propostas,
            fechados, conversao, nota_conversao_cor, valor_total,
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

        st.markdown('<div style="height:0.4rem"></div>', unsafe_allow_html=True)
        st.markdown(
            '<h3 style="margin:0.6rem 0 0;font-family:Orbitron,sans-serif;font-size:0.85rem;font-weight:600;'
            'letter-spacing:0.12em;text-transform:uppercase;color:#F5F7FA;">Precisa de atenção hoje</h3>',
            unsafe_allow_html=True,
        )

        cartoes_atencao: list[tuple[str, str, str, str]] = []
        propostas_df = dados[(dados["status"] == "Proposta Enviada") & (dados["valor_proposta"].fillna(0) > 0)]
        if not propostas_df.empty:
            atualizado_dt = pd.to_datetime(propostas_df["atualizado_em"], errors="coerce", utc=True)
            dias_desde = (pd.Timestamp.now(tz="UTC") - atualizado_dt).dt.days
            idx_mais_antiga = dias_desde.idxmax() if dias_desde.notna().any() else propostas_df.index[0]
            linha = propostas_df.loc[idx_mais_antiga]
            dias = int(dias_desde.loc[idx_mais_antiga]) if pd.notna(dias_desde.loc[idx_mais_antiga]) else None
            nota = (
                f"R$ {linha['valor_proposta']:,.0f} enviados há {dias} dia(s) — sem retorno."
                if dias is not None
                else f"R$ {linha['valor_proposta']:,.0f} aguardando retorno."
            )
            cartoes_atencao.append(("Proposta vencendo", linha["nome_empresa"], nota, "#ff8f8f"))

        vistoria_df = dados[dados["status"] == "Vistoria Técnica / Diagnóstico"].copy()
        if not vistoria_df.empty and "proximo_contato" in vistoria_df.columns:
            vistoria_df["_data"] = pd.to_datetime(vistoria_df["proximo_contato"], errors="coerce").dt.date
            vistoria_hoje = vistoria_df[vistoria_df["_data"] == hoje]
            if not vistoria_hoje.empty:
                linha = vistoria_hoje.iloc[0]
                cartoes_atencao.append((
                    "Vistoria hoje", linha["nome_empresa"],
                    f"Diagnóstico técnico agendado — {linha.get('endereco') or linha.get('cidade') or 'endereço não informado'}.",
                    "#7DD3FC",
                ))

        sem_contato_df = dados[
            (dados["status"] == "Novos Leads")
            & (dados["proximo_contato"].isna() | (dados["proximo_contato"].astype(str).str.strip() == ""))
        ]
        if not sem_contato_df.empty:
            cartoes_atencao.append((
                "Sem contato", f"{len(sem_contato_df)} leads em Novos Leads",
                "Nenhuma tentativa registrada desde a captura.", "#8A94A6",
            ))

        if cartoes_atencao:
            colunas_atencao = st.columns(len(cartoes_atencao))
            for coluna, (kicker, titulo, nota, cor) in zip(colunas_atencao, cartoes_atencao):
                with coluna:
                    st.markdown(
                        f"""
                        <div class="attn-card">
                          <div class="attn-kicker" style="color:{cor}">{kicker}</div>
                          <div class="attn-title">{titulo}</div>
                          <div class="attn-note">{nota}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
        else:
            st.markdown(
                '<div class="dashed-empty"><div class="dashed-title">Tudo em dia</div>'
                '<p>Nenhuma pendência crítica identificada para hoje.</p></div>',
                unsafe_allow_html=True,
            )

        st.divider()

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

            atividades_recentes = listar_atividades(8)
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
        st.info("Sua base ainda está vazia. Busque ou cadastre o primeiro lead.")

if aba_funil:
    st.subheader("Pipeline comercial")
    dados_funil = listar_leads()
    etapas_kanban = [etapa for etapa in STATUS if etapa not in ("Fechado / Contrato", "Descartado")]
    cols = st.columns(len(etapas_kanban))

    for i, etapa in enumerate(etapas_kanban):
        with cols[i]:
            leads_na_etapa = dados_funil[dados_funil["status"] == etapa]
            total_valor_etapa = leads_na_etapa['valor_proposta'].sum()
            st.subheader(f"{etapa} ({len(leads_na_etapa)})", divider="blue")
            st.caption(f"Valor em propostas: R$ {total_valor_etapa:,.2f}")
            if leads_na_etapa.empty:
                st.markdown(
                    '<div class="dashed-empty" style="padding:1.1rem 0.9rem;">'
                    '<p style="margin:0;">Nada nesta etapa. Mova um lead pelo seletor do cartão.</p></div>',
                    unsafe_allow_html=True,
                )
            for _, lead in leads_na_etapa.sort_values("pontuacao", ascending=False).iterrows():
                _render_kanban_card(lead, etapa)

def exibir_diagnostico_fonte(nicho: str):
    """Mostra ao usuário qual fonte de dados será usada e por quê."""
    if not nicho:
        return

    fontes_ideais = roteamento_por_nicho(nicho)   # -> lista de fontes "ideais" pro nicho (ex: pode incluir "Google Places")
    fontes_reais = resolver_fontes_reais(nicho)   # -> lista de fontes realmente disponíveis (considerando chave de API etc.)
    fonte_principal = fontes_reais[0]             # -> primeira/prioritária
    with st.expander("🔍 **Diagnóstico da Fonte de Dados**", expanded=True):
        st.write(f"Para o nicho **'{nicho}'**, o roteador automático selecionou a seguinte fonte:")

        if fonte_principal == "Bacen":
            st.success(
                "**Fonte: 🏦 Banco Central (Bacen)**\n\n"
                "Esta é uma fonte oficial de alta qualidade para instituições financeiras. A busca será precisa."
            )
        elif fonte_principal == FONTE_CVM:
            st.success(
                "**Fonte: 📈 CVM - Corretoras**\n\n"
                "Fonte oficial para corretoras e distribuidoras de valores. A busca será precisa."
            )
        elif fonte_principal == FONTE_B3:
            st.success(
                "**Fonte: 🏢 B3 - Empresas Listadas**\n\n"
                "Fonte oficial para companhias abertas, enriquecida com dados da Receita Federal."
            )
        elif fonte_principal == FONTE_OSM:
            if "Google Places" in fontes_ideais:
                st.warning(
                    "**Fonte: 🗺️ OpenStreetMap (Alternativa Gratuita)**\n\n"
                    "O ideal para este nicho seria o **Google Places**, mas a chave não foi encontrada em `.streamlit/secrets.toml` nem no ambiente. O sistema usará o OpenStreetMap como alternativa."
                )
                st.caption("O OpenStreetMap depende de dados colaborativos. A qualidade e a quantidade dos resultados podem variar, e o CNPJ não é verificado.")
        elif fonte_principal == "Google Places":
            st.success("**Fonte: 📍 Google Places**\n\nEsta é a melhor fonte para negócios locais, com alta cobertura e qualidade de dados.")

        st.caption("Dica: Para usar nichos de fontes oficiais, tente termos como 'banco', 'corretora de valores' ou 'empresa listada na bolsa'.")

if aba_prospeccao:
    def _sugerir_nicho():
        with st.expander("💡 Sugerir nicho por perfil de cliente (ICP)"):
            perfil_escolhido = st.selectbox(
                "Selecione um perfil estratégico",
                options=list(PERFIS_ICP.keys()),
                index=None,
                placeholder="Escolha um perfil...",
            )
            if perfil_escolhido:
                sugestao = PERFIS_ICP[perfil_escolhido]["consulta_sugerida"]
                st.info(f"**Sugestão de busca para o nicho:** `{sugestao}`")

    st.subheader("Prospecção")
    st.markdown('<div class="trace-line"></div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns([2, 2, 1])
    nicho_busca = c1.text_input(
        "Nicho ou segmento",
        placeholder="Ex.: cooperativa de crédito; deixe vazio para todos",
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
        FONTES_AUTOMACAO,
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
        st.caption(
            "Bancos/crédito/pagamentos → Bacen; corretoras/mercado de capitais → CVM; "
            "companhias abertas/B3 → B3. Outros nichos usam Google Places se houver chave; "
            "sem ela, usam OpenStreetMap. "
            "Aprovação automática: score mínimo de 70/100."
        )
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
    if st.button("Encontrar oportunidades", type="primary", width="stretch"):
        st.session_state.pop("resultados_busca", None)
        nicho_obrigatorio = fonte_busca != "Bacen"
        if not local_busca.strip() or (nicho_obrigatorio and not nicho_busca.strip()):
            st.warning("Informe a localização e, para esta fonte, também o nicho.")
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
                    if not encontrados:
                        st.warning(
                            "Nenhuma empresa encontrada com esses filtros. "
                            "Verifique o 'Diagnóstico da Fonte de Dados' acima para entender o motivo ou tente um nicho/local diferente."
                        )
                except (requests.RequestException, RuntimeError) as erro:
                    st.error(str(erro))
                except ValueError as erro:
                    st.error(str(erro))

    resultados = st.session_state.get("resultados_busca", [])
    if resultados:
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

        st.dataframe(tabela_resultados[colunas_disponiveis], width="stretch", hide_index=True)
        if st.button("Adicionar resultados à base"):
            inseridos, duplicados = salvar_leads(resultados)
            st.success(f"{inseridos} lead(s) adicionado(s). {duplicados} duplicado(s) ignorado(s).")

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
        if st.button("Cancelar", use_container_width=True, key="cancelar_exclusao_campanha"):
            st.session_state["confirmar_exclusao_campanha_id"] = None
            st.rerun()
    with col_excluir:
        if st.button(
            "Excluir definitivamente", type="primary", use_container_width=True,
            disabled=not confirma_habilitado, key="confirmar_exclusao_campanha_botao",
        ):
            excluir_campanha(campanha_id)
            st.session_state["confirmar_exclusao_campanha_id"] = None
            st.session_state["aviso_automacao"] = f"Campanha '{nome}' excluída; o histórico foi preservado."
            st.rerun()


if aba_automacao:
    estado_worker = status_worker()
    _worker_dot_style = "" if estado_worker["online"] else "background:var(--weak);animation:none;"
    _worker_texto = (
        "Worker online — as campanhas agendadas estão sendo monitoradas."
        if estado_worker["online"]
        else "Worker offline — inicie com `python worker.py` para executar a agenda em segundo plano."
    )
    st.markdown(
        f"""
        <div style="display:flex;align-items:center;gap:0.6rem;background:rgba(37,104,255,.06);
                    border:1px solid rgba(125,211,252,.22);border-left:2px solid var(--primary);
                    border-radius:3px;padding:0.7rem 0.9rem;font-size:0.82rem;color:#C3CBD8;">
          <span class="dot" style="width:6px;height:6px;border-radius:50%;background:var(--primary);flex:none;{_worker_dot_style}"></span>
          {_worker_texto}
        </div>
        """,
        unsafe_allow_html=True,
    )

    aviso_automacao = st.session_state.pop("aviso_automacao", None)
    if aviso_automacao:
        st.success(aviso_automacao)

    tab_campanhas, tab_motor_continuo, tab_extracao_url = st.tabs(
        ["Campanhas Agendadas", "Motor Contínuo", "Extração por URL"]
    )

    with tab_campanhas:
        st.subheader("Campanhas automáticas")
        st.caption("Crie e gerencie campanhas que buscam leads em horários agendados.")

        def _sugerir_nicho_campanha():
            with st.expander("💡 Sugerir nicho por perfil de cliente (ICP)"):
                perfil_escolhido = st.selectbox(
                    "Selecione um perfil estratégico",
                    options=list(PERFIS_ICP.keys()),
                    index=None,
                    placeholder="Escolha um perfil para ver a sugestão de busca...",
                    key="sugestao_campanha"
                )
                if perfil_escolhido:
                    sugestao = PERFIS_ICP[perfil_escolhido]["consulta_sugerida"]
                    st.info(f"**Sugestão de busca para o nicho:** `{sugestao}`")

        with st.expander("Criar nova campanha", expanded=not bool(listar_campanhas())):
            with st.form("nova_campanha", clear_on_submit=True):
                c1, c2 = st.columns(2)
                nome_campanha = c1.text_input("Nome da campanha", placeholder="Clínicas de Campinas")
                fonte_campanha = c2.selectbox("Fonte", FONTES_AUTOMACAO)
                nicho_campanha = c1.text_input(
                    "Nicho ou segmento",
                    placeholder="Ex.: Todos ou cooperativa de crédito",
                    max_chars=120,
                )
                local_campanha = c2.text_input(
                    "Município, UF",
                    placeholder="Campinas, SP",
                    max_chars=120,
                )
                limite_campanha = c1.number_input("Leads por execução", min_value=1, max_value=100, value=8)
                horario_campanha = c2.time_input("Horário diário", value=datetime.strptime("08:00", "%H:%M").time())
                _sugerir_nicho_campanha()
                ativa_campanha = st.checkbox("Ativar imediatamente", value=True)
                criar = st.form_submit_button("Criar campanha", type="primary", width="stretch")
            if criar:
                try:
                    campanha_id = criar_campanha(
                        nome_campanha,
                        nicho_campanha,
                        local_campanha,
                        fonte_campanha,
                        int(limite_campanha),
                        horario_campanha.strftime("%H:%M"),
                        ativa_campanha,
                    )
                    st.session_state["aviso_automacao"] = f"Campanha #{campanha_id} criada."
                    st.rerun()
                except ValueError as erro:
                    st.error(str(erro))

        campanhas_atuais = listar_campanhas() # type: ignore
        if campanhas_atuais:
            colunas_campanhas = st.columns(min(3, len(campanhas_atuais)) or 1)
            for indice_campanha, campanha in enumerate(campanhas_atuais):
                ativa = bool(campanha.get("ativa"))
                chip_classe = "chip-accent" if ativa else "chip-neutral"
                with colunas_campanhas[indice_campanha % len(colunas_campanhas)]:
                    st.markdown(
                        f"""
                        <div class="campaign-card">
                          <div class="head">
                            <div class="name">{campanha['nome']}</div>
                            <span class="chip {chip_classe}">{"Ativa" if ativa else "Pausada"}</span>
                          </div>
                          <div class="scope">{campanha.get('nicho') or 'Todos'} · {campanha.get('localizacao') or '—'} · {campanha.get('fonte')}</div>
                          <div class="metrics">
                            <div><div class="m-label">Limite/dia</div><div class="m-val">{campanha.get('limite_diario')}</div></div>
                            <div><div class="m-label">Horário</div><div class="m-val">{campanha.get('horario')}</div></div>
                            <div><div class="m-label">Última</div><div class="m-val" style="font-family:Inter,sans-serif;font-size:0.78rem;">{campanha.get('ultima_execucao') or 'nunca'}</div></div>
                          </div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    st.markdown('<div style="height:0.6rem"></div>', unsafe_allow_html=True)

            opcoes_campanha = {
                f"#{campanha['id']} — {campanha['nome']}": campanha["id"] for campanha in campanhas_atuais
            }
            campanha_escolhida = st.selectbox("Gerenciar campanha", list(opcoes_campanha))
            campanha_escolhida_id = opcoes_campanha[campanha_escolhida]
            campanha_escolhida_nome = next(
                c["nome"] for c in campanhas_atuais if c["id"] == campanha_escolhida_id
            )
            executar_col, alternar_col, excluir_col = st.columns(3)
            if executar_col.button("Executar agora", type="primary", width="stretch"):
                with st.spinner("Executando a campanha..."):
                    resultado_execucao = executar_campanha(campanha_escolhida_id)
                if resultado_execucao["status"] == "Sucesso":
                    st.success(resultado_execucao["mensagem"])
                elif resultado_execucao["status"] == "Ignorada":
                    st.info(resultado_execucao["mensagem"])
                else:
                    st.error(resultado_execucao["mensagem"])
            if alternar_col.button("Ativar/pausar", width="stretch"):
                ativa_nova = alternar_campanha(campanha_escolhida_id)
                st.session_state["aviso_automacao"] = "Campanha ativada." if ativa_nova else "Campanha pausada."
                st.rerun()
            if excluir_col.button("Excluir campanha", width="stretch"):
                st.session_state["confirmar_exclusao_campanha_id"] = campanha_escolhida_id
                st.session_state["confirmar_exclusao_campanha_nome"] = campanha_escolhida_nome
                st.rerun()

            _campanha_id_para_excluir = st.session_state.get("confirmar_exclusao_campanha_id")
            if _campanha_id_para_excluir:
                _confirmar_exclusao_campanha(
                    int(_campanha_id_para_excluir), st.session_state.get("confirmar_exclusao_campanha_nome", "")
                )
        else:
            st.info("Nenhuma campanha configurada.")

        execucoes = listar_execucoes(20)
        if execucoes:
            st.subheader("Histórico de execuções")
            st.dataframe(
                pd.DataFrame(execucoes)[
                    ["inicio_em", "campanha_nome", "status", "encontrados", "inseridos", "duplicados", "mensagem"]
                ],
                width="stretch",
                hide_index=True,
            )

    with tab_motor_continuo:
        st.subheader("Alvos do Motor Contínuo")
        st.caption("Defina nichos e localizações para que o Motor Contínuo busque leads automaticamente usando as melhores fontes disponíveis.")

        def _sugerir_nicho_alvo():
            with st.expander("💡 Sugerir nicho por perfil de cliente (ICP)"):
                perfil_escolhido = st.selectbox(
                    "Selecione um perfil estratégico",
                    options=list(PERFIS_ICP.keys()),
                    index=None,
                    placeholder="Escolha um perfil para ver a sugestão de busca...",
                    key="sugestao_alvo"
                )
                if perfil_escolhido:
                    sugestao = PERFIS_ICP[perfil_escolhido]["consulta_sugerida"]
                    st.info(f"**Sugestão de busca para o nicho:** `{sugestao}`")

        with st.expander("Adicionar novo alvo contínuo", expanded=not bool(listar_alvos_continuos())):
            with st.form("novo_alvo_continuo", clear_on_submit=True):
                c1, c2 = st.columns(2)
                nome_alvo = c1.text_input("Nome do alvo", placeholder="Clínicas de Campinas")
                nicho_alvo = c1.text_input(
                    "Nicho ou segmento",
                    placeholder="Ex.: cooperativa de crédito",
                    max_chars=120,
                )
                local_alvo = c2.text_input(
                    "Município, UF",
                    placeholder="Campinas, SP",
                    max_chars=120,
                )
                _sugerir_nicho_alvo()
                ativo_alvo = st.checkbox("Ativar imediatamente", value=True)
                adicionar_alvo = st.form_submit_button("Adicionar alvo", type="primary", width="stretch")
            if adicionar_alvo:
                try:
                    alvo_id = criar_alvo_continuo(
                        nome_alvo,
                        nicho_alvo,
                        local_alvo,
                        ativo_alvo,
                    )
                    st.session_state["aviso_automacao"] = f"Alvo contínuo #{alvo_id} criado."
                    st.rerun()
                except ValueError as erro:
                    st.error(str(erro))

        alvos_atuais = listar_alvos_continuos()
        if alvos_atuais:
            tabela_alvos = pd.DataFrame(alvos_atuais)
            tabela_alvos["ativa"] = tabela_alvos["ativa"].map({1: "Sim", 0: "Não"})
            st.dataframe(
                tabela_alvos[
                    ["id", "nome", "nicho", "localizacao", "ativa", "criado_em"]
                ],
                width="stretch",
                hide_index=True,
            )

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

    with tab_extracao_url:
        st.subheader("Extração complementar por URL")
        st.caption("Extrai CNPJs do texto visível de uma página e mantém somente empresas ativas na BrasilAPI.")
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
        if st.button("Encontrar empresas na página", type="primary", width="stretch"):
            if not url_rastreamento.strip():
                st.warning("Informe a URL que será analisada.")
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
            st.dataframe(tabela_robo, width="stretch", hide_index=True)
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
        if st.button("Cancelar", use_container_width=True, key="cancelar_exclusao_lead"):
            st.session_state["confirmar_exclusao_lead_id"] = None
            st.rerun()
    with col_excluir:
        if st.button(
            "Excluir definitivamente", type="primary", use_container_width=True,
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

    todos = listar_leads()
    nichos = ["Todos"] + (sorted(todos["nicho"].dropna().unique().tolist()) if not todos.empty else [])
    f1, f2, f3 = st.columns([2, 1, 1])
    termo = f1.text_input("Pesquisar na base", placeholder="Empresa, cidade ou CNPJ", key="busca_empresas")
    filtro_nicho = f2.selectbox("Filtrar nicho", nichos)
    filtro_status = f3.selectbox("Filtrar status", ["Todos"] + STATUS)
    base = listar_leads(termo, filtro_nicho, filtro_status)

    if base.empty:
        st.markdown(
            f"""
            <div class="dashed-empty">
              <div class="dashed-title">Nenhuma empresa com esses filtros</div>
              <p>A base tem {len(todos)} registro(s). Limpe os filtros ou cadastre a empresa manualmente.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.caption(
            f"{len(base)} de {len(todos)} empresas · dica: clique com o botão direito no cabeçalho de uma "
            "coluna (ex.: Empresa) para fixá-la durante a rolagem horizontal."
        )
        if base["origem"].astype(str).str.contains("OpenStreetMap", case=False, na=False).any():
            st.caption(
                "Parte desta base contém dados © "
                "[OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL."
            )
        colunas = [
            "id", "cnpj", "nome_empresa", "razao_social", "decisor", "nicho",
            "segmento_icp", "servicos_recomendados", "valor_proposta", "proximo_contato", "endereco", "cidade",
            "telefone", "site", "email", "status",
            "status_receita", "origem", "pontuacao", "motivo_qualificacao", "observacoes",
        ]
        # A grade mostra só os campos essenciais (o resto fica em "Detalhes completos"
        # abaixo) para evitar a rolagem horizontal densa de antes; os campos ocultos
        # continuam presentes nos dados e participam normalmente da atualização.
        colunas_visiveis = [
            "nome_empresa", "cidade", "nicho", "segmento_icp",
            "pontuacao", "status", "valor_proposta", "proximo_contato",
        ]
        editado = st.data_editor(
            base[colunas],
            width="stretch",
            hide_index=True,
            column_order=colunas_visiveis,
            disabled=[
                "id", "cnpj", "status_receita", "origem", "pontuacao",
                "motivo_qualificacao", "segmento_icp", "servicos_recomendados",
            ],
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "cnpj": st.column_config.TextColumn("CNPJ"),
                "nome_empresa": st.column_config.TextColumn("Empresa"),
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
        if st.button("Salvar alterações", type="primary", width="stretch"):
            quantidade = atualizar_leads(editado, base[colunas])
            st.success(f"{quantidade} lead(s) atualizado(s).")
            st.rerun()

        opcoes_empresas = {
            f"{linha['nome_empresa']} — #{linha['id']} · {linha['cidade'] or '—'} · {linha['status']}": int(linha["id"])
            for _, linha in base.iterrows()
        }

        with st.expander("Detalhes completos de uma empresa"):
            rotulo_detalhe = st.selectbox(
                "Selecione a empresa", ["Selecione..."] + list(opcoes_empresas.keys()), key="detalhe_empresa_select"
            )
            if rotulo_detalhe != "Selecione...":
                linha_detalhe = base[base["id"] == opcoes_empresas[rotulo_detalhe]].iloc[0]
                campos_detalhe = [
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
                            f'<div class="detail-label">{rotulo}</div><div class="detail-value">{valor}</div>'
                            '<div style="height:0.6rem"></div>',
                            unsafe_allow_html=True,
                        )

        st.divider()
        csv_data = base.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Exportar Visão Atual para CSV",
            data=csv_data,
            file_name=f"leads_scorpions_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            width="stretch",
        )

        st.markdown('<div style="height:0.8rem"></div>', unsafe_allow_html=True)
        col_cnpj_card, col_delete_card = st.columns(2)
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
                consultar_clicado = col_cnpj_botao.button("Consultar", type="primary", use_container_width=True)
                if consultar_clicado:
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
                            st.success("Empresa adicionada ao pipeline.")
                        elif duplicados:
                            st.info("Essa empresa já está cadastrada na base.")

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
                    "Excluir", key="abrir_confirmacao_exclusao", use_container_width=True
                ):
                    st.session_state["confirmar_exclusao_lead_id"] = opcoes_empresas[rotulo_excluir]
                    st.rerun()

        _lead_id_para_excluir = st.session_state.get("confirmar_exclusao_lead_id")
        if _lead_id_para_excluir:
            _linha_alvo = base[base["id"] == _lead_id_para_excluir]
            if _linha_alvo.empty:
                st.session_state["confirmar_exclusao_lead_id"] = None
            else:
                _linha_alvo = _linha_alvo.iloc[0]
                _confirmar_exclusao_lead(
                    int(_lead_id_para_excluir), _linha_alvo["nome_empresa"], _linha_alvo.get("cidade"), _linha_alvo["status"]
                )

st.markdown(
    """
    <div class="app-footer">
    <span>Scorpions CRM • Pipeline comercial e prospecção</span>
      <span>Atualizado em 2026</span>
    </div>
    """,
    unsafe_allow_html=True,
)

if aba_manual:
    aviso_nova_empresa = st.session_state.pop("aviso_nova_empresa", None)
    if aviso_nova_empresa:
        st.markdown(
            f'<span class="chip chip-accent" style="font-size:0.78rem;padding:0.35rem 0.7rem;">{aviso_nova_empresa}</span>',
            unsafe_allow_html=True,
        )

    # As chaves dos campos levam um sufixo de "versão" que só muda após um
    # cadastro bem-sucedido: popar st.session_state de um text_input dentro de
    # um form não garante, na prática, que o widget volte a ficar vazio (o
    # Streamlit pode preservar o valor já digitado). Trocar a chave força o
    # widget a nascer de novo, sem estado anterior.
    _versao_form_nova = st.session_state.get("versao_form_nova_empresa", 0)

    def _campo_nova(nome_campo: str) -> str:
        return f"novo_empresa_{nome_campo}_{_versao_form_nova}"

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
        c1, c2 = st.columns(2)
        nome = c1.text_input("Empresa *", key=_campo_nova("nome"))
        if _erro_nome:
            c1.markdown('<div style="font-size:0.72rem;color:#ff8f8f;margin-top:-0.6rem;">Obrigatório para salvar o registro.</div>', unsafe_allow_html=True)
        razao_social = c2.text_input("Razão social", key=_campo_nova("razao"))
        nicho = c1.text_input("Nicho *", key=_campo_nova("nicho"))
        if _erro_nicho:
            c1.markdown('<div style="font-size:0.72rem;color:#ff8f8f;margin-top:-0.6rem;">Obrigatório — define a fonte de prospecção.</div>', unsafe_allow_html=True)
        cnpj_manual = c2.text_input("CNPJ", key=_campo_nova("cnpj"))
        decisor = c1.text_input("Contato/decisor", key=_campo_nova("decisor"))
        cidade = c2.text_input("Cidade", key=_campo_nova("cidade"))
        endereco = c1.text_input("Endereço", key=_campo_nova("endereco"))
        telefone = c1.text_input("Telefone", key=_campo_nova("telefone"))
        email = c2.text_input("E-mail", key=_campo_nova("email"))
        site = c1.text_input("Site", key=_campo_nova("site"))
        c1, c2 = st.columns(2)
        valor_proposta_manual = c2.number_input(
            "Valor da Proposta (R$)", min_value=0.0, value=0.0, format="%.2f", key=_campo_nova("valor")
        )
        status_manual = c1.selectbox("Status", STATUS, key=_campo_nova("status"))
        proximo_contato_manual = c1.date_input("Próximo Contato", value=None, key=_campo_nova("proximo"))
        segmento_icp_manual = c2.selectbox(
            "Segmento ICP (opcional)",
            options=[""] + list(PERFIS_ICP.keys()),
            help="Se deixado em branco, será classificado automaticamente.",
            key=_campo_nova("icp"),
        )
        observacoes = st.text_area("Observações", key=_campo_nova("obs"))
        enviar = st.form_submit_button("Cadastrar lead", type="primary", width="stretch")

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
                st.session_state["versao_form_nova_empresa"] = _versao_form_nova + 1
                st.session_state["aviso_nova_empresa"] = f"Cadastrada e enviada a {status_manual}"
                st.rerun()
            else:
                st.warning("Esse lead já existe na base.")
