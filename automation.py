"""Motor de campanhas automáticas do Scorpions CRM.

Este módulo não depende do Streamlit. Ele pode ser usado tanto pela interface
quanto pelo worker executado em segundo plano.
"""

from __future__ import annotations

import email
import json
import os
import re
import smtplib
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo

import requests

from crm_strategy import (
    enriquecer_lead_icp,
    mapear_status_funil,
    STATUS_FUNIL,
    PERFIS_ICP,
)
from niche_sources import (
    buscar_corretoras_cvm,
    buscar_empresas_b3,
    normalizar_cnpj,
    normalizar_texto,
    resolver_fontes_reais,
    roteamento_por_nicho,
    separar_cidade_uf,
)


APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("CRM_DB_PATH", str(APP_DIR / "scorpions_base.db")))
GOOGLE_PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
IBGE_MUNICIPIOS_URL = (
    "https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios"
)
OVERPASS_API_URL = (
    os.getenv("OVERPASS_API_URL", "https://overpass-api.de/api/interpreter").strip()
    or "https://overpass-api.de/api/interpreter"
)
BACEN_ENTIDADES_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/BcBase/versao/v2/odata/"
    "EntidadesSupervisionadas(dataBase=@dataBase)"
)
BACEN_CONTATOS_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/"
    "Instituicoes_em_funcionamento/versao/v1/odata/{recurso}"
)
BACEN_RECURSOS_CONTATO = (
    "SedesBancoComMultCE",
    "SedesCooperativas",
    "SedesSociedades",
    "SedesConsorcios",
)
FUSO_LOCAL = ZoneInfo("America/Sao_Paulo")
FONTE_AUTOMATICA = "Automático por nicho"
FONTE_CVM = "CVM - Corretoras"
FONTE_B3 = "B3 - Empresas listadas"
FONTE_OSM = "OpenStreetMap"
FONTES_AUTOMACAO = [
 FONTE_AUTOMATICA,
    FONTE_AUTOMATICA,
    "Bacen",
    FONTE_CVM,
    FONTE_B3,
    FONTE_OSM,
    "Google Places",
    "Demonstração",
 "Motor Contínuo (todas as fontes)",
]
LIMITE_MAXIMO = 100
LIMIAR_QUALIFICACAO = 70
_BLOQUEIO_OVERPASS = threading.Lock()

CABECALHOS_DADOS_PUBLICOS = {
    "Accept": "application/json",
    "User-Agent": "ScorpionsCRM/1.0 (consulta local com atribuicao OpenStreetMap)",
}

# O OSM não possui um catálogo empresarial uniforme. Estas regras usam somente
# tags documentadas e nomes comerciais para manter a consulta pequena e aderente.
REGRAS_OSM: tuple[tuple[tuple[str, ...], tuple[tuple[str, str], ...], str], ...] = (
    (
        ("tecnologia", "software", "informatica", "ti", "computador", "eletronico"),
        (
            ("office", r"^(it|software)$"),
        ),
        r"tecnologia|technology|software|sistemas?|inform[aá]tica|digital|tech",
    ),
    (
        ("clinica", "medico", "dentista", "odontologia", "saude"),
        (
            ("amenity", r"^(clinic|doctors|dentist)$"),
        ),
        r"cl[ií]nica|odontolog|dentista|m[eé]dic|sa[uú]de",
    ),
    (
        ("restaurante", "lanchonete", "cafeteria", "alimentacao"),
        (("amenity", r"^(restaurant|fast_food|cafe|food_court)$"),),
        r"restaurante|lanchonete|caf[eé]|bistr[oô]|pizzaria",
    ),
    (
        ("farmacia", "drogaria"),
        (("amenity", r"^pharmacy$"),),
        r"farm[aá]cia|drogaria",
    ),
    (
        ("academia", "fitness", "pilates"),
        (("leisure", r"^fitness_centre$"),),
        r"academia|fitness|pilates|crossfit",
    ),
    (
        ("escola", "colegio", "educacao", "curso"),
        (("amenity", r"^(school|college|kindergarten|language_school|music_school)$"),),
        r"escola|col[eé]gio|educa[cç][aã]o|cursos?",
    ),
    (
        ("hotel", "pousada", "hospedagem", "hostel"),
        (("tourism", r"^(hotel|hostel|guest_house|motel)$"),),
        r"hotel|pousada|hostel|hospedagem",
    ),
    (
        ("supermercado", "mercado", "mercearia"),
        (("shop", r"^(supermarket|convenience|grocery)$"),),
        r"supermercado|mercado|mercearia",
    ),
    (
        ("advogado", "advocacia", "juridico"),
        (("office", r"^lawyer$"),),
        r"advocacia|advogados?|jur[ií]dic",
    ),
    (
        ("contabilidade", "contador", "contabil"),
        (("office", r"^accountant$"),),
        r"contabilidade|contador|cont[aá]bil",
    ),
    (
        ("imobiliaria", "imoveis", "corretor imobiliario"),
        (("office", r"^estate_agent$"),),
        r"imobili[aá]ria|im[oó]veis|estate",
    ),
    (
        ("seguro", "seguros"),
        (("office", r"^insurance$"),),
        r"seguros?|corretora de seguros",
    ),
    (
        ("marketing", "publicidade", "propaganda", "agencia"),
        (("office", r"^(advertising_agency|marketing)$"),),
        r"marketing|publicidade|propaganda|ag[eê]ncia",
    ),
    (
        ("consultoria", "consultor"),
        (("office", r"^consulting$"),),
        r"consultoria|consulting|consultores?",
    ),
    (
        ("construcao", "construtora", "engenharia"),
        (
            ("office", r"^(construction_company|engineer)$"),
        ),
        r"constru[cç][aã]o|construtora|engenharia",
    ),
)

UF_POR_SIGLA = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas",
    "BA": "Bahia", "CE": "Ceará", "DF": "Distrito Federal",
    "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão",
    "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais",
    "PA": "Pará", "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco",
    "PI": "Piauí", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima",
    "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe",
    "TO": "Tocantins",
}

SINONIMOS_NICHO_BACEN = {
    "banco": ("banco",),
    "fintech": (
        "sociedade de credito direto",
        "sociedade de emprestimo entre pessoas",
        "instituicao de pagamento",
    ),
    "cooperativa": ("cooperativa de credito",),
    "consorcio": ("administradora de consorcio", "consorcio"),
    "pagamento": ("instituicao de pagamento", "pagamento"),
    "credito": ("credito",),
    "financeira": ("financeira", "financiamento e investimento"),
    "cambio": ("cambio",),
}


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(instante: datetime | None = None) -> str:
    return (instante or agora_utc()).astimezone(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conectar() -> Iterator[sqlite3.Connection]:
    conexao = sqlite3.connect(DB_PATH, timeout=30)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA busy_timeout = 30000")
    conexao.execute("PRAGMA foreign_keys = ON")
    try:
        yield conexao
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


def iniciar_banco_automacao() -> None:
    """Cria as tabelas do worker sem alterar ou remover leads existentes."""
    with conectar() as conexao:
        conexao.execute("PRAGMA journal_mode = WAL")
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
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS campanhas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                nicho TEXT NOT NULL,
                localizacao TEXT NOT NULL,
                fonte TEXT NOT NULL,
                limite_diario INTEGER NOT NULL DEFAULT 20,
                horario TEXT NOT NULL DEFAULT '08:00',
                ativa INTEGER NOT NULL DEFAULT 1,
                executando INTEGER NOT NULL DEFAULT 0,
                bloqueada_em TEXT,
                ultima_execucao TEXT,
                criada_em TEXT NOT NULL,
                atualizada_em TEXT NOT NULL
            )
            """
        )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS execucoes_automacao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campanha_id INTEGER,
                campanha_nome TEXT NOT NULL,
                inicio_em TEXT NOT NULL,
                fim_em TEXT,
                status TEXT NOT NULL,
                encontrados INTEGER NOT NULL DEFAULT 0,
                inseridos INTEGER NOT NULL DEFAULT 0,
                duplicados INTEGER NOT NULL DEFAULT 0,
                mensagem TEXT,
                FOREIGN KEY (campanha_id) REFERENCES campanhas(id) ON DELETE SET NULL
            )
            """
        )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS estado_automacao (
                chave TEXT PRIMARY KEY,
                valor TEXT,
                atualizado_em TEXT NOT NULL
            )
            """
        )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_fontes (
                chave TEXT PRIMARY KEY,
                fonte TEXT NOT NULL,
                resposta_json TEXT NOT NULL,
                expira_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL
            )
            """
        )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS bloqueios_fontes (
                chave TEXT PRIMARY KEY,
                bloqueado_ate TEXT NOT NULL,
                atualizado_em TEXT NOT NULL
            )
            """
        )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS atividades_comerciais (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER,
                tipo TEXT NOT NULL,
                descricao TEXT NOT NULL,
                usuario TEXT NOT NULL DEFAULT 'sistema',
                criado_em TEXT NOT NULL,
                FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE SET NULL
            )
            """
        )
        colunas_leads = {
            linha["name"] for linha in conexao.execute("PRAGMA table_info(leads)").fetchall()
        }
        for coluna, tipo in {
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
        }.items():
            if coluna not in colunas_leads:
                conexao.execute(f"ALTER TABLE leads ADD COLUMN {coluna} {tipo}")

        if "status" in colunas_leads:
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
        conexao.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_cnpj "
            "ON leads(cnpj) WHERE cnpj IS NOT NULL AND cnpj <> ''"
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
            "CREATE INDEX IF NOT EXISTS idx_campanhas_ativas ON campanhas(ativa, horario)"
        )
        conexao.execute(
            "CREATE INDEX IF NOT EXISTS idx_execucoes_inicio ON execucoes_automacao(inicio_em DESC)"
        )
        conexao.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_fontes_expiracao ON cache_fontes(expira_em)"
        )
        conexao.execute(
            "CREATE INDEX IF NOT EXISTS idx_atividades_lead_data "
            "ON atividades_comerciais(lead_id, criado_em DESC)"
        )

        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS alvos_continuos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                nicho TEXT NOT NULL,
                localizacao TEXT NOT NULL,
                ativa INTEGER NOT NULL DEFAULT 1,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL
            )
            """
        )
        conexao.execute("CREATE INDEX IF NOT EXISTS idx_alvos_continuos_ativa ON alvos_continuos(ativa)")

def criar_campanha(
    nome: str,
    nicho: str,
    localizacao: str,
    fonte: str,
    limite_diario: int,
    horario: str,
    ativa: bool = True,
) -> int:
    nome = nome.strip()
    nicho = nicho.strip()
    localizacao = localizacao.strip()
    # A fonte "Motor Contínuo" não exige nicho/localização na campanha, pois usa os alvos cadastrados.
    if not nome or (fonte != "Motor Contínuo (todas as fontes)" and (not nicho or not localizacao)):
        raise ValueError("Nome, nicho e localização são obrigatórios.")
    if fonte not in FONTES_AUTOMACAO:
        raise ValueError("Fonte de automação inválida.")
    try:
        datetime.strptime(horario, "%H:%M")
    except ValueError as erro:
        raise ValueError("O horário deve estar no formato HH:MM.") from erro

    limite = max(1, min(int(limite_diario), LIMITE_MAXIMO))
    agora = iso_utc()
    with conectar() as conexao:
        cursor = conexao.execute(
            """
            INSERT INTO campanhas (
                nome, nicho, localizacao, fonte, limite_diario, horario,
                ativa, criada_em, atualizada_em
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (nome, nicho, localizacao, fonte, limite, horario, int(ativa), agora, agora),
        )
        return int(cursor.lastrowid)


