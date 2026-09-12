"""Testes do adapter de São Paulo (SISSEL). Usa DataFrames sintéticos que
reproduzem o layout real (cabeçalho variável + linhas de processo) -- não
depende de arquivo .xls real baixado."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

_TMP = Path(tempfile.gettempdir()) / "scorpions_test_obras_sp.duckdb"
_TMP.unlink(missing_ok=True)
os.environ["OBRAS_DUMP_DB"] = str(_TMP)

import obras_dump as od  # noqa: E402
from obras_sp import (  # noqa: E402
    _achar_colunas,
    _data,
    _endereco_e_numero,
    _tipo_do_texto,
    _uso_da_categoria,
    listar_arquivos_mes,
    mapear_linha_sissel,
    parsear_dataframe,
)

# Réplica minimalista do layout real: título (linhas 0-2), cabeçalho na
# linha 3, duas linhas de processo, uma linha de rodapé (sem "Alvará" válido).
_COLS = 12
_CABECALHO = [None] * _COLS
_CABECALHO[1] = "Alvará"
_CABECALHO[2] = "Descrição"
_CABECALHO[3] = "SQL_Incra"
_CABECALHO[4] = "Categoria de uso"
_CABECALHO[5] = "Bairro"
_CABECALHO[6] = "Área da construção (m²)"
_CABECALHO[7] = "Proprietário"
_CABECALHO[8] = "Endereço"
_CABECALHO[9] = "Aprovação"
_CABECALHO[10] = "Administração regional"


def _linha_processo(over: dict[int, object] | None = None):
    linha = [None] * _COLS
    linha[1] = "2026.00.100-00"
    linha[2] = "ALVARA DE APROVACAO E EXECUCAO DE EDIFICACAO NOVA"
    linha[3] = "046.062.0027-8"
    linha[4] = "IND.1A"
    linha[5] = "VILA GUMERCINDO"
    linha[6] = "1.067,33"
    linha[7] = "ASPECT MIDIA IND ELETR COMERCIO E SERV LTDA"
    linha[8] = "R   PEDRALIA 00399 "
    linha[9] = "23/01/2026"
    linha[10] = "IPIRANGA"
    for chave, valor in (over or {}).items():
        linha[chave] = valor
    return linha


def _df(linhas_processo):
    linhas = [[None] * _COLS, [None] * _COLS, [None] * _COLS, _CABECALHO, *linhas_processo]
    return pd.DataFrame(linhas)


class FuncoesPurasTest(unittest.TestCase):
    def test_tipo_reconhece_variacoes_comuns(self):
        self.assertEqual(_tipo_do_texto("ALVARA DE APROVACAO E EXECUCAO DE EDIFICACAO NOVA"), "execucao")
        self.assertEqual(_tipo_do_texto("CERTIFICADO DE CONCLUSAO"), "habite-se")
        self.assertEqual(_tipo_do_texto("REVALIDACAO DO ALVARA DE FUNCIONAMENTO"), "outro")
        self.assertEqual(_tipo_do_texto("PROJETO MODIFICATIVO DE ALVARA DE APROVACAO"), "aprovacao")

    def test_uso_por_prefixo_da_categoria(self):
        self.assertEqual(_uso_da_categoria("IND.1A"), "industrial")
        self.assertEqual(_uso_da_categoria("NR3.04"), "comercial")
        self.assertEqual(_uso_da_categoria("R2V"), "residencial")
        self.assertEqual(_uso_da_categoria("HIS 2"), "residencial")
        self.assertEqual(_uso_da_categoria(""), "outro")

    def test_endereco_extrai_logradouro_e_numero(self):
        self.assertEqual(_endereco_e_numero("R   PEDRALIA 00399 "), ("R PEDRALIA", "399"))
        self.assertEqual(
            _endereco_e_numero("R PASCOAL RANIERI 33 SONORA GARDEN"), ("R PASCOAL RANIERI", "33")
        )
        self.assertEqual(_endereco_e_numero(""), ("", ""))
        self.assertEqual(_endereco_e_numero("SEM NUMERO NENHUM"), ("SEM NUMERO NENHUM", ""))

    def test_data_formato_br(self):
        self.assertEqual(_data("23/01/2026"), date(2026, 1, 23))
        self.assertIsNone(_data(""))
        self.assertIsNone(_data("data invalida"))


class MapeamentoTest(unittest.TestCase):
    def test_mapear_linha_sissel_produz_registro_canonico_completo(self):
        valores = {
            "id_alvara": "2026.00.356-00", "_descricao": "ALVARA DE APROVACAO E EXECUCAO DE EDIFICACAO NOVA",
            "sql_iptu": "046.062.0027-8", "_categoria_uso": "IND.1A", "bairro": "VILA GUMERCINDO",
            "area_construida": "1.067,33", "proprietario": "ASPECT MIDIA IND ELETR COMERCIO E SERV LTDA",
            "_endereco_bruto": "R   PEDRALIA 00399 ", "_data_emissao_bruta": "23/01/2026",
            "subprefeitura": "IPIRANGA",
        }
        r = mapear_linha_sissel(valores)
        self.assertEqual(r["id_alvara"], "2026.00.356-00")
        self.assertEqual(r["cidade"], "sao-paulo")
        self.assertEqual(r["tipo"], "execucao")
        self.assertEqual(r["uso"], "industrial")
        self.assertEqual(r["endereco"], "R PEDRALIA")
        self.assertEqual(r["numero"], "399")
        self.assertEqual(r["data_emissao"], date(2026, 1, 23))
        self.assertEqual(r["municipio_nome"], "São Paulo")
        self.assertEqual(r["uf"], "SP")
        self.assertEqual(r["proprietario"], "ASPECT MIDIA IND ELETR COMERCIO E SERV LTDA")


class PlanilhaTest(unittest.TestCase):
    def test_acha_cabecalho_mesmo_com_titulo_de_tamanho_variavel(self):
        df = _df([_linha_processo()])
        idx, colunas = _achar_colunas(df)
        self.assertEqual(idx, 3)
        self.assertEqual(colunas["id_alvara"], 1)

    def test_cabecalho_ausente_levanta_erro_claro(self):
        with self.assertRaises(ValueError):
            _achar_colunas(pd.DataFrame([[None] * 5] * 5))

    def test_parsear_dataframe_ignora_linhas_sem_numero_de_alvara_valido(self):
        df = _df([
            _linha_processo(),
            _linha_processo({1: "Página:1 / 1"}),  # rodapé
            _linha_processo({1: None}),             # vazia
        ])
        registros = parsear_dataframe(df)
        self.assertEqual(len(registros), 1)
        self.assertEqual(registros[0]["id_alvara"], "2026.00.100-00")

    def test_registro_carrega_no_store_canonico(self):
        registros = parsear_dataframe(_df([_linha_processo()]))
        with od.conectar() as con:
            con.execute("DROP TABLE IF EXISTS alvaras")
            con.execute("DROP TABLE IF EXISTS obras_competencias")
            od.migrar_esquema(con)
            n = od.carregar_alvaras(con, "sao-paulo", "2026-01", registros)
            self.assertEqual(n, 1)
            relevantes = od.alvaras_relevantes(con, "sao-paulo", "2026-01")
        self.assertEqual(len(relevantes), 1)
        self.assertEqual(relevantes[0]["uso"], "industrial")
        self.assertEqual(relevantes[0]["area_construida"], 1067.33)
        self.assertEqual(relevantes[0]["proprietario"], "ASPECT MIDIA IND ELETR COMERCIO E SERV LTDA")


class IndiceTest(unittest.TestCase):
    def test_listar_arquivos_mes_extrai_competencia_da_url(self):
        html = (
            '<a href="https://prefeitura.sp.gov.br/documents/d/licenciamento/sissel_2026_01-xls">jan</a>'
            '<a href="https://prefeitura.sp.gov.br/documents/d/licenciamento/sissel_2025_12-xls">dez</a>'
        )

        class _Resp:
            status_code = 200
            text = html

            def raise_for_status(self):
                pass

        class _Sessao:
            def get(self, url, **kw):
                return _Resp()

        resultado = listar_arquivos_mes(sessao=_Sessao())
        self.assertEqual(
            resultado["2026-01"], "https://prefeitura.sp.gov.br/documents/d/licenciamento/sissel_2026_01-xls"
        )
        self.assertEqual(
            resultado["2025-12"], "https://prefeitura.sp.gov.br/documents/d/licenciamento/sissel_2025_12-xls"
        )


if __name__ == "__main__":
    unittest.main()
