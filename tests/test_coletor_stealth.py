"""Testes de exemplo do ``ColetorPaginas`` com o fallback stealth
(spec stealth-fallback-integration, tarefa 2.13; Req 1.8, 3.1, 3.2, 12.1).

Cobrem:

- os padrões do construtor (``coletar_via_playwright_stealth``,
  ``TRAVA_NAVEGADOR`` e ``config.coleta_dominios_stealth()``) e a construção
  sem I/O;
- a chamada ao Fallback_Stealth só com ``url``, ``allowlist`` e ``timeout``;
- o Fallback_Stealth padrão de ponta a ponta, com ``abrir_contexto_patchright``
  e ``obter_fuso_ip`` substituídos por monkeypatch em ``tools.buscador_stealth``
  (headed, ``channel="chrome"``, perfil de ``config.stealth_profile_dir()``), e
  URL fora da allowlist sem abrir contexto;
- allowlist e timeout lidos de ``config``;
- exemplos de ``motivo_elegivel_fallback`` e ``resultado_tentativa`` para cada
  motivo de ``FalhaBusca``.

Nenhum teste importa ``patchright`` nem abre navegador: o contexto e a página
são fakes próprios deste módulo (``tests/test_buscador_stealth.py`` não é
reaproveitado porque importa as exceções do Patchright).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import config
from coleta_paginas.coletor import ColetorPaginas, ConfigColeta
from coleta_paginas.reputacao import (
    STATUS_BLOQUEIO,
    motivo_elegivel_fallback,
    resultado_tentativa,
)
from db.armazem_paginas import ArmazemPaginas
from tests.fakes_stealth import (
    ALLOWLIST_PADRAO,
    CABECALHOS_HTML,
    MOTIVOS_FALHA_BUSCA,
    ArmazemEspiao,
    FallbackFake,
    TransporteRoteiro,
    TravaFake,
    fixar_ambiente_stealth,
    ok,
    peca_cenario,
)
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from tests.test_coletor_paginas import resultado
from tools import buscador_stealth as bs
from tools.buscador_paginas import FalhaBusca, PaginaBaixada

RAIZ = Path(__file__).resolve().parent.parent
INSTANTE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
TIMEOUT_S = 5.0
URL_MERCADOCAR = "https://www.mercadocar.com.br/produto/filtro-83061"
URL_FORA = "https://www.ebay.co.uk/itm/filtro-83061"
HTML_STEALTH = "<html><head><title>Filtro</title></head><body>Filtro 83061</body></html>"
FUSO = "America/Sao_Paulo"


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------


def relogio_fixo() -> datetime:
    return INSTANTE


def config_coleta(timeout_s: float = TIMEOUT_S) -> ConfigColeta:
    return ConfigColeta(timeout_s=timeout_s, teto_aceitos_ambiente=None, janela_reuso_dias=30)


def novo_armazem(tmp_path: Path, cls: type[ArmazemPaginas] = ArmazemPaginas) -> ArmazemPaginas:
    return cls(
        tmp_path / "paginas_teste.db",
        caminho_rule_store=tmp_path / "rule_store.db",
        relogio=relogio_fixo,
    )


def bloqueado(status: int = 403) -> tuple[int, dict[str, str], bytes]:
    return (status, {}, b"")


def pagina_stealth(url: str, corpo: bytes = b"<html>stealth</html>") -> PaginaBaixada:
    return PaginaBaixada(
        status_http=200,
        content_type="text/html; charset=utf-8",
        charset="utf-8",
        url_final=url,
        corpo=corpo,
    )


def coletar_uma(coletor: ColetorPaginas, url: str, **kwargs: Any):
    return coletor.coletar(peca_cenario(), [resultado(url, 1)], **kwargs)


class _Proibido:
    """Chamável que derruba o teste se for chamado (prova de ausência de I/O)."""

    def __init__(self, nome: str) -> None:
        self.nome = nome

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"{self.nome} não deveria ser chamado")


# Fakes mínimos do contexto/página do Playwright (sem importar patchright) ----


class _Requisicao:
    def __init__(self, url: str, frame: object) -> None:
        self.url = url
        self.frame = frame

    def is_navigation_request(self) -> bool:
        return True


class _Resposta:
    def __init__(self, url: str, status: int, requisicao: _Requisicao) -> None:
        self.url = url
        self.status = status
        self.headers = dict(CABECALHOS_HTML)
        self.request = requisicao


class _Rota:
    def __init__(self) -> None:
        self.abortada = False

    def abort(self, codigo: str = "failed") -> None:
        self.abortada = True

    def continue_(self) -> None:
        pass


class _Pagina:
    def __init__(self, status: int = 200, html: str = HTML_STEALTH) -> None:
        self.main_frame = object()
        self.url = "about:blank"
        self._status = status
        self._html = html
        self._handlers: dict[str, list[Any]] = {}
        self._rota: Any = None
        self.goto_chamadas: list[dict[str, Any]] = []

    def on(self, evento: str, handler: Any) -> None:
        self._handlers.setdefault(evento, []).append(handler)

    def route(self, padrao: str, handler: Any) -> None:
        self._rota = handler

    def goto(self, url: str, *, wait_until: str, timeout: float) -> _Resposta:
        self.goto_chamadas.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        requisicao = _Requisicao(url, self.main_frame)
        rota = _Rota()
        if self._rota is not None:
            self._rota(rota, requisicao)
        assert not rota.abortada
        for handler in self._handlers.get("request", []):
            handler(requisicao)
        resposta = _Resposta(url, self._status, requisicao)
        for handler in self._handlers.get("response", []):
            handler(resposta)
        self.url = url
        return resposta

    def wait_for_load_state(self, estado: str, *, timeout: float) -> None:
        pass

    def content(self) -> str:
        return self._html

    def title(self) -> str:
        return "Filtro"


class _Contexto:
    def __init__(self, pagina: _Pagina) -> None:
        self.pages = [pagina]
        self.fechamentos = 0

    def new_page(self) -> _Pagina:  # pragma: no cover - pages nunca vazio aqui
        raise AssertionError("new_page não esperado")

    def cookies(self) -> list[Mapping[str, Any]]:
        return []

    def close(self) -> None:
        self.fechamentos += 1


class _Abridor:
    def __init__(self, contexto: _Contexto) -> None:
        self.contexto = contexto
        self.chamadas: list[dict[str, Any]] = []

    def __call__(self, **opcoes: Any) -> _Contexto:
        self.chamadas.append(opcoes)
        return self.contexto


class _Fuso:
    def __init__(self) -> None:
        self.chamadas: list[Path] = []

    def __call__(self, perfil: Path) -> str:
        self.chamadas.append(perfil)
        return FUSO


@pytest.fixture
def fallback_padrao_fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Prepara o Fallback_Stealth padrão sem navegador: perfil em ``tmp_path``,
    allowlist padrão, display presente e ``abrir_contexto_patchright`` /
    ``obter_fuso_ip`` substituídos em ``tools.buscador_stealth``."""
    perfil = fixar_ambiente_stealth(monkeypatch, tmp_path)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    pagina = _Pagina()
    abridor = _Abridor(_Contexto(pagina))
    fuso = _Fuso()
    monkeypatch.setattr(bs, "abrir_contexto_patchright", abridor)
    monkeypatch.setattr(bs, "obter_fuso_ip", fuso)
    return perfil, pagina, abridor, fuso


