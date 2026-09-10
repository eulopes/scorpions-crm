"""Testes do enriquecimento firmográfico via Receita/BrasilAPI.

O mapper (mapear_snapshot_receita) é função pura -- testado sem rede.
buscar_snapshot_receita é testado com uma sessão falsa, sem HTTP real.
"""

from __future__ import annotations

import json
import unittest

import requests

from receita_snapshot import buscar_snapshot_receita, mapear_snapshot_receita

# CNPJ real e válido (Petrobras) -- só os dígitos verificadores importam aqui.
CNPJ_VALIDO = "33.000.167/0001-01"

PAYLOAD_BASE = {
    "cnpj": "33000167000101",
    "razao_social": "PETROLEO BRASILEIRO S A PETROBRAS",
    "nome_fantasia": "PETROBRAS",
    "capital_social": 205431960490.63,
    "descricao_porte": "DEMAIS",
    "cnae_fiscal": 610801,
    "cnae_fiscal_descricao": "Extração de petróleo e gás natural",
    "cnaes_secundarios": [
        {"codigo": 1921700, "descricao": "Fabricação de produtos do refino de petróleo"},
        {"codigo": 3520401, "descricao": "Produção de gás"},
    ],
    "descricao_situacao_cadastral": "ATIVA",
    "data_situacao_cadastral": "2005-11-03",
    "natureza_juridica": "204-6 - Sociedade Anônima Aberta",
    "logradouro": "REPUBLICA DO CHILE",
    "numero": "65",
    "bairro": "CENTRO",
    "municipio": "RIO DE JANEIRO",
    "uf": "RJ",
    "cep": "20031912",
    "qsa": [
        {"nome_socio": "FULANO DE TAL", "qualificacao_socio": "10 - Diretor"},
        {"nome_socio": "BELTRANO DE TAL", "qualificacao_socio": "10 - Diretor"},
    ],
}


