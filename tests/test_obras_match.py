"""Testes do casamento alvará -> empresa (Fase 2). Módulo puro, sem banco."""

from __future__ import annotations

import unittest

from obras_match import (
    assinatura_endereco,
    casar_alvaras,
    indexar_estabelecimentos,
    indexar_estabelecimentos_por_nome,
    indexar_leads,
    indexar_leads_por_nome,
    normalizar_nome_empresa,
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


class NomeTest(unittest.TestCase):
    def test_sufixo_societario_e_pontuacao_nao_atrapalham(self):
        a = normalizar_nome_empresa("Aspect Midia Ind Eletr Comercio e Serv LTDA")
        b = normalizar_nome_empresa("ASPECT MIDIA IND. ELETR. COMERCIO E SERVICOS LTDA.")
        self.assertTrue(a & b)
        self.assertNotIn("ltda", a)

    def test_nome_bate_mesmo_com_endereco_totalmente_diferente(self):
        leads = indexar_leads([
            {"id": 5, "endereco": "Rua Sem Nenhuma Relação, 1", "cidade": "Guarulhos, SP"},
        ])
        leads_nome = indexar_leads_por_nome([
            {"id": 5, "nome_empresa": "Aspect Midia Ind Eletr Comercio e Serv"},
        ])
        alvara = {
            "id_alvara": "A1", "endereco": "Rua Pedralia", "numero": "399",
            "municipio_nome": "São Paulo", "proprietario": "ASPECT MIDIA IND ELETR COMERCIO E SERV LTDA",
        }
        r = casar_alvaras([alvara], indice_leads=leads, indice_leads_nome=leads_nome)
        self.assertEqual(len(r["em_lead"]), 1)
        self.assertEqual(r["em_lead"][0]["lead_id"], 5)
        self.assertEqual(r["em_lead"][0]["por"], "nome")

    def test_nome_vence_endereco_quando_ambos_casam(self):
        leads = indexar_leads([{"id": 1, "endereco": "Rua Pedralia, 399", "cidade": "São Paulo, SP"}])
        leads_nome = indexar_leads_por_nome([{"id": 2, "nome_empresa": "Aspect Midia"}])
        alvara = {"endereco": "Rua Pedralia", "numero": "399", "municipio_nome": "São Paulo",
                  "proprietario": "ASPECT MIDIA"}
        r = casar_alvaras([alvara], indice_leads=leads, indice_leads_nome=leads_nome)
        self.assertEqual(r["em_lead"][0]["lead_id"], 2)
        self.assertEqual(r["em_lead"][0]["por"], "nome")

    def test_cai_para_nome_de_estabelecimento_da_receita(self):
        estabs_nome = indexar_estabelecimentos_por_nome([
            {"cnpj": "111", "nome_fantasia": "B2F Marketing e Eventos"},
        ])
        alvara = {"endereco": "Rua Desconhecida", "numero": "1", "municipio_nome": "São Paulo",
                  "proprietario": "B2F MARKETING E EVENTOS LTDA"}
        r = casar_alvaras([alvara], indice_leads=[], indice_estabelecimentos_nome=estabs_nome)
        self.assertEqual(len(r["novo_de_receita"]), 1)
        self.assertEqual(r["novo_de_receita"][0]["estabelecimento"]["cnpj"], "111")
        self.assertEqual(r["novo_de_receita"][0]["por"], "nome")

    def test_sem_proprietario_nao_quebra(self):
        r = casar_alvaras([{"endereco": "", "numero": "", "municipio_nome": ""}], indice_leads=[])
        self.assertEqual(len(r["sem_ocupante"]), 1)


if __name__ == "__main__":
    unittest.main()