def listar_campanhas() -> list[dict[str, Any]]:
    with conectar() as conexao:
        linhas = conexao.execute(
            "SELECT * FROM campanhas ORDER BY ativa DESC, nome COLLATE NOCASE"
        ).fetchall()
    return [dict(linha) for linha in linhas]


def obter_campanha(campanha_id: int) -> dict[str, Any] | None:
    with conectar() as conexao:
        linha = conexao.execute(
            "SELECT * FROM campanhas WHERE id = ?", (int(campanha_id),)
        ).fetchone()
    return dict(linha) if linha else None


def alternar_campanha(campanha_id: int) -> bool:
    agora = iso_utc()
    with conectar() as conexao:
        conexao.execute(
            "UPDATE campanhas SET ativa = CASE ativa WHEN 1 THEN 0 ELSE 1 END, atualizada_em = ? WHERE id = ?",
            (agora, int(campanha_id)),
        )
        linha = conexao.execute(
            "SELECT ativa FROM campanhas WHERE id = ?", (int(campanha_id),)
        ).fetchone()
    if not linha:
        raise ValueError("Campanha não encontrada.")
    return bool(linha["ativa"])


def excluir_campanha(campanha_id: int) -> None:
    with conectar() as conexao:
        conexao.execute("DELETE FROM campanhas WHERE id = ?", (int(campanha_id),))


