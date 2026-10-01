"""Fakes, oráculo e estratégias da feature stealth-fallback-integration (tarefa 2.3).

Módulo auxiliar de testes (sem ``test_*``). Contém:

- ``FallbackFake``: Fallback_Stealth fake com roteiro por URL ou por chamada,
  registro das chamadas, ação opcional durante a chamada e medidor de
  concorrência;
- ``TravaFake``: Trava_Navegador fake com roteiro de ``acquire``;
- ``ArmazemEspiao``: ``ArmazemPaginas`` real (SQLite em ``tmp_path``) com
  contadores e falhas injetáveis na reputação e na gravação;
- ``oraculo_stealth``: reimplementação independente do fluxograma "Fluxo de
  uma Fonte_Selecionada" do design. Não importa ``coleta_paginas.coletor`` nem
  ``coleta_paginas.reputacao``: Status_Bloqueio, Motivos_Falha_Reputacao, a
  regra de exclusão (3 falhas consecutivas) e a pertença à allowlist estão
  escritos aqui de novo, de propósito;
- ``ControleCancelamento``: dispara o Sinal_Cancelamento antes de uma fonte,
  durante a busca urllib ou durante o fallback de uma fonte;
- estratégias Hypothesis dos cenários (``cenarios_stealth``);
- ``fixar_ambiente_stealth``: fixa ``ESTAGIARIO_COLETA_DOMINIOS_STEALTH`` e
  ``ESTAGIARIO_STEALTH_PROFILE_DIR`` (em ``tmp_path``) para testes que usam os
  padrões do coletor.

Os fakes de transporte (``TransporteRoteiro``, ``ok``) e os auxiliares de peça e
resultado vêm de ``tests/test_coletor_paginas.py``. Esse import carrega o
coletor de forma transitiva, mas o oráculo em si não usa nada dele.

Os módulos de teste que usam este arquivo continuam responsáveis por importar
``guarda_rede_autouse`` e ``isolamento_coleta_autouse``.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from hypothesis import strategies as st

from coleta_paginas.modelos import PecaConsultada, ResultadoBusca
from coleta_paginas.url import normalizar_url
from db.armazem_paginas import (
    ArmazemPaginas,
    ErroArmazenamento,
    NovoRegistroColeta,
    TentativaFetch,
)
from tests.test_coletor_paginas import (
    CABECALHOS_HTML,
    CODIGO,
    MARCA,
    TransporteRoteiro,
    ok,
    peca,
    resultado,
)
from tools.buscador_paginas import ErroTransporte, FalhaBusca, PaginaBaixada

__all__ = [
    "ALLOWLIST_PADRAO",
    "CAMADA_STEALTH_ORACULO",
    "CAMADA_URLLIB_ORACULO",
    "HOSTS",
    "MOTIVOS_FALHA_BUSCA",
    "STATUS_BLOQUEIO_ORACULO",
    "STATUS_REUSO",
    "ArmazemEspiao",
    "CenarioStealth",
    "ChamadaFallback",
    "ControleCancelamento",
    "EntradaEsperada",
    "ExecucaoCenario",
    "FallbackFake",
    "FonteCenario",
    "PontoCancelamento",
    "ResultadoOraculo",
    "TentativaEsperada",
    "TravaFake",
    "UrllibCorpoVazio",
    "UrllibErro",
    "UrllibNaoHtml",
    "UrllibPagina",
    "UrllibStatus",
    "cenarios_stealth",
    "entrada_esperada_de",
    "fixar_ambiente_stealth",
    "item_transporte",
    "oraculo_stealth",
    "pares_reputacao",
    "peca_cenario",
    "preparar_execucao",
    "resultados_do_cenario",
    "semear_cenario",
    "semear_reputacao",
    "semear_reuso",
    "tentativas_novas",
    "maior_id_tentativa",
    # reexportados de tests/test_coletor_paginas.py
    "CABECALHOS_HTML",
    "CODIGO",
    "MARCA",
    "TransporteRoteiro",
    "ok",
]

# ---------------------------------------------------------------------------
# Constantes do oráculo (reescritas aqui; não vêm de coleta_paginas.reputacao)
# ---------------------------------------------------------------------------

CAMADA_URLLIB_ORACULO = "urllib"
CAMADA_STEALTH_ORACULO = "playwright_stealth"
CAMADAS_ORACULO = (CAMADA_URLLIB_ORACULO, CAMADA_STEALTH_ORACULO)

STATUS_BLOQUEIO_ORACULO = frozenset({403, 429, 503})  # decisão Q1
LIMITE_FALHAS_ORACULO = 3  # decisão Q4 (regra sem janela temporal)

STATUS_REUSO = 200  # status do Registro_Coleta pré-semeado para reuso

ALLOWLIST_PADRAO = frozenset({"mercadocar.com.br"})

# Hosts do gerador: casos que casam com a allowlist padrão (igual, subdomínios) e
# sufixos maliciosos que não casam.
HOSTS = (
    "mercadocar.com.br",
    "www.mercadocar.com.br",
    "loja.mercadocar.com.br",
    "mercadocar.com.br.evil.com",
    "evilmercadocar.com.br",
    "mercadocar.com",
    "outro.com.br",
    "autodoc.parts",
    "ebay.co.uk",
)

# Todos os motivos de FalhaBusca (urllib + stealth), escritos de novo aqui.
MOTIVOS_FALHA_BUSCA = (
    "rede",
    "timeout",
    "status_http",
    "nao_html",
    "tamanho_excedido",
    "corpo_vazio",
    "redirecionamento_nao_permitido",
    "redirecionamentos_excedidos",
    "codificacao_nao_suportada",
    "codificacao_invalida",
    "url_invalida",
    "destino_nao_permitido",
    "dominio_nao_permitido_stealth",
    "ambiente_sem_display",
    "navegador_indisponivel",
)

CONTENT_TYPE_HTML = CABECALHOS_HTML["content-type"]


def _normalizar_dominio(dominio: str) -> str:
    return dominio.strip().casefold().rstrip(".")


def _na_allowlist(host: str, allowlist: Iterable[str]) -> bool:
    """Host igual a um item ou subdomínio dele (reimplementação independente)."""
    alvo = _normalizar_dominio(host)
    if not alvo:
        return False
    for item in allowlist:
        dominio = _normalizar_dominio(item)
        if dominio and (alvo == dominio or alvo.endswith("." + dominio)):
            return True
    return False


def _sha(corpo: bytes) -> str:
    return hashlib.sha256(corpo).hexdigest()


# ---------------------------------------------------------------------------
# Roteiro urllib (abstrato) e conversão para o TransporteRoteiro
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UrllibPagina:
    """200..299 HTML com corpo não vazio → ``PaginaBaixada``."""

    corpo: bytes
    status: int = 200


@dataclass(frozen=True)
class UrllibStatus:
    """Status fora de 2xx (sem redirecionamento) → ``status_http``."""

    status: int


@dataclass(frozen=True)
class UrllibErro:
    """``ErroTransporte`` → ``rede`` ou ``timeout`` sem status."""

    tipo: Literal["rede", "timeout"]


@dataclass(frozen=True)
class UrllibNaoHtml:
    """200 com Content-Type não HTML → ``nao_html`` com status 200."""


@dataclass(frozen=True)
class UrllibCorpoVazio:
    """200 HTML sem corpo → ``corpo_vazio`` com status 200."""


RoteiroUrllib = UrllibPagina | UrllibStatus | UrllibErro | UrllibNaoHtml | UrllibCorpoVazio


def item_transporte(item: RoteiroUrllib) -> tuple[int, dict[str, str], bytes] | ErroTransporte:
    """Entrada do ``TransporteRoteiro`` que produz o desfecho abstrato ``item``."""
    if isinstance(item, UrllibPagina):
        return (item.status, dict(CABECALHOS_HTML), item.corpo)
    if isinstance(item, UrllibStatus):
        return (item.status, {}, b"")
    if isinstance(item, UrllibErro):
        return ErroTransporte(item.tipo)
    if isinstance(item, UrllibNaoHtml):
        return (200, {"content-type": "application/json"}, b"{}")
    if isinstance(item, UrllibCorpoVazio):
        return (200, dict(CABECALHOS_HTML), b"")
    raise TypeError(f"item urllib desconhecido: {item!r}")


def _desfecho_urllib(url: str, item: RoteiroUrllib) -> PaginaBaixada | FalhaBusca:
    """Desfecho esperado do BuscadorPaginas para ``item`` (sem redirecionamento)."""
    if isinstance(item, UrllibPagina):
        return PaginaBaixada(
            status_http=item.status,
            content_type=CONTENT_TYPE_HTML,
            charset="utf-8",
            url_final=url,
            corpo=item.corpo,
        )
    if isinstance(item, UrllibStatus):
        return FalhaBusca("status_http", url, status_http=item.status)
    if isinstance(item, UrllibErro):
        return FalhaBusca(item.tipo, url)
    if isinstance(item, UrllibNaoHtml):
        return FalhaBusca("nao_html", url, status_http=200, content_type="application/json")
    if isinstance(item, UrllibCorpoVazio):
        return FalhaBusca("corpo_vazio", url, status_http=200, content_type=CONTENT_TYPE_HTML)
    raise TypeError(f"item urllib desconhecido: {item!r}")


# ---------------------------------------------------------------------------
# Cenário
# ---------------------------------------------------------------------------

ItemFallback = PaginaBaixada | FalhaBusca | BaseException


@dataclass(frozen=True)
class FonteCenario:
    """Uma Fonte_Selecionada do cenário.

    - URL: ``https://{host}/peca/{indice}`` (única por índice; já normalizada);
    - ``dominio_resultado``: ``ResultadoBusca.dominio`` (``None`` → host da URL);
      o Dominio_Fonte é esse valor normalizado;
    - ``urllib``: desfecho abstrato da busca urllib;
    - ``fallback``: desfecho do Fallback_Stealth para a URL (``None`` →
      ``FalhaBusca("rede", url)``); uma exceção é levantada pelo fake;
    - ``reuso_corpo``: corpo de um Registro_Coleta pré-gravado dentro da
      Janela_Reuso (``None`` → sem reuso).
    """

    indice: int
    host: str
    urllib: RoteiroUrllib
    fallback: ItemFallback | None = None
    dominio_resultado: str | None = None
    reuso_corpo: bytes | None = None

    @property
    def url(self) -> str:
        return f"https://{self.host}/peca/{self.indice}"

    @property
    def dominio_fonte(self) -> str:
        bruto = self.dominio_resultado
        if bruto is None or not bruto.strip():
            bruto = self.host
        return _normalizar_dominio(bruto)

    @property
    def item_fallback(self) -> ItemFallback:
        return self.fallback if self.fallback is not None else FalhaBusca("rede", self.url)


FaseCancelamento = Literal["antes_da_fonte", "durante_urllib", "durante_fallback"]


@dataclass(frozen=True)
class PontoCancelamento:
    """Quando o Sinal_Cancelamento liga.

    - ``antes_da_fonte``: antes da checagem do laço para a fonte ``indice``
      (``indice == 0`` → já ligado antes de ``coletar``);
    - ``durante_urllib``: durante a chamada ao transporte da fonte ``indice``
      (só dispara se essa chamada acontecer);
    - ``durante_fallback``: durante a chamada ao fallback da fonte ``indice``
      (só dispara se essa chamada acontecer).
    """

    fase: FaseCancelamento
    indice: int


Par = tuple[str, str]  # (Dominio_Fonte normalizado, camada)


@dataclass(frozen=True)
class CenarioStealth:
    """Entrada do oráculo e dos fakes.

    - ``reputacao``: (domínio, camada) → sucessos (True) e falhas (False) em
      ordem cronológica, gravados antes de ``coletar``;
    - ``trava``: roteiro de ``acquire`` (esgotado → obtida);
    - ``exclusoes``: URLs recusadas por ``motivo_recusa_url`` (viram
      ``url_invalida`` depois das selecionadas), com o motivo esperado;
    - ``consultas_falham``/``registros_falham``: pares cuja consulta
      (``dominio_excluido``) ou registro (``registrar_tentativa``) falha com erro
      de armazenamento no ``ArmazemEspiao``;
    - ``gravacao_falha``: URLs cuja ``gravar_coleta`` falha no ``ArmazemEspiao``.
    """

    fontes: tuple[FonteCenario, ...]
    allowlist: frozenset[str] = ALLOWLIST_PADRAO
    reputacao: Mapping[Par, tuple[bool, ...]] = field(default_factory=dict)
    cancelamento: PontoCancelamento | None = None
    trava: tuple[bool, ...] = ()
    exclusoes: tuple[tuple[str, str], ...] = ()
    consultas_falham: frozenset[Par] = frozenset()
    registros_falham: frozenset[Par] = frozenset()
    gravacao_falha: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Saída do oráculo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntradaEsperada:
    url: str
    desfecho: str
    motivo: str | None
    status_http: int | None
    hash_conteudo: str | None
    camada: str | None
    motivo_urllib: str | None


@dataclass(frozen=True)
class TentativaEsperada:
    dominio: str
    camada: str
    sucesso: bool
    motivo: str | None
    status_http: int | None


@dataclass(frozen=True)
class ResultadoOraculo:
    """Comportamento esperado de ``coletar(stealth=True)`` para o cenário.

    ``excecao`` é a classe levantada pelo fallback (``coletar`` deve propagar),
    e nesse caso ``entradas`` só tem as fontes anteriores à exceção.
    ``stealth_inicio`` traz ``(url, motivo_urllib)`` de cada evento esperado,
    na ordem (um por chamada ao fallback).
    """

    chamadas_transporte: tuple[str, ...]
    chamadas_fallback: tuple[str, ...]
    stealth_inicio: tuple[tuple[str, str | None], ...]
    acquires: int
    entradas: tuple[EntradaEsperada, ...]
    tentativas: tuple[TentativaEsperada, ...]
    excecao: type[BaseException] | None = None


def entrada_esperada_de(entrada: Any) -> EntradaEsperada:
    """Projeta uma ``EntradaRelatorio`` real nos campos comparados pelo oráculo."""
    return EntradaEsperada(
        url=entrada.url,
        desfecho=entrada.desfecho,
        motivo=entrada.motivo,
        status_http=entrada.status_http,
        hash_conteudo=entrada.hash_conteudo,
        camada=getattr(entrada, "camada", None),
        motivo_urllib=getattr(entrada, "motivo_urllib", None),
    )


# ---------------------------------------------------------------------------
# Oráculo
# ---------------------------------------------------------------------------


def _resultado_tentativa(desfecho: PaginaBaixada | FalhaBusca) -> bool | None:
    if isinstance(desfecho, PaginaBaixada):
        return True
    if desfecho.motivo in ("rede", "timeout"):
        return False
    if desfecho.motivo == "status_http" and desfecho.status_http in STATUS_BLOQUEIO_ORACULO:
        return False
    return None


def oraculo_stealth(cenario: CenarioStealth) -> ResultadoOraculo:
    """Sequência do fluxograma "Fluxo de uma Fonte_Selecionada" com o Stealth_Efetivo habilitado."""
    historico: dict[Par, list[bool]] = {
        par: list(valores) for par, valores in cenario.reputacao.items()
    }
    trava = list(cenario.trava)
    ponto = cenario.cancelamento
    cancelado = ponto is not None and ponto.fase == "antes_da_fonte" and ponto.indice <= 0

    transporte: list[str] = []
    fallback: list[str] = []
    inicios: list[tuple[str, str | None]] = []
    entradas: list[EntradaEsperada] = []
    tentativas: list[TentativaEsperada] = []
    acquires = 0

    def excluida(dominio: str, camada: str) -> bool:
        if not dominio or (dominio, camada) in cenario.consultas_falham:
            return False
        ultimas = historico.get((dominio, camada), [])[-LIMITE_FALHAS_ORACULO:]
        return len(ultimas) >= LIMITE_FALHAS_ORACULO and not any(ultimas)

    def registrar(dominio: str, camada: str, desfecho: PaginaBaixada | FalhaBusca) -> None:
        sucesso = _resultado_tentativa(desfecho)
        if sucesso is None or not dominio or (dominio, camada) in cenario.registros_falham:
            return
        historico.setdefault((dominio, camada), []).append(sucesso)
        tentativas.append(
            TentativaEsperada(
                dominio=dominio,
                camada=camada,
                sucesso=sucesso,
                motivo=None if sucesso else desfecho.motivo,  # type: ignore[union-attr]
                status_http=desfecho.status_http,
            )
        )

    def entrada(fonte: FonteCenario, desfecho: str, motivo: str | None, status: int | None,
                camada: str | None, motivo_urllib: str | None,
                hash_conteudo: str | None = None) -> None:
        entradas.append(
            EntradaEsperada(fonte.url, desfecho, motivo, status, hash_conteudo, camada, motivo_urllib)
        )

    def gravar(fonte: FonteCenario, pagina: PaginaBaixada, camada: str,
               motivo_urllib: str | None) -> None:
        if fonte.url in cenario.gravacao_falha:
            entrada(fonte, "falha", "erro_armazenamento", pagina.status_http, camada, motivo_urllib)
        else:
            entrada(fonte, "armazenado", None, pagina.status_http, camada, motivo_urllib,
                    _sha(pagina.corpo))

    def dispara(fase: FaseCancelamento, indice: int) -> bool:
        return ponto is not None and ponto.fase == fase and ponto.indice == indice

    for posicao, fonte in enumerate(cenario.fontes):
        if dispara("antes_da_fonte", posicao):
            cancelado = True
        if cancelado:
            entrada(fonte, "falha", "cancelado", None, None, None)
            continue
        if fonte.reuso_corpo is not None:
            entrada(fonte, "reaproveitado", None, STATUS_REUSO, None, None, _sha(fonte.reuso_corpo))
            continue

        dominio = fonte.dominio_fonte
        excluida_urllib = excluida(dominio, CAMADA_URLLIB_ORACULO)
        elegivel = _na_allowlist(fonte.host, cenario.allowlist) and not excluida(
            dominio, CAMADA_STEALTH_ORACULO
        )
        if excluida_urllib:
            if not elegivel:
                entrada(fonte, "falha", "dominio_excluido", None, None, None)
                continue
            camada_anterior, motivo_urllib = None, "dominio_excluido"
        else:
            transporte.append(fonte.url)
            if dispara("durante_urllib", posicao):
                cancelado = True
            desfecho = _desfecho_urllib(fonte.url, fonte.urllib)
            registrar(dominio, CAMADA_URLLIB_ORACULO, desfecho)
            if isinstance(desfecho, PaginaBaixada):
                gravar(fonte, desfecho, CAMADA_URLLIB_ORACULO, None)
                continue
            bloqueio = (
                desfecho.motivo == "status_http"
                and desfecho.status_http in STATUS_BLOQUEIO_ORACULO
            )
            if not (elegivel and bloqueio):
                entrada(fonte, "falha", desfecho.motivo, desfecho.status_http,
                        CAMADA_URLLIB_ORACULO, None)
                continue
            camada_anterior, motivo_urllib = CAMADA_URLLIB_ORACULO, desfecho.motivo

        if cancelado:
            entrada(fonte, "falha", "cancelado", None, camada_anterior, motivo_urllib)
            continue
        acquires += 1
        obtida = trava.pop(0) if trava else True
        if not obtida:
            entrada(fonte, "falha", "navegador_indisponivel", None, camada_anterior, motivo_urllib)
            continue

        inicios.append((fonte.url, motivo_urllib))
        fallback.append(fonte.url)
        if dispara("durante_fallback", posicao):
            cancelado = True
        item = fonte.item_fallback
        if isinstance(item, BaseException):
            return ResultadoOraculo(
                tuple(transporte), tuple(fallback), tuple(inicios), acquires,
                tuple(entradas), tuple(tentativas), excecao=type(item),
            )
        registrar(dominio, CAMADA_STEALTH_ORACULO, item)
        if isinstance(item, FalhaBusca):
            entrada(fonte, "falha", item.motivo, item.status_http, CAMADA_STEALTH_ORACULO,
                    motivo_urllib)
        else:
            gravar(fonte, item, CAMADA_STEALTH_ORACULO, motivo_urllib)

    for url, motivo in cenario.exclusoes:
        entradas.append(EntradaEsperada(url, "url_invalida", motivo, None, None, None, None))

    return ResultadoOraculo(
        tuple(transporte), tuple(fallback), tuple(inicios), acquires,
        tuple(entradas), tuple(tentativas),
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChamadaFallback:
    url: str
    kwargs: Mapping[str, Any]

    @property
    def allowlist(self) -> Any:
        return self.kwargs.get("allowlist")

    @property
    def timeout(self) -> Any:
        return self.kwargs.get("timeout")


class FallbackFake:
    """Fallback_Stealth fake.

    ``roteiro``: ``Mapping`` URL → item, ou ``Sequence`` de itens consumidos em
    ordem. Item ``PaginaBaixada``/``FalhaBusca`` é devolvido; exceção (instância
    ou classe) é levantada. Sem item para a chamada → ``padrao`` (se ``None``,
    ``AssertionError``). Toda chamada é registrada com os kwargs recebidos.

    ``durante(url)`` roda no meio da chamada (ligar cancelamento, por exemplo);
    ``dormir`` segura a chamada por N segundos; ``linha_do_tempo`` recebe
    ``("fallback", url)`` no início de cada chamada. ``max_em_andamento`` mede a
    concorrência entre threads.
    """

    def __init__(
        self,
        roteiro: Mapping[str, ItemFallback | type[BaseException]]
        | Sequence[ItemFallback | type[BaseException]]
        | None = None,
        *,
        padrao: ItemFallback | None = None,
        durante: Callable[[str], None] | None = None,
        dormir: float = 0.0,
        linha_do_tempo: list[Any] | None = None,
    ) -> None:
        self._por_url: dict[str, Any] | None = None
        self._fila: list[Any] | None = None
        if isinstance(roteiro, Mapping):
            self._por_url = dict(roteiro)
        elif roteiro is not None:
            self._fila = list(roteiro)
        self._padrao = padrao
        self._durante = durante
        self._dormir = dormir
        self.linha_do_tempo = linha_do_tempo
        self.chamadas: list[ChamadaFallback] = []
        self._trava = threading.Lock()
        self.em_andamento = 0
        self.max_em_andamento = 0

    @property
    def urls(self) -> list[str]:
        return [chamada.url for chamada in self.chamadas]

    def _item(self, url: str) -> Any:
        if self._por_url is not None and url in self._por_url:
            return self._por_url[url]
        if self._fila:
            return self._fila.pop(0)
        if self._padrao is not None:
            return self._padrao
        raise AssertionError(f"chamada inesperada ao fallback: {url}")

    def __call__(self, url: str, **kwargs: Any) -> PaginaBaixada | FalhaBusca:
        with self._trava:
            self.chamadas.append(ChamadaFallback(url, dict(kwargs)))
            self.em_andamento += 1
            self.max_em_andamento = max(self.max_em_andamento, self.em_andamento)
            if self.linha_do_tempo is not None:
                self.linha_do_tempo.append(("fallback", url))
        try:
            if self._durante is not None:
                self._durante(url)
            if self._dormir:
                time.sleep(self._dormir)
            item = self._item(url)
            if isinstance(item, type) and issubclass(item, BaseException):
                raise item()
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            with self._trava:
                self.em_andamento -= 1


class TravaFake:
    """Trava_Navegador fake: ``acquire`` segue o roteiro (esgotado → ``padrao``)."""

    def __init__(self, roteiro: Iterable[bool] = (), *, padrao: bool = True) -> None:
        self._roteiro = list(roteiro)
        self._padrao = padrao
        self.acquires: list[tuple[bool, float]] = []
        self.releases = 0
        self.detida = False

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        self.acquires.append((blocking, timeout))
        obtida = self._roteiro.pop(0) if self._roteiro else self._padrao
        if obtida:
            if self.detida:
                raise AssertionError("TravaFake adquirida de novo sem release")
            self.detida = True
        return obtida

    def release(self) -> None:
        if not self.detida:
            raise RuntimeError("release de TravaFake não adquirida")
        self.detida = False
        self.releases += 1

    @property
    def chamadas(self) -> int:
        return len(self.acquires) + self.releases


ExcecaoInjetada = type[BaseException] | Callable[[], BaseException]


def _criar_excecao(fabrica: ExcecaoInjetada) -> BaseException:
    if isinstance(fabrica, type) and issubclass(fabrica, ErroArmazenamento):
        return fabrica("falha injetada")
    if isinstance(fabrica, type) and issubclass(fabrica, sqlite3.Error):
        return fabrica("falha injetada")
    return fabrica()  # type: ignore[call-arg]


class ArmazemEspiao(ArmazemPaginas):
    """``ArmazemPaginas`` real com contadores e falhas injetáveis.

    ``chamadas`` conta ``registrar_tentativa``, ``ultimas_tentativas``,
    ``dominio_excluido`` e ``gravar_coleta`` (``dominio_excluido`` da base chama
    ``ultimas_tentativas``, que também é contado). Falhas:

    - ``falhar(metodo, pares=..., chamadas=..., excecao=...)``: levanta antes de
      tocar no banco quando o par (domínio normalizado, camada) está em
      ``pares`` ou o índice 0-based da chamada do método está em ``chamadas``;
    - ``falhar_gravacao(urls, excecao=...)``: ``gravar_coleta`` falha para os
      registros com ``url_original`` em ``urls``.
    """

    METODOS_REPUTACAO = ("registrar_tentativa", "ultimas_tentativas", "dominio_excluido")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.chamadas: Counter[str] = Counter()
        self._falhas: dict[str, tuple[frozenset[Par], frozenset[int], ExcecaoInjetada]] = {}
        self._gravacao_falha: frozenset[str] = frozenset()
        self._excecao_gravacao: ExcecaoInjetada = ErroArmazenamento

    def falhar(
        self,
        metodo: str,
        *,
        pares: Collection[Par] = (),
        chamadas: Collection[int] = (),
        excecao: ExcecaoInjetada = ErroArmazenamento,
    ) -> None:
        if metodo not in self.METODOS_REPUTACAO:
            raise ValueError(f"método sem injeção de falha: {metodo}")
        normalizados = frozenset((_normalizar_dominio(d), c) for d, c in pares)
        self._falhas[metodo] = (normalizados, frozenset(chamadas), excecao)

    def falhar_gravacao(
        self, urls: Collection[str], *, excecao: ExcecaoInjetada = ErroArmazenamento
    ) -> None:
        self._gravacao_falha = frozenset(urls)
        self._excecao_gravacao = excecao

    @property
    def chamadas_reputacao(self) -> int:
        return sum(self.chamadas[m] for m in self.METODOS_REPUTACAO)

    def _contar_e_talvez_falhar(self, metodo: str, dominio: str, camada: str) -> None:
        indice = self.chamadas[metodo]
        self.chamadas[metodo] += 1
        regra = self._falhas.get(metodo)
        if regra is None:
            return
        pares, indices, excecao = regra
        if (_normalizar_dominio(dominio), camada) in pares or indice in indices:
            raise _criar_excecao(excecao)

    def registrar_tentativa(self, dominio: str, camada: str, **kwargs: Any) -> None:
        self._contar_e_talvez_falhar("registrar_tentativa", dominio, camada)
        super().registrar_tentativa(dominio, camada, **kwargs)

    def ultimas_tentativas(self, dominio: str, camada: str, *args: Any) -> tuple[TentativaFetch, ...]:
        self._contar_e_talvez_falhar("ultimas_tentativas", dominio, camada)
        return super().ultimas_tentativas(dominio, camada, *args)

    def dominio_excluido(self, dominio: str, camada: str, *args: Any) -> bool:
        self._contar_e_talvez_falhar("dominio_excluido", dominio, camada)
        return super().dominio_excluido(dominio, camada, *args)

    def gravar_coleta(self, conteudo: bytes, registro: NovoRegistroColeta) -> str:
        self.chamadas["gravar_coleta"] += 1
        if registro.url_original in self._gravacao_falha:
            raise _criar_excecao(self._excecao_gravacao)
        return super().gravar_coleta(conteudo, registro)


# ---------------------------------------------------------------------------
# Cancelamento
# ---------------------------------------------------------------------------


class ControleCancelamento:
    """Liga o Sinal_Cancelamento no ponto do cenário.

    Use ``cancelado`` como parâmetro de ``coletar``, ``observador`` como
    observador (repassa os eventos a ``proximo``), ``envolver_transporte`` no
    transporte do coletor e ``durante_fallback`` como ``durante`` do
    ``FallbackFake``.
    """

    def __init__(
        self,
        ponto: PontoCancelamento | None,
        urls: Sequence[str],
        *,
        proximo: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.evento = threading.Event()
        self._ponto = ponto
        self._urls = list(urls)
        self._proximo = proximo
        self._entradas = 0
        if ponto is not None and ponto.fase == "antes_da_fonte" and ponto.indice <= 0:
            self.evento.set()

    def cancelado(self) -> bool:
        return self.evento.is_set()

    def _alvo(self, fase: FaseCancelamento) -> str | None:
        if self._ponto is None or self._ponto.fase != fase:
            return None
        if not 0 <= self._ponto.indice < len(self._urls):
            return None
        return self._urls[self._ponto.indice]

    def observador(self, evento: Mapping[str, Any]) -> None:
        if evento.get("evento") == "coleta_paginas.entrada":
            self._entradas += 1
            ponto = self._ponto
            if ponto is not None and ponto.fase == "antes_da_fonte" and self._entradas == ponto.indice:
                self.evento.set()
        if self._proximo is not None:
            self._proximo(evento)

    def envolver_transporte(self, transporte: Callable[..., Any]) -> Callable[..., Any]:
        alvo = self._alvo("durante_urllib")

        def transporte_com_cancelamento(url: str, cabecalhos: Mapping[str, str], timeout: float):
            if alvo is not None and url == alvo:
                self.evento.set()
            return transporte(url, cabecalhos, timeout)

        return transporte_com_cancelamento

    def durante_fallback(self, url: str) -> None:
        alvo = self._alvo("durante_fallback")
        if alvo is not None and url == alvo:
            self.evento.set()


# ---------------------------------------------------------------------------
# Montagem do cenário (armazém, peça, resultados, fakes)
# ---------------------------------------------------------------------------


def peca_cenario() -> PecaConsultada:
    return peca()


def resultados_do_cenario(cenario: CenarioStealth) -> list[ResultadoBusca]:
    """Resultados ``alta`` com posições crescentes: a seleção mantém a ordem das
    fontes (o teto deve ser ``>= len(cenario.fontes)``). As exclusões vêm depois."""
    resultados = [
        replace(resultado(fonte.url, posicao + 1), dominio=fonte.dominio_resultado)
        for posicao, fonte in enumerate(cenario.fontes)
    ]
    base = len(cenario.fontes) + 1
    resultados.extend(
        resultado(url, base + j) for j, (url, _motivo) in enumerate(cenario.exclusoes)
    )
    return resultados


def pares_reputacao(cenario: CenarioStealth) -> tuple[Par, ...]:
    """Todos os pares (Dominio_Fonte, camada) que o cenário pode tocar."""
    dominios = sorted({f.dominio_fonte for f in cenario.fontes if f.dominio_fonte}
                      | {d for d, _ in cenario.reputacao})
    return tuple((d, c) for d in dominios for c in CAMADAS_ORACULO)


def semear_reputacao(armazem: ArmazemPaginas, reputacao: Mapping[Par, Sequence[bool]]) -> None:
    """Grava a reputação pré-semeada pela API da base (sem passar pelo espião)."""
    for (dominio, camada), valores in reputacao.items():
        for sucesso in valores:
            ArmazemPaginas.registrar_tentativa(
                armazem,
                dominio,
                camada,
                sucesso=sucesso,
                motivo=None if sucesso else "status_http",
                status_http=200 if sucesso else 403,
            )


def semear_reuso(
    armazem: ArmazemPaginas,
    url: str,
    corpo: bytes,
    coletado_em: datetime,
    *,
    dominio: str = "origem.com",
) -> str:
    """Registro_Coleta reaproveitável para ``url`` (pela API da base)."""
    registro = NovoRegistroColeta(
        codigo_peca="ORIGEM1",
        marca_peca="Origem",
        url_original=url,
        url_normalizada=normalizar_url(url),
        url_final=url,
        dominio=dominio,
        confianca="alta",
        codigo_confirmado=True,
        marca_confirmada=True,
        nome_reforcado=False,
        motivo_decisao="origem",
        status_http=STATUS_REUSO,
        content_type=CONTENT_TYPE_HTML,
        charset="utf-8",
        coletado_em=coletado_em,
    )
    return ArmazemPaginas.gravar_coleta(armazem, corpo, registro)


def maior_id_tentativa(armazem: ArmazemPaginas, pares: Iterable[Par]) -> int:
    ids = [t.id for d, c in pares for t in ArmazemPaginas.ultimas_tentativas(armazem, d, c, 10**6)]
    return max(ids, default=0)


def semear_cenario(armazem: ArmazemPaginas, cenario: CenarioStealth, agora: datetime) -> int:
    """Reputação e reuso pré-existentes; devolve o maior id de tentativa depois de semear."""
    semear_reputacao(armazem, cenario.reputacao)
    for fonte in cenario.fontes:
        if fonte.reuso_corpo is not None:
            semear_reuso(armazem, fonte.url, fonte.reuso_corpo, agora)
    if isinstance(armazem, ArmazemEspiao):
        armazem.falhar("dominio_excluido", pares=cenario.consultas_falham)
        armazem.falhar("registrar_tentativa", pares=cenario.registros_falham)
        armazem.falhar_gravacao(cenario.gravacao_falha)
    return maior_id_tentativa(armazem, pares_reputacao(cenario))


def tentativas_novas(
    armazem: ArmazemPaginas, cenario: CenarioStealth, depois_de_id: int
) -> tuple[TentativaEsperada, ...]:
    """Tentativa_Fetch com ``id > depois_de_id`` nos pares do cenário, por ``id``."""
    todas = sorted(
        (
            t
            for d, c in pares_reputacao(cenario)
            for t in ArmazemPaginas.ultimas_tentativas(armazem, d, c, 10**6)
            if t.id > depois_de_id
        ),
        key=lambda t: t.id,
    )
    return tuple(
        TentativaEsperada(t.dominio, t.camada, t.sucesso, t.motivo, t.status_http) for t in todas
    )


@dataclass
class ExecucaoCenario:
    """Fakes prontos para um cenário. ``transporte`` vai ao coletor;
    ``roteiro_transporte.chamadas`` registra as URLs pedidas."""

    roteiro_transporte: TransporteRoteiro
    transporte: Callable[..., Any]
    fallback: FallbackFake
    trava: TravaFake
    cancelamento: ControleCancelamento


def preparar_execucao(
    cenario: CenarioStealth,
    *,
    linha_do_tempo: list[Any] | None = None,
    proximo_observador: Callable[[Mapping[str, Any]], None] | None = None,
) -> ExecucaoCenario:
    urls = [fonte.url for fonte in cenario.fontes]
    controle = ControleCancelamento(cenario.cancelamento, urls, proximo=proximo_observador)
    roteiro = TransporteRoteiro({f.url: item_transporte(f.urllib) for f in cenario.fontes})
    fallback = FallbackFake(
        {f.url: f.item_fallback for f in cenario.fontes},
        durante=controle.durante_fallback,
        linha_do_tempo=linha_do_tempo,
    )
    return ExecucaoCenario(
        roteiro_transporte=roteiro,
        transporte=controle.envolver_transporte(roteiro),
        fallback=fallback,
        trava=TravaFake(cenario.trava),
        cancelamento=controle,
    )


# ---------------------------------------------------------------------------
# Ambiente
# ---------------------------------------------------------------------------


def fixar_ambiente_stealth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    dominios: str = "mercadocar.com.br",
) -> Path:
    """Fixa a Allowlist_Stealth e o perfil (em ``tmp_path``); devolve o perfil.

    O diretório do perfil não é criado aqui (o fallback real o cria se faltar).
    """
    perfil = tmp_path / "perfil_stealth"
    monkeypatch.setenv("ESTAGIARIO_COLETA_DOMINIOS_STEALTH", dominios)
    monkeypatch.setenv("ESTAGIARIO_STEALTH_PROFILE_DIR", str(perfil))
    return perfil


# ---------------------------------------------------------------------------
# Estratégias Hypothesis
# ---------------------------------------------------------------------------

corpos = st.binary(min_size=0, max_size=24).map(lambda b: b"<html>" + b)

roteiros_urllib: st.SearchStrategy[RoteiroUrllib] = st.one_of(
    st.builds(UrllibStatus, st.sampled_from([403, 429, 503])),  # Status_Bloqueio
    st.builds(UrllibStatus, st.sampled_from([403, 429, 503])),  # peso extra
    st.builds(UrllibStatus, st.sampled_from([400, 401, 404, 410, 500, 502, 504])),
    st.builds(UrllibPagina, corpos, st.sampled_from([200, 203])),
    st.builds(UrllibErro, st.sampled_from(["rede", "timeout"])),
    st.just(UrllibNaoHtml()),
    st.just(UrllibCorpoVazio()),
)


@st.composite
def falhas_busca(draw: st.DrawFn, url: str) -> FalhaBusca:
    motivo = draw(st.sampled_from(MOTIVOS_FALHA_BUSCA))
    status = (
        draw(st.sampled_from([403, 429, 503, 404, 500])) if motivo == "status_http" else None
    )
    return FalhaBusca(motivo, url, status_http=status)  # type: ignore[arg-type]


@st.composite
def paginas_stealth(draw: st.DrawFn, url: str) -> PaginaBaixada:
    return PaginaBaixada(
        status_http=draw(st.sampled_from([200, 203])),
        content_type=CONTENT_TYPE_HTML,
        charset="utf-8",
        url_final=url,
        corpo=b"<html>stealth " + draw(st.binary(max_size=24)),
    )


def itens_fallback(url: str, *, com_excecao: bool = False) -> st.SearchStrategy[ItemFallback]:
    opcoes: list[st.SearchStrategy[Any]] = [paginas_stealth(url), paginas_stealth(url), falhas_busca(url)]
    if com_excecao:
        opcoes.append(st.sampled_from([RuntimeError("x"), KeyError("y"), ValueError("z")]))
    return st.one_of(*opcoes)


def _variantes_dominio(host: str) -> st.SearchStrategy[str | None]:
    return st.one_of(
        st.none(),
        st.none(),
        st.just(f" {host.upper()}. "),
        st.sampled_from(HOSTS),  # Dominio_Fonte diferente do host da URL
    )


@st.composite
def fontes_cenario(
    draw: st.DrawFn, indice: int, *, com_reuso: bool = True, com_excecao: bool = False
) -> FonteCenario:
    host = draw(st.sampled_from(HOSTS))
    url = f"https://{host}/peca/{indice}"
    return FonteCenario(
        indice=indice,
        host=host,
        urllib=draw(roteiros_urllib),
        fallback=draw(itens_fallback(url, com_excecao=com_excecao)),
        dominio_resultado=draw(_variantes_dominio(host)),
        reuso_corpo=draw(st.one_of(st.none(), st.none(), st.none(), corpos))
        if com_reuso
        else None,
    )


allowlists = st.one_of(
    st.just(ALLOWLIST_PADRAO),
    st.just(ALLOWLIST_PADRAO),
    st.just(frozenset()),
    st.just(frozenset({"mercadocar.com.br", "outro.com.br"})),
    st.just(frozenset({"www.mercadocar.com.br"})),
)

historicos = st.one_of(
    st.just(()),
    st.just((False, False, False)),  # Camada_Excluida
    st.lists(st.booleans(), max_size=4).map(tuple),
)

EXCLUSOES_POSSIVEIS = (
    ("ftp://mercadocar.com.br/peca", "url_invalida"),
    ("http://127.0.0.1/peca", "destino_nao_permitido"),
    ("http://localhost/peca", "destino_nao_permitido"),
)


@st.composite
def cenarios_stealth(
    draw: st.DrawFn,
    *,
    max_fontes: int = 4,
    com_reuso: bool = True,
    com_cancelamento: bool = True,
    com_trava: bool = True,
    com_exclusoes: bool = True,
    com_falhas_armazem: bool = False,
    com_excecao: bool = False,
) -> CenarioStealth:
    """Cenário completo. Falhas de armazém e exceções do fallback só entram quando
    pedidas (Properties 7 e 9); os demais eixos são ligados por padrão."""
    n = draw(st.integers(min_value=0, max_value=max_fontes))
    fontes = tuple(
        draw(fontes_cenario(i, com_reuso=com_reuso, com_excecao=com_excecao)) for i in range(n)
    )
    dominios = sorted({f.dominio_fonte for f in fontes})
    reputacao = {
        (d, c): draw(historicos) for d in dominios for c in CAMADAS_ORACULO
    }
    reputacao = {par: valores for par, valores in reputacao.items() if valores}

    cancelamento = None
    if com_cancelamento and n and draw(st.booleans()):
        cancelamento = PontoCancelamento(
            fase=draw(st.sampled_from(["antes_da_fonte", "durante_urllib", "durante_fallback"])),
            indice=draw(st.integers(min_value=0, max_value=n - 1)),
        )
    trava = tuple(draw(st.lists(st.booleans(), max_size=n))) if com_trava else ()
    exclusoes = (
        tuple(draw(st.lists(st.sampled_from(EXCLUSOES_POSSIVEIS), max_size=2, unique=True)))
        if com_exclusoes
        else ()
    )

    consultas_falham: frozenset[Par] = frozenset()
    registros_falham: frozenset[Par] = frozenset()
    gravacao_falha: frozenset[str] = frozenset()
    if com_falhas_armazem:
        pares = [(d, c) for d in dominios for c in CAMADAS_ORACULO]
        if pares:
            consultas_falham = frozenset(draw(st.lists(st.sampled_from(pares), max_size=len(pares))))
            registros_falham = frozenset(draw(st.lists(st.sampled_from(pares), max_size=len(pares))))
        if fontes:
            urls = [f.url for f in fontes]
            gravacao_falha = frozenset(draw(st.lists(st.sampled_from(urls), max_size=len(urls))))

    return CenarioStealth(
        fontes=fontes,
        allowlist=draw(allowlists),
        reputacao=reputacao,
        cancelamento=cancelamento,
        trava=trava,
        exclusoes=exclusoes,
        consultas_falham=consultas_falham,
        registros_falham=registros_falham,
        gravacao_falha=gravacao_falha,
    )
