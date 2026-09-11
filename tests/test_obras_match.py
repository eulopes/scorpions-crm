"""Testes do casamento alvará -> empresa (Fase 2). Módulo puro, sem banco."""

from __future__ import annotations

import unittest

from obras_match import (
    assinatura_endereco,
    casar_alvaras,
    indexar_estabelecimentos,
    indexar_leads,
)


class AssinaturaTest(unittest.TestCase):
    def test_abreviacao_acento_e_stopword_nao_contam(self):
        a = assinatura_endereco("Av. Eng. Luís Carlos Berrini", "1500", "São Paulo")
        b = assinatura_endereco("AVENIDA ENGENHEIRO LUIS CARLOS BERRINI, 1500", municipio="SAO PAULO")
        self.assertEqual(a[1], "1500")
        self.assertEqual(a[2], b[2])
        # "avenida" é stopword; "eng"/"engenheiro" divergem, mas o resto casa
        self.assertTrue(a[0] & b[0])

    def test_numero_apos_virgula_vence_cep(self):
        assin = assinatura_endereco("Rua Haddock Lobo, 595 - Cerqueira Cesar - CEP 01414001", "", "São Paulo")
        self.assertEqual(assin[1], "595")

    def test_endereco_vazio_gera_assinatura_vazia(self):
        self.assertEqual(assinatura_endereco("", "", "")[0], frozenset())


class CasamentoTest(unittest.TestCase):
    def setUp(self):
        self.leads = indexar_leads([
            {"id": 10, "endereco": "Avenida Paulista, 1000 - Bela Vista", "cidade": "São Paulo, SP"},
            {"id": 11, "endereco": "Rodovia Raposo Tavares km 90", "cidade": "Sorocaba, SP"},
        ])
        self.estabs = indexar_estabelecimentos([
            {"cnpj": "111", "logradouro": "RUA FIDENCIO RAMOS", "numero": "223",
             "municipio_nome": "SAO PAULO"},
        ])

    def _alvara(self, **o):
        base = {"id_alvara": "A1", "endereco": "Av Paulista", "numero": "1000",
                "municipio_nome": "São Paulo"}
        base.update(o)
        return base

    def test_casa_com_lead_existente(self):
        r = casar_alvaras([self._alvara()], indice_leads=self.leads)
        self.assertEqual(len(r["em_lead"]), 1)
        self.assertEqual(r["em_lead"][0]["lead_id"], 10)
        self.assertEqual(r["em_lead"][0]["confianca"], "alta")

    def test_numero_diferente_no_mesmo_logradouro_nao_casa(self):
        r = casar_alvaras([self._alvara(numero="4500")], indice_leads=self.leads)
        self.assertEqual(r["em_lead"], [])
        self.assertEqual(len(r["sem_ocupante"]), 1)

    def test_municipio_diferente_nao_casa(self):
        r = casar_alvaras(
            [self._alvara(endereco="Rodovia Raposo Tavares", numero="", municipio_nome="Cotia")],
            indice_leads=self.leads,
        )
        self.assertEqual(r["em_lead"], [])

    def test_cai_para_estabelecimento_da_receita(self):
        alvara = self._alvara(id_alvara="A2", endereco="Rua Fidêncio Ramos", numero="223",
                              municipio_nome="São Paulo")
        r = casar_alvaras([alvara], indice_leads=self.leads, indice_estabelecimentos=self.estabs)
        self.assertEqual(len(r["novo_de_receita"]), 1)
        self.assertEqual(r["novo_de_receita"][0]["estabelecimento"]["cnpj"], "111")
        self.assertEqual(r["novo_de_receita"][0]["confianca"], "media")

    def test_sem_ninguem_no_endereco(self):
        r = casar_alvaras(
            [self._alvara(endereco="Rua Inexistente Qualquer", numero="7")],
            indice_leads=self.leads, indice_estabelecimentos=self.estabs,
        )
        self.assertEqual(len(r["sem_ocupante"]), 1)
        self.assertEqual(r["sem_ocupante"][0]["motivo"], "sem empresa no endereço")

    def test_lead_vence_estabelecimento_quando_ambos_casam(self):
        estabs = indexar_estabelecimentos([
            {"cnpj": "999", "logradouro": "AVENIDA PAULISTA", "numero": "1000",
             "municipio_nome": "SAO PAULO"},
        ])
        r = casar_alvaras([self._alvara()], indice_leads=self.leads, indice_estabelecimentos=estabs)
        self.assertEqual(len(r["em_lead"]), 1)
        self.assertEqual(r["novo_de_receita"], [])


if __name__ == "__main__":
    unittest.main()
