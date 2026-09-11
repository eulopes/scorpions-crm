"""Testes do store canônico de alvarás/obras (Fase 2). DuckDB temporário via
OBRAS_DUMP_DB, setado antes do import.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

_TMP = Path(tempfile.gettempdir()) / "scorpions_test_obras.duckdb"
_TMP.unlink(missing_ok=True)
os.environ["OBRAS_DUMP_DB"] = str(_TMP)

import obras_dump as od  # noqa: E402


def _alvara(**over):
    base = {
        "id_alvara": "2026/EXEC/00001", "cidade": "sao-paulo",
        "data_emissao": date(2026, 8, 12), "tipo": "execucao", "uso": "comercial",
        "area_construida": 1800.0, "endereco": "Av. Pres. Juscelino Kubitschek",
        "numero": "1909", "bairro": "Itaim Bibi", "municipio_nome": "São Paulo",
        "uf": "SP", "cep": "04543-907", "sql_iptu": "086.123.0045-6",
        "subprefeitura": "Pinheiros", "lat": -23.59, "lng": -46.68,
    }
    base.update(over)
    return base


class NormalizacaoTest(unittest.TestCase):
    def test_expande_abreviacao_e_tira_acento(self):
        a = od.normalizar_endereco("Av. Pres. Juscelino Kubitschek")
        b = od.normalizar_endereco("AVENIDA PRESIDENTE JUSCELINO KUBISTCHEK")  # typo mantido
        self.assertIn("avenida presidente juscelino", a)
        self.assertTrue(a.startswith("avenida presidente juscelino"))
        self.assertNotEqual(a, b)  # o typo em b não é corrigido, só normalizado

    def test_chave_inclui_numero_e_municipio(self):
        k1 = od.chave_endereco("Rua A", "100", "São Paulo")
        k2 = od.chave_endereco("R A", "100", "SAO PAULO")
        k3 = od.chave_endereco("Rua A", "2000", "São Paulo")
        self.assertEqual(k1, k2)          # abreviação + acento não importam
        self.assertNotEqual(k1, k3)       # número diferente = obra diferente
        self.assertIn("100", k1)

    def test_competencia_invalida(self):
        for ruim in ("2026-13", "2026/08", "", "x"):
            with self.assertRaises(ValueError):
                od._validar_competencia(ruim)


class StoreTest(unittest.TestCase):
    def setUp(self):
        with od.conectar() as con:
            con.execute("DROP TABLE IF EXISTS alvaras")
            con.execute("DROP TABLE IF EXISTS obras_competencias")
            od.migrar_esquema(con)

    def test_migracao_idempotente(self):
        with od.conectar() as con:
            od.migrar_esquema(con)
            od.migrar_esquema(con)
            self.assertEqual(od.competencias_carregadas(con), [])

    def test_carga_canoniza_tipo_uso_area_cep(self):
        registros = [
            _alvara(tipo="EXECUÇÃO", uso="Comercial", area_construida="1.800,50", cep="04543907"),
            _alvara(id_alvara="X2", tipo="coisa estranha", uso="galpão", area_construida="abc"),
        ]
        with od.conectar() as con:
            n = od.carregar_alvaras(con, "sao-paulo", "2026-08", registros)
            self.assertEqual(n, 2)
            linhas = con.execute(
                "SELECT id_alvara, tipo, uso, area_construida, cep, chave_endereco "
                "FROM alvaras WHERE competencia='2026-08' ORDER BY id_alvara"
            ).fetchall()
        (id1, tipo1, uso1, area1, cep1, chave1) = linhas[0]
        self.assertEqual(tipo1, "execucao")
        self.assertEqual(uso1, "comercial")
        self.assertEqual(area1, 1800.5)
        self.assertEqual(cep1, "04543907")
        self.assertIn("avenida presidente juscelino", chave1)
        # valores fora do vocabulário caem em 'outro' / None
        self.assertEqual(linhas[1][1], "outro")
        self.assertEqual(linhas[1][2], "outro")
        self.assertIsNone(linhas[1][3])

    def test_recarga_nao_duplica(self):
        with od.conectar() as con:
            od.carregar_alvaras(con, "sao-paulo", "2026-08", [_alvara()])
            od.carregar_alvaras(con, "sao-paulo", "2026-08", [_alvara()])
            total = con.execute("SELECT count(*) FROM alvaras").fetchone()[0]
            self.assertEqual(total, 1)
            self.assertEqual(od.competencias_carregadas(con), [("sao-paulo", "2026-08")])

    def test_registro_sem_id_e_ignorado(self):
        with od.conectar() as con:
            n = od.carregar_alvaras(con, "sao-paulo", "2026-08", [_alvara(id_alvara="")])
        self.assertEqual(n, 0)

    def test_relevantes_filtra_area_uso_e_tipo(self):
        registros = [
            _alvara(id_alvara="GRANDE-COM", uso="comercial", tipo="execucao", area_construida=2000),
            _alvara(id_alvara="PEQUENA-COM", uso="comercial", tipo="execucao", area_construida=120),
            _alvara(id_alvara="GRANDE-RES", uso="residencial", tipo="execucao", area_construida=5000),
            _alvara(id_alvara="GRANDE-COM-DEMO", uso="comercial", tipo="demolicao", area_construida=3000),
            _alvara(id_alvara="SEM-AREA-IND", uso="industrial", tipo="aprovacao", area_construida=None),
        ]
        with od.conectar() as con:
            od.carregar_alvaras(con, "sao-paulo", "2026-08", registros)
            ids = [r["id_alvara"] for r in od.alvaras_relevantes(con, "sao-paulo", "2026-08")]
        self.assertIn("GRANDE-COM", ids)
        self.assertIn("SEM-AREA-IND", ids)          # area nula não descarta
        self.assertNotIn("PEQUENA-COM", ids)        # abaixo de 500 m²
        self.assertNotIn("GRANDE-RES", ids)         # residencial
        self.assertNotIn("GRANDE-COM-DEMO", ids)    # demolição fora do recorte


if __name__ == "__main__":
    unittest.main()