def criar_alvo_continuo(
    nome: str,
    nicho: str,
    localizacao: str,
    ativa: bool = True,
) -> int:
    """Cria um novo alvo para o motor de busca contínua."""
    nome = nome.strip()
    nicho = nicho.strip()
    localizacao = localizacao.strip()
    if not nome or not nicho or not localizacao:
        raise ValueError("Nome, nicho e localização são obrigatórios para um alvo contínuo.")

    agora = iso_utc()
    with conectar() as conexao:
        cursor = conexao.execute(
            """
            INSERT INTO alvos_continuos (
                nome, nicho, localizacao, ativa, criado_em, atualizado_em
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (nome, nicho, localizacao, int(ativa), agora, agora),
        )
        return int(cursor.lastrowid)


def listar_alvos_continuos() -> list[dict[str, Any]]:
    """Lista todos os alvos contínuos cadastrados."""
    with conectar() as conexao:
        linhas = conexao.execute(
            "SELECT * FROM alvos_continuos ORDER BY ativa DESC, nome COLLATE NOCASE"
        ).fetchall()
    return [dict(linha) for linha in linhas]


def registrar_atividade(
    tipo: str,
    descricao: str,
    lead_id: int | None = None,
    usuario: str = "sistema",
) -> None:
    """Registra uma ação comercial sem interromper o fluxo principal."""
    if not tipo.strip() or not descricao.strip():
        return
    with conectar() as conexao:
        conexao.execute(
            """
            INSERT INTO atividades_comerciais (lead_id, tipo, descricao, usuario, criado_em)
            VALUES (?, ?, ?, ?, ?)
            """,
            (lead_id, tipo.strip(), descricao.strip(), usuario.strip() or "sistema", iso_utc()),
        )


def listar_atividades(limite: int = 20) -> list[dict[str, Any]]:
    """Retorna as atividades mais recentes para a visão executiva."""
    limite_seguro = max(1, min(int(limite), 100))
    with conectar() as conexao:
        linhas = conexao.execute(
            """
            SELECT atividades_comerciais.*, leads.nome_empresa
            FROM atividades_comerciais
            LEFT JOIN leads ON leads.id = atividades_comerciais.lead_id
            ORDER BY atividades_comerciais.criado_em DESC
            LIMIT ?
            """,
            (limite_seguro,),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def alternar_alvo_continuo(alvo_id: int) -> bool:
    """Ativa ou desativa um alvo contínuo."""
    agora = iso_utc()
    with conectar() as conexao:
        conexao.execute(
            "UPDATE alvos_continuos SET ativa = CASE ativa WHEN 1 THEN 0 ELSE 1 END, atualizada_em = ? WHERE id = ?",
            (agora, int(alvo_id)),
        )
        linha = conexao.execute("SELECT ativa FROM alvos_continuos WHERE id = ?", (int(alvo_id),)).fetchone()
    if not linha:
        raise ValueError("Alvo contínuo não encontrado.")
    return bool(linha["ativa"])


def excluir_alvo_continuo(alvo_id: int) -> None:
    """Exclui um alvo contínuo."""
    with conectar() as conexao:
        conexao.execute("DELETE FROM alvos_continuos WHERE id = ?", (int(alvo_id),))


def listar_execucoes(limite: int = 50) -> list[dict[str, Any]]:
    with conectar() as conexao:
        linhas = conexao.execute(
            "SELECT * FROM execucoes_automacao ORDER BY inicio_em DESC LIMIT ?",
            (max(1, min(int(limite), 500)),),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def atualizar_heartbeat() -> None:
    agora = iso_utc()
    with conectar() as conexao:
        conexao.execute(
            """
            INSERT INTO estado_automacao (chave, valor, atualizado_em)
            VALUES ('worker_heartbeat', 'online', ?)
            ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor, atualizado_em = excluded.atualizado_em
            """,
            (agora,),
        )


def status_worker(tolerancia_segundos: int = 150) -> dict[str, Any]:
    with conectar() as conexao:
        linha = conexao.execute(
            "SELECT valor, atualizado_em FROM estado_automacao WHERE chave = 'worker_heartbeat'"
        ).fetchone()
    if not linha:
        return {"online": False, "atualizado_em": None}
    try:
        instante = datetime.fromisoformat(linha["atualizado_em"])
        online = agora_utc() - instante.astimezone(timezone.utc) <= timedelta(seconds=tolerancia_segundos)
    except (TypeError, ValueError):
        online = False
    return {"online": online, "atualizado_em": linha["atualizado_em"]}


def _campanha_deve_executar(campanha: dict[str, Any], instante: datetime) -> bool:
    if not campanha.get("ativa"):
        return False
    local = instante.astimezone(FUSO_LOCAL)
    hora, minuto = (int(parte) for parte in campanha["horario"].split(":"))
    agendado = local.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    if local < agendado:
        return False
    ultima = campanha.get("ultima_execucao")
    if not ultima:
        return True
    try:
        ultima_data = datetime.fromisoformat(ultima).astimezone(FUSO_LOCAL).date()
    except (TypeError, ValueError):
        return True
    return ultima_data < local.date()


def campanhas_pendentes(instante: datetime | None = None) -> list[dict[str, Any]]:
    agora = instante or agora_utc()
    limite_cooldown = iso_utc(agora - timedelta(minutes=15))
    with conectar() as conexao:
        falhas_recentes = {
            int(linha["campanha_id"])
            for linha in conexao.execute(
                """
                SELECT execucao.campanha_id
                FROM execucoes_automacao AS execucao
                JOIN (
                    SELECT campanha_id, MAX(id) AS ultimo_id
                    FROM execucoes_automacao
                    WHERE campanha_id IS NOT NULL
                    GROUP BY campanha_id
                ) AS ultima ON ultima.ultimo_id = execucao.id
                WHERE execucao.status = 'Erro' AND execucao.fim_em >= ?
                """,
                (limite_cooldown,),
            ).fetchall()
        }
    return [
        campanha
        for campanha in listar_campanhas()
        if int(campanha["id"]) not in falhas_recentes
        and _campanha_deve_executar(campanha, agora)
    ]


def _slug(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-") or "campanha"


def _gerar_demonstracao(campanha: dict[str, Any]) -> list[dict[str, Any]]:
    sufixos = ["Prime", "Central", "Horizonte", "Conecta", "Ideal", "Mais", "Nova", "Ponto"]
    quantidade = min(int(campanha["limite_diario"]), len(sufixos))
    nicho = campanha["nicho"]
    localizacao = campanha["localizacao"]
    return [
        {
            "place_id": f"auto-demo:{campanha['id']}:{indice}",
            "cnpj": None,
            "nome_empresa": f"{nicho} {sufixos[indice]}",
            "razao_social": "",
            "decisor": "",
            "nicho": nicho,
            "endereco": f"Endereço demonstrativo {indice + 1}, {localizacao}",
            "cidade": localizacao,
            "telefone": "",
            "site": "",
            "email": "",
            "status": "Novos Leads",
            "status_receita": "",
            "origem": "Automação demonstrativa",
            "observacoes": (
                f"Lead fictício criado pela campanha '{campanha['nome']}'. "
                "Não realizar contato; use apenas para validar o fluxo."
            ),
        }
        for indice in range(quantidade)
    ]


def _cache_obter(chave: str) -> Any | None:
    agora = iso_utc()
    with conectar() as conexao:
        linha = conexao.execute(
            "SELECT resposta_json FROM cache_fontes WHERE chave = ? AND expira_em > ?",
            (chave, agora),
        ).fetchone()
    if not linha:
        return None
    try:
        return json.loads(linha["resposta_json"])
    except (TypeError, json.JSONDecodeError):
        with conectar() as conexao:
            conexao.execute("DELETE FROM cache_fontes WHERE chave = ?", (chave,))
        return None


def _cache_salvar(chave: str, fonte: str, dados: Any, duracao: timedelta) -> None:
    agora = iso_utc()
    expira = iso_utc(agora_utc() + duracao)
    resposta_json = json.dumps(dados, ensure_ascii=False, separators=(",", ":"))
    with conectar() as conexao:
        conexao.execute(
            """
            INSERT INTO cache_fontes (chave, fonte, resposta_json, expira_em, atualizado_em)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chave) DO UPDATE SET
                fonte = excluded.fonte,
                resposta_json = excluded.resposta_json,
                expira_em = excluded.expira_em,
                atualizado_em = excluded.atualizado_em
            """,
            (chave, fonte, resposta_json, expira, agora),
        )
        conexao.execute("DELETE FROM cache_fontes WHERE expira_em <= ?", (agora,))


def _adquirir_bloqueio_fonte(chave: str, espera_segundos: float = 70) -> bool:
    prazo = time.monotonic() + espera_segundos
    while time.monotonic() < prazo:
        agora = iso_utc()
        bloqueado_ate = iso_utc(agora_utc() + timedelta(seconds=90))
        with conectar() as conexao:
            cursor = conexao.execute(
                """
                INSERT INTO bloqueios_fontes (chave, bloqueado_ate, atualizado_em)
                VALUES (?, ?, ?)
                ON CONFLICT(chave) DO UPDATE SET
                    bloqueado_ate = excluded.bloqueado_ate,
                    atualizado_em = excluded.atualizado_em
                WHERE bloqueios_fontes.bloqueado_ate <= ?
                """,
                (chave, bloqueado_ate, agora, agora),
            )
        if cursor.rowcount == 1:
            return True
        time.sleep(0.5)
    return False


def _liberar_bloqueio_fonte(chave: str) -> None:
    agora = iso_utc()
    with conectar() as conexao:
        conexao.execute(
            "UPDATE bloqueios_fontes SET bloqueado_ate = ?, atualizado_em = ? WHERE chave = ?",
            (agora, agora, chave),
        )


def _codigo_municipio_ibge(cidade: str, uf: str) -> str:
    if not uf:
        raise ValueError(
            "O OpenStreetMap precisa da UF para delimitar o município, por exemplo: Campinas, SP."
        )
    chave_cache = f"ibge:municipios:v1:{uf}"
    municipios = _cache_obter(chave_cache)
    if not isinstance(municipios, list):
        resposta = requests.get(
            IBGE_MUNICIPIOS_URL.format(uf=uf),
            params={"orderBy": "nome"},
            headers=CABECALHOS_DADOS_PUBLICOS,
            timeout=25,
        )
        if not resposta.ok:
            raise RuntimeError(
                f"A API de localidades do IBGE respondeu {resposta.status_code}."
            )
        try:
            municipios = resposta.json()
        except ValueError as erro:
            raise RuntimeError("A API de localidades do IBGE devolveu dados inválidos.") from erro
        if not isinstance(municipios, list):
            raise RuntimeError("A API de localidades do IBGE não devolveu uma lista de municípios.")
        _cache_salvar(chave_cache, "IBGE Localidades", municipios, timedelta(days=30))

    cidade_alvo = normalizar_texto(cidade)
    municipio = next(
        (
            item
            for item in municipios
            if isinstance(item, dict)
            and normalizar_texto(item.get("nome")) == cidade_alvo
        ),
        None,
    )
    codigo = str(municipio.get("id") if municipio else "")
    if not re.fullmatch(r"\d{7}", codigo):
        raise ValueError(
            f"Município '{cidade}, {uf}' não encontrado na base oficial do IBGE."
        )
    return codigo


def _regra_openstreetmap(nicho: str) -> tuple[tuple[tuple[str, str], ...], str]:
    termo = normalizar_texto(nicho)
    for marcadores, filtros_tags, filtro_nome in REGRAS_OSM:
        if any(
            re.search(rf"(?<!\w){re.escape(marcador)}(?!\w)", termo)
            for marcador in marcadores
        ):
            return filtros_tags, filtro_nome

    stopwords = {
        "a", "as", "com", "da", "das", "de", "do", "dos", "e", "em", "empresa",
        "empresas", "para", "por", "servico", "servicos", "setor", "segmento",
    }
    palavras = [
        palavra
        for palavra in re.findall(r"[a-z0-9]+", termo)
        if len(palavra) >= 3 and palavra not in stopwords
    ][:6]
    if not palavras:
        raise ValueError("Informe um nicho mais específico para consultar o OpenStreetMap.")
    return (), "|".join(re.escape(palavra) for palavra in palavras)


def _consulta_overpass(
    codigo_ibge: str,
    filtros_tags: tuple[tuple[str, str], ...],
    filtro_nome: str,
) -> str:
    if filtros_tags:
        chave, expressao = filtros_tags[0]
        busca_candidatos = (
            f'nwr(area.areaBusca)["name"]["{chave}"~"{expressao}",i]->.candidatos;'
        )
    else:
        busca_candidatos = (
            f'nwr(area.areaBusca)["name"~"{filtro_nome}",i]->.candidatos;'
        )
    filtros_contato = (
        "phone", "contact:phone", "mobile", "contact:mobile", "contact:whatsapp",
        "email", "contact:email", "website", "contact:website", "url",
        "contact:instagram", "contact:facebook",
    )
    consultas_contato = [f'  nwr.candidatos["{chave}"];' for chave in filtros_contato]
    return "\n".join(
        [
            "[out:json][timeout:45];",
            f'rel["IBGE:GEOCODIGO"="{codigo_ibge}"]["admin_level"="8"]->.municipio;',
            ".municipio out tags;",
            ".municipio map_to_area->.areaBusca;",
            busca_candidatos,
            "(",
            *consultas_contato,
            ");",
            "out tags center qt 200;",
        ]
    )


def _primeira_tag(tags: dict[str, Any], *nomes: str) -> str:
    return next(
        (str(tags.get(nome) or "").strip() for nome in nomes if str(tags.get(nome) or "").strip()),
        "",
    )


def _url_social_openstreetmap(tags: dict[str, Any]) -> str:
    for rede, chaves in (
        ("instagram", ("contact:instagram", "instagram")),
        ("facebook", ("contact:facebook", "facebook")),
    ):
        valor = _primeira_tag(tags, *chaves)
        if not valor:
            continue
        if valor.lower().startswith(("http://", "https://")):
            return valor
        usuario = valor.strip().lstrip("@").strip("/")
        if usuario and re.fullmatch(r"[A-Za-z0-9._-]+", usuario):
            return f"https://www.{rede}.com/{usuario}"
    return ""


def _telefone_openstreetmap(tags: dict[str, Any]) -> str:
    valor = _primeira_tag(
        tags, "contact:phone", "phone", "contact:mobile", "mobile", "contact:whatsapp"
    )
    return valor if len(re.sub(r"\D", "", valor)) >= 8 else ""


def _email_openstreetmap(tags: dict[str, Any]) -> str:
    valor = _primeira_tag(tags, "contact:email", "email")
    candidatos = re.split(r"[;,\s]+", valor)
    return next(
        (
            email
            for email in candidatos
            if re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email)
        ),
        "",
    )


def _site_openstreetmap(tags: dict[str, Any]) -> str:
    valor = _primeira_tag(tags, "contact:website", "website", "url")
    if not valor:
        return _url_social_openstreetmap(tags)
    candidato = valor.split(";")[0].strip()
    if candidato and not candidato.lower().startswith(("http://", "https://")):
        candidato = f"https://{candidato}"
    analisado = urlparse(candidato)
    return candidato if analisado.scheme in {"http", "https"} and analisado.hostname else ""


def _endereco_openstreetmap(tags: dict[str, Any]) -> str:
    endereco_completo = _primeira_tag(tags, "addr:full")
    if endereco_completo:
        return endereco_completo
    rua = _primeira_tag(tags, "addr:street", "addr:place")
    numero = _primeira_tag(tags, "addr:housenumber")
    bairro = _primeira_tag(tags, "addr:suburb", "addr:neighbourhood")
    cep = _primeira_tag(tags, "addr:postcode")
    primeira_linha = ", ".join(parte for parte in (rua, numero) if parte)
    partes = [primeira_linha, bairro, f"CEP {cep}" if cep else ""]
    return " - ".join(parte for parte in partes if parte)


def _leads_openstreetmap(
    elementos: list[Any],
    nicho: str,
    cidade: str,
    uf: str,
    filtros_tags: tuple[tuple[str, str], ...],
) -> list[dict[str, Any]]:
    leads: list[dict[str, Any]] = []
    vistos: set[str] = set()
    assinaturas_por_nome: dict[str, set[str]] = {}
    for elemento in elementos:
        if not isinstance(elemento, dict):
            continue
        tags = elemento.get("tags")
        if not isinstance(tags, dict):
            continue
        if tags.get("boundary") == "administrative" and tags.get("IBGE:GEOCODIGO"):
            continue
        nome = _primeira_tag(tags, "name", "brand", "operator")
        tipo = str(elemento.get("type") or "").strip()
        identificador = str(elemento.get("id") or "").strip()
        if not nome or tipo not in {"node", "way", "relation"} or not identificador.isdigit():
            continue

        telefone = _telefone_openstreetmap(tags)
        email = _email_openstreetmap(tags)
        site = _site_openstreetmap(tags)
        if not any(_contato_util(contato) for contato in (telefone, email, site)):
            continue

        place_id = f"osm:{tipo}:{identificador}"
        if place_id in vistos:
            continue
        vistos.add(place_id)
        nome_normalizado = normalizar_texto(nome)
        endereco = _endereco_openstreetmap(tags)
        assinaturas = {
            assinatura
            for assinatura in (
                re.sub(r"\D", "", telefone),
                normalizar_texto(email),
                normalizar_texto(site).removeprefix("https ").removeprefix("http ").removeprefix("www "),
                normalizar_texto(endereco),
            )
            if assinatura
        }
        if assinaturas_por_nome.get(nome_normalizado, set()) & assinaturas:
            continue
        assinaturas_por_nome.setdefault(nome_normalizado, set()).update(assinaturas)

        categoria_confirmada = any(
            re.fullmatch(expressao, str(tags.get(chave) or ""), flags=re.IGNORECASE)
            for chave, expressao in filtros_tags[:1]
        )
        cnpj = normalizar_cnpj(
            _primeira_tag(tags, "cnpj", "ref:cnpj", "ref:vatin", "contact:cnpj")
        )
        categorias = [
            f"{chave}={tags[chave]}"
            for chave in ("office", "shop", "amenity", "healthcare", "tourism", "leisure", "craft")
            if tags.get(chave)
        ]
        url_elemento = f"https://www.openstreetmap.org/{tipo}/{identificador}"
        descricao_categoria = ", ".join(categorias) or "nome comercial compatível"
        leads.append(
            {
                "place_id": place_id,
                "cnpj": cnpj or None,
                "nome_empresa": nome,
                "razao_social": "",
                "decisor": "",
                "nicho": nicho,
                "endereco": endereco,
                "cidade": f"{cidade}, {uf}",
                "telefone": telefone,
                "site": site,
                "email": email,
                "status": "Novos Leads",
                "status_receita": "Não verificada (OpenStreetMap)",
                "origem": "OpenStreetMap contributors (ODbL)",
                "observacoes": (
                    f"Categoria OSM: {descricao_categoria}. Registro: {url_elemento}. "
                    "Situação cadastral não verificada; confirme os dados antes do contato."
                ),
                "_aderencia_osm": "tag" if categoria_confirmada else "nome",
                "_categoria_osm_confirmada": categoria_confirmada,
            }
        )

    leads.sort(
        key=lambda lead: (
            -sum(_contato_util(lead.get(campo)) for campo in ("telefone", "email", "site")),
            -int(bool(lead.get("endereco"))),
            normalizar_texto(lead.get("nome_empresa")),
        )
    )
    return leads


def _priorizar_leads_openstreetmap(
    leads: list[dict[str, Any]], limite: int
) -> list[dict[str, Any]]:
    with conectar() as conexao:
        cadastrados = {
            str(linha["place_id"])
            for linha in conexao.execute(
                "SELECT place_id FROM leads WHERE place_id LIKE 'osm:%'"
            ).fetchall()
        }
    novos = [lead for lead in leads if str(lead.get("place_id") or "") not in cadastrados]
    existentes = [lead for lead in leads if str(lead.get("place_id") or "") in cadastrados]
    return (novos + existentes)[:limite]


def buscar_openstreetmap(nicho: str, localizacao: str, limite: int) -> list[dict[str, Any]]:
    """Busca negócios locais no Overpass sem chave e com atribuição explícita ao OSM."""
    cidade, uf = separar_cidade_uf(localizacao)
    if not cidade or not uf:
        raise ValueError(
            "O OpenStreetMap exige Município, UF; a busca nacional seria ampla demais."
        )
    limite = max(1, min(int(limite), LIMITE_MAXIMO))
    codigo_ibge = _codigo_municipio_ibge(cidade, uf)
    chave_cache = f"osm:leads:v2:{codigo_ibge}:{normalizar_texto(nicho)}"
    em_cache = _cache_obter(chave_cache)
    if isinstance(em_cache, list):
        return _priorizar_leads_openstreetmap(
            [lead for lead in em_cache if isinstance(lead, dict)], limite
        )

    # Impede consultas Overpass simultâneas dentro do app; o cache evita repetições por 24 h.
    with _BLOQUEIO_OVERPASS:
        em_cache = _cache_obter(chave_cache)
        if isinstance(em_cache, list):
            return _priorizar_leads_openstreetmap(
                [lead for lead in em_cache if isinstance(lead, dict)], limite
            )

        if not _adquirir_bloqueio_fonte("overpass:consulta"):
            raise RuntimeError(
                "Outra consulta ao OpenStreetMap ainda está em andamento. Tente novamente em instantes."
            )
        try:
            # Outro processo pode ter preenchido o cache enquanto aguardávamos o bloqueio.
            em_cache = _cache_obter(chave_cache)
            if isinstance(em_cache, list):
                return _priorizar_leads_openstreetmap(
                    [lead for lead in em_cache if isinstance(lead, dict)], limite
                )

            filtros_tags, filtro_nome = _regra_openstreetmap(nicho)
            consulta = _consulta_overpass(codigo_ibge, filtros_tags, filtro_nome)
            resposta = requests.post(
                OVERPASS_API_URL,
                data={"data": consulta},
                headers=CABECALHOS_DADOS_PUBLICOS,
                timeout=65,
            )
            if not resposta.ok:
                if resposta.status_code in {429, 502, 503, 504}:
                    raise RuntimeError(
                        "O servidor público do OpenStreetMap está ocupado. Aguarde alguns minutos e tente novamente."
                    )
                raise RuntimeError(
                    f"O servidor público do OpenStreetMap respondeu {resposta.status_code}."
                )
            try:
                dados = resposta.json()
            except ValueError as erro:
                raise RuntimeError("O OpenStreetMap devolveu uma resposta inválida.") from erro
            elementos = dados.get("elements") if isinstance(dados, dict) else None
            if not isinstance(elementos, list):
                raise RuntimeError("O OpenStreetMap não devolveu a lista de resultados esperada.")
            municipio_encontrado = any(
                isinstance(elemento, dict)
                and elemento.get("type") == "relation"
                and str((elemento.get("tags") or {}).get("IBGE:GEOCODIGO") or "")
                == codigo_ibge
                for elemento in elementos
            )
            if not municipio_encontrado:
                raise RuntimeError(
                    "O limite municipal oficial não foi encontrado no OpenStreetMap; "
                    "nenhum resultado foi armazenado."
                )
            leads = _leads_openstreetmap(
                elementos, nicho, cidade, uf, filtros_tags
            )
            duracao_cache = timedelta(hours=24 if leads else 6)
            _cache_salvar(chave_cache, FONTE_OSM, leads, duracao_cache)
            return _priorizar_leads_openstreetmap(leads, limite)
        finally:
            _liberar_bloqueio_fonte("overpass:consulta")


def _escapar_odata(texto: str) -> str:
    return texto.replace("'", "''")


def _consultar_odata(url: str, parametros: dict[str, Any], timeout: int) -> requests.Response:
    # O servidor Olinda do Bacen não interpreta corretamente espaços codificados como "+".
    # quote_via=quote força "%20", que é aceito pelo endpoint OData.
    consulta = urlencode(parametros, quote_via=quote)
    return requests.get(
        f"{url}?{consulta}",
        headers={"Accept": "application/json", "User-Agent": "ScorpionsCRM/1.0"},
        timeout=timeout,
    )


def _endereco_bacen(contato: dict[str, Any]) -> str:
    logradouro = str(contato.get("ENDERECO") or "").strip()
    complemento = str(contato.get("COMPLEMENTO") or "").strip()
    bairro = str(contato.get("BAIRRO") or "").strip()
    cep = str(contato.get("CEP") or "").strip()
    partes = [logradouro]
    if complemento:
        partes.append(complemento)
    if bairro:
        partes.append(bairro)
    if cep:
        partes.append(f"CEP {cep}")
    return " - ".join(parte for parte in partes if parte)


def _site_bacen(valor: Any) -> str:
    site = str(valor or "").strip()
    if site and not site.lower().startswith(("http://", "https://")):
        site = f"https://{site}"
    return site


def _telefone_bacen(contato: dict[str, Any]) -> str:
    ddd = re.sub(r"\D", "", str(contato.get("DDD") or ""))
    numero = re.sub(r"\D", "", str(contato.get("TELEFONE") or ""))
    if not numero:
        return ""
    if ddd and not numero.startswith(ddd):
        return f"({ddd}) {numero}"
    return numero


def _buscar_contatos_bacen(cidade: str, uf: str) -> dict[str, dict[str, Any]]:
    filtros: list[str] = []
    if cidade:
        cidade_odata = _escapar_odata(cidade.upper())
        filtros.append(f"MUNICIPIO eq '{cidade_odata}'")
    if uf:
        filtros.append(f"UF eq '{uf}'")

    contatos: dict[str, dict[str, Any]] = {}
    for recurso in BACEN_RECURSOS_CONTATO:
        try:
            # Este serviço legado retorna erro quando recebe $skip; o filtro por município
            # mantém a resposta pequena, então usamos um único limite amplo.
            parametros: dict[str, Any] = {"$top": 5_000, "$format": "json"}
            if filtros:
                parametros["$filter"] = " and ".join(filtros)
            resposta = _consultar_odata(
                BACEN_CONTATOS_URL.format(recurso=recurso),
                parametros,
                timeout=25,
            )
            if not resposta.ok:
                continue
            itens = resposta.json().get("value", [])
            if not isinstance(itens, list):
                continue
        except (requests.RequestException, ValueError, AttributeError):
            continue

        for item in itens:
            if not isinstance(item, dict):
                continue
            cnpj_basico = re.sub(r"\D", "", str(item.get("CNPJ") or ""))
            if len(cnpj_basico) == 8 and cnpj_basico not in contatos:
                contatos[cnpj_basico] = item
    return contatos


def buscar_bacen_instituicoes(nicho: str, localizacao: str, limite: int) -> list[dict[str, Any]]:
    """Busca instituições financeiras ativas em duas bases públicas do Bacen, sem chave."""
    cidade, uf = separar_cidade_uf(localizacao, permite_brasil=True)
    limite = max(1, min(int(limite), LIMITE_MAXIMO))

    filtros = ["codigoTipoSituacaoPessoaJuridica eq '3'"]
    if cidade:
        cidade_odata = _escapar_odata(cidade.casefold())
        filtros.append(f"tolower(nomeDoMunicipio) eq '{cidade_odata}'")
    if uf:
        filtros.append(f"nomeDaUnidadeFederativa eq '{_escapar_odata(UF_POR_SIGLA[uf])}'")

    campos = ",".join(
        [
            "database", "codigoIdentificadorBacen", "codigoCNPJ14", "codigoCNPJ8",
            "nomeEntidadeInteresse", "nomeFantasia", "nomeDoMunicipio",
            "nomeDaUnidadeFederativa", "descricaoTipoEntidadeSupervisionada",
            "descricaoTipoSituacaoPessoaJuridica", "descricaoNaturezaJuridica",
        ]
    )
    parametros_base = {
        "$filter": " and ".join(filtros),
        "$select": campos,
        "$orderby": "nomeEntidadeInteresse",
        "$top": 500,
        "$format": "json",
    }

    entidades: list[dict[str, Any]] | None = None
    ultimo_erro = ""
    hoje_local = agora_utc().astimezone(FUSO_LOCAL).date()
    for atraso in range(8):
        data_base = hoje_local - timedelta(days=atraso)
        registros_data: list[dict[str, Any]] = []
        consulta_valida = True
        for deslocamento in range(0, 5_000, 500):
            parametros = {
                "@dataBase": f"'{data_base:%m-%d-%Y}'",
                **parametros_base,
                "$skip": deslocamento,
            }
            try:
                resposta = _consultar_odata(BACEN_ENTIDADES_URL, parametros, timeout=30)
                if not resposta.ok:
                    ultimo_erro = f"status {resposta.status_code}"
                    consulta_valida = False
                    break
                dados = resposta.json()
                valor = dados.get("value") if isinstance(dados, dict) else None
                if not isinstance(valor, list):
                    ultimo_erro = "resposta sem a lista de registros"
                    consulta_valida = False
                    break
                registros_data.extend(item for item in valor if isinstance(item, dict))
                if len(valor) < 500:
                    break
            except requests.RequestException as erro:
                ultimo_erro = str(erro)
                consulta_valida = False
                break
            except (ValueError, AttributeError):
                ultimo_erro = "resposta JSON inválida"
                consulta_valida = False
                break
        if consulta_valida:
            entidades = registros_data
            break

    if entidades is None:
        raise RuntimeError(f"Não foi possível consultar a base pública do Bacen: {ultimo_erro}.")

    termo = normalizar_texto(nicho)
    termos_amplos = {
        "", "todos", "todas", "bacen", "financeiro",
        "instituicao financeira", "instituicoes financeiras",
    }
    consulta_ampla = termo in termos_amplos or any(
        expressao in termo
        for expressao in (
            "setor financeiro", "mercado financeiro", "servico financeiro",
            "servicos financeiros",
        )
    )
    if not consulta_ampla:
        termos_equivalentes = {
            equivalente
            for marcador, equivalentes in SINONIMOS_NICHO_BACEN.items()
            if marcador in termo
            for equivalente in equivalentes
        } or {termo}
        entidades = [
            entidade
            for entidade in entidades
            if any(
                equivalente in normalizar_texto(
                    f"{entidade.get('descricaoTipoEntidadeSupervisionada', '')} "
                    f"{entidade.get('nomeEntidadeInteresse', '')}"
                )
                for equivalente in termos_equivalentes
            )
        ]

    contatos = _buscar_contatos_bacen(cidade, uf)
    leads: list[dict[str, Any]] = []
    for entidade in entidades[:limite]:
        cnpj = re.sub(r"\D", "", str(entidade.get("codigoCNPJ14") or ""))
        if len(cnpj) != 14:
            continue
        cnpj_basico = str(entidade.get("codigoCNPJ8") or cnpj[:8])
        contato = contatos.get(cnpj_basico, {})
        razao_social = str(entidade.get("nomeEntidadeInteresse") or "Instituição sem nome").strip()
        nome = str(entidade.get("nomeFantasia") or razao_social).strip()
        municipio = str(entidade.get("nomeDoMunicipio") or cidade).strip()
        nome_uf = str(entidade.get("nomeDaUnidadeFederativa") or UF_POR_SIGLA.get(uf, "")).strip()
        sigla_entidade = uf or next(
            (
                sigla
                for sigla, nome_estado in UF_POR_SIGLA.items()
                if normalizar_texto(nome_estado) == normalizar_texto(nome_uf)
            ),
            nome_uf,
        )
        tipo = str(entidade.get("descricaoTipoEntidadeSupervisionada") or "Instituição financeira").strip()
        data_base = str(entidade.get("database") or "").strip()
        natureza = str(entidade.get("descricaoNaturezaJuridica") or "").strip()
        observacoes = "Entidade autorizada em atividade segundo a base pública do Banco Central do Brasil."
        if data_base:
            observacoes += f" Data-base: {data_base}."
        if natureza:
            observacoes += f" Natureza jurídica: {natureza}."
        observacoes += " Confirme os dados antes de realizar contato comercial."

        leads.append(
            {
                "place_id": f"bacen:{entidade.get('codigoIdentificadorBacen') or cnpj}",
                "cnpj": cnpj,
                "nome_empresa": nome,
                "razao_social": razao_social,
                "decisor": "",
                "nicho": tipo,
                "endereco": _endereco_bacen(contato),
                "cidade": ", ".join(parte for parte in [municipio, sigla_entidade] if parte),
                "telefone": _telefone_bacen(contato),
                "site": _site_bacen(contato.get("SITIO_NA_INTERNET")),
                "email": str(contato.get("E_MAIL") or "").strip(),
                "status": "Novos Leads",
                "status_receita": "Autorizada em Atividade (Bacen)",
                "origem": "Banco Central do Brasil",
                "observacoes": observacoes,
            }
        )
    return leads


def _buscar_bacen(campanha: dict[str, Any]) -> list[dict[str, Any]]:
    return buscar_bacen_instituicoes(
        campanha["nicho"], campanha["localizacao"], int(campanha["limite_diario"])
    )


def _cidade_google_place(item: dict[str, Any]) -> str:
    componentes = item.get("addressComponents")
    cidade = uf = ""
    if isinstance(componentes, list):
        for tipo_cidade in ("locality", "administrative_area_level_2", "postal_town"):
            componente = next(
                (
                    parte
                    for parte in componentes
                    if isinstance(parte, dict) and tipo_cidade in parte.get("types", [])
                ),
                None,
            )
            if componente:
                cidade = str(
                    componente.get("longText") or componente.get("shortText") or ""
                ).strip()
                break
        componente_uf = next(
            (
                parte
                for parte in componentes
                if isinstance(parte, dict)
                and "administrative_area_level_1" in parte.get("types", [])
            ),
            None,
        )
        if componente_uf:
            uf = str(
                componente_uf.get("shortText") or componente_uf.get("longText") or ""
            ).strip().upper()

    if not cidade:
        endereco = str(item.get("formattedAddress") or "")
        correspondencia = re.search(r",\s*([^,]+?)\s*-\s*([A-Z]{2})(?:,|$)", endereco)
        if correspondencia:
            cidade, uf = correspondencia.group(1).strip(), correspondencia.group(2)
    return ", ".join(parte for parte in (cidade, uf) if parte)


def _buscar_google_places(campanha: dict[str, Any]) -> list[dict[str, Any]]:
    chave = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if not chave:
        raise RuntimeError("GOOGLE_PLACES_API_KEY não configurada no ambiente do worker.")

    limite = min(int(campanha["limite_diario"]), LIMITE_MAXIMO)
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": chave,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.addressComponents,places.nationalPhoneNumber,"
            "places.websiteUri,places.types,nextPageToken"
        ),
    }
    resultados: list[dict[str, Any]] = []
    token: str | None = None

    while len(resultados) < limite:
        corpo: dict[str, Any] = {
            "textQuery": f"{campanha['nicho']} em {campanha['localizacao']}",
            "pageSize": min(20, limite - len(resultados)),
            "languageCode": "pt-BR",
            "regionCode": "BR",
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
                detalhe = resposta.json().get("error", {}).get("message", resposta.text)
            except ValueError:
                detalhe = resposta.text
            raise RuntimeError(f"Google Places respondeu {resposta.status_code}: {detalhe}")

        dados = resposta.json()
        for item in dados.get("places", []):
            resultados.append(
                {
                    "place_id": item.get("id", ""),
                    "cnpj": None,
                    "nome_empresa": item.get("displayName", {}).get("text", "Sem nome"),
                    "razao_social": "",
                    "decisor": "",
                    "nicho": campanha["nicho"],
                    "endereco": item.get("formattedAddress", ""),
                    "cidade": _cidade_google_place(item),
                    "telefone": item.get("nationalPhoneNumber", ""),
                    "site": item.get("websiteUri", ""),
                    "email": "",
                    "status": "Novos Leads",
                    "status_receita": "",
                    "origem": "Automação Google Places",
                    "observacoes": f"Encontrado automaticamente pela campanha '{campanha['nome']}'.",
                }
            )
            if len(resultados) >= limite:
                break
        token = dados.get("nextPageToken")
        if not token or len(resultados) >= limite:
            break
        time.sleep(1)
    return resultados


def buscar_leads_motor_continuo(
    campanha: dict[str, Any],
) -> list[dict[str, Any]]:
    """Varre todos os alvos contínuos ativos e busca leads automaticamente para cada um."""
    alvos = listar_alvos_continuos()
    alvos_ativos = [alvo for alvo in alvos if alvo["ativa"]]
    if not alvos_ativos:
        raise RuntimeError("Nenhum alvo contínuo ativo configurado. Cadastre alvos na aba 'Automação'.")

    leads_encontrados: list[dict[str, Any]] = []
    vistos: set[str] = set()
    total_alvos = len(alvos_ativos)
    alvos_processados = 0
    
    for alvo in alvos_ativos:
        alvos_processados += 1
        try:
            # Limite por alvo para não estourar APIs e distribuir a busca.
            # O limite total da campanha é o que realmente importa.
            limite_por_alvo = max(1, int(campanha["limite_diario"] / total_alvos))
            
            candidatos = buscar_leads_automaticamente(
                alvo["nicho"],
                alvo["localizacao"],
                limite_por_alvo,
                f"Campanha '{campanha['nome']}' (Alvo: {alvo['nome']})",
            )
            for lead in candidatos:
                # Deduplicação global entre alvos
                chave = normalizar_cnpj(lead.get("cnpj")) or str(lead.get("place_id") or "").strip()
                if chave and chave in vistos:
                    continue
                if chave:
                    vistos.add(chave)
                leads_encontrados.append(lead)
                if len(leads_encontrados) >= campanha["limite_diario"]:
                    return leads_encontrados
        except (requests.RequestException, RuntimeError, ValueError) as erro:
            print(f"AVISO: Falha ao processar alvo '{alvo['nome']}': {erro}")
            continue
    return leads_encontrados


def _localizacao_nacional(localizacao: str) -> bool:
    return normalizar_texto(localizacao) in {"", "brasil", "nacional", "todo o brasil"}


def _localizacao_compativel(lead: dict[str, Any], localizacao: str) -> bool:
    if _localizacao_nacional(localizacao):
        return True
    cidade_alvo, uf_alvo = separar_cidade_uf(localizacao)
    cidade_lead = str(lead.get("cidade") or "").strip()
    endereco_lead = str(lead.get("endereco") or "").strip()
    try:
        cidade_encontrada, uf_encontrada = separar_cidade_uf(cidade_lead)
    except ValueError:
        cidade_encontrada, uf_encontrada = cidade_lead, ""
    if normalizar_texto(cidade_encontrada) != normalizar_texto(cidade_alvo):
        return False
    if not uf_alvo:
        return True
    if uf_encontrada:
        return uf_encontrada == uf_alvo
    local_completo = normalizar_texto(f"{cidade_lead} {endereco_lead}")
    return bool(re.search(rf"(?:^|\s|[,/\-]){re.escape(uf_alvo.casefold())}(?:$|\s|[,/\-])", local_completo))


def _contato_util(valor: Any) -> bool:
    texto = str(valor or "").strip()
    marcador = normalizar_texto(texto)
    invalidos = {
        "-", "--", "n/a", "na", "null", "none", "nao informado", "nao informada",
        "sem contato", "sem email", "sem e-mail", "sem site", "sem telefone",
    }
    return bool(texto and marcador not in invalidos and any(c.isalnum() for c in texto))


def _nicho_compativel(lead: dict[str, Any], nicho: str, fonte: str) -> bool:
    if fonte not in resolver_fontes_reais(nicho):
        return False
    origem = normalizar_texto(lead.get("origem"))
    pesquisavel = normalizar_texto(
        " ".join(
            str(lead.get(campo) or "")
            for campo in ("nicho", "nome_empresa", "razao_social", "observacoes")
        )
    )
    termo = normalizar_texto(nicho)
    if fonte == "Google Places":
        return termo == normalizar_texto(lead.get("nicho"))
    if fonte == FONTE_OSM:
        return lead.get("_aderencia_osm") in {"tag", "nome"} and (
            termo == normalizar_texto(lead.get("nicho"))
        )
    if fonte == FONTE_CVM:
        return "cvm" in origem and any(
            marcador in pesquisavel
            for marcador in ("corretora", "distribuidora", "valores mobiliarios", "ctvm", "dtvm")
        )
    if fonte == FONTE_B3:
        return "b3" in origem
    if fonte == "Bacen":
        if "banco central" not in origem and "bacen" not in origem:
            return False
        termos_amplos = {
            "bacen", "financeiro", "instituicao financeira",
            "instituicoes financeiras",
        }
        if termo in termos_amplos or any(
            expressao in termo
            for expressao in (
                "setor financeiro", "mercado financeiro", "servico financeiro",
                "servicos financeiros",
            )
        ):
            return True
        equivalentes = {
            equivalente
            for marcador, sinonimos in SINONIMOS_NICHO_BACEN.items()
            if marcador in termo
            for equivalente in sinonimos
        } or {termo}
        return any(equivalente in pesquisavel for equivalente in equivalentes)
    return False


def _calcular_score_comercial(lead: dict[str, Any], nicho: str, localizacao: str) -> tuple[int, list[str]]:
    """Avalia a intenção comercial do lead para priorizar oportunidades melhores."""
    score = 0
    motivos: list[str] = []

    if lead.get("site"):
        score += 10
        motivos.append("site disponível")
    if lead.get("email"):
        score += 15
        motivos.append("e-mail informado")
    if lead.get("telefone"):
        score += 12
        motivos.append("telefone informado")
    if lead.get("cnpj"):
        score += 10
        motivos.append("CNPJ identificado")

    nicho_normalizado = normalizar_texto(nicho)
    texto_empresa = normalizar_texto(
        " ".join(
            parte for parte in (
                lead.get("nome_empresa"),
                lead.get("razao_social"),
                lead.get("nicho"),
                lead.get("segmento_icp") or "",
            )
            if parte
        )
    )
    if nicho_normalizado and nicho_normalizado in texto_empresa:
        score += 12
        motivos.append("match forte de nicho")
    elif any(palavra in texto_empresa for palavra in re.findall(r"[a-z0-9]+", nicho_normalizado)):
        score += 6
        motivos.append("compatibilidade parcial de nicho")

    if localizacao and normalizar_texto(localizacao) in normalizar_texto(str(lead.get("cidade") or "")):
        score += 14
        motivos.append("localização direta")

    segmento = str(lead.get("segmento_icp") or "").strip()
    if segmento and segmento not in {"Não classificado", ""}:
        score += 10
        motivos.append("segmento ICP definido")

    if lead.get("origem") and "BrasilAPI" in str(lead.get("origem")):
        score += 8
        motivos.append("fonte cadastral robusta")

    if lead.get("status_receita") and "ativa" in normalizar_texto(str(lead.get("status_receita"))):
        score += 8
        motivos.append("empresa ativa")

    return min(score, 100), motivos


def qualificar_leads_por_nicho(
    leads: list[dict[str, Any]],
    nicho: str,
    localizacao: str,
    fonte_resolvida: str,
    limite: int | None = None,
) -> list[dict[str, Any]]:
    """Pontua e mantém apenas candidatos aderentes, localizados e contatáveis."""
    fontes_esperadas = resolver_fontes_reais(nicho)
    if fonte_resolvida not in fontes_esperadas:
        return []

    fonte_oficial = fonte_resolvida in {"Bacen", FONTE_CVM, FONTE_B3}
    negativos = ("baixada", "cancelada", "inapta", "suspensa", "inativa")
    ativos = ("ativa", "atividade", "funcionamento normal")
    aprovados: list[dict[str, Any]] = []
    maximo = max(1, min(int(limite or LIMITE_MAXIMO), LIMITE_MAXIMO))

    for original in leads:
        lead = dict(original)
        if not _nicho_compativel(lead, nicho, fonte_resolvida):
            continue
        situacao = normalizar_texto(lead.get("status_receita"))
        if any(marcador in situacao for marcador in negativos):
            continue
        situacao_ativa = any(marcador in situacao for marcador in ativos)
        if fonte_oficial and not situacao_ativa:
            continue
        if not _localizacao_compativel(lead, localizacao):
            continue

        contatos = []
        for nome in ("telefone", "email", "site"):
            if _contato_util(lead.get(nome)):
                contatos.append(nome)
            else:
                lead[nome] = ""
        if not contatos:
            continue

        lead = enriquecer_lead_icp(lead)
        score_tecnico = 35
        motivos = [f"nicho compatível via {fonte_resolvida}"]
        score_tecnico += 15
        motivos.append("localização compatível")
        score_tecnico += 10
        motivos.append("fonte apropriada ao segmento")
        if situacao_ativa:
            score_tecnico += 15
            motivos.append("atividade confirmada")
        else:
            motivos.append("situação cadastral não verificada")

        if fonte_resolvida == FONTE_OSM and lead.get("_categoria_osm_confirmada"):
            score_tecnico += 5
            motivos.append("categoria comercial confirmada no OpenStreetMap")

        cnpj = normalizar_cnpj(lead.get("cnpj"))
        if cnpj:
            lead["cnpj"] = cnpj
            score_tecnico += 5
            motivos.append("CNPJ válido")
        score_tecnico += 8 * len(contatos)
        motivos.append("contato disponível: " + ", ".join(contatos))

        score_comercial, motivos_comerciais = _calcular_score_comercial(lead, nicho, localizacao)
        pontuacao = min(100, score_tecnico + score_comercial)
        motivos.extend(motivos_comerciais)

        lead["pontuacao"] = min(pontuacao, 100)
        lead["motivo_qualificacao"] = "; ".join(motivos) + "."
        if lead["pontuacao"] >= LIMIAR_QUALIFICACAO:
            aprovados.append(lead)
        if len(aprovados) >= maximo:
            break
    return sorted(aprovados, key=lambda item: int(item.get("pontuacao") or 0), reverse=True)


def buscar_leads_por_fonte(
    fonte: str,
    nicho: str,
    localizacao: str,
    limite: int,
    nome_campanha: str = "Busca manual",
) -> list[dict[str, Any]]:
    """Executa uma fonte real específica sem aplicar fallback demonstrativo."""
    limite = max(1, min(int(limite), LIMITE_MAXIMO))
    if fonte == "Bacen":
        return buscar_bacen_instituicoes(nicho, localizacao, limite)
    if fonte == FONTE_CVM:
        return buscar_corretoras_cvm(nicho, localizacao, limite)
    if fonte == FONTE_B3:
        return buscar_empresas_b3(nicho, localizacao, limite)
    if fonte == FONTE_OSM:
        return buscar_openstreetmap(nicho, localizacao, limite)
    if fonte == "Google Places":
        return _buscar_google_places(
            {
                "nome": nome_campanha,
                "nicho": nicho,
                "localizacao": localizacao,
                "limite_diario": limite,
            }
        )
    raise RuntimeError(f"Fonte não suportada: {fonte}")


def buscar_leads_automaticamente(
    nicho: str,
    localizacao: str,
    limite: int,
    nome_campanha: str = "Busca automática",
) -> list[dict[str, Any]]:
    """Seleciona a fonte pelo nicho e devolve somente leads aprovados pelo filtro."""
    nicho = str(nicho or "").strip()
    if not nicho:
        raise ValueError("Informe o nicho para o roteamento automático.")
    if not str(localizacao or "").strip():
        raise ValueError("Informe o município e a UF, ou use 'Brasil' para uma busca nacional.")

    limite = max(1, min(int(limite), LIMITE_MAXIMO))
    fontes = resolver_fontes_reais(nicho)

    aprovados: list[dict[str, Any]] = []
    vistos: set[str] = set()
    total_candidatos = 0
    erros: list[str] = []
    for fonte in fontes:
        quantidade_candidatos = min(LIMITE_MAXIMO, max(limite * 3, limite))
        try:
            candidatos = buscar_leads_por_fonte(
                fonte,
                nicho,
                localizacao,
                quantidade_candidatos,
                nome_campanha,
            )
        except (requests.RequestException, RuntimeError, ValueError) as erro:
            erros.append(f"{fonte}: {erro}")
            continue
        total_candidatos += len(candidatos)
        qualificados = qualificar_leads_por_nicho(
            candidatos,
            nicho,
            localizacao,
            fonte,
            limite - len(aprovados),
        )
        for lead in qualificados:
            chave = (
                normalizar_cnpj(lead.get("cnpj"))
                or str(lead.get("place_id") or "").strip()
                or normalizar_texto(f"{lead.get('nome_empresa')}|{lead.get('endereco')}")
            )
            if chave and chave in vistos:
                continue
            if chave:
                vistos.add(chave)
            aprovados.append(lead)
            if len(aprovados) >= limite:
                return aprovados

    if aprovados:
        return aprovados
    if total_candidatos:
        raise RuntimeError(
            f"{total_candidatos} candidato(s) foram encontrados, mas nenhum passou pelo filtro "
            "de nicho, localização, situação cadastral quando disponível e contato."
        )
    if erros:
        raise RuntimeError("Não foi possível consultar as fontes: " + " | ".join(erros))
    return []


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(instante: datetime | None = None) -> str:
    return (instante or agora_utc()).astimezone(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conectar() -> Iterator[sqlite3.Connection]:
    conexao = sqlite3.connect(DB_PATH, timeout=30)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA busy_timeout = 30000")
    conexao.execute("PRAGMA foreign_keys = ON")
    try:
        yield conexao
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


def _lead_existente_id(
    conexao: sqlite3.Connection,
    lead: dict[str, Any],
    cnpj: str | None,
) -> int | None:
    if cnpj:
        linha = conexao.execute("SELECT id FROM leads WHERE cnpj = ?", (cnpj,)).fetchone()
        if linha:
            return int(linha["id"])
    place_id = str(lead.get("place_id") or "").strip()
    if place_id:
        linha = conexao.execute(
            "SELECT id FROM leads WHERE place_id = ?", (place_id,)
        ).fetchone()
        if linha:
            return int(linha["id"])
    if not cnpj and not place_id:
        nome = str(lead.get("nome_empresa") or "").strip()
        endereco = str(lead.get("endereco") or "").strip()
        if nome and endereco:
            linha = conexao.execute(
                "SELECT id FROM leads WHERE nome_empresa = ? AND endereco = ? "
                "AND (cnpj IS NULL OR cnpj = '') AND (place_id IS NULL OR place_id = '')",
                (nome, endereco),
            ).fetchone()
            if linha:
                return int(linha["id"])
    return None


def _enriquecer_lead_existente(
    conexao: sqlite3.Connection,
    lead_id: int,
    lead: dict[str, Any],
    cnpj: str | None,
    agora: str,
) -> None:
    pontuacao = lead.get("pontuacao")
    parametros = {
        "id": lead_id,
        "place_id": str(lead.get("place_id") or "").strip(),
        "cnpj": cnpj or "",
        "razao_social": str(lead.get("razao_social") or "").strip(),
        "decisor": str(lead.get("decisor") or "").strip(),
        "endereco": str(lead.get("endereco") or "").strip(),
        "cidade": str(lead.get("cidade") or "").strip(),
        "telefone": str(lead.get("telefone") or "").strip(),
        "site": str(lead.get("site") or "").strip(),
        "email": str(lead.get("email") or "").strip(),
        "status_receita": str(lead.get("status_receita") or "").strip(),
        "observacoes": str(lead.get("observacoes") or "").strip(),
        "pontuacao": int(pontuacao) if pontuacao is not None else None,
        "motivo": str(lead.get("motivo_qualificacao") or "").strip(),
        "segmento_icp": str(lead.get("segmento_icp") or "").strip(),
        "servicos_recomendados": str(lead.get("servicos_recomendados") or "").strip(),
        "valor_proposta": float(lead.get("valor_proposta") or 0.0),
        "proximo_contato": str(lead.get("proximo_contato") or "").strip() or None,
        "agora": agora,
    }
    conexao.execute(
        """
        UPDATE leads
        SET place_id = CASE WHEN COALESCE(place_id, '') = '' THEN :place_id ELSE place_id END,
            cnpj = CASE WHEN COALESCE(cnpj, '') = '' THEN :cnpj ELSE cnpj END,
            razao_social = CASE WHEN COALESCE(razao_social, '') = '' THEN :razao_social ELSE razao_social END,
            decisor = CASE WHEN COALESCE(decisor, '') = '' THEN :decisor ELSE decisor END,
            endereco = CASE WHEN COALESCE(endereco, '') = '' THEN :endereco ELSE endereco END,
            cidade = CASE WHEN COALESCE(cidade, '') = '' THEN :cidade ELSE cidade END,
            telefone = CASE WHEN COALESCE(telefone, '') = '' THEN :telefone ELSE telefone END,
            site = CASE WHEN COALESCE(site, '') = '' THEN :site ELSE site END,
            email = CASE WHEN COALESCE(email, '') = '' THEN :email ELSE email END,
            status_receita = CASE
                WHEN COALESCE(status_receita, '') = '' THEN :status_receita ELSE status_receita END,
            observacoes = CASE
                WHEN COALESCE(observacoes, '') = '' THEN :observacoes ELSE observacoes END,
            segmento_icp = CASE WHEN COALESCE(segmento_icp, '') = '' THEN :segmento_icp ELSE segmento_icp END,
            servicos_recomendados = CASE WHEN COALESCE(servicos_recomendados, '') = '' THEN :servicos_recomendados ELSE servicos_recomendados END,
            valor_proposta = CASE WHEN valor_proposta IS NULL THEN :valor_proposta ELSE valor_proposta END,
            proximo_contato = CASE WHEN proximo_contato IS NULL THEN :proximo_contato ELSE proximo_contato END,
            motivo_qualificacao = CASE
                WHEN :pontuacao IS NOT NULL
                 AND (pontuacao IS NULL OR :pontuacao >= pontuacao)
                THEN CASE WHEN :motivo <> '' THEN :motivo ELSE motivo_qualificacao END
                ELSE motivo_qualificacao END,
            pontuacao = CASE
                WHEN :pontuacao IS NOT NULL
                 AND (pontuacao IS NULL OR :pontuacao >= pontuacao)
                THEN :pontuacao ELSE pontuacao END,
            atualizado_em = :agora
        WHERE id = :id
        """,
        parametros,
    )


def _salvar_leads(leads: list[dict[str, Any]]) -> tuple[int, int]:
    agora = iso_utc()
    inseridos = 0
    leads_enriquecidos = [enriquecer_lead_icp(lead) for lead in leads]
    with conectar() as conexao:
        for lead in leads_enriquecidos:
            cnpj_normalizado = normalizar_cnpj(lead.get("cnpj")) or None
            try:
                cursor = conexao.execute(
                    """
                    INSERT INTO leads (
                        place_id, cnpj, nome_empresa, razao_social, decisor, nicho,
                        endereco, cidade, telefone, site, email, status, valor_proposta, proximo_contato, status_receita,
                        origem, observacoes, pontuacao, motivo_qualificacao, segmento_icp,
                        servicos_recomendados, criado_em, atualizado_em
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lead.get("place_id"), cnpj_normalizado, lead["nome_empresa"],
                        lead.get("razao_social", ""), lead.get("decisor", ""), lead["nicho"],
                        lead.get("endereco", ""), lead.get("cidade", ""),
                        lead.get("telefone", ""), lead.get("site", ""), lead.get("email", ""),
                        lead.get("status", "Novos Leads"),
                        float(lead.get("valor_proposta") or 0.0),
                        lead.get("proximo_contato"),
                        lead.get("status_receita", ""),
                        lead.get("origem", "Automação"), lead.get("observacoes", ""),
                        lead.get("pontuacao"), lead.get("motivo_qualificacao", ""),
                        lead.get("segmento_icp"), lead.get("servicos_recomendados"),
                        agora, agora,
                    ),
                )
                inseridos += 1
                conexao.execute(
                    """
                    INSERT INTO atividades_comerciais (lead_id, tipo, descricao, usuario, criado_em)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        int(cursor.lastrowid),
                        "lead_criado",
                        f"Lead '{lead['nome_empresa']}' adicionado à base.",
                        "sistema",
                        agora,
                    ),
                )
            except sqlite3.IntegrityError:
                lead_id = _lead_existente_id(conexao, lead, cnpj_normalizado)
                if lead_id is None:
                    raise
                _enriquecer_lead_existente(conexao, lead_id, lead, cnpj_normalizado, agora)
    return inseridos, len(leads_enriquecidos) - inseridos


def salvar_leads_no_banco(leads: list[dict[str, Any]]) -> tuple[int, int]:
    """Salva leads e enriquece duplicados sem sobrescrever o trabalho comercial."""
    return _salvar_leads(leads)


def _adquirir_campanha(campanha_id: int) -> bool:
    agora = iso_utc()
    limite_bloqueio = iso_utc(agora_utc() - timedelta(minutes=30))
    with conectar() as conexao:
        cursor = conexao.execute(
            """
            UPDATE campanhas
            SET executando = 1, bloqueada_em = ?, atualizada_em = ?
            WHERE id = ?
              AND (executando = 0 OR bloqueada_em IS NULL OR bloqueada_em < ?)
            """,
            (agora, agora, int(campanha_id), limite_bloqueio),
        )
        return cursor.rowcount == 1


def _liberar_campanha(campanha_id: int, executada_em: str, sucesso: bool) -> None:
    with conectar() as conexao:
        if sucesso:
            conexao.execute(
                """
                UPDATE campanhas
                SET executando = 0, bloqueada_em = NULL, ultima_execucao = ?, atualizada_em = ?
                WHERE id = ?
                """,
                (executada_em, executada_em, int(campanha_id)),
            )
        else:
            conexao.execute(
                """
                UPDATE campanhas
                SET executando = 0, bloqueada_em = NULL, atualizada_em = ?
                WHERE id = ?
                """,
                (executada_em, int(campanha_id)),
            )


def executar_campanha(campanha_id: int) -> dict[str, Any]:
    campanha = obter_campanha(campanha_id)
    if not campanha:
        return {"status": "Erro", "mensagem": "Campanha não encontrada."}
    if not _adquirir_campanha(campanha_id):
        return {"status": "Ignorada", "mensagem": "A campanha já está em execução."}

    inicio = iso_utc()
    with conectar() as conexao:
        cursor = conexao.execute(
            """
            INSERT INTO execucoes_automacao (
                campanha_id, campanha_nome, inicio_em, status, mensagem
            ) VALUES (?, ?, ?, 'Executando', '')
            """,
            (int(campanha_id), campanha["nome"], inicio),
        )
        execucao_id = int(cursor.lastrowid)

    status = "Sucesso"
    encontrados = inseridos = duplicados = 0
    mensagem = ""
    try:
        if campanha["fonte"] == "Demonstração":
            leads = _gerar_demonstracao(campanha)
        elif campanha["fonte"] == FONTE_AUTOMATICA:
            leads = buscar_leads_automaticamente(
                campanha["nicho"],
                campanha["localizacao"],
                int(campanha["limite_diario"]),
                campanha["nome"],
            )
        elif campanha["fonte"] == "Motor Contínuo (todas as fontes)":
            leads = buscar_leads_motor_continuo(campanha)
        else:
            leads = buscar_leads_por_fonte(
                campanha["fonte"],
                campanha["nicho"],
                campanha["localizacao"],
                int(campanha["limite_diario"]),
                campanha["nome"],
            )
        encontrados = len(leads)
        inseridos, duplicados = _salvar_leads(leads)
        mensagem = f"{encontrados} encontrado(s), {inseridos} novo(s) e {duplicados} duplicado(s)."
    except (requests.RequestException, RuntimeError, ValueError) as erro:
        status = "Erro"
        mensagem = str(erro)
    except Exception as erro:  # Protege o loop do worker e registra a falha inesperada.
        status = "Erro"
        mensagem = f"Falha inesperada: {erro}"
    finally:
        fim = iso_utc()
        with conectar() as conexao:
            conexao.execute(
                """
                UPDATE execucoes_automacao
                SET fim_em = ?, status = ?, encontrados = ?, inseridos = ?,
                    duplicados = ?, mensagem = ?
                WHERE id = ?
                """,
                (fim, status, encontrados, inseridos, duplicados, mensagem, execucao_id),
            )
        _liberar_campanha(campanha_id, fim, status == "Sucesso")

    return {
        "status": status,
        "campanha_id": campanha_id,
        "campanha": campanha["nome"],
        "encontrados": encontrados,
        "inseridos": inseridos,
        "duplicados": duplicados,
        "mensagem": mensagem,
    }


def _enviar_alerta_vencimento(lead: dict[str, Any]):
    host = os.getenv("SMTP_HOST")
    port_str = os.getenv("SMTP_PORT")
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_SENDER_EMAIL")
    recipient = os.getenv("SMTP_RECIPIENT_EMAIL")
    crm_base_url = os.getenv("CRM_BASE_URL", "http://localhost:8501")

    if not all((host, port_str, user, password, sender, recipient)):
        print(f"AVISO: Configuração de SMTP incompleta. Alerta para o lead #{lead['id']} não enviado.")
        return

    try:
        port = int(port_str)
    except (ValueError, TypeError):
        print(f"ERRO: SMTP_PORT ('{port_str}') é inválido.")
        return

    try:
        data_contato = datetime.strptime(lead["proximo_contato"], "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        data_contato = "Data Inválida"

    subject = f"Alerta de Follow-up Vencido: {lead['nome_empresa']}"
    body = f"""
    <p>Olá,</p>
    <p>Este é um alerta de que o prazo para o próximo contato com o lead <strong>{lead['nome_empresa']}</strong>
    venceu em <strong>{data_contato}</strong>.</p>
    <p><strong>Detalhes do Lead:</strong></p>
    <ul>
        <li><strong>ID:</strong> {lead['id']}</li>
        <li><strong>Empresa:</strong> {lead['nome_empresa']}</li>
        <li><strong>Status Atual:</strong> {lead['status']}</li>
        <li><strong>Valor da Proposta:</strong> R$ {lead.get('valor_proposta') or 0.0:,.2f}</li>
        <li><strong>Telefone:</strong> {lead.get('telefone') or 'N/A'}</li>
        <li><strong>E-mail:</strong> {lead.get('email') or 'N/A'}</li>
        <li><strong>Observações:</strong> {lead.get('observacoes') or 'N/A'}</li>
    </ul>
    <p>Acesse o lead no CRM para mais detalhes e para registrar o contato: <a href="{crm_base_url}/?lead_id={lead['id']}">{crm_base_url}/?lead_id={lead['id']}</a></p>
    <p>--<br>Scorpions CRM</p>
    """
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = recipient

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(sender, [recipient], msg.as_string())
            print(f"INFO: Alerta de vencimento enviado para o lead #{lead['id']}.")
    except Exception as e:
        print(f"ERRO: Falha ao enviar e-mail de alerta para o lead #{lead['id']}: {e}")
        raise


def _verificar_e_alertar_vencidos() -> int:
    """Busca leads com 'Próximo Contato' vencido e envia alertas por e-mail."""
    if not os.getenv("SMTP_HOST"):
        return 0

    hoje_str = agora_utc().date().isoformat()
    with conectar() as conexao:
        # A data do alerta é comparada com a data do próximo contato para permitir
        # que um novo alerta seja enviado se a data for adiada e vencer novamente.
        leads_vencidos = conexao.execute(
            """
            SELECT * FROM leads
            WHERE proximo_contato IS NOT NULL AND proximo_contato <> '' AND proximo_contato < ?
              AND (alerta_vencido_em IS NULL OR alerta_vencido_em < proximo_contato)
              AND status NOT IN ('Fechado / Contrato', 'Descartado')
            """,
            (hoje_str,),
        ).fetchall()

    enviados = 0
    if not leads_vencidos:
        return 0

    for linha in leads_vencidos:
        lead = dict(linha)
        try:
            _enviar_alerta_vencimento(lead)
            with conectar() as conexao:
                conexao.execute(
                    "UPDATE leads SET alerta_vencido_em = ? WHERE id = ?", (hoje_str, lead["id"])
                )
            enviados += 1
        except Exception:
            # O erro já foi logado em _enviar_alerta_vencimento.
            # Continuamos para o próximo lead.
            continue
    return enviados


def executar_tarefas_rotineiras() -> list[dict[str, Any]]:
    atualizar_heartbeat()
    _verificar_e_alertar_vencidos()
    return [executar_campanha(campanha["id"]) for campanha in campanhas_pendentes(agora_utc())]


iniciar_banco_automacao()