# ---------------------------------------------------------------------------
# Construtor: padrões e ausência de I/O (Req 12.1)
# ---------------------------------------------------------------------------


def test_padroes_do_construtor(tmp_path: Path) -> None:
    coletor = ColetorPaginas(novo_armazem(tmp_path), config=config_coleta())
    assert coletor._fallback is bs.coletar_via_playwright_stealth
    assert coletor._trava is bs.TRAVA_NAVEGADOR
    # None = config.coleta_dominios_stealth() lida a cada coleta com o fallback ligado.
    assert coletor._allowlist_stealth is None


def test_injecao_substitui_os_padroes(tmp_path: Path) -> None:
    fallback, trava = FallbackFake(), TravaFake()
    allowlist = frozenset({"outro.com.br"})
    coletor = ColetorPaginas(
        novo_armazem(tmp_path),
        config=config_coleta(),
        fallback_stealth=fallback,
        allowlist_stealth=allowlist,
        trava_navegador=trava,
    )
    assert coletor._fallback is fallback
    assert coletor._trava is trava
    assert coletor._allowlist_stealth is allowlist


def test_construcao_sem_io(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    perfil = fixar_ambiente_stealth(monkeypatch, tmp_path)
    for nome in ("coleta_dominios_stealth", "stealth_profile_dir", "coleta_stealth_habilitada"):
        monkeypatch.setattr(config, nome, _Proibido(f"config.{nome}"))
    monkeypatch.setattr(bs, "abrir_contexto_patchright", _Proibido("abrir_contexto_patchright"))
    monkeypatch.setattr(bs, "obter_fuso_ip", _Proibido("obter_fuso_ip"))
    armazem = novo_armazem(tmp_path, ArmazemEspiao)
    fallback, trava = FallbackFake(), TravaFake()

    ColetorPaginas(armazem, config=config_coleta())
    ColetorPaginas(
        armazem, config=config_coleta(), fallback_stealth=fallback, trava_navegador=trava
    )

    assert fallback.chamadas == []
    assert trava.chamadas == 0
    assert armazem.chamadas_reputacao == 0
    assert armazem.chamadas["gravar_coleta"] == 0
    assert not perfil.exists()
    assert not bs.TRAVA_NAVEGADOR.locked()


def test_importar_e_construir_o_coletor_nao_carrega_patchright(tmp_path: Path) -> None:
    # Subprocesso: outros módulos da suíte (test_buscador_stealth) importam patchright.
    codigo = (
        "import sys\n"
        "from pathlib import Path\n"
        "from coleta_paginas.coletor import ColetorPaginas, ConfigColeta\n"
        "from db.armazem_paginas import ArmazemPaginas\n"
        f"base = Path({str(tmp_path)!r})\n"
        "armazem = ArmazemPaginas(base / 'sub.db', caminho_rule_store=base / 'rs.db')\n"
        "ColetorPaginas(armazem, config=ConfigColeta(5.0, None, 30))\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in ('patchright', 'playwright')))\n"
    )
    saida = subprocess.run(
        [sys.executable, "-c", codigo], cwd=RAIZ, capture_output=True, text=True, timeout=60
    )
    assert saida.returncode == 0, saida.stderr
    assert saida.stdout.strip() == "[]"


def test_trava_padrao_e_a_do_processo(tmp_path: Path) -> None:
    detida_durante: list[bool] = []
    fallback = FallbackFake(
        {URL_MERCADOCAR: pagina_stealth(URL_MERCADOCAR)},
        durante=lambda _url: detida_durante.append(bs.TRAVA_NAVEGADOR.locked()),
    )
    coletor = ColetorPaginas(
        novo_armazem(tmp_path),
        transporte=TransporteRoteiro({URL_MERCADOCAR: bloqueado(403)}),
        config=config_coleta(),
        relogio=relogio_fixo,
        fallback_stealth=fallback,
        allowlist_stealth=ALLOWLIST_PADRAO,
    )
    relatorio = coletar_uma(coletor, URL_MERCADOCAR, stealth=True)
    assert detida_durante == [True]
    assert not bs.TRAVA_NAVEGADOR.locked()
    assert relatorio.entradas[0].desfecho == "armazenado"


# ---------------------------------------------------------------------------
# Chamada ao fallback: só url, allowlist e timeout (Req 3.1, 3.2)
# ---------------------------------------------------------------------------


def test_fallback_chamado_so_com_url_allowlist_e_timeout(tmp_path: Path) -> None:
    allowlist = frozenset({"mercadocar.com.br"})
    fallback = FallbackFake({URL_MERCADOCAR: pagina_stealth(URL_MERCADOCAR)})
    trava = TravaFake()
    coletor = ColetorPaginas(
        novo_armazem(tmp_path),
        transporte=TransporteRoteiro({URL_MERCADOCAR: bloqueado(429)}),
        config=config_coleta(),
        relogio=relogio_fixo,
        fallback_stealth=fallback,
        allowlist_stealth=allowlist,
        trava_navegador=trava,
    )
    coletar_uma(coletor, URL_MERCADOCAR, stealth=True)

    assert len(fallback.chamadas) == 1
    chamada = fallback.chamadas[0]
    assert chamada.url == URL_MERCADOCAR
    # Nenhum profile_dir, abrir_contexto, obter_fuso ou opção de modo headless.
    assert set(chamada.kwargs) == {"allowlist", "timeout"}
    assert chamada.allowlist == allowlist
    assert chamada.timeout == TIMEOUT_S
    assert trava.acquires == [(True, TIMEOUT_S)]
    assert trava.releases == 1


def test_contrato_do_fallback_padrao_nao_expoe_modo_headless() -> None:
    import inspect

    parametros = set(inspect.signature(bs.coletar_via_playwright_stealth).parameters)
    assert {"url", "allowlist", "timeout"} <= parametros
    assert not any("headless" in nome or "headed" in nome for nome in parametros)


# ---------------------------------------------------------------------------
# Fallback padrão com contexto fake (Req 1.8, 3.1, 3.2, 12.1)
# ---------------------------------------------------------------------------


def test_fallback_padrao_abre_chrome_headed_com_perfil_do_config(
    tmp_path: Path, fallback_padrao_fake
) -> None:
    perfil, pagina, abridor, fuso = fallback_padrao_fake
    transporte = TransporteRoteiro({URL_MERCADOCAR: bloqueado(403)})
    armazem = novo_armazem(tmp_path)
    coletor = ColetorPaginas(
        armazem, transporte=transporte, config=config_coleta(), relogio=relogio_fixo
    )

    relatorio = coletar_uma(coletor, URL_MERCADOCAR, stealth=True)

    entrada = relatorio.entradas[0]
    assert (entrada.desfecho, entrada.camada, entrada.motivo_urllib) == (
        "armazenado",
        "playwright_stealth",
        "status_http",
    )
    assert entrada.status_http == 200
    assert transporte.chamadas == [URL_MERCADOCAR]
    assert abridor.chamadas == [
        {
            "user_data_dir": str(perfil),
            "channel": "chrome",
            "headless": False,
            "no_viewport": True,
            "timezone_id": FUSO,
        }
    ]
    assert fuso.chamadas == [perfil]
    assert perfil.is_dir()
    assert abridor.contexto.fechamentos == 1
    # O timeout repassado pelo coletor é o prazo de goto (ms).
    assert pagina.goto_chamadas[0]["url"] == URL_MERCADOCAR
    assert pagina.goto_chamadas[0]["timeout"] == TIMEOUT_S * 1000
    assert armazem.ler_conteudo(entrada.hash_conteudo) == HTML_STEALTH.encode("utf-8")
    assert not bs.TRAVA_NAVEGADOR.locked()


@pytest.mark.parametrize(
    "url",
    [URL_FORA, "https://mercadocar.com.br.evil.com/p", "https://evilmercadocar.com.br/p"],
)
def test_url_fora_da_allowlist_nao_abre_contexto(
    tmp_path: Path, fallback_padrao_fake, url: str
) -> None:
    _perfil, _pagina, abridor, fuso = fallback_padrao_fake
    coletor = ColetorPaginas(
        novo_armazem(tmp_path),
        transporte=TransporteRoteiro({url: bloqueado(403)}),
        config=config_coleta(),
        relogio=relogio_fixo,
    )

    relatorio = coletar_uma(coletor, url, stealth=True)

    entrada = relatorio.entradas[0]
    assert (entrada.desfecho, entrada.motivo, entrada.status_http, entrada.camada) == (
        "falha",
        "status_http",
        403,
        "urllib",
    )
    assert entrada.motivo_urllib is None
    assert abridor.chamadas == []
    assert fuso.chamadas == []


@pytest.mark.parametrize(
    ("url", "sem_display", "motivo"),
    [
        (URL_FORA, False, "dominio_nao_permitido_stealth"),
        ("http://127.0.0.1/produto", False, "destino_nao_permitido"),
        ("ftp://www.mercadocar.com.br/produto", False, "url_invalida"),
        (URL_MERCADOCAR, True, "ambiente_sem_display"),
    ],
)
def test_barreiras_do_fallback_padrao_na_forma_chamada_pelo_coletor(
    monkeypatch: pytest.MonkeyPatch, fallback_padrao_fake, url: str, sem_display: bool, motivo: str
) -> None:
    perfil, _pagina, abridor, fuso = fallback_padrao_fake
    if sem_display:
        monkeypatch.delenv("DISPLAY", raising=False)
    resultado_fallback = bs.coletar_via_playwright_stealth(
        url, allowlist=ALLOWLIST_PADRAO, timeout=TIMEOUT_S
    )
    assert isinstance(resultado_fallback, FalhaBusca)
    assert resultado_fallback.motivo == motivo
    assert abridor.chamadas == []
    assert fuso.chamadas == []
    assert not perfil.exists()


# ---------------------------------------------------------------------------
# Allowlist e timeout lidos de config (Req 1.8)
# ---------------------------------------------------------------------------


def test_allowlist_e_timeout_vem_de_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixar_ambiente_stealth(monkeypatch, tmp_path, dominios="mercadocar.com.br, Outro.com.br.")
    monkeypatch.setenv("ESTAGIARIO_COLETA_TIMEOUT", "7")
    url_outro = "https://loja.outro.com.br/p"
    fallback = FallbackFake(padrao=pagina_stealth(url_outro))
    trava = TravaFake()
    coletor = ColetorPaginas(
        novo_armazem(tmp_path),
        transporte=TransporteRoteiro({url_outro: bloqueado(503)}),
        relogio=relogio_fixo,  # config=None → ConfigColeta.do_ambiente()
        fallback_stealth=fallback,
        trava_navegador=trava,
    )

    relatorio = coletar_uma(coletor, url_outro, stealth=True)

    assert relatorio.entradas[0].camada == "playwright_stealth"
    assert fallback.chamadas[0].allowlist == config.coleta_dominios_stealth()
    assert fallback.chamadas[0].allowlist == frozenset({"mercadocar.com.br", "outro.com.br"})
    assert fallback.chamadas[0].timeout == config.coleta_timeout() == 7.0
    # Prazo_Trava_Navegador = timeout efetivo da coleta, sem variável nova.
    assert trava.acquires == [(True, 7.0)]


def test_allowlist_padrao_e_relida_a_cada_coleta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixar_ambiente_stealth(monkeypatch, tmp_path)
    url_outro = "https://outro.com.br/p"
    fallback = FallbackFake(padrao=pagina_stealth(URL_MERCADOCAR))
    transporte = TransporteRoteiro({URL_MERCADOCAR: bloqueado(403), url_outro: bloqueado(403)})
    coletor = ColetorPaginas(
        novo_armazem(tmp_path),
        transporte=transporte,
        config=config_coleta(),
        relogio=relogio_fixo,
        fallback_stealth=fallback,
        trava_navegador=TravaFake(),
    )

    primeira = coletar_uma(coletor, url_outro, stealth=True)
    assert primeira.entradas[0].camada == "urllib"
    assert fallback.chamadas == []

    monkeypatch.setenv("ESTAGIARIO_COLETA_DOMINIOS_STEALTH", "outro.com.br")
    segunda = coletar_uma(coletor, url_outro, stealth=True)
    assert segunda.entradas[0].camada == "playwright_stealth"
    assert [c.allowlist for c in fallback.chamadas] == [frozenset({"outro.com.br"})]

    monkeypatch.setenv("ESTAGIARIO_COLETA_DOMINIOS_STEALTH", "")  # vazio desliga
    terceira = coletar_uma(coletor, URL_MERCADOCAR, stealth=True)
    assert terceira.entradas[0].camada == "urllib"
    assert len(fallback.chamadas) == 1


def test_allowlist_injetada_prevalece_sobre_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixar_ambiente_stealth(monkeypatch, tmp_path)
    monkeypatch.setattr(
        config, "coleta_dominios_stealth", _Proibido("config.coleta_dominios_stealth")
    )
    injetada = frozenset({"mercadocar.com.br"})
    fallback = FallbackFake(padrao=pagina_stealth(URL_MERCADOCAR))
    coletor = ColetorPaginas(
        novo_armazem(tmp_path),
        transporte=TransporteRoteiro({URL_MERCADOCAR: bloqueado(403)}),
        config=config_coleta(),
        relogio=relogio_fixo,
        fallback_stealth=fallback,
        allowlist_stealth=injetada,
        trava_navegador=TravaFake(),
    )
    coletar_uma(coletor, URL_MERCADOCAR, stealth=True)
    assert fallback.chamadas[0].allowlist is injetada


def test_sem_stealth_a_allowlist_nao_e_lida(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        config, "coleta_dominios_stealth", _Proibido("config.coleta_dominios_stealth")
    )
    fallback, trava = FallbackFake(), TravaFake()
    coletor = ColetorPaginas(
        novo_armazem(tmp_path),
        transporte=TransporteRoteiro({URL_MERCADOCAR: ok(b"<html>urllib</html>")}),
        config=config_coleta(),
        relogio=relogio_fixo,
        fallback_stealth=fallback,
        trava_navegador=trava,
    )
    relatorio = coletar_uma(coletor, URL_MERCADOCAR)
    assert relatorio.entradas[0].desfecho == "armazenado"
    assert relatorio.entradas[0].camada is None
    assert fallback.chamadas == []
    assert trava.chamadas == 0


# ---------------------------------------------------------------------------
# Regras puras: motivo_elegivel_fallback e resultado_tentativa
# ---------------------------------------------------------------------------


def test_status_bloqueio() -> None:
    assert STATUS_BLOQUEIO == frozenset({403, 429, 503})


# (motivo, status_http, elegível ao fallback, resultado da Tentativa_Fetch)
EXEMPLOS_FALHA = [
    ("status_http", 403, True, False),
    ("status_http", 429, True, False),
    ("status_http", 503, True, False),
    ("status_http", 404, False, None),
    ("status_http", 500, False, None),
    ("status_http", 502, False, None),
    ("status_http", 401, False, None),
    ("status_http", None, False, None),
    ("rede", None, False, False),
    ("timeout", None, False, False),
    ("timeout", 200, False, False),
    ("nao_html", 200, False, None),
    ("tamanho_excedido", 200, False, None),
    ("corpo_vazio", 200, False, None),
    ("redirecionamento_nao_permitido", None, False, None),
    ("redirecionamentos_excedidos", None, False, None),
    ("codificacao_nao_suportada", 200, False, None),
    ("codificacao_invalida", 200, False, None),
    ("url_invalida", None, False, None),
    ("destino_nao_permitido", None, False, None),
    ("dominio_nao_permitido_stealth", None, False, None),
    ("ambiente_sem_display", None, False, None),
    ("navegador_indisponivel", None, False, None),
    # Status de bloqueio com outro motivo não conta como bloqueio.
    ("nao_html", 403, False, None),
    ("redirecionamento_nao_permitido", 503, False, None),
]


def test_exemplos_cobrem_todos_os_motivos() -> None:
    assert {motivo for motivo, *_ in EXEMPLOS_FALHA} == set(MOTIVOS_FALHA_BUSCA)


@pytest.mark.parametrize(("motivo", "status", "elegivel", "tentativa"), EXEMPLOS_FALHA)
def test_motivo_elegivel_fallback_e_resultado_tentativa(
    motivo: str, status: int | None, elegivel: bool, tentativa: bool | None
) -> None:
    falha = FalhaBusca(motivo, URL_MERCADOCAR, status_http=status)  # type: ignore[arg-type]
    assert motivo_elegivel_fallback(falha) is elegivel
    assert resultado_tentativa(falha) is tentativa


@pytest.mark.parametrize("status", [200, 203, 299])
def test_resultado_tentativa_de_pagina_baixada_e_sucesso(status: int) -> None:
    pagina = PaginaBaixada(
        status_http=status,
        content_type="text/html",
        charset="utf-8",
        url_final=URL_MERCADOCAR,
        corpo=b"<html></html>",
    )
    assert resultado_tentativa(pagina) is True
