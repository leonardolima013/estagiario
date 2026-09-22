import pytest

from arbitration.datas import arbitrar_data_de_application, extrair_anos
from tests.fakes import FakeLLMProvider

_BRAND_ID = 1


class LLMQueNuncaDeveriaSerChamado:
    def gerar_json(self, *args, **kwargs):
        raise AssertionError("LLM não deveria ser chamado quando a application tem anos extraíveis")


def test_extrair_anos_intervalo_simples():
    assert extrair_anos("CITROEN - BERLINGO 1.6 16V 1991/2001") == [1991, 2001]


def test_extrair_anos_multiplas_linhas():
    texto = "PEUGEOT - 206 1.6 16V 1993/1995\nPEUGEOT - 307 1.6 16V 1994/1997"
    assert extrair_anos(texto) == [1993, 1995, 1994, 1997]


def test_extrair_anos_ignora_ano_de_2_digitos():
    # '09/13' é ambíguo (mês? ano abreviado?) — não deve virar 2009/2013 por engano.
    assert extrair_anos("PEUGEOT - 207 1.6 16V 09/13") == []


def test_extrair_anos_ignora_todos_sem_ano():
    assert extrair_anos("PEUGEOT - 208 1.6 16V MOTOR EC5 TODOS") == []


def test_extrair_anos_texto_vazio_ou_none():
    assert extrair_anos(None) == []
    assert extrair_anos("") == []


def test_born_at_e_deprecated_at_derivados_da_application_sem_chamar_llm(fazer_registro):
    registros = [
        fazer_registro(1, "X", application="CITROEN - BERLINGO 1991/2001", born_at=1980, deprecated_at=1980),
        fazer_registro(2, "Y", application="PEUGEOT - 206 1993/2022", born_at=1975, deprecated_at=1975),
    ]

    decisao_born = arbitrar_data_de_application(
        registros, "born_at", llm=LLMQueNuncaDeveriaSerChamado(), rule_store=None, brand_id=_BRAND_ID
    )
    decisao_deprecated = arbitrar_data_de_application(
        registros, "deprecated_at", llm=LLMQueNuncaDeveriaSerChamado(), rule_store=None, brand_id=_BRAND_ID
    )

    assert decisao_born.valor == 1991
    assert decisao_born.fonte == "normalizacao"
    assert decisao_deprecated.valor == 2022
    assert decisao_deprecated.fonte == "normalizacao"


def test_sem_ano_na_application_cai_pro_fallback_com_llm(fazer_registro):
    registros = [
        fazer_registro(1, "X", application="MOTOR EC5 TODOS", born_at=1980, deprecated_at=None),
        fazer_registro(2, "Y", application=None, born_at=1990, deprecated_at=None),
    ]
    fake_llm = FakeLLMProvider(
        {"part_id_escolhido": 2, "justificativa": "mais recente", "confianca": "alta"}
    )

    decisao = arbitrar_data_de_application(registros, "born_at", llm=fake_llm, rule_store=None, brand_id=_BRAND_ID)

    assert decisao.fonte == "julgamento_modelo"
    assert decisao.valor == 1990


def test_rejeita_campo_que_nao_e_data_de_application(fazer_registro):
    registros = [fazer_registro(1, "X"), fazer_registro(2, "Y")]

    with pytest.raises(ValueError):
        arbitrar_data_de_application(registros, "width", llm=None, rule_store=None, brand_id=_BRAND_ID)