class _RespostaFalsa:
    def __init__(self, *, status_code=200, payload=None, erro=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._erro = erro

    def raise_for_status(self):
        if self._erro:
            raise self._erro

    def json(self):
        return self._payload


class _SessaoFalsa:
    def __init__(self, resposta):
        self._resposta = resposta
        self.chamadas = []

    def get(self, url, **kwargs):
        self.chamadas.append(url)
        return self._resposta


class MapperTest(unittest.TestCase):
    def test_payload_completo_mapeia_todos_os_campos(self):
        resultado = mapear_snapshot_receita(PAYLOAD_BASE)
        self.assertEqual(resultado["capital_social"], 205431960490.63)
        self.assertEqual(resultado["porte"], "DEMAIS")
        self.assertEqual(resultado["cnae_principal"], "610801 · Extração de petróleo e gás natural")
        self.assertEqual(resultado["situacao_cadastral"], "ATIVA")
        self.assertEqual(resultado["data_situacao_cadastral"], "2005-11-03")
        self.assertEqual(resultado["natureza_juridica"], "204-6 - Sociedade Anônima Aberta")
        self.assertEqual(resultado["qtde_socios"], 2)
        self.assertTrue(resultado["qsa_hash"])
        self.assertIn("REPUBLICA DO CHILE, 65", resultado["address"])
        self.assertIn("RIO DE JANEIRO/RJ", resultado["address"])
        self.assertIn("CEP 20031-912", resultado["address"])

    def test_cnaes_secundarios_viram_json_ordenado_e_sem_repeticao(self):
        payload = dict(PAYLOAD_BASE)
        payload["cnaes_secundarios"] = [
            {"codigo": 3520401, "descricao": "Produção de gás"},
            {"codigo": 3520401, "descricao": "Produção de gás"},
            {"codigo": 1921700, "descricao": "Fabricação de produtos do refino de petróleo"},
        ]
        lista = json.loads(mapear_snapshot_receita(payload)["cnaes_secundarios_json"])
        self.assertEqual(lista, sorted(set(lista)))
        self.assertEqual(len(lista), 2)

    def test_capital_social_aceita_numero_e_string(self):
        self.assertEqual(mapear_snapshot_receita({"capital_social": 1000000})["capital_social"], 1000000.0)
        self.assertEqual(
            mapear_snapshot_receita({"capital_social": "1.000.000,00"})["capital_social"], 1000000.0
        )
        self.assertIsNone(mapear_snapshot_receita({"capital_social": ""})["capital_social"])
        self.assertIsNone(mapear_snapshot_receita({"capital_social": "n/a"})["capital_social"])

    def test_campos_ausentes_viram_none_sem_quebrar(self):
        resultado = mapear_snapshot_receita({})
        for campo in (
            "capital_social", "porte", "cnae_principal", "cnaes_secundarios_json",
            "situacao_cadastral", "data_situacao_cadastral", "natureza_juridica", "qsa_hash",
        ):
            self.assertIsNone(resultado[campo], campo)
        self.assertEqual(resultado["qtde_socios"], 0)

    def test_qsa_hash_estavel_para_reordenacao_muda_ao_entrar_socio(self):
        base = mapear_snapshot_receita(PAYLOAD_BASE)["qsa_hash"]

        reordenado = dict(PAYLOAD_BASE)
        reordenado["qsa"] = list(reversed(PAYLOAD_BASE["qsa"]))
        self.assertEqual(base, mapear_snapshot_receita(reordenado)["qsa_hash"])

        com_novo_socio = dict(PAYLOAD_BASE)
        com_novo_socio["qsa"] = PAYLOAD_BASE["qsa"] + [
            {"nome_socio": "NOVO SOCIO", "qualificacao_socio": "22 - Sócio"}
        ]
        resultado = mapear_snapshot_receita(com_novo_socio)
        self.assertNotEqual(base, resultado["qsa_hash"])
        self.assertEqual(resultado["qtde_socios"], 3)

    def test_porte_como_codigo_nao_vira_o_valor(self):
        self.assertEqual(mapear_snapshot_receita({"porte": "05"})["porte"], "05")
        self.assertEqual(
            mapear_snapshot_receita({"porte": "05", "descricao_porte": "DEMAIS"})["porte"], "DEMAIS"
        )

    def test_tipo_invalido_levanta(self):
        with self.assertRaises(TypeError):
            mapear_snapshot_receita("não é dict")


class BuscarSnapshotTest(unittest.TestCase):
    def test_cnpj_invalido_devolve_none_sem_chamar_rede(self):
        sessao = _SessaoFalsa(_RespostaFalsa())
        self.assertIsNone(buscar_snapshot_receita("123", sessao=sessao))
        self.assertEqual(sessao.chamadas, [])

    def test_404_devolve_none(self):
        sessao = _SessaoFalsa(_RespostaFalsa(status_code=404))
        self.assertIsNone(buscar_snapshot_receita(CNPJ_VALIDO, sessao=sessao))

    def test_erro_de_rede_devolve_none(self):
        sessao = _SessaoFalsa(_RespostaFalsa(erro=requests.ConnectionError("timeout")))
        self.assertIsNone(buscar_snapshot_receita(CNPJ_VALIDO, sessao=sessao))

    def test_sucesso_devolve_mapeado_com_raw(self):
        sessao = _SessaoFalsa(_RespostaFalsa(payload=PAYLOAD_BASE))
        resultado = buscar_snapshot_receita(CNPJ_VALIDO, sessao=sessao)
        self.assertIsNotNone(resultado)
        self.assertEqual(resultado["porte"], "DEMAIS")
        self.assertEqual(resultado["_raw"], PAYLOAD_BASE)
        self.assertEqual(len(sessao.chamadas), 1)
        self.assertIn("33000167000101", sessao.chamadas[0])

    def test_json_invalido_devolve_none(self):
        sessao = _SessaoFalsa(_RespostaFalsa(payload=["lista", "não", "dict"]))
        self.assertIsNone(buscar_snapshot_receita(CNPJ_VALIDO, sessao=sessao))


if __name__ == "__main__":
    unittest.main()
