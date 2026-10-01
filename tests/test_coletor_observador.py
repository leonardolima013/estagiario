"""Testes unitários do observador de ``ColetorPaginas.coletar``
(html-extract-on-web-search, tarefa 2.3; Req 8.1, 8.2, 8.5).

O observador recebe cada evento que o coletor manda ao logger ``coleta_paginas``,
na ordem de emissão, independente do nível do logger. Uma exceção do observador
atravessa ``coletar``. Como o evento de uma entrada sai depois do processamento
daquela fonte (design.md, "coleta_paginas/coletor.py"), a fonte já gravada fica
completa e nenhuma fonte seguinte é buscada ou gravada. O coletor não altera a
configuração de ``logging``.

Sem rede (guarda de rede), armazém em ``tmp_path``, transporte fake e relógio fake.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pytest

from coleta_paginas.coletor import (
    ATRIBUTO_EVENTO,
    EVENTO_ENTRADA,
    EVENTO_RESUMO,
)
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from tests.test_coletor_paginas import (  # noqa: F401  (armazem e relogio são fixtures)
    CODIGO,
    MARCA,
    TransporteRoteiro,
    armazem,
    contar,
    novo_coletor,
    ok,
    peca,
    relogio,
    resultado,
    sha,
)
from tools.buscador_paginas import ErroTransporte

URL_A, URL_B, URL_C = "https://a.com/p", "https://b.com/p", "https://c.com/p"
CORPO_A, CORPO_B = b"<html>A</html>", b"<html>B</html>"


class ObservadorFalho(RuntimeError):
    """Exceção levantada pelo observador de teste."""


def _roteiro() -> TransporteRoteiro:
    return TransporteRoteiro(
        {URL_A: ok(CORPO_A), URL_B: ok(CORPO_B), URL_C: ErroTransporte("timeout")}
    )


def _resultados():
    # A, B e C selecionadas; a última é recusada como url_invalida.
    return [
        resultado(URL_A, 1),
        resultado(URL_B, 2),
        resultado(URL_C, 3),
        resultado("ftp://x.com/p", 4),
    ]


def _eventos_logger(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        getattr(r, ATRIBUTO_EVENTO)
        for r in caplog.records
        if r.name == "coleta_paginas" and hasattr(r, ATRIBUTO_EVENTO)
    ]


def _config_logger(nome: str | None) -> tuple:
    lg = logging.getLogger(nome)
    return (lg.level, tuple(lg.handlers), lg.propagate, lg.disabled, tuple(lg.filters))


# ---------------------------------------------------------------------------
# Mesmos eventos do logger, na ordem
# ---------------------------------------------------------------------------


def test_observador_recebe_os_mesmos_eventos_do_logger_na_ordem(armazem, relogio, caplog):
    caplog.set_level(logging.INFO, logger="coleta_paginas")
    recebidos: list[Mapping[str, Any]] = []
    coletor = novo_coletor(armazem, _roteiro(), relogio)

    relatorio = coletor.coletar(peca(), _resultados(), observador=recebidos.append)

    do_logger = _eventos_logger(caplog)
    assert [dict(e) for e in recebidos] == do_logger
    # Um evento por entrada do relatório, na ordem, e o resumo por último.
    assert [e["evento"] for e in recebidos] == [EVENTO_ENTRADA] * len(
        relatorio.entradas
    ) + [EVENTO_RESUMO]
    assert [(e["url"], e["desfecho"]) for e in recebidos[:-1]] == [
        (en.url, en.desfecho) for en in relatorio.entradas
    ]
    assert [e["desfecho"] for e in recebidos[:-1]] == [
        "armazenado",
        "armazenado",
        "falha",
        "url_invalida",
    ]


def test_observador_recebe_eventos_com_logger_em_warning(armazem, relogio, caplog):
    caplog.set_level(logging.WARNING, logger="coleta_paginas")
    recebidos: list[Mapping[str, Any]] = []
    coletor = novo_coletor(armazem, _roteiro(), relogio)

    relatorio = coletor.coletar(peca(), _resultados(), observador=recebidos.append)

    # Logger em WARNING não emite INFO, mas o observador recebe tudo.
    assert _eventos_logger(caplog) == []
    assert len(recebidos) == len(relatorio.entradas) + 1
    assert recebidos[-1]["evento"] == EVENTO_RESUMO
    assert recebidos[-1]["contagem"] == {
        "armazenado": 2,
        "reaproveitado": 0,
        "falha": 1,
        "url_invalida": 1,
    }


def test_observador_recebe_resumo_de_nao_enriquecivel(armazem, relogio):
    recebidos: list[Mapping[str, Any]] = []
    transporte = _roteiro()
    coletor = novo_coletor(armazem, transporte, relogio)

    relatorio = coletor.coletar(
        peca(marca="   "), _resultados(), observador=recebidos.append
    )

    assert relatorio.status == "nao_enriquecivel"
    assert [e["evento"] for e in recebidos] == [EVENTO_RESUMO]
    assert recebidos[0]["status"] == "nao_enriquecivel"
    assert transporte.chamadas == []


# ---------------------------------------------------------------------------
# Exceção do observador atravessa coletar sem escrita parcial
# ---------------------------------------------------------------------------


def test_excecao_do_observador_atravessa_sem_escrita_parcial(armazem, relogio):
    transporte = _roteiro()
    coletor = novo_coletor(armazem, transporte, relogio)
    recebidos: list[Mapping[str, Any]] = []

    def observador(evento: Mapping[str, Any]) -> None:
        recebidos.append(evento)
        raise ObservadorFalho("falhou")

    with pytest.raises(ObservadorFalho):
        coletor.coletar(peca(), _resultados(), observador=observador)

    # O primeiro evento é o da fonte A, emitido depois da gravação de A.
    assert [(e["url"], e["desfecho"]) for e in recebidos] == [(URL_A, "armazenado")]
    # A ficou gravada por inteiro; B e C não foram buscadas nem gravadas.
    assert transporte.chamadas == [URL_A]
    registros = armazem.registros_da_peca(CODIGO, MARCA)
    assert [r.url_normalizada for r in registros] == [URL_A]
    assert registros[0].hash_conteudo == sha(CORPO_A)
    assert armazem.ler_conteudo(sha(CORPO_A)) == CORPO_A
    assert contar(armazem, "conteudo_pagina") == 1
    assert contar(armazem, "registro_coleta") == 1


def test_excecao_do_observador_no_resumo_atravessa_com_gravacoes_completas(
    armazem, relogio
):
    transporte = _roteiro()
    coletor = novo_coletor(armazem, transporte, relogio)

    def observador(evento: Mapping[str, Any]) -> None:
        if evento["evento"] == EVENTO_RESUMO:
            raise ObservadorFalho("falhou no resumo")

    with pytest.raises(ObservadorFalho):
        coletor.coletar(peca(), _resultados(), observador=observador)

    assert transporte.chamadas == [URL_A, URL_B, URL_C]
    assert {r.url_normalizada for r in armazem.registros_da_peca(CODIGO, MARCA)} == {
        URL_A,
        URL_B,
    }
    assert contar(armazem, "conteudo_pagina") == 2
    assert contar(armazem, "registro_coleta") == 2


# ---------------------------------------------------------------------------
# Configuração de logging inalterada
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("falhar", [False, True], ids=["sucesso", "excecao"])
def test_configuracao_de_logging_inalterada(armazem, relogio, falhar):
    nomes = (None, "coleta_paginas")
    antes = {nome: _config_logger(nome) for nome in nomes}
    coletor = novo_coletor(armazem, _roteiro(), relogio)

    def observador(evento: Mapping[str, Any]) -> None:
        if falhar:
            raise ObservadorFalho("falhou")

    if falhar:
        with pytest.raises(ObservadorFalho):
            coletor.coletar(peca(), _resultados(), observador=observador)
    else:
        coletor.coletar(peca(), _resultados(), observador=observador)

    assert {nome: _config_logger(nome) for nome in nomes} == antes
