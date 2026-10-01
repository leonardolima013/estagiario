"""Testes de ``tools.buscador_stealth`` (fallback de coleta stealth, isolado).

Sem rede e sem navegador: contexto, página, requisições e respostas são fakes
que imitam o subconjunto da API síncrona do Playwright usado pelo módulo
(inclusive a semântica de ``page.route`` não ver saltos de redirecionamento). As
exceções são as classes reais do Patchright, para exercitar a classificação de
erros. O fuso é sempre injetado; ip-api.com nunca é consultado.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from patchright.sync_api import Error as ErroPlaywright
from patchright.sync_api import TimeoutError as TimeoutPlaywright

from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tools import buscador_stealth as bs
from tools.buscador_paginas import LIMITE_CORPO_BYTES, FalhaBusca, PaginaBaixada

RAIZ = Path(__file__).resolve().parent.parent
URL = "https://www.mercadocar.com.br/produto/filtro-83061"
HTML = "<html><head><title>Filtro</title></head><body>Filtro de óleo 83061</body></html>"
CT_HTML = {"content-type": "text/html; charset=utf-8"}
FUSO = "America/Sao_Paulo"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRequisicao:
    def __init__(self, url: str, frame: object, *, navegacao: bool = True) -> None:
        self.url = url
        self.frame = frame
        self._navegacao = navegacao

    def is_navigation_request(self) -> bool:
        return self._navegacao


class FakeResposta:
    def __init__(self, url: str, status: int, headers: dict[str, str], request: FakeRequisicao):
        self.url = url
        self.status = status
        self.headers = headers
        self.request = request


class FakeRota:
    def __init__(self) -> None:
        self.abortada = False
        self.continuada = False

    def abort(self, codigo: str = "failed") -> None:
        self.abortada = True

    def continue_(self) -> None:
        self.continuada = True


class Relogio:
    def __init__(self) -> None:
        self.agora = 0.0

    def __call__(self) -> float:
        return self.agora


class FakePagina:
    """Página fake.

    ``cadeia``: saltos da navegação do frame principal ``(url, status, headers)``;
    ``page.route`` só é chamado no primeiro salto, como no Playwright.
    ``subrecursos``: URLs de requisições não-navegação feitas durante o goto.
    ``navegacao_js``: URL de uma nova navegação (não redirect) disparada durante
    a espera por ``networkidle`` — passa por ``page.route``.
    """

    def __init__(
        self,
        *,
        cadeia: list[tuple[str, int, dict[str, str]]] | None = None,
        html: str = HTML,
        erro_goto: BaseException | None = None,
        erro_networkidle: BaseException | None = None,
        erro_content: BaseException | None = None,
        subrecursos: tuple[str, ...] = (),
        navegacao_js: str | None = None,
        relogio: Relogio | None = None,
        custo_goto_s: float = 0.0,
        titulo: str = "Filtro",
    ) -> None:
        self.main_frame = object()
        self.url = "about:blank"
        self._cadeia = cadeia
        self._html = html
        self._erro_goto = erro_goto
        self._erro_networkidle = erro_networkidle
        self._erro_content = erro_content
        self._subrecursos = subrecursos
        self._navegacao_js = navegacao_js
        self._relogio = relogio
        self._custo_goto_s = custo_goto_s
        self._titulo = titulo
        self._handlers: dict[str, list[Any]] = {}
        self._rota: Any = None
        self.goto_chamadas: list[dict[str, Any]] = []
        self.espera_chamadas: list[tuple[str, float]] = []
        self.content_chamadas = 0
        self.rotas: list[tuple[str, FakeRota]] = []

    # API usada pelo módulo -------------------------------------------------
    def on(self, evento: str, handler: Any) -> None:
        self._handlers.setdefault(evento, []).append(handler)

    def route(self, padrao: str, handler: Any) -> None:
        assert padrao == "**/*"
        self._rota = handler

    def _emitir(self, evento: str, objeto: object) -> None:
        for handler in self._handlers.get(evento, []):
            handler(objeto)

    def _rotear(self, requisicao: FakeRequisicao) -> FakeRota:
        rota = FakeRota()
        self.rotas.append((requisicao.url, rota))
        if self._rota is not None:
            self._rota(rota, requisicao)
        return rota

    def goto(self, url: str, *, wait_until: str, timeout: float) -> FakeResposta | None:
        self.goto_chamadas.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        if self._relogio is not None:
            self._relogio.agora += self._custo_goto_s
        cadeia = self._cadeia or [(url, 200, CT_HTML)]
        resposta: FakeResposta | None = None
        for indice, (destino, status, headers) in enumerate(cadeia):
            requisicao = FakeRequisicao(destino, self.main_frame)
            if indice == 0 and self._rotear(requisicao).abortada:
                raise ErroPlaywright("net::ERR_BLOCKED_BY_CLIENT")
            self._emitir("request", requisicao)
            resposta = FakeResposta(destino, status, headers, requisicao)
            self._emitir("response", resposta)
            self.url = destino
        for sub in self._subrecursos:
            self._rotear(FakeRequisicao(sub, self.main_frame, navegacao=False))
        if self._erro_goto is not None:
            raise self._erro_goto
        return resposta

    def wait_for_load_state(self, estado: str, *, timeout: float) -> None:
        self.espera_chamadas.append((estado, timeout))
        if self._navegacao_js is not None:
            requisicao = FakeRequisicao(self._navegacao_js, self.main_frame)
            if not self._rota_abortou(requisicao):
                self._emitir("request", requisicao)
                self._emitir(
                    "response", FakeResposta(self._navegacao_js, 200, CT_HTML, requisicao)
                )
                self.url = self._navegacao_js
        if self._erro_networkidle is not None:
            raise self._erro_networkidle

    def _rota_abortou(self, requisicao: FakeRequisicao) -> bool:
        return self._rotear(requisicao).abortada

    def content(self) -> str:
        self.content_chamadas += 1
        if self._erro_content is not None:
            raise self._erro_content
        return self._html

    def title(self) -> str:
        return self._titulo


class FakeContexto:
    def __init__(self, pagina: FakePagina, *, erro_close: BaseException | None = None) -> None:
        self.pages = [pagina]
        self.fechamentos = 0
        self._erro_close = erro_close

    def new_page(self) -> FakePagina:  # pragma: no cover - pages nunca vazio aqui
        raise AssertionError("new_page não esperado")

    def cookies(self) -> list[dict[str, Any]]:
        return [
            {"name": "_abck", "domain": ".mercadocar.com.br", "value": "SEGREDO"},
            {"name": "sessao", "domain": "www.mercadocar.com.br", "value": "SEGREDO2"},
        ]

    def close(self) -> None:
        self.fechamentos += 1
        if self._erro_close is not None:
            raise self._erro_close


class Abridor:
    def __init__(self, contexto: FakeContexto | None = None, *, erro: BaseException | None = None):
        self.contexto = contexto
        self.erro = erro
        self.chamadas: list[dict[str, Any]] = []

    def __call__(self, **opcoes: Any) -> FakeContexto:
        self.chamadas.append(opcoes)
        if self.erro is not None:
            raise self.erro
        assert self.contexto is not None
        return self.contexto


class FusoFake:
    def __init__(self) -> None:
        self.chamadas: list[Path] = []

    def __call__(self, perfil: Path) -> str:
        self.chamadas.append(perfil)
        return FUSO


# ---------------------------------------------------------------------------
# Fixtures e helper
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("ESTAGIARIO_COLETA_DOMINIOS_STEALTH", raising=False)
    monkeypatch.delenv("ESTAGIARIO_COLETA_TIMEOUT", raising=False)
    bs.limpar_cache_fuso()
    yield
    bs.limpar_cache_fuso()


@pytest.fixture
def perfil(tmp_path: Path) -> Path:
    return tmp_path / "chrome-profile"


def _coletar(
    perfil: Path,
    url: str = URL,
    pagina: FakePagina | None = None,
    *,
    contexto: FakeContexto | None = None,
    abridor: Abridor | None = None,
    **kwargs: Any,
):
    pagina = pagina or FakePagina()
    contexto = contexto or FakeContexto(pagina)
    abridor = abridor or Abridor(contexto)
    kwargs.setdefault("obter_fuso", FusoFake())
    kwargs.setdefault("timeout", 15)
    resultado = bs.coletar_via_playwright_stealth(
        url, profile_dir=perfil, abrir_contexto=abridor, **kwargs
    )
    return resultado, abridor, contexto, pagina


# ---------------------------------------------------------------------------
# Allowlist (pura)
# ---------------------------------------------------------------------------

ALLOW = frozenset({"mercadocar.com.br"})


@pytest.mark.parametrize(
    ("url", "esperado"),
    [
        ("https://mercadocar.com.br/", True),
        ("https://www.mercadocar.com.br/x", True),
        ("https://WWW.MercadoCar.com.br./x", True),
        ("https://loja.www.mercadocar.com.br:8443/x", True),
        ("https://mercadocar.com.br.evil.com/", False),
        ("https://evilmercadocar.com.br/", False),
        ("https://mercadocar.com/", False),
        ("https://autodoc.parts/", False),
        ("nao e url", False),
        ("", False),
    ],
)
def test_dominio_na_allowlist(url: str, esperado: bool) -> None:
    assert bs.dominio_na_allowlist(url, ALLOW) is esperado


def test_allowlist_vazia_nao_casa_nada() -> None:
    assert bs.dominio_na_allowlist(URL, frozenset()) is False


# ---------------------------------------------------------------------------
# Barreiras antes de abrir o navegador
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "motivo"),
    [
        ("http://127.0.0.1/", "destino_nao_permitido"),
        ("http://localhost/admin", "destino_nao_permitido"),
        ("http://192.168.0.10/", "destino_nao_permitido"),
        ("http://2130706433/", "destino_nao_permitido"),
        ("ftp://mercadocar.com.br/", "url_invalida"),
        ("", "url_invalida"),
    ],
)
def test_ssrf_recusa_antes_de_abrir_navegador(perfil: Path, url: str, motivo: str) -> None:
    fuso = FusoFake()
    # A allowlist inclui os próprios hosts: a recusa vem do SSRF, que vem antes.
    allow = frozenset({"mercadocar.com.br", "localhost", "127.0.0.1", "192.168.0.10"})
    resultado, abridor, _, _ = _coletar(perfil, url, allowlist=allow, obter_fuso=fuso)

    assert isinstance(resultado, FalhaBusca)
    assert resultado.motivo == motivo
    assert abridor.chamadas == []
    assert fuso.chamadas == []


@pytest.mark.parametrize(
    "url",
    [
        "https://www.autodoc.parts/filtro",
        "https://autodoc.parts/",
        "https://www.auto-doc.ie/peca",
        "https://www.ebay.co.uk/itm/1",
        "https://ebay.co.uk/",
    ],
)
def test_fora_da_allowlist_padrao_nunca_abre_navegador(perfil: Path, url: str) -> None:
    fuso = FusoFake()
    resultado, abridor, _, _ = _coletar(perfil, url, obter_fuso=fuso)  # allowlist do config

    assert isinstance(resultado, FalhaBusca)
    assert resultado.motivo == "dominio_nao_permitido_stealth"
    assert abridor.chamadas == []
    assert fuso.chamadas == []
    assert not perfil.exists()


def test_allowlist_desligada_por_env_vazia(perfil: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESTAGIARIO_COLETA_DOMINIOS_STEALTH", "")
    resultado, abridor, _, _ = _coletar(perfil)

    assert isinstance(resultado, FalhaBusca)
    assert resultado.motivo == "dominio_nao_permitido_stealth"
    assert abridor.chamadas == []


@pytest.mark.parametrize("display", [None, "", "   "])
def test_sem_display_nao_abre_navegador(
    perfil: Path, monkeypatch: pytest.MonkeyPatch, display: str | None
) -> None:
    if display is None:
        monkeypatch.delenv("DISPLAY", raising=False)
    else:
        monkeypatch.setenv("DISPLAY", display)
    resultado, abridor, _, _ = _coletar(perfil)

    assert isinstance(resultado, FalhaBusca)
    assert resultado.motivo == "ambiente_sem_display"
    assert abridor.chamadas == []


def test_wayland_display_basta(perfil: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    resultado, _, _, _ = _coletar(perfil)
    assert isinstance(resultado, PaginaBaixada)


# ---------------------------------------------------------------------------
# Abertura do contexto
# ---------------------------------------------------------------------------


def test_kwargs_do_contexto_headed_chrome_real(perfil: Path) -> None:
    resultado, abridor, _, _ = _coletar(perfil)

    assert isinstance(resultado, PaginaBaixada)
    assert abridor.chamadas == [
        {
            "user_data_dir": str(perfil),
            "channel": "chrome",
            "headless": False,
            "no_viewport": True,
            "timezone_id": FUSO,
        }
    ]
    assert perfil.is_dir()


def test_nao_existe_parametro_para_headless() -> None:
    for funcao in (bs.coletar_via_playwright_stealth, bs.diagnosticar_stealth):
        assert "headless" not in inspect.signature(funcao).parameters


def test_falha_ao_iniciar_navegador(perfil: Path) -> None:
    abridor = Abridor(erro=ErroPlaywright("ProcessSingleton: perfil em uso"))
    resultado, _, _, _ = _coletar(perfil, abridor=abridor)

    assert isinstance(resultado, FalhaBusca)
    assert resultado.motivo == "navegador_indisponivel"


# ---------------------------------------------------------------------------
# Navegação, status e conteúdo
# ---------------------------------------------------------------------------


def test_sucesso(perfil: Path) -> None:
    resultado, _, contexto, pagina = _coletar(perfil)

    assert resultado == PaginaBaixada(
        status_http=200,
        content_type="text/html; charset=utf-8",
        charset="utf-8",
        url_final=URL,
        corpo=HTML.encode("utf-8"),
    )
    assert pagina.goto_chamadas == [
        {"url": URL, "wait_until": "domcontentloaded", "timeout": 15000.0}
    ]
    assert [estado for estado, _ in pagina.espera_chamadas] == ["networkidle"]
    assert contexto.fechamentos == 1


def test_status_e_o_da_ultima_navegacao_apos_redirect(perfil: Path) -> None:
    final = "https://www.mercadocar.com.br/produto/filtro-83061/"
    pagina = FakePagina(
        cadeia=[
            (URL, 301, {"location": final}),
            (final, 200, {"content-type": "text/html"}),
        ]
    )
    resultado, _, _, _ = _coletar(perfil, pagina=pagina)

    assert isinstance(resultado, PaginaBaixada)
    assert (resultado.status_http, resultado.url_final, resultado.content_type) == (
        200,
        final,
        "text/html",
    )


def test_status_403_vira_status_http(perfil: Path) -> None:
    pagina = FakePagina(cadeia=[(URL, 403, CT_HTML)])
    resultado, _, contexto, _ = _coletar(perfil, pagina=pagina)

    assert isinstance(resultado, FalhaBusca)
    assert (resultado.motivo, resultado.status_http) == ("status_http", 403)
    assert contexto.fechamentos == 1


def test_redirect_para_destino_proibido(perfil: Path) -> None:
    pagina = FakePagina(
        cadeia=[
            (URL, 302, {"location": "http://127.0.0.1/admin"}),
            ("http://127.0.0.1/admin", 200, CT_HTML),
        ]
    )
    resultado, _, contexto, _ = _coletar(perfil, pagina=pagina)

    assert resultado == FalhaBusca("redirecionamento_nao_permitido", "http://127.0.0.1/admin")
    assert pagina.content_chamadas == 0
    assert contexto.fechamentos == 1


def test_navegacao_posterior_para_destino_proibido_e_abortada_pela_rota(perfil: Path) -> None:
    pagina = FakePagina(navegacao_js="http://10.0.0.5/painel")
    resultado, _, _, _ = _coletar(perfil, pagina=pagina)

    assert resultado == FalhaBusca("redirecionamento_nao_permitido", "http://10.0.0.5/painel")
    abortadas = [url for url, rota in pagina.rotas if rota.abortada]
    assert abortadas == ["http://10.0.0.5/painel"]
    assert pagina.content_chamadas == 0


def test_subrecurso_privado_e_abortado_sem_derrubar_a_pagina(perfil: Path) -> None:
    pagina = FakePagina(subrecursos=("http://192.168.1.1/pixel.gif", "https://cdn.exemplo.com/a.js"))
    resultado, _, _, _ = _coletar(perfil, pagina=pagina)

    assert isinstance(resultado, PaginaBaixada)
    estados = {url: (rota.abortada, rota.continuada) for url, rota in pagina.rotas}
    assert estados["http://192.168.1.1/pixel.gif"] == (True, False)
    assert estados["https://cdn.exemplo.com/a.js"] == (False, True)
    assert estados[URL] == (False, True)


def test_url_final_fora_da_allowlist(perfil: Path) -> None:
    pagina = FakePagina(
        cadeia=[(URL, 302, {"location": "https://outro.com/"}), ("https://outro.com/", 200, CT_HTML)]
    )
    resultado, _, _, _ = _coletar(perfil, pagina=pagina)

    assert resultado == FalhaBusca("dominio_nao_permitido_stealth", "https://outro.com/")


def test_networkidle_estourado_nao_e_falha(perfil: Path) -> None:
    pagina = FakePagina(erro_networkidle=TimeoutPlaywright("networkidle 15000ms"))
    resultado, _, contexto, _ = _coletar(perfil, pagina=pagina)

    assert isinstance(resultado, PaginaBaixada)
    assert resultado.corpo == HTML.encode("utf-8")
    assert contexto.fechamentos == 1


def test_prazo_total_cobre_goto_e_espera(perfil: Path) -> None:
    relogio = Relogio()
    pagina = FakePagina(relogio=relogio, custo_goto_s=10.0)
    resultado, _, _, _ = _coletar(perfil, pagina=pagina, relogio_monotonico=relogio)

    assert isinstance(resultado, PaginaBaixada)
    assert pagina.espera_chamadas == [("networkidle", pytest.approx(5000.0))]

    relogio2 = Relogio()
    pagina2 = FakePagina(relogio=relogio2, custo_goto_s=16.0)
    resultado2, _, _, _ = _coletar(perfil, pagina=pagina2, relogio_monotonico=relogio2)
    assert isinstance(resultado2, PaginaBaixada)
    assert pagina2.espera_chamadas == []


def test_conteudo_acima_do_limite(perfil: Path) -> None:
    pagina = FakePagina(html="a" * (LIMITE_CORPO_BYTES + 1))
    resultado, _, contexto, _ = _coletar(perfil, pagina=pagina)

    assert isinstance(resultado, FalhaBusca)
    assert resultado.motivo == "tamanho_excedido"
    assert contexto.fechamentos == 1


def test_conteudo_no_limite_e_aceito(perfil: Path) -> None:
    pagina = FakePagina(html="a" * LIMITE_CORPO_BYTES)
    resultado, _, _, _ = _coletar(perfil, pagina=pagina)
    assert isinstance(resultado, PaginaBaixada)


@pytest.mark.parametrize("html", ["", "   \n"])
def test_conteudo_vazio(perfil: Path, html: str) -> None:
    resultado, _, _, _ = _coletar(perfil, pagina=FakePagina(html=html))

    assert isinstance(resultado, FalhaBusca)
    assert resultado.motivo == "corpo_vazio"


def test_sem_resposta_principal_vira_rede(perfil: Path) -> None:
    class PaginaSemResposta(FakePagina):
        def goto(self, url: str, *, wait_until: str, timeout: float) -> None:
            self.url = url
            return None

    resultado, _, _, _ = _coletar(perfil, pagina=PaginaSemResposta())
    assert resultado == FalhaBusca("rede", URL)


@pytest.mark.parametrize(
    ("erro", "motivo"),
    [
        (TimeoutPlaywright("Timeout 15000ms exceeded"), "timeout"),
        (ErroPlaywright("net::ERR_CONNECTION_RESET"), "rede"),
    ],
)
def test_erros_do_goto(perfil: Path, erro: BaseException, motivo: str) -> None:
    resultado, _, contexto, _ = _coletar(perfil, pagina=FakePagina(erro_goto=erro))

    assert resultado == FalhaBusca(motivo, URL)
    assert contexto.fechamentos == 1


def test_excecao_inesperada_atravessa_mas_fecha_o_contexto(perfil: Path) -> None:
    pagina = FakePagina(erro_content=RuntimeError("bug"))
    contexto = FakeContexto(pagina)
    with pytest.raises(RuntimeError, match="bug"):
        _coletar(perfil, pagina=pagina, contexto=contexto)
    assert contexto.fechamentos == 1


def test_erro_no_close_nao_propaga(perfil: Path) -> None:
    pagina = FakePagina()
    contexto = FakeContexto(pagina, erro_close=ErroPlaywright("browser já fechado"))
    resultado, _, _, _ = _coletar(perfil, pagina=pagina, contexto=contexto)

    assert isinstance(resultado, PaginaBaixada)
    assert contexto.fechamentos == 1


# ---------------------------------------------------------------------------
# Perfil: fuso, WebRTC e preservação
# ---------------------------------------------------------------------------


class ConsultaFuso:
    def __init__(self, fuso: str | None = "America/Manaus") -> None:
        self.fuso = fuso
        self.chamadas = 0

    def __call__(self) -> str | None:
        self.chamadas += 1
        return self.fuso


def test_fuso_consultado_uma_vez_entre_varias_coletas(perfil: Path) -> None:
    consulta = ConsultaFuso()
    abridor = Abridor(FakeContexto(FakePagina()))

    for _ in range(3):
        abridor.contexto = FakeContexto(FakePagina())
        _coletar(
            perfil,
            abridor=abridor,
            obter_fuso=lambda p: bs.obter_fuso_ip(p, consultar=consulta),
        )

    assert consulta.chamadas == 1
    assert [c["timezone_id"] for c in abridor.chamadas] == ["America/Manaus"] * 3


def test_fuso_reaproveitado_do_arquivo_do_perfil(perfil: Path) -> None:
    from datetime import datetime, timedelta, timezone

    agora = datetime(2025, 1, 10, 12, tzinfo=timezone.utc)
    consulta = ConsultaFuso()
    assert bs.obter_fuso_ip(perfil, consultar=consulta, agora=lambda: agora) == "America/Manaus"
    dados = json.loads((perfil / bs.ARQUIVO_CACHE_FUSO).read_text(encoding="utf-8"))
    assert dados["timezone"] == "America/Manaus"

    # Processo "novo": sem cache em memória, lê do arquivo sem consultar.
    bs.limpar_cache_fuso()

    def proibida() -> str:
        raise AssertionError("não deveria consultar ip-api")

    depois = agora + timedelta(hours=23)
    assert bs.obter_fuso_ip(perfil, consultar=proibida, agora=lambda: depois) == "America/Manaus"

    # Arquivo com mais de 24 h: consulta de novo.
    bs.limpar_cache_fuso()
    expirado = agora + timedelta(hours=25)
    outra = ConsultaFuso("America/Recife")
    assert bs.obter_fuso_ip(perfil, consultar=outra, agora=lambda: expirado) == "America/Recife"
    assert outra.chamadas == 1


@pytest.mark.parametrize("retorno", [None, "Nao/Existe", ""])
def test_fuso_falha_usa_padrao_sem_cachear(perfil: Path, retorno: str | None) -> None:
    consulta = ConsultaFuso(retorno)
    assert bs.obter_fuso_ip(perfil, consultar=consulta) == bs.FUSO_PADRAO
    assert bs.obter_fuso_ip(perfil, consultar=consulta) == bs.FUSO_PADRAO
    assert consulta.chamadas == 2
    assert not (perfil / bs.ARQUIVO_CACHE_FUSO).exists()


def _preferences(perfil: Path) -> Path:
    return perfil / "Default" / "Preferences"


def test_perfil_preservado_e_webrtc_preserva_outras_chaves(perfil: Path) -> None:
    (perfil / "Default").mkdir(parents=True)
    sentinela = perfil / "Default" / "Cookies"
    sentinela.write_bytes(b"sentinela")
    _preferences(perfil).write_text(
        json.dumps({"profile": {"name": "Pessoa 1"}, "webrtc": {"multiple_routes_enabled": False}}),
        encoding="utf-8",
    )

    resultado, _, _, _ = _coletar(perfil)
    _coletar(perfil, pagina=FakePagina(cadeia=[(URL, 403, CT_HTML)]))

    assert isinstance(resultado, PaginaBaixada)
    assert sentinela.read_bytes() == b"sentinela"
    prefs = json.loads(_preferences(perfil).read_text(encoding="utf-8"))
    assert prefs == {
        "profile": {"name": "Pessoa 1"},
        "webrtc": {"multiple_routes_enabled": False, "ip_handling_policy": "disable_non_proxied_udp"},
    }


def test_webrtc_cria_preferences_quando_ausente(perfil: Path) -> None:
    assert bs.aplicar_politica_webrtc(perfil) is True
    prefs = json.loads(_preferences(perfil).read_text(encoding="utf-8"))
    assert prefs == {"webrtc": {"ip_handling_policy": "disable_non_proxied_udp"}}


@pytest.mark.parametrize("conteudo", [b"{nao e json", b"[1, 2]", b'{"webrtc": 3}', b"\xff\xfe"])
def test_preferences_invalido_nao_e_reescrito(perfil: Path, conteudo: bytes) -> None:
    (perfil / "Default").mkdir(parents=True)
    _preferences(perfil).write_bytes(conteudo)

    resultado, _, _, _ = _coletar(perfil)

    assert isinstance(resultado, PaginaBaixada)
    assert _preferences(perfil).read_bytes() == conteudo


# ---------------------------------------------------------------------------
# Diagnóstico
# ---------------------------------------------------------------------------


def test_diagnostico_expoe_so_nomes_e_dominios_de_cookies(perfil: Path) -> None:
    pagina = FakePagina()
    abridor = Abridor(FakeContexto(pagina))
    saida = bs.diagnosticar_stealth(
        URL, profile_dir=perfil, abrir_contexto=abridor, obter_fuso=FusoFake(), timeout=15
    )

    assert saida["desfecho"] == "pagina"
    assert saida["status_http"] == 200
    assert saida["titulo"] == "Filtro"
    assert saida["cookies"] == [
        {"nome": "_abck", "dominio": ".mercadocar.com.br"},
        {"nome": "sessao", "dominio": "www.mercadocar.com.br"},
    ]
    assert "SEGREDO" not in json.dumps(saida)
    assert saida["status_navegacoes"] == [{"url": URL, "status": 200}]
    assert abridor.contexto.fechamentos == 1


def test_diagnostico_respeita_allowlist(perfil: Path) -> None:
    abridor = Abridor(FakeContexto(FakePagina()))
    saida = bs.diagnosticar_stealth(
        "https://www.ebay.co.uk/", profile_dir=perfil, abrir_contexto=abridor, obter_fuso=FusoFake()
    )

    assert (saida["desfecho"], saida["motivo"]) == ("falha", "dominio_nao_permitido_stealth")
    assert saida["cookies"] == [] and saida["titulo"] is None
    assert abridor.chamadas == []


def test_diagnostico_registra_status_mesmo_em_falha(perfil: Path) -> None:
    pagina = FakePagina(cadeia=[(URL, 403, CT_HTML)])
    saida = bs.diagnosticar_stealth(
        URL,
        profile_dir=perfil,
        abrir_contexto=Abridor(FakeContexto(pagina)),
        obter_fuso=FusoFake(),
    )
    assert (saida["motivo"], saida["status_http"]) == ("status_http", 403)
    assert saida["status_navegacoes"] == [{"url": URL, "status": 403}]


# ---------------------------------------------------------------------------
# Import preguiçoso
# ---------------------------------------------------------------------------


def test_importar_modulo_nao_carrega_patchright() -> None:
    codigo = (
        "import json, sys\n"
        "import tools.buscador_stealth\n"
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m.split('.')[0] in ('patchright', 'playwright', 'greenlet', 'pyee'))))\n"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith("ESTAGIARIO_RUN_") or k == "PYTHONPATH")
    }
    processo = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=RAIZ,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert processo.returncode == 0, processo.stderr
    assert json.loads(processo.stdout.strip().splitlines()[-1]) == []
