"""Testes da ingestão do dump mensal da Receita (Fase 1) -- usa um DuckDB
temporário (RECEITA_DUMP_DB setado antes do import) e fixtures pequenos
construídos na hora, sem rede e sem baixar os ~15 GB reais.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

_TMP = Path(tempfile.gettempdir()) / "scorpions_test_receita_dump.duckdb"
_TMP.unlink(missing_ok=True)
os.environ["RECEITA_DUMP_DB"] = str(_TMP)

import receita_dump as rd  # noqa: E402

_COLS = rd.COLUNAS_ESTAB


def _linha_estab(**over: str) -> str:
    base = {c: "" for c in _COLS}
    base.update({
        "cnpj_basico": "33000167", "cnpj_ordem": "1", "cnpj_dv": "1",
        "matriz_filial": "1", "nome_fantasia": "POSTO CENTRAL",
        "situacao_cadastral": "02", "data_situacao_cadastral": "20051103",
        "data_inicio_atividade": "19660101", "cnae_principal": "4731800",
        "tipo_logradouro": "AVENIDA", "logradouro": "REPUBLICA DO CHILE",
        "numero": "65", "bairro": "CENTRO", "cep": "20031-170", "uf": "RJ",
        "municipio": "6001", "ddd1": "21", "telefone1": "32240000",
        "email": "contato@exemplo.com",
    })
    base.update(over)
    return ";".join(f'"{base[c]}"' for c in _COLS)


def _defaults(**over: str) -> dict[str, str]:
    """Overrides para _linha_estab -- os defaults já vivem lá."""
    return dict(over)


def _cnpj(basico8: str, ordem: str = "0001") -> str:
    """Compõe um CNPJ de 14 dígitos com dígitos verificadores corretos."""
    base = f"{int(basico8):08d}{int(ordem):04d}"

    def dv(seq: str) -> str:
        pesos = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
        pesos = pesos[-len(seq):]
        resto = sum(int(d) * p for d, p in zip(seq, pesos)) % 11
        return "0" if resto < 2 else str(11 - resto)

    d1 = dv(base)
    d2 = dv(base + d1)
    return base + d1 + d2


def _zip_estab(linhas: list[str], nome_membro: str = "K3241.ESTABELE") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(nome_membro, ("\r\n".join(linhas) + "\r\n").encode("latin-1"))
    return buffer.getvalue()


class ContratoTest(unittest.TestCase):
    def test_competencia_invalida_levanta(self):
        for ruim in ("2026-13", "2026/08", "202608", "", "agosto"):
            with self.assertRaises(ValueError):
                rd.url_competencia(ruim)

    def test_url_e_lista_de_arquivos(self):
        self.assertTrue(rd.url_competencia("2026-08").endswith("/2026-08"))
        arquivos = rd.arquivos_da_competencia()
        self.assertEqual(len(arquivos), 12)
        self.assertIn("Estabelecimentos0.zip", arquivos)
        self.assertIn("Municipios.zip", arquivos)
        self.assertEqual(len(rd.arquivos_da_competencia(com_referencia=False)), 10)


class CargaTest(unittest.TestCase):
    def setUp(self):
        with rd.conectar() as con:
            con.execute("DROP TABLE IF EXISTS estabelecimentos")
            con.execute("DROP TABLE IF EXISTS competencias_carregadas")
            con.execute("DROP TABLE IF EXISTS municipios")
            con.execute("DROP TABLE IF EXISTS cnaes")
            rd.migrar_esquema(con)

    def test_migracao_idempotente(self):
        with rd.conectar() as con:
            rd.migrar_esquema(con)
            rd.migrar_esquema(con)
            self.assertEqual([], rd.competencias_carregadas(con))

    def test_carga_de_zip_compoe_cnpj_mapeia_situacao_e_datas(self):
        dados = _zip_estab([
            _linha_estab(),
            _linha_estab(cnpj_ordem="2", matriz_filial="2", situacao_cadastral="08",
                         nome_fantasia="FILIAL SUL", municipio="7107", uf="SP"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "Estabelecimentos0.zip"
            caminho.write_bytes(dados)
            with rd.conectar() as con:
                total = rd.carregar_estabelecimentos(con, "2026-08", [caminho])
                self.assertEqual(total, 2)
                linhas = con.execute(
                    "SELECT cnpj, matriz_filial, situacao, data_situacao, "
                    "data_inicio_atividade, uf, municipio_codigo, telefone "
                    "FROM estabelecimentos WHERE competencia='2026-08' ORDER BY cnpj"
                ).fetchall()
        (cnpj1, mf1, sit1, dsit1, dini1, uf1, mun1, tel1) = linhas[0]
        self.assertEqual(cnpj1, "33000167000101")
        self.assertEqual(mf1, "1")
        self.assertEqual(sit1, "ATIVA")
        self.assertEqual(str(dsit1), "2005-11-03")
        self.assertEqual(str(dini1), "1966-01-01")
        self.assertEqual(uf1, "RJ")
        self.assertEqual(mun1, "6001")
        self.assertEqual(tel1, "2132240000")
        self.assertEqual(linhas[1][0], "33000167000201")
        self.assertEqual(linhas[1][2], "BAIXADA")

    def test_recarga_da_mesma_competencia_nao_duplica(self):
        dados = _zip_estab([_linha_estab()])
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "Estabelecimentos0.zip"
            caminho.write_bytes(dados)
            with rd.conectar() as con:
                rd.carregar_estabelecimentos(con, "2026-08", [caminho])
                total = rd.carregar_estabelecimentos(con, "2026-08", [caminho])
                self.assertEqual(total, 1)
                self.assertEqual(rd.competencias_carregadas(con), ["2026-08"])

    def test_carga_de_csv_cru_sem_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv = Path(tmp) / "estab.csv"
            csv.write_bytes((_linha_estab(cnpj_basico="11222333") + "\r\n").encode("latin-1"))
            with rd.conectar() as con:
                total = rd.carregar_estabelecimentos(con, "2026-09", [csv])
        self.assertEqual(total, 1)

    def test_carregar_referencia_municipios(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv = Path(tmp) / "mun.csv"
            csv.write_bytes('"6001";"RIO DE JANEIRO"\r\n"7107";"SAO PAULO"\r\n'.encode("latin-1"))
            with rd.conectar() as con:
                n = rd.carregar_referencia(con, "municipios", csv)
                nome = con.execute("SELECT nome FROM municipios WHERE codigo='6001'").fetchone()[0]
        self.assertEqual(n, 2)
        self.assertEqual(nome, "RIO DE JANEIRO")


def _carregar_competencia(comp: str, linhas_over: list[dict]) -> None:
    dados = _zip_estab([_linha_estab(**over) for over in linhas_over])
    with tempfile.TemporaryDirectory() as tmp:
        caminho = Path(tmp) / "Estabelecimentos0.zip"
        caminho.write_bytes(dados)
        with rd.conectar() as con:
            rd.migrar_esquema(con)
            rd.carregar_estabelecimentos(con, comp, [caminho])


class DiffTest(unittest.TestCase):
    def setUp(self):
        with rd.conectar() as con:
            for t in ("estabelecimentos", "competencias_carregadas", "municipios", "cnaes"):
                con.execute(f"DROP TABLE IF EXISTS {t}")
            rd.migrar_esquema(con)

    def _diff(self, **kw):
        with rd.conectar() as con:
            return rd.diff_competencias(con, "2026-08", "2026-07", **kw)

    def test_competencia_nao_carregada_levanta(self):
        _carregar_competencia("2026-08", [_defaults()])
        with self.assertRaises(ValueError):
            self._diff()

    def test_nova_filial_e_novo_estabelecimento(self):
        antiga = [_defaults(cnpj_basico="10000001", matriz_filial="1"),
                  _defaults(cnpj_basico="10000001", cnpj_ordem="2", matriz_filial="2")]
        nova = antiga + [
            _defaults(cnpj_basico="10000001", cnpj_ordem="3", matriz_filial="2",
                      data_inicio_atividade="20260805", nome_fantasia="FILIAL NOVA"),
            _defaults(cnpj_basico="20000002", matriz_filial="1",
                      data_inicio_atividade="20260810", nome_fantasia="EMPRESA NOVA"),
        ]
        _carregar_competencia("2026-07", antiga)
        _carregar_competencia("2026-08", nova)
        eventos = {e["nome_fantasia"]: e["tipo"] for e in self._diff()}
        self.assertEqual(eventos["FILIAL NOVA"], "NOVA_FILIAL")
        self.assertEqual(eventos["EMPRESA NOVA"], "NOVO_ESTABELECIMENTO")

    def test_reativacao_e_baixa(self):
        _carregar_competencia("2026-07", [
            _defaults(cnpj_basico="30000003", situacao_cadastral="08"),   # BAIXADA
            _defaults(cnpj_basico="40000004", situacao_cadastral="02"),   # ATIVA
        ])
        _carregar_competencia("2026-08", [
            _defaults(cnpj_basico="30000003", situacao_cadastral="02"),   # -> ATIVA
            _defaults(cnpj_basico="40000004", situacao_cadastral="08"),   # -> BAIXADA
        ])
        por_cnpj = {e["cnpj_basico"]: e["tipo"] for e in self._diff()}
        self.assertEqual(por_cnpj["30000003"], "REATIVACAO")
        self.assertEqual(por_cnpj["40000004"], "BAIXA")

    def test_mudanca_de_endereco(self):
        _carregar_competencia("2026-07", [_defaults(cnpj_basico="50000005", logradouro="RUA A", numero="10")])
        _carregar_competencia("2026-08", [_defaults(cnpj_basico="50000005", logradouro="RUA B", numero="99")])
        eventos = self._diff()
        self.assertEqual(len(eventos), 1)
        self.assertEqual(eventos[0]["tipo"], "MUDANCA_ENDERECO")
        self.assertIn("RUA A", eventos[0]["logradouro_antes"])

    def test_abertura_antiga_e_descartada_por_padrao(self):
        _carregar_competencia("2026-07", [_defaults(cnpj_basico="60000006")])
        _carregar_competencia("2026-08", [
            _defaults(cnpj_basico="60000006"),
            _defaults(cnpj_basico="70000007", data_inicio_atividade="20180101",
                      nome_fantasia="CNPJ ANTIGO QUE APARECEU"),
        ])
        self.assertEqual(self._diff(), [])
        incluindo = self._diff(ignorar_abertura_anterior_a=None)
        self.assertEqual([e["nome_fantasia"] for e in incluindo], ["CNPJ ANTIGO QUE APARECEU"])

    def test_filtro_por_uf_e_municipio(self):
        _carregar_competencia("2026-07", [_defaults(cnpj_basico="80000008")])
        _carregar_competencia("2026-08", [
            _defaults(cnpj_basico="80000008"),
            _defaults(cnpj_basico="90000009", uf="SP", municipio="7107",
                      data_inicio_atividade="20260801", nome_fantasia="SP"),
            _defaults(cnpj_basico="91000009", uf="RJ", municipio="6001",
                      data_inicio_atividade="20260801", nome_fantasia="RJ"),
        ])
        so_sp = self._diff(ufs=("SP",))
        self.assertEqual([e["nome_fantasia"] for e in so_sp], ["SP"])
        so_mun = self._diff(municipios=("6001",))
        self.assertEqual([e["nome_fantasia"] for e in so_mun], ["RJ"])
        so_tipo = self._diff(tipos=("REATIVACAO",))
        self.assertEqual(so_tipo, [])


def _evento(tipo: str, **over) -> dict:
    base = {
        "tipo": tipo, "cnpj": _cnpj("11111111", "0002"), "cnpj_basico": "11111111",
        "matriz_filial": "2", "nome_fantasia": "CD SOROCABA", "situacao": "ATIVA",
        "uf": "SP", "municipio_codigo": "7145", "municipio_nome": "SOROCABA",
        "cnae_principal": "5211701", "cnae_descricao": "Armazéns gerais - emissão de warrant",
        "data_inicio_atividade": date.today() - timedelta(days=40),
        "logradouro": "ROD RAPOSO TAVARES", "numero": "KM 90", "bairro": "IPORANGA",
        "cep": "18087-000", "email": "log@exemplo.com", "telefone": "1533000000",
        "situacao_antes": None, "logradouro_antes": None, "municipio_antes": None,
    }
    base.update(over)
    return base


class MatcherTest(unittest.TestCase):
    def test_nova_filial_logistica_vira_candidato_classificado(self):
        r = rd.candidatos_do_diff([_evento("NOVA_FILIAL")])
        self.assertEqual(len(r["candidatos"]), 1)
        c = r["candidatos"][0]
        self.assertEqual(c["evento_tipo"], "NOVA_FILIAL")
        self.assertEqual(c["lead"]["segmento_icp"], "Galpões Logísticos & Indústrias")
        self.assertTrue(c["lead"]["servicos_recomendados"])
        self.assertEqual(c["lead"]["cidade"], "SOROCABA, SP")
        self.assertEqual(c["lead"]["origem"], rd.FONTE_RECEITA_DUMP)
        self.assertEqual(c["mudanca"]["type"], "nova_filial_receita")
        self.assertEqual(c["mudanca"]["after"], "SOROCABA, SP")
        self.assertEqual(c["mudanca"]["days_between"], 40)

    def test_nao_classificado_e_omitido_por_padrao(self):
        ev = _evento("NOVO_ESTABELECIMENTO", cnae_descricao="Atividades de organizações associativas",
                     nome_fantasia="ASSOCIACAO XPTO", matriz_filial="1")
        self.assertEqual(rd.candidatos_do_diff([ev])["candidatos"], [])
        incluido = rd.candidatos_do_diff([ev], incluir_nao_classificado=True)["candidatos"]
        self.assertEqual(len(incluido), 1)
        self.assertEqual(incluido[0]["lead"]["segmento_icp"], "Não classificado")

    def test_baixa_vai_para_supressoes(self):
        cnpj = _cnpj("99999999", "0001")
        r = rd.candidatos_do_diff([_evento("BAIXA", cnpj=cnpj)])
        self.assertEqual(r["candidatos"], [])
        self.assertEqual(r["supressoes"], [cnpj])

    def test_cnpj_invalido_e_descartado(self):
        self.assertEqual(
            rd.candidatos_do_diff([_evento("NOVA_FILIAL", cnpj="11111111000199")])["candidatos"], []
        )

    def test_reativacao_e_mudanca_endereco_geram_payload_certo(self):
        rea = rd.candidatos_do_diff([_evento("REATIVACAO", situacao_antes="INAPTA")])["candidatos"][0]
        self.assertEqual(rea["mudanca"]["type"], "situacao_cadastral_alterada")
        self.assertTrue(rea["mudanca"]["reativacao"])
        self.assertEqual(rea["mudanca"]["before"], "INAPTA")

        end = rd.candidatos_do_diff([_evento("MUDANCA_ENDERECO", logradouro_antes="RUA VELHA")])["candidatos"][0]
        self.assertEqual(end["mudanca"]["type"], "endereco_alterado")
        self.assertEqual(end["mudanca"]["before"], "RUA VELHA")
        self.assertIn("SOROCABA, SP", end["mudanca"]["after"])


class _RespFalsa:
    def __init__(self, conteudo: bytes):
        self._conteudo = conteudo
        self.status_code = 200

    def raise_for_status(self): ...
    def __enter__(self): return self
    def __exit__(self, *a): ...
    def iter_content(self, chunk_size=0):
        yield self._conteudo


class _SessaoFalsa:
    def __init__(self, conteudo: bytes):
        self._conteudo = conteudo
        self.urls: list[str] = []

    def get(self, url, **kw):
        self.urls.append(url)
        return _RespFalsa(self._conteudo)


class DownloadTest(unittest.TestCase):
    def test_baixa_so_o_solicitado_e_pula_existente(self):
        sessao = _SessaoFalsa(b"conteudo-zip")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            primeiro = rd.baixar_competencia(
                "2026-08", apenas=("Estabelecimentos0.zip",), sessao=sessao, destino_base=base
            )
            self.assertEqual(len(primeiro), 1)
            self.assertTrue(primeiro[0].exists())
            self.assertEqual(primeiro[0].read_bytes(), b"conteudo-zip")
            self.assertEqual(len(sessao.urls), 1)
            self.assertTrue(sessao.urls[0].endswith("/2026-08/Estabelecimentos0.zip"))
            # segunda chamada: arquivo já existe -> não baixa de novo
            rd.baixar_competencia(
                "2026-08", apenas=("Estabelecimentos0.zip",), sessao=sessao, destino_base=base
            )
            self.assertEqual(len(sessao.urls), 1)


if __name__ == "__main__":
    unittest.main()
