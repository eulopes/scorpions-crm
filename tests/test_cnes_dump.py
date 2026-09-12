"""Testes da ingestão do CNES (Fase 3) -- usa um DuckDB temporário
(CNES_DUMP_DB setado antes do import) e fixtures pequenos construídos na
hora, sem rede e sem baixar o arquivo real (~56 MB).
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

_TMP = Path(tempfile.gettempdir()) / "scorpions_test_cnes_dump.duckdb"
_TMP.unlink(missing_ok=True)
os.environ["CNES_DUMP_DB"] = str(_TMP)

import cnes_dump as cnd  # noqa: E402

_COLS = cnd.COLUNAS_CSV


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


def _linha_cnes(**over: str) -> dict[str, str]:
    base = {
        "CO_CNES": "0000019", "CO_UNIDADE": "3550308000019", "CO_UF": "35",
        "CO_IBGE": "355030", "NU_CNPJ_MANTENEDORA": "",
        "NO_RAZAO_SOCIAL": "CLINICA TESTE LTDA", "NO_FANTASIA": "CLINICA TESTE",
        "TP_UNIDADE": "36", "CO_CEP": "01310100", "NO_LOGRADOURO": "AV PAULISTA",
        "NU_ENDERECO": "1000", "NO_BAIRRO": "BELA VISTA", "NU_TELEFONE": "(11)30000000",
        "NU_LATITUDE": "-23.5", "NU_LONGITUDE": "-46.6", "NU_CNPJ": _cnpj("11222333"),
        "NO_EMAIL": "contato@clinicateste.com.br", "CO_NATUREZA_JUR": "2062",
        "DS_ESFERA_ADMINISTRATIVA": "MUNICIPAL",
    }
    base.update(over)
    return base


def _csv_bytes(linhas: list[dict[str, str]]) -> bytes:
    cabecalho = ";".join(_COLS)
    corpo = "\r\n".join(
        ";".join(f'"{linha[c]}"' for c in _COLS) for linha in linhas
    )
    return (cabecalho + "\r\n" + corpo + "\r\n").encode("utf-8")


def _zip_cnes(linhas: list[dict[str, str]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cnes_estabelecimentos.csv", _csv_bytes(linhas))
    return buffer.getvalue()


class ContratoTest(unittest.TestCase):
    def test_competencia_invalida_levanta(self):
        for ruim in ("2026-09", "2026/09/12", "12-09-2026", ""):
            with self.assertRaises(ValueError):
                cnd._validar_competencia(ruim)

    def test_competencia_valida_passa(self):
        self.assertEqual(cnd._validar_competencia("2026-09-12"), "2026-09-12")

    def test_tipos_unidade_relevantes_tem_nome_legivel(self):
        for codigo in cnd.TP_UNIDADE_RELEVANTES:
            self.assertIn(codigo, cnd._NOME_TP_UNIDADE)
        # Consultório isolado (22) fica de fora por porte tipicamente pequeno.
        self.assertNotIn("22", cnd.TP_UNIDADE_RELEVANTES)


class CargaTest(unittest.TestCase):
    def setUp(self):
        with cnd.conectar() as con:
            con.execute("DROP TABLE IF EXISTS estabelecimentos_saude")
            con.execute("DROP TABLE IF EXISTS competencias_carregadas")
            cnd.migrar_esquema(con)

    def test_migracao_idempotente(self):
        with cnd.conectar() as con:
            cnd.migrar_esquema(con)
            cnd.migrar_esquema(con)
            self.assertEqual([], cnd.competencias_carregadas(con))

    def test_competencia_mais_recente_carregada(self):
        with cnd.conectar() as con:
            self.assertIsNone(cnd.competencia_mais_recente_carregada(con))
            for comp in ("2026-07-01", "2026-09-01", "2026-08-01"):
                con.execute(
                    "INSERT INTO competencias_carregadas (competencia, linhas) VALUES (?, 0)", [comp]
                )
            self.assertEqual(cnd.competencia_mais_recente_carregada(con), "2026-09-01")
            self.assertEqual(
                cnd.competencia_mais_recente_carregada(con, anterior_a="2026-09-01"), "2026-08-01"
            )
            self.assertIsNone(
                cnd.competencia_mais_recente_carregada(con, anterior_a="2026-07-01")
            )

    def test_carga_de_zip_normaliza_campos(self):
        dados = _zip_cnes([_linha_cnes()])
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "cnes_estabelecimentos_csv.zip"
            caminho.write_bytes(dados)
            with cnd.conectar() as con:
                total = cnd.carregar_estabelecimentos(con, "2026-09-12", caminho)
                self.assertEqual(total, 1)
                linha = con.execute(
                    "SELECT co_cnes, cnpj, tp_unidade, natureza_jur, cep, telefone, "
                    "logradouro, numero, latitude "
                    "FROM estabelecimentos_saude WHERE competencia='2026-09-12'"
                ).fetchone()
        (co_cnes, cnpj, tp, nat, cep, tel, log, num, lat) = linha
        self.assertEqual(co_cnes, "0000019")
        self.assertEqual(cnpj, _cnpj("11222333"))
        self.assertEqual(tp, "36")
        self.assertEqual(nat, "2062")
        self.assertEqual(cep, "01310100")
        self.assertEqual(tel, "1130000000")
        self.assertEqual(log, "AV PAULISTA")
        self.assertEqual(num, "1000")
        self.assertAlmostEqual(lat, -23.5)

    def test_recarga_da_mesma_competencia_nao_duplica(self):
        dados = _zip_cnes([_linha_cnes()])
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "cnes_estabelecimentos_csv.zip"
            caminho.write_bytes(dados)
            with cnd.conectar() as con:
                cnd.carregar_estabelecimentos(con, "2026-09-12", caminho)
                total = cnd.carregar_estabelecimentos(con, "2026-09-12", caminho)
                self.assertEqual(total, 1)
                self.assertEqual(cnd.competencias_carregadas(con), ["2026-09-12"])

    def test_carga_de_csv_cru_sem_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv = Path(tmp) / "estab.csv"
            csv.write_bytes(_csv_bytes([_linha_cnes(CO_CNES="0000099")]))
            with cnd.conectar() as con:
                total = cnd.carregar_estabelecimentos(con, "2026-09-12", csv)
        self.assertEqual(total, 1)


def _carregar_competencia(comp: str, linhas: list[dict]) -> None:
    dados = _zip_cnes(linhas)
    with tempfile.TemporaryDirectory() as tmp:
        caminho = Path(tmp) / "cnes.zip"
        caminho.write_bytes(dados)
        with cnd.conectar() as con:
            cnd.migrar_esquema(con)
            cnd.carregar_estabelecimentos(con, comp, caminho)


class DiffTest(unittest.TestCase):
    def setUp(self):
        with cnd.conectar() as con:
            for t in ("estabelecimentos_saude", "competencias_carregadas"):
                con.execute(f"DROP TABLE IF EXISTS {t}")
            cnd.migrar_esquema(con)

    def _diff(self, **kw):
        with cnd.conectar() as con:
            return cnd.diff_dumps(con, "2026-09-12", "2026-08-01", **kw)

    def test_competencia_nao_carregada_levanta(self):
        _carregar_competencia("2026-09-12", [_linha_cnes()])
        with self.assertRaises(ValueError):
            self._diff()

    def test_novo_estabelecimento_relevante_e_detectado(self):
        _carregar_competencia("2026-08-01", [])
        _carregar_competencia("2026-09-12", [_linha_cnes(NO_FANTASIA="CLINICA NOVA")])
        eventos = self._diff()
        self.assertEqual(len(eventos), 1)
        self.assertEqual(eventos[0]["tipo"], "NOVO_ESTABELECIMENTO")
        self.assertEqual(eventos[0]["nome_fantasia"], "CLINICA NOVA")

    def test_estabelecimento_publico_e_filtrado(self):
        _carregar_competencia("2026-08-01", [])
        _carregar_competencia("2026-09-12", [
            _linha_cnes(CO_CNES="0000020", NO_FANTASIA="UBS PUBLICA", CO_NATUREZA_JUR="1023")
        ])
        self.assertEqual(self._diff(), [])

    def test_sem_cnpj_proprio_e_filtrado(self):
        _carregar_competencia("2026-08-01", [])
        _carregar_competencia("2026-09-12", [_linha_cnes(NU_CNPJ="")])
        self.assertEqual(self._diff(), [])

    def test_tipo_unidade_nao_relevante_e_filtrado(self):
        _carregar_competencia("2026-08-01", [])
        _carregar_competencia("2026-09-12", [_linha_cnes(TP_UNIDADE="22")])  # consultório isolado
        self.assertEqual(self._diff(), [])

    def test_mudanca_de_endereco(self):
        _carregar_competencia("2026-08-01", [_linha_cnes(NO_LOGRADOURO="RUA A", NU_ENDERECO="10")])
        _carregar_competencia("2026-09-12", [_linha_cnes(NO_LOGRADOURO="RUA B", NU_ENDERECO="99")])
        eventos = self._diff()
        self.assertEqual(len(eventos), 1)
        self.assertEqual(eventos[0]["tipo"], "MUDANCA_ENDERECO")
        self.assertIn("RUA A", eventos[0]["logradouro_antes"])

    def test_descadastrado_aparece_quando_relevante(self):
        _carregar_competencia("2026-08-01", [_linha_cnes()])
        _carregar_competencia("2026-09-12", [])
        eventos = self._diff()
        self.assertEqual(len(eventos), 1)
        self.assertEqual(eventos[0]["tipo"], "DESCADASTRADO")

    def test_filtro_por_uf(self):
        _carregar_competencia("2026-08-01", [])
        _carregar_competencia("2026-09-12", [
            _linha_cnes(CO_CNES="0000021", CO_UF="35", NO_FANTASIA="SP"),
            _linha_cnes(CO_CNES="0000022", CO_UF="33", NU_CNPJ=_cnpj("22333444"), NO_FANTASIA="RJ"),
        ])
        so_sp = self._diff(ufs=("SP",))
        self.assertEqual([e["nome_fantasia"] for e in so_sp], ["SP"])


def _evento(tipo: str, **over) -> dict:
    base = {
        "tipo": tipo, "co_cnes": "0000019", "uf": "SP", "municipio_codigo": "355030",
        "cnpj": _cnpj("11222333"), "cnpj_mantenedora": None,
        "razao_social": "CLINICA TESTE LTDA", "nome_fantasia": "CLINICA TESTE",
        "tp_unidade": "36", "natureza_jur": "2062",
        "logradouro": "AV PAULISTA", "numero": "1000", "bairro": "BELA VISTA",
        "cep": "01310100", "telefone": "1130000000", "email": "contato@exemplo.com",
        "logradouro_antes": None,
    }
    base.update(over)
    return base


class MatcherTest(unittest.TestCase):
    def test_novo_estabelecimento_vira_candidato_classificado(self):
        r = cnd.candidatos_do_diff([_evento("NOVO_ESTABELECIMENTO")])
        self.assertEqual(len(r["candidatos"]), 1)
        c = r["candidatos"][0]
        self.assertEqual(c["evento_tipo"], "NOVO_ESTABELECIMENTO")
        self.assertEqual(c["lead"]["segmento_icp"], "Clínicas, Hospitais & Laboratórios")
        self.assertTrue(c["lead"]["servicos_recomendados"])
        self.assertEqual(c["lead"]["origem"], cnd.FONTE_CNES)
        self.assertEqual(c["mudanca"]["type"], "novo_estabelecimento_saude")
        self.assertEqual(c["mudanca"]["source"], cnd.FONTE_CNES)

    def test_todos_tipos_relevantes_classificam_como_clinicas(self):
        for codigo in cnd.TP_UNIDADE_RELEVANTES:
            with self.subTest(tp_unidade=codigo):
                r = cnd.candidatos_do_diff([_evento("NOVO_ESTABELECIMENTO", tp_unidade=codigo)])
                self.assertEqual(len(r["candidatos"]), 1)
                self.assertEqual(
                    r["candidatos"][0]["lead"]["segmento_icp"], "Clínicas, Hospitais & Laboratórios"
                )

    def test_cnpj_invalido_e_descartado(self):
        r = cnd.candidatos_do_diff([_evento("NOVO_ESTABELECIMENTO", cnpj="11111111000199")])
        self.assertEqual(r["candidatos"], [])

    def test_descadastrado_vai_para_supressoes(self):
        cnpj = _cnpj("99999999")
        r = cnd.candidatos_do_diff([_evento("DESCADASTRADO", cnpj=cnpj)])
        self.assertEqual(r["candidatos"], [])
        self.assertEqual(r["supressoes"], [cnpj])

    def test_mudanca_de_endereco_gera_payload_certo(self):
        r = cnd.candidatos_do_diff([_evento("MUDANCA_ENDERECO", logradouro_antes="RUA VELHA")])
        c = r["candidatos"][0]
        self.assertEqual(c["mudanca"]["type"], "endereco_alterado_saude")
        self.assertEqual(c["mudanca"]["before"], "RUA VELHA")
        self.assertIn("AV PAULISTA", c["mudanca"]["after"])


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
    def test_baixa_e_pula_se_ja_existe(self):
        sessao = _SessaoFalsa(b"conteudo-zip")
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "cnes.zip"
            caminho = cnd.baixar_dump(sessao=sessao, destino=destino)
            self.assertEqual(caminho, destino)
            self.assertEqual(destino.read_bytes(), b"conteudo-zip")
            self.assertEqual(len(sessao.urls), 1)
            self.assertEqual(sessao.urls[0], cnd.URL_DUMP)
            # segunda chamada: arquivo já existe -> não baixa de novo
            cnd.baixar_dump(sessao=sessao, destino=destino)
            self.assertEqual(len(sessao.urls), 1)


if __name__ == "__main__":
    unittest.main()
