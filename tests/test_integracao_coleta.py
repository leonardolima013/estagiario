"""Testes unitários da Integracao_Coleta (html-extract-on-web-search, tarefa 5.11).

Usam o ``ColetorPaginas`` real sobre ``ArmazemPaginas`` em ``tmp_path`` e um
transporte fake roteirizado (reaproveitado de ``tests/test_coletor_paginas.py``).
Nenhum teste abre rede (``guarda_rede_autouse``) nem toca ``db/paginas.db`` da
raiz (``isolamento_coleta_autouse`` aponta ``ESTAGIARIO_PAGINAS_DB_PATH`` para
``tmp_path``).

Requisitos: 2.6, 5.1, 7.7, 8.6, 9.4, 9.8.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

import config
import db.armazem_paginas as armazem_mod
from arbitration.pesquisa_web import AtivacaoPesquisa, ContextoPesquisaWeb
from coleta_paginas.coletor import ColetorPaginas
from coleta_paginas.integracao import (
    PREFIXO_AVISO,
    IntegracaoColeta,
    resolver_coleta_efetiva,
)
from db.armazem_paginas import ArmazemPaginas
from loop.tracing import TraceCollector
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from tests.test_coletor_paginas import (
    CODIGO,
    MARCA,
    Relogio,
    TransporteRoteiro,
    config_coleta,
    ok,
)
from verification.serper_client import ResultadoOrganico

URL_A = "https://a.com/p"
URL_B = "https://b.com/p"
CORPO_A = b"<html>A</html>"
CORPO_B = b"<html>B</html>"


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------


def organico(url: str, posicao: int) -> ResultadoOrganico:
    return ResultadoOrganico(
        title=f"Filtro de óleo {CODIGO} {MARCA}",
        link=url,
        snippet="Peça automotiva",
        position=posicao,
    )


def ativacao(
    *,
    urls: tuple[str, ...] = (URL_A,),
    codigo: str | None = CODIGO,
    marca: str | None = MARCA,
    situacao: str = "realizada",
) -> AtivacaoPesquisa:
    resultados = (
        tuple(organico(url, i + 1) for i, url in enumerate(urls))
        if situacao == "realizada"
        else None
    )
    return AtivacaoPesquisa(
        codigo=codigo,
        marca=marca,
        nomes_conflitantes=("Filtro de óleo", "Filtro óleo"),
        metodo="serper",
        situacao=situacao,
        resultados=resultados,
    )


def contexto(
    *,
    coleta: str = "habilitada",
    canal: list[str] | None = None,
) -> ContextoPesquisaWeb:
    return ContextoPesquisaWeb(
        pesquisa_habilitada=True,
        coleta_efetiva=coleta,
        cancel_event=None,
        canal_avisos=canal.append if canal is not None else None,
    )


class FabricaContadora:
    """Fábrica de ``ColetorPaginas`` real que conta as construções."""

    def __init__(self, tmp_path: Path, transporte: TransporteRoteiro, relogio: Relogio):
        self.tmp_path = tmp_path
        self.transporte = transporte
        self.relogio = relogio
        self.chamadas = 0

    def __call__(self) -> ColetorPaginas:
        self.chamadas += 1
        armazem = ArmazemPaginas(
            self.tmp_path / "paginas.db",
            caminho_rule_store=self.tmp_path / "rule_store.db",
            relogio=self.relogio,
        )
        return ColetorPaginas(
            armazem,
            transporte=self.transporte,
            config=config_coleta(),
            relogio=self.relogio,
        )


@pytest.fixture
def relogio() -> Relogio:
    return Relogio()


@pytest.fixture
def rule_store_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """RuleStore em ``tmp_path`` para a fábrica padrão (distinto do armazém)."""
    caminho = tmp_path / "rule_store.db"
    monkeypatch.setenv("ESTAGIARIO_DB_PATH", str(caminho))
    return caminho


# ---------------------------------------------------------------------------
# Req 2.6 — reuso dentro da Janela_Reuso
# ---------------------------------------------------------------------------


def test_reuso_entre_duas_ativacoes_da_mesma_peca_sem_nova_requisicao(tmp_path, relogio):
    transporte = TransporteRoteiro({URL_A: ok(CORPO_A)})
    fabrica = FabricaContadora(tmp_path, transporte, relogio)
    integracao = IntegracaoColeta(fabrica_coletor=fabrica)

    primeiro = integracao.processar(ativacao(), contexto=contexto(), trace=None)
    relogio.avancar(timedelta(days=1))
    segundo = integracao.processar(ativacao(), contexto=contexto(), trace=None)

    assert primeiro.desfecho == segundo.desfecho == "executada"
    assert [e.desfecho for e in primeiro.relatorio.entradas] == ["armazenado"]
    assert [e.desfecho for e in segundo.relatorio.entradas] == ["reaproveitado"]
    assert (
        segundo.relatorio.entradas[0].hash_conteudo
        == primeiro.relatorio.entradas[0].hash_conteudo
    )
    # Uma única requisição HTTP nas duas ativações.
    assert transporte.chamadas == [URL_A]


# ---------------------------------------------------------------------------
# Req 7.7, 8.6 — sem trace e sem canal, o mesmo desfecho
# ---------------------------------------------------------------------------


def _processar_em_diretorio(
    base: Path, relogio: Relogio, *, com_publicacao: bool, coleta: str
):
    base.mkdir()
    transporte = TransporteRoteiro({URL_A: ok(CORPO_A), URL_B: ok(CORPO_B)})
    integracao = IntegracaoColeta(
        fabrica_coletor=FabricaContadora(base, transporte, relogio),
        relogio_monotonico=lambda: 0.0,
    )
    canal: list[str] | None = [] if com_publicacao else None
    trace = TraceCollector() if com_publicacao else None
    desfecho = integracao.processar(
        ativacao(urls=(URL_A, URL_B)),
        contexto=contexto(coleta=coleta, canal=canal),
        trace=trace,
    )
    return desfecho, canal, trace, transporte


@pytest.mark.parametrize("coleta", ["habilitada", "desabilitada"])
def test_trace_e_canal_ausentes_produzem_o_mesmo_desfecho(tmp_path, relogio, coleta):
    com, canal, trace, transporte_com = _processar_em_diretorio(
        tmp_path / "com", relogio, com_publicacao=True, coleta=coleta
    )
    sem, _, _, transporte_sem = _processar_em_diretorio(
        tmp_path / "sem", relogio, com_publicacao=False, coleta=coleta
    )

    assert sem == com
    assert transporte_sem.chamadas == transporte_com.chamadas
    # Com publicação, o trace e o canal efetivamente receberam o desfecho.
    eventos = [e for e in trace.eventos() if e.nome == "coletar_paginas"]
    assert len(eventos) == 1
    assert canal and all(m.startswith(PREFIXO_AVISO) for m in canal)
    if coleta == "habilitada":
        assert com.desfecho == "executada"
        assert transporte_com.chamadas == [URL_A, URL_B]
    else:
        assert com.desfecho == "nao_executada" and com.motivo == "desabilitada"
        assert transporte_com.chamadas == []


# ---------------------------------------------------------------------------
# Req 9.4 — configuracao_invalida com o nome da variável
# ---------------------------------------------------------------------------


def test_configuracao_invalida_publica_variavel_sem_chamar_o_coletor(
    monkeypatch, tmp_path, relogio
):
    monkeypatch.setenv("ESTAGIARIO_COLETA_HABILITADA", "talvez")
    coleta = resolver_coleta_efetiva(None, config.coleta_habilitada())
    assert coleta == "invalida"

    transporte = TransporteRoteiro({URL_A: ok(CORPO_A)})
    fabrica = FabricaContadora(tmp_path, transporte, relogio)
    integracao = IntegracaoColeta(fabrica_coletor=fabrica)
    canal: list[str] = []
    trace = TraceCollector()

    desfecho = integracao.processar(
        ativacao(), contexto=contexto(coleta=coleta, canal=canal), trace=trace
    )

    assert desfecho.desfecho == "nao_executada"
    assert desfecho.motivo == "configuracao_invalida"
    assert desfecho.variavel == "ESTAGIARIO_COLETA_HABILITADA"
    assert fabrica.chamadas == 0 and transporte.chamadas == []
    assert not (tmp_path / "paginas.db").exists()

    assert len(canal) == 1
    assert canal[0].startswith(PREFIXO_AVISO + " ")
    assert json.loads(canal[0][len(PREFIXO_AVISO) + 1 :]) == {
        "desfecho": "nao_executada",
        "motivo": "configuracao_invalida",
        "variavel": "ESTAGIARIO_COLETA_HABILITADA",
    }
    (evento,) = [e for e in trace.eventos() if e.nome == "coletar_paginas"]
    assert evento.status == "nao_executada"
    assert evento.saida["variavel"] == "ESTAGIARIO_COLETA_HABILITADA"
    # O valor recebido nunca é ecoado.
    assert "talvez" not in canal[0] and "talvez" not in json.dumps(evento.saida)


def test_opcao_coleta_ignora_chave_invalida(monkeypatch):
    monkeypatch.setenv("ESTAGIARIO_COLETA_HABILITADA", "talvez")
    chave = config.coleta_habilitada()
    assert resolver_coleta_efetiva(True, chave) == "habilitada"
    assert resolver_coleta_efetiva(False, chave) == "desabilitada"


# ---------------------------------------------------------------------------
# Req 5.1 — ConflitoCaminhoArmazemError vira erro e não fica em cache
# ---------------------------------------------------------------------------


def test_conflito_caminho_armazem_vira_erro_e_coletor_nao_fica_em_cache(
    monkeypatch, tmp_path, isolamento_coleta_autouse
):
    caminho_armazem = isolamento_coleta_autouse.paginas_db_path
    # RuleStore no mesmo arquivo do armazém: ArmazemPaginas() levanta o conflito.
    monkeypatch.setenv("ESTAGIARIO_DB_PATH", str(caminho_armazem))

    transporte = TransporteRoteiro({URL_A: ok(CORPO_A)})
    integracao = IntegracaoColeta(transporte=transporte, config_coleta=config_coleta())
    canal: list[str] = []
    trace = TraceCollector()

    falha = integracao.processar(
        ativacao(), contexto=contexto(canal=canal), trace=trace
    )

    assert falha.desfecho == "erro"
    assert falha.motivo == "ConflitoCaminhoArmazemError"
    assert falha.relatorio is None
    assert transporte.chamadas == []
    assert not caminho_armazem.exists()
    # Só o nome da classe; nenhuma mensagem (com caminhos) da exceção.
    assert len(canal) == 1
    assert json.loads(canal[0][len(PREFIXO_AVISO) + 1 :]) == {
        "desfecho": "erro",
        "motivo": "ConflitoCaminhoArmazemError",
    }
    assert str(caminho_armazem) not in canal[0]
    (evento,) = [e for e in trace.eventos() if e.nome == "coletar_paginas"]
    assert evento.status == "erro"
    assert str(caminho_armazem) not in json.dumps(evento.saida)

    # Corrigida a configuração, a mesma instância constrói o coletor de novo.
    monkeypatch.setenv("ESTAGIARIO_DB_PATH", str(tmp_path / "rule_store.db"))
    sucesso = integracao.processar(ativacao(), contexto=contexto(), trace=None)

    assert sucesso.desfecho == "executada"
    assert [e.desfecho for e in sucesso.relatorio.entradas] == ["armazenado"]
    assert transporte.chamadas == [URL_A]
    assert caminho_armazem.exists()


# ---------------------------------------------------------------------------
# Req 9.8 — armazém aberto só na primeira ativação elegível
# ---------------------------------------------------------------------------


def test_armazem_aberto_so_na_primeira_ativacao_elegivel(
    monkeypatch, rule_store_tmp, isolamento_coleta_autouse
):
    caminho_armazem = isolamento_coleta_autouse.paginas_db_path
    aberturas: list[Path] = []
    original = armazem_mod.ArmazemPaginas

    class ArmazemContador(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            aberturas.append(self.db_path)

    # A fábrica padrão importa ArmazemPaginas do módulo no momento da chamada.
    monkeypatch.setattr(armazem_mod, "ArmazemPaginas", ArmazemContador)

    transporte = TransporteRoteiro({URL_A: ok(CORPO_A), URL_B: ok(CORPO_B)})
    integracao = IntegracaoColeta(transporte=transporte, config_coleta=config_coleta())

    nao_elegiveis = [
        (ativacao(), contexto(coleta="desabilitada")),
        (ativacao(situacao="nao_realizada"), contexto()),
        (ativacao(situacao="sem_estruturados"), contexto()),
        (ativacao(marca=None), contexto()),  # Trava_Entrada
    ]
    desfechos = [
        integracao.processar(a, contexto=c, trace=None).desfecho for a, c in nao_elegiveis
    ]
    assert desfechos == ["nao_executada", "nao_executada", "nao_executada", "nao_enriquecivel"]
    assert aberturas == []
    assert not caminho_armazem.exists()

    primeiro = integracao.processar(ativacao(urls=(URL_A,)), contexto=contexto(), trace=None)
    assert primeiro.desfecho == "executada"
    assert aberturas == [caminho_armazem]
    assert caminho_armazem.exists()

    segundo = integracao.processar(ativacao(urls=(URL_B,)), contexto=contexto(), trace=None)
    terceiro = integracao.processar(ativacao(urls=()), contexto=contexto(), trace=None)
    assert segundo.desfecho == terceiro.desfecho == "executada"
    assert aberturas == [caminho_armazem]
    assert transporte.chamadas == [URL_A, URL_B]
