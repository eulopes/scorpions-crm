"""Níveis de usuário e regras de acesso do Scorpions CRM.

Quatro níveis, do mais restrito ao mais amplo: vendedor/operação, supervisor
(líder de equipe), gerente e diretor. Cada usuário recebe um nível na criação
da conta; só o diretor cria contas e define o nível de cada uma.

O módulo não depende do Streamlit nem do banco — só descreve as regras.
`app.py` consulta este dicionário para decidir o que mostrar/permitir.
"""

from __future__ import annotations

from typing import Any


NIVEIS: dict[str, dict[str, Any]] = {
    "vendedor": {
        "ordem": 1,
        "nome": "Vendedor / Operação",
        "descricao": (
            "Usa o CRM no dia a dia: prospecta, segue o funil e atualiza o "
            "status dos próprios leads."
        ),
        "escopo_leads": "proprios",
        "paginas_visiveis": (
            "Visão geral", "Pipeline", "Prospecção", "Empresas", "Nova empresa",
        ),
        "pode_criar_leads": True,
        "pode_editar_leads": True,
        "pode_excluir_leads": False,
        "pode_ver_automacao": False,
        "pode_gerenciar_campanhas": False,
        "pode_ver_pagina_equipe": False,
        "escopo_gestao_usuarios": None,   # não gerencia ninguém
        "escopo_atribuicao_carteira": None,
        "pode_criar_usuario": False,
    },
    "supervisor": {
        "ordem": 2,
        "nome": "Supervisor / Líder de equipe",
        "descricao": (
            "Acompanha a própria equipe: vê todos os vendedores e leads dela, "
            "muda status de conta, atribui carteira e administra o funil."
        ),
        "escopo_leads": "equipe",
        "paginas_visiveis": (
            "Visão geral", "Pipeline", "Prospecção", "Empresas", "Nova empresa",
            "Automação", "Equipe",
        ),
        "pode_criar_leads": True,
        "pode_editar_leads": True,
        "pode_excluir_leads": False,
        "pode_ver_automacao": True,
        "pode_gerenciar_campanhas": False,
        "pode_ver_pagina_equipe": True,
        "escopo_gestao_usuarios": "equipe",       # ativar/desativar vendedores da própria equipe
        "escopo_atribuicao_carteira": "equipe",    # atribuir leads aos vendedores da própria equipe
        "pode_criar_usuario": False,
    },
    "gerente": {
        "ordem": 3,
        "nome": "Gerente",
        "descricao": (
            "Enxerga toda a operação, administra os supervisores, redistribui "
            "carteira entre equipes e tem o relatório geral."
        ),
        "escopo_leads": "todos",
        "paginas_visiveis": (
            "Visão geral", "Pipeline", "Prospecção", "Empresas", "Nova empresa",
            "Automação", "Equipe",
        ),
        "pode_criar_leads": True,
        "pode_editar_leads": True,
        "pode_excluir_leads": True,
        "pode_ver_automacao": True,
        "pode_gerenciar_campanhas": True,
        "pode_ver_pagina_equipe": True,
        "escopo_gestao_usuarios": "niveis_1_2",   # ativar/desativar vendedor e supervisor
        "escopo_atribuicao_carteira": "todas",     # reatribui carteira entre equipes
        "pode_criar_usuario": False,
    },
    "diretor": {
        "ordem": 4,
        "nome": "Diretor",
        "descricao": (
            "Controle total: cria contas, define o nível de cada uma e "
            "administra toda a equipe, dados e configurações."
        ),
        "escopo_leads": "todos",
        "paginas_visiveis": (
            "Visão geral", "Pipeline", "Prospecção", "Empresas", "Nova empresa",
            "Automação", "Equipe",
        ),
        "pode_criar_leads": True,
        "pode_editar_leads": True,
        "pode_excluir_leads": True,
        "pode_ver_automacao": True,
        "pode_gerenciar_campanhas": True,
        "pode_ver_pagina_equipe": True,
        "escopo_gestao_usuarios": "todos",
        "escopo_atribuicao_carteira": "todas",
        "pode_criar_usuario": True,   # único nível que cria contas novas
    },
}

ORDEM_NIVEIS = tuple(sorted(NIVEIS, key=lambda chave: NIVEIS[chave]["ordem"]))


def nivel_valido(nivel: str) -> bool:
    return nivel in NIVEIS


def rotulo_nivel(nivel: str) -> str:
    return NIVEIS.get(nivel, {}).get("nome", nivel)


def paginas_visiveis(nivel: str) -> tuple[str, ...]:
    return tuple(NIVEIS.get(nivel, {}).get("paginas_visiveis", ()))


def pode(nivel: str, permissao: str) -> Any:
    """Devolve o valor da permissão para o nível (bool, string de escopo ou None)."""
    return NIVEIS.get(nivel, {}).get(permissao)


def niveis_administraveis_por(nivel: str) -> tuple[str, ...]:
    """Quais níveis esse usuário pode ativar/desativar (não criar — só o diretor cria)."""
    escopo = pode(nivel, "escopo_gestao_usuarios")
    if escopo == "todos":
        return ORDEM_NIVEIS
    if escopo == "niveis_1_2":
        return ("vendedor", "supervisor")
    if escopo == "equipe":
        return ("vendedor",)
    return ()
