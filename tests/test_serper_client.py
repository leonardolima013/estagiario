"""Testes de exemplo/borda do Cliente_Serper (verification/serper_client.py).

Usam um transporte HTTP FAKE injetado — nenhuma requisição de rede real, nenhuma
API Serper real. A SERPER_API_KEY nunca aparece neste arquivo.
"""

from __future__ import annotations

import json

import pytest

from verification.serper_client import (
    ClienteSerper,
    ResultadoOrganico,
    SerperRequisicaoError,
    extrair_organicos,
    montar_corpo_busca,
)


def test_cabecalho_x_api_key_e_corpo_corretos():
    """R3.1, R3.3: transporte recebe X-API-KEY e o corpo esperado."""
    capturado = {}

    def transporte_fake(url, dados, cabecalhos, timeout):
        capturado["url"] = url
        capturado["dados"] = json.loads(dados.decode("utf-8"))
        capturado["cabecalhos"] = cabecalhos
        capturado["timeout"] = timeout
        return {"organic": []}

    cliente = ClienteSerper(api_key="chave-de-teste-sintetica", timeout=15, transporte=transporte_fake)
    cliente.buscar("ABC123", "Bosch")

    assert capturado["cabecalhos"]["X-API-KEY"] == "chave-de-teste-sintetica"
    assert capturado["dados"] == {"q": "ABC123 Bosch", "gl": "br", "hl": "pt-br"}
    assert capturado["timeout"] == 15
    assert capturado["url"].endswith("/search")


def test_extrai_organicos_da_resposta():
    """R3.6: extrai título/link/snippet/posição de `organic`."""
    resposta = {
        "organic": [
            {"title": "T1", "link": "https://a.com", "snippet": "S1", "position": 1},
            {"title": "T2", "link": "https://b.com", "snippet": "S2"},
        ]
    }
    organicos = extrair_organicos(resposta)
    assert organicos == [
        ResultadoOrganico(title="T1", link="https://a.com", snippet="S1", position=1),
        ResultadoOrganico(title="T2", link="https://b.com", snippet="S2", position=None),
    ]


def test_resposta_sem_organic_extrai_lista_vazia():
    """R3.9: resposta sem `organic` ou vazia -> lista vazia."""
    assert extrair_organicos({}) == []
    assert extrair_organicos({"organic": []}) == []


def test_falha_de_rede_vira_serper_requisicao_error():
    """R3.8: erro no transporte propaga como SerperRequisicaoError."""

    def transporte_falha(url, dados, cabecalhos, timeout):
        raise SerperRequisicaoError("Falha de rede/timeout ao consultar a API Serper.")

    cliente = ClienteSerper(api_key="chave-sintetica", timeout=5, transporte=transporte_falha)
    with pytest.raises(SerperRequisicaoError):
        cliente.buscar("ABC", "Marca")


def test_chave_nunca_aparece_em_erro_de_rede():
    """R8.3: a chave não vaza na mensagem de erro."""
    chave = "chave-super-secreta-sintetica-123"

    def transporte_falha(url, dados, cabecalhos, timeout):
        raise SerperRequisicaoError("Falha de rede/timeout ao consultar a API Serper: URLError.")

    cliente = ClienteSerper(api_key=chave, timeout=5, transporte=transporte_falha)
    with pytest.raises(SerperRequisicaoError) as exc:
        cliente.buscar("ABC", "Marca")
    assert chave not in str(exc.value)


def test_montar_corpo_busca_puro():
    """R3.1: função pura de montagem de corpo."""
    assert montar_corpo_busca("X1", "Y2") == {"q": "X1 Y2", "gl": "br", "hl": "pt-br"}
