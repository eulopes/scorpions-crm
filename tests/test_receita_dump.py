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
