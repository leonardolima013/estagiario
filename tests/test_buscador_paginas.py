"""Testes unitários de ``tools.buscador_paginas`` (html-extract-save).

Seções:

- ``transporte_urllib`` (tarefa 6.2): o transporte padrão sobre ``urllib``, com
  ``OpenerDirector.open`` substituído por monkeypatch — nenhuma conexão real.
- ``BuscadorPaginas.buscar`` (tarefa 6.7): acrescentada depois.

A guarda de rede é ativada em todos os testes do módulo.
"""

from __future__ import annotations

import http.client
import io
import socket
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable

import pytest

from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tools import buscador_paginas as bp
from tools.buscador_paginas import (
    CABECALHOS_FIXOS,
    TAMANHO_BLOCO_LEITURA,
    ErroTransporte,
    RespostaHTTP,
    montar_opener,
    transporte_urllib,
)

URL = "https://exemplo.com/peca/123"
_VARIAVEIS_PROXY = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "REQUEST_METHOD",
)


# ---------------------------------------------------------------------------
# transporte_urllib
# ---------------------------------------------------------------------------


@pytest.fixture
def sem_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for nome in _VARIAVEIS_PROXY:
        monkeypatch.delenv(nome, raising=False)


def _mensagem(*pares: tuple[str, str]) -> http.client.HTTPMessage:
    mensagem = http.client.HTTPMessage()
    for nome, valor in pares:
        mensagem[nome] = valor
    return mensagem


class _RespostaFake:
    """Imita o objeto devolvido por ``OpenerDirector.open`` em caso de 2xx."""

    def __init__(
        self,
        corpo: bytes = b"",
        *,
        status: int | None = 200,
        codigo: int = 200,
        cabecalhos: http.client.HTTPMessage | None = None,
        ler: Callable[[int], bytes] | None = None,
    ) -> None:
        self.status = status
        self._codigo = codigo
        self.headers = cabecalhos if cabecalhos is not None else _mensagem()
        self._fluxo = io.BytesIO(corpo)
        self._ler = ler
        self.tamanhos_lidos: list[int] = []
        self.fechado = False

    def getcode(self) -> int:
        return self._codigo

    def read(self, tamanho: int) -> bytes:
        self.tamanhos_lidos.append(tamanho)
        if self._ler is not None:
            return self._ler(tamanho)
        return self._fluxo.read(tamanho)

    def close(self) -> None:
        self.fechado = True


class _Captura:
    """Registra as chamadas de ``OpenerDirector.open`` feitas pelo transporte."""

    def __init__(self) -> None:
        self.chamadas: list[tuple[urllib.request.OpenerDirector, urllib.request.Request, float]] = []


def _substituir_open(
    monkeypatch: pytest.MonkeyPatch, efeito: Callable[[], object] | BaseException
) -> _Captura:
    """Troca ``OpenerDirector.open``: levanta ``efeito`` se for exceção, senão
    devolve ``efeito()``."""
    captura = _Captura()

    def open_fake(self, requisicao, data=None, timeout=None):  # noqa: ANN001
        captura.chamadas.append((self, requisicao, timeout))
        if isinstance(efeito, BaseException):
            raise efeito
        return efeito()

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_fake)
    return captura


def _http_error(
    codigo: int, corpo: bytes, *cabecalhos: tuple[str, str]
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, codigo, "msg", _mensagem(*cabecalhos), io.BytesIO(corpo))


# --- Montagem do opener (Req 5.2, 5.3) --------------------------------------


def test_opener_so_tem_os_handlers_permitidos(sem_proxy: None) -> None:
    opener = montar_opener()
    tipos = [type(h) for h in opener.handlers]

    proibidos = (
        urllib.request.HTTPCookieProcessor,
        urllib.request.FileHandler,
        urllib.request.FTPHandler,
        urllib.request.DataHandler,
        urllib.request.UnknownHandler,
        urllib.request.HTTPBasicAuthHandler,
        urllib.request.HTTPDigestAuthHandler,
        urllib.request.ProxyBasicAuthHandler,
        urllib.request.ProxyDigestAuthHandler,
    )
    for handler in opener.handlers:
        assert not isinstance(handler, proibidos), type(handler)

    # Nenhum redirecionador automático: só a subclasse que não segue.
    redirecionadores = [
        h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
    ]
    assert [type(h) for h in redirecionadores] == [bp._SemRedirecionamento]

    for esperado in (
        urllib.request.HTTPHandler,
        urllib.request.HTTPSHandler,
        urllib.request.HTTPDefaultErrorHandler,
        urllib.request.HTTPErrorProcessor,
        bp._SemRedirecionamento,
    ):
        assert esperado in tipos

    # Só http/https têm handler de abertura registrado no opener.
    assert set(opener.handle_open) == {"http", "https"}


def test_opener_sem_user_agent_padrao(sem_proxy: None) -> None:
    assert montar_opener().addheaders == []


def test_opener_https_verifica_certificado(sem_proxy: None) -> None:
    (https,) = [h for h in montar_opener().handlers if isinstance(h, urllib.request.HTTPSHandler)]
    contexto = https._context
    assert contexto.verify_mode == ssl.CERT_REQUIRED
    assert contexto.check_hostname is True


def test_opener_registra_proxy_do_ambiente(monkeypatch: pytest.MonkeyPatch, sem_proxy: None) -> None:
    # Sem variáveis de proxy o ProxyHandler não tem métodos e não é registrado;
    # com uma variável definida ele passa a fazer parte do opener.
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    opener = montar_opener()
    assert any(isinstance(h, urllib.request.ProxyHandler) for h in opener.handlers)


@pytest.mark.parametrize("codigo", [301, 302, 303, 307, 308])
def test_opener_nao_segue_redirecionamento(codigo: int, sem_proxy: None) -> None:
    """A cadeia de erros do opener transforma o 3xx em ``HTTPError`` sem
    requisitar o ``Location`` (nenhuma conexão: a guarda de rede derrubaria)."""
    opener = montar_opener()
    requisicao = urllib.request.Request(URL, method="GET")
    cabecalhos = _mensagem(("Location", "https://outro.exemplo.com/"))
    with pytest.raises(urllib.error.HTTPError) as info:
        opener.error("http", requisicao, io.BytesIO(b""), codigo, "msg", cabecalhos)
    assert info.value.code == codigo


def test_sem_redirecionamento_devolve_none() -> None:
    handler = bp._SemRedirecionamento()
    requisicao = urllib.request.Request(URL)
    assert handler.redirect_request(requisicao, None, 302, "msg", _mensagem(), "https://x/") is None
    for codigo in (301, 302, 303, 307, 308):
        metodo = getattr(handler, f"http_error_{codigo}")
        assert metodo(requisicao, io.BytesIO(b""), codigo, "msg", _mensagem()) is None


# --- Requisição enviada (Req 5.3) -------------------------------------------


def test_requisicao_usa_get_cabecalhos_fixos_e_timeout(
    monkeypatch: pytest.MonkeyPatch, sem_proxy: None
) -> None:
    captura = _substituir_open(monkeypatch, lambda: _RespostaFake(b"ok"))

    transporte_urllib(URL, CABECALHOS_FIXOS, 7.5)

    ((opener, requisicao, timeout),) = captura.chamadas
    assert timeout == 7.5
    assert requisicao.full_url == URL
    assert requisicao.get_method() == "GET"
    assert requisicao.data is None
    enviados = {nome.lower(): valor for nome, valor in requisicao.header_items()}
    assert enviados == {nome.lower(): valor for nome, valor in CABECALHOS_FIXOS.items()}
    assert opener.addheaders == []
    assert not any(isinstance(h, urllib.request.HTTPCookieProcessor) for h in opener.handlers)


def test_caracterizacao_cabecalhos_fixos_de_navegador() -> None:
    """Req 5.3: conjunto exato de cabeçalhos de navegação, gzip/identity, sem credenciais."""
    assert list(CABECALHOS_FIXOS.items()) == [
        (
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        ),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"),
        ("Accept-Language", "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"),
        ("Accept-Encoding", "gzip, identity"),
        ("Sec-Fetch-Dest", "document"),
        ("Sec-Fetch-Mode", "navigate"),
        ("Sec-Fetch-Site", "none"),
        ("Sec-Fetch-User", "?1"),
        ("Upgrade-Insecure-Requests", "1"),
    ]
    assert CABECALHOS_FIXOS["User-Agent"] == bp.USER_AGENT
    nomes = {nome.lower() for nome in CABECALHOS_FIXOS}
    assert not nomes & {"cookie", "authorization", "proxy-authorization"}
    with pytest.raises(TypeError):
        CABECALHOS_FIXOS["Cookie"] = "x"  # type: ignore[index]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://exemplo.com/arquivo.html",
        "file:///etc/passwd",
        "data:text/html,<p>oi</p>",
        "gopher://exemplo.com/",
        "/relativa/sem/esquema",
        "",
    ],
)
def test_esquema_nao_http_levanta_rede_sem_abrir(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    captura = _substituir_open(monkeypatch, lambda: _RespostaFake(b"x"))
    with pytest.raises(ErroTransporte) as info:
        transporte_urllib(url, CABECALHOS_FIXOS, 5.0)
    assert info.value.tipo == "rede"
    assert captura.chamadas == []


def test_url_malformada_levanta_rede(monkeypatch: pytest.MonkeyPatch) -> None:
    captura = _substituir_open(monkeypatch, lambda: _RespostaFake(b"x"))
    with pytest.raises(ErroTransporte) as info:
        transporte_urllib("http://[::1/", CABECALHOS_FIXOS, 5.0)
    assert info.value.tipo == "rede"
    assert captura.chamadas == []


# --- HTTPError vira RespostaHTTP (Req 5.2, 5.8) -----------------------------


def test_http_error_302_vira_resposta_sem_seguir(monkeypatch: pytest.MonkeyPatch) -> None:
    erro = _http_error(302, b"movido", ("Location", "/novo"), ("X-Teste", "a"))
    captura = _substituir_open(monkeypatch, erro)

    resposta = transporte_urllib(URL, CABECALHOS_FIXOS, 5.0)

    assert len(captura.chamadas) == 1
    assert isinstance(resposta, RespostaHTTP)
    assert resposta.status == 302
    assert resposta.cabecalhos["location"] == "/novo"
    assert resposta.cabecalhos["x-teste"] == "a"
    assert b"".join(resposta.blocos) == b"movido"
    resposta.fechar()
    assert erro.fp is None or erro.fp.closed


def test_http_error_404_vira_resposta(monkeypatch: pytest.MonkeyPatch) -> None:
    erro = _http_error(404, b"nao achei", ("Content-Type", "text/html"))
    _substituir_open(monkeypatch, erro)

    resposta = transporte_urllib(URL, CABECALHOS_FIXOS, 5.0)

    assert resposta.status == 404
    assert resposta.cabecalhos == {"content-type": "text/html"}
    assert b"".join(resposta.blocos) == b"nao achei"


def test_fechar_pode_ser_chamado_sem_ler_corpo(monkeypatch: pytest.MonkeyPatch) -> None:
    _substituir_open(monkeypatch, _http_error(500, b"erro"))
    resposta = transporte_urllib(URL, CABECALHOS_FIXOS, 5.0)
    resposta.fechar()
    resposta.fechar()


# --- Erros de rede/timeout (Req 5.8) ----------------------------------------


@pytest.mark.parametrize(
    ("excecao", "tipo"),
    [
        (urllib.error.URLError(TimeoutError()), "timeout"),
        (urllib.error.URLError(socket.timeout("lento")), "timeout"),
        (TimeoutError(), "timeout"),
        (socket.timeout(), "timeout"),
        (ssl.SSLError(1, "certificado"), "rede"),
        (ssl.SSLCertVerificationError(1, "verificacao"), "rede"),
        (urllib.error.URLError(ssl.SSLError(1, "tls")), "rede"),
        (urllib.error.URLError(ConnectionRefusedError()), "rede"),
        (urllib.error.URLError(socket.gaierror(-2, "Name or service not known")), "rede"),
        (ConnectionResetError(), "rede"),
        (OSError("genérico"), "rede"),
        (http.client.InvalidURL("porta"), "rede"),
        (http.client.RemoteDisconnected("fechou"), "rede"),
        (UnicodeError("idna"), "rede"),
    ],
)
def test_erro_de_abertura_vira_erro_transporte(
    excecao: BaseException, tipo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _substituir_open(monkeypatch, excecao)
    with pytest.raises(ErroTransporte) as info:
        transporte_urllib(URL, CABECALHOS_FIXOS, 5.0)
    assert info.value.tipo == tipo
    assert info.value.args == (tipo,)


def test_excecao_generica_atravessa(monkeypatch: pytest.MonkeyPatch) -> None:
    _substituir_open(monkeypatch, RuntimeError("guarda de rede"))
    with pytest.raises(RuntimeError, match="guarda de rede"):
        transporte_urllib(URL, CABECALHOS_FIXOS, 5.0)


# --- Resposta 2xx e leitura em blocos (Req 5.2, 5.8) ------------------------


def test_resposta_2xx_cabecalhos_minusculos_primeiro_valor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cabecalhos = _mensagem(
        ("Content-Type", "text/html; charset=utf-8"),
        ("Set-Cookie", "a=1"),
        ("Set-Cookie", "b=2"),
    )
    fake = _RespostaFake(b"<p>oi</p>", cabecalhos=cabecalhos)
    _substituir_open(monkeypatch, lambda: fake)

    resposta = transporte_urllib(URL, CABECALHOS_FIXOS, 5.0)

    assert resposta.status == 200
    assert resposta.cabecalhos == {
        "content-type": "text/html; charset=utf-8",
        "set-cookie": "a=1",
    }
    assert b"".join(resposta.blocos) == b"<p>oi</p>"
    resposta.fechar()
    assert fake.fechado


def test_status_cai_no_getcode_quando_ausente(monkeypatch: pytest.MonkeyPatch) -> None:
    _substituir_open(monkeypatch, lambda: _RespostaFake(b"x", status=None, codigo=203))
    assert transporte_urllib(URL, CABECALHOS_FIXOS, 5.0).status == 203


def test_corpo_cru_lido_em_blocos_de_64_kib(monkeypatch: pytest.MonkeyPatch) -> None:
    corpo = b"\x1f\x8b" + b"a" * (TAMANHO_BLOCO_LEITURA * 2 + 10)
    fake = _RespostaFake(corpo, cabecalhos=_mensagem(("Content-Encoding", "gzip")))
    _substituir_open(monkeypatch, lambda: fake)

    blocos = list(transporte_urllib(URL, CABECALHOS_FIXOS, 5.0).blocos)

    assert b"".join(blocos) == corpo  # sem decodificar Content-Encoding
    assert [len(b) for b in blocos] == [TAMANHO_BLOCO_LEITURA, TAMANHO_BLOCO_LEITURA, 12]
    assert set(fake.tamanhos_lidos) == {TAMANHO_BLOCO_LEITURA}


def _ler_depois_falhar(excecao: BaseException) -> Callable[[int], bytes]:
    estado = {"lidos": 0}

    def ler(_tamanho: int) -> bytes:
        estado["lidos"] += 1
        if estado["lidos"] == 1:
            return b"parcial"
        raise excecao

    return ler


@pytest.mark.parametrize(
    ("excecao", "tipo"),
    [
        (TimeoutError(), "timeout"),
        (socket.timeout(), "timeout"),
        (ConnectionResetError(), "rede"),
        (ssl.SSLError(1, "tls"), "rede"),
        (http.client.IncompleteRead(b"x", 10), "rede"),
        (OSError("leitura"), "rede"),
    ],
)
def test_erro_durante_iteracao_dos_blocos_vira_erro_transporte(
    excecao: BaseException, tipo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _RespostaFake(ler=_ler_depois_falhar(excecao))
    _substituir_open(monkeypatch, lambda: fake)

    resposta = transporte_urllib(URL, CABECALHOS_FIXOS, 5.0)
    blocos = iter(resposta.blocos)
    assert next(blocos) == b"parcial"
    with pytest.raises(ErroTransporte) as info:
        next(blocos)
    assert info.value.tipo == tipo


def test_erro_durante_iteracao_do_http_error_vira_erro_transporte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fp = _RespostaFake(ler=_ler_depois_falhar(TimeoutError()))
    erro = urllib.error.HTTPError(URL, 503, "msg", _mensagem(), fp)  # type: ignore[arg-type]
    _substituir_open(monkeypatch, erro)

    resposta = transporte_urllib(URL, CABECALHOS_FIXOS, 5.0)
    with pytest.raises(ErroTransporte) as info:
        list(resposta.blocos)
    assert info.value.tipo == "timeout"


def test_excecao_generica_na_iteracao_atravessa(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _RespostaFake(ler=_ler_depois_falhar(RuntimeError("guarda")))
    _substituir_open(monkeypatch, lambda: fake)
    resposta = transporte_urllib(URL, CABECALHOS_FIXOS, 5.0)
    with pytest.raises(RuntimeError, match="guarda"):
        list(resposta.blocos)


def test_fechar_engole_erro_de_rede(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FechaComErro(_RespostaFake):
        def close(self) -> None:
            raise ConnectionResetError()

    _substituir_open(monkeypatch, lambda: _FechaComErro(b"x"))
    transporte_urllib(URL, CABECALHOS_FIXOS, 5.0).fechar()



# ---------------------------------------------------------------------------
# BuscadorPaginas.buscar (tarefa 6.7)
# ---------------------------------------------------------------------------
#
# O transporte é um fake roteirizado: cada chamada consome o próximo item do
# roteiro (``_Resposta`` ou ``ErroTransporte``), registra URL/cabeçalhos/timeout
# e devolve uma ``RespostaHTTP`` cujo corpo é um iterador instrumentado (conta
# os blocos consumidos) e cujo ``fechar`` conta as chamadas. Chamada além do
# roteiro levanta ``IndexError``, que atravessa o buscador e derruba o teste.

import gzip  # noqa: E402
import zlib  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

from tools.buscador_paginas import (  # noqa: E402
    LIMITE_CORPO_BYTES,
    BuscadorPaginas,
    FalhaBusca,
    PaginaBaixada,
    resolver_timeout,
)

_MIB = 1024 * 1024
_HTML = "text/html; charset=utf-8"


class _Corpo:
    """Iterável de blocos que registra o consumo; itens exceção são levantados."""

    def __init__(self, blocos: list[bytes | BaseException]) -> None:
        self._blocos = list(blocos)
        self.iniciado = False
        self.consumidos = 0

    def __iter__(self):  # noqa: ANN204
        self.iniciado = True
        for item in self._blocos:
            if isinstance(item, BaseException):
                raise item
            self.consumidos += 1
            yield item


@dataclass
class _Resposta:
    status: int
    cabecalhos: dict[str, str] = field(default_factory=dict)
    blocos: list[bytes | BaseException] = field(default_factory=list)
    erro_ao_fechar: ErroTransporte | None = None
    fechamentos: int = 0
    corpo: _Corpo | None = None

    def para_http(self) -> RespostaHTTP:
        self.corpo = _Corpo(self.blocos)

        def fechar() -> None:
            self.fechamentos += 1
            if self.erro_ao_fechar is not None:
                raise self.erro_ao_fechar

        return RespostaHTTP(
            status=self.status, cabecalhos=self.cabecalhos, blocos=self.corpo, fechar=fechar
        )


class _TransporteRoteirizado:
    def __init__(self, *roteiro: _Resposta | ErroTransporte) -> None:
        self._roteiro = list(roteiro)
        self.chamadas: list[tuple[str, object, float]] = []
        self.respostas: list[_Resposta] = []

    def __call__(self, url: str, cabecalhos, timeout: float) -> RespostaHTTP:  # noqa: ANN001
        self.chamadas.append((url, cabecalhos, timeout))
        item = self._roteiro.pop(0)
        if isinstance(item, ErroTransporte):
            raise item
        self.respostas.append(item)
        return item.para_http()

    @property
    def urls(self) -> list[str]:
        return [url for url, _, _ in self.chamadas]


class _RelogioFake:
    """Relógio monotônico que avança ``passo`` a cada leitura."""

    def __init__(self, passo: float) -> None:
        self.agora = 0.0
        self.passo = passo

    def __call__(self) -> float:
        valor = self.agora
        self.agora += self.passo
        return valor


def _html(
    corpo: bytes | list[bytes | BaseException] = b"<html>ok</html>",
    *,
    status: int = 200,
    content_type: str | None = _HTML,
    **extras: str,
) -> _Resposta:
    cabecalhos: dict[str, str] = {}
    if content_type is not None:
        cabecalhos["Content-Type"] = content_type
    for nome, valor in extras.items():
        cabecalhos[nome.replace("_", "-")] = valor
    blocos = corpo if isinstance(corpo, list) else [corpo]
    return _Resposta(status, cabecalhos, blocos)


def _redir(location: str | None, status: int = 302) -> _Resposta:
    cabecalhos = {"Location": location} if location is not None else {}
    return _Resposta(status, cabecalhos, [b"movido"])


def _buscador(
    transporte: _TransporteRoteirizado,
    *,
    validar_destino: Callable[[str], str | None] = lambda _url: None,
    timeout: float | None = 10.0,
    relogio: Callable[[], float] = lambda: 0.0,
) -> BuscadorPaginas:
    return BuscadorPaginas(
        validar_destino=validar_destino,
        transporte=transporte,
        timeout=timeout,
        relogio_monotonico=relogio,
    )


def _em_blocos(dados: bytes, tamanho: int) -> list[bytes | BaseException]:
    return [dados[i : i + tamanho] for i in range(0, len(dados), tamanho)]


def _todas_fechadas_uma_vez(transporte: _TransporteRoteirizado) -> bool:
    return all(r.fechamentos == 1 for r in transporte.respostas)


# --- Sucesso e charset (Req 5.7, 5.10) --------------------------------------


@pytest.mark.parametrize(
    ("content_type", "charset"),
    [
        ("text/html; charset=utf-8", "utf-8"),
        ('text/html; charset="ISO-8859-1"', "ISO-8859-1"),
        ("text/html; charset='windows-1252'", "windows-1252"),
        ("TEXT/HTML ; Charset=UTF-8", "UTF-8"),
        ("text/html; boundary=x; charset=latin1", "latin1"),
        ("application/xhtml+xml;charset=utf-8", "utf-8"),
        ("text/html", None),
        ("text/html; charset=", None),
        ("text/html; charset", None),
    ],
)
def test_sucesso_preserva_content_type_e_extrai_charset(
    content_type: str, charset: str | None
) -> None:
    transporte = _TransporteRoteirizado(
        _html([b"<html>", b"corpo", b"</html>"], content_type=content_type)
    )

    desfecho = _buscador(transporte).buscar(URL)

    assert desfecho == PaginaBaixada(
        status_http=200,
        content_type=content_type,
        charset=charset,
        url_final=URL,
        corpo=b"<html>corpo</html>",
    )
    assert _todas_fechadas_uma_vez(transporte)


def test_uma_requisicao_com_cabecalhos_fixos_timeout_e_url_sem_espacos() -> None:
    transporte = _TransporteRoteirizado(_html(status=203))

    desfecho = _buscador(transporte, timeout=7.0).buscar(f"  {URL}\n")

    assert isinstance(desfecho, PaginaBaixada)
    assert desfecho.status_http == 203
    assert desfecho.url_final == URL
    ((url, cabecalhos, timeout),) = transporte.chamadas
    assert url == URL
    assert cabecalhos is CABECALHOS_FIXOS
    assert timeout == 7.0


def test_nomes_de_cabecalho_da_resposta_sao_case_insensitive() -> None:
    resposta = _Resposta(
        200,
        {"CONTENT-TYPE": "text/html", "Content-Encoding": "GZIP ", "content-LENGTH": "10"},
        [gzip.compress(b"<p>x</p>")],
    )
    desfecho = _buscador(_TransporteRoteirizado(resposta)).buscar(URL)
    assert isinstance(desfecho, PaginaBaixada)
    assert desfecho.corpo == b"<p>x</p>"


# --- Status (Req 5.9) --------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_3xx_sem_location_vira_status_http(status: int) -> None:
    transporte = _TransporteRoteirizado(_redir(None, status))

    desfecho = _buscador(transporte).buscar(URL)

    assert desfecho == FalhaBusca("status_http", URL, status_http=status)
    assert len(transporte.chamadas) == 1
    assert transporte.respostas[0].corpo.iniciado is False
    assert _todas_fechadas_uma_vez(transporte)


@pytest.mark.parametrize("location", ["", "   "])
def test_3xx_com_location_vazio_vira_status_http(location: str) -> None:
    transporte = _TransporteRoteirizado(_redir(location))
    assert _buscador(transporte).buscar(URL) == FalhaBusca("status_http", URL, status_http=302)
    assert len(transporte.chamadas) == 1


@pytest.mark.parametrize("status", [100, 199, 300, 304, 305, 400, 404, 429, 500, 503])
def test_status_fora_de_2xx_vira_status_http(status: int) -> None:
    # 304/305/300 não são redirecionamentos seguidos, mesmo com Location.
    resposta = _html(status=status, Location="https://outro.exemplo.com/")
    transporte = _TransporteRoteirizado(resposta)

    desfecho = _buscador(transporte).buscar(URL)

    assert desfecho == FalhaBusca("status_http", URL, status_http=status)
    assert len(transporte.chamadas) == 1
    assert transporte.respostas[0].corpo.iniciado is False


# --- Redirecionamentos (Req 5.5, 5.6, 5.13) ---------------------------------


def test_cadeia_de_redirecionamentos_resolve_location_relativo() -> None:
    validados: list[str] = []

    def validar(url: str) -> str | None:
        validados.append(url)
        return None

    transporte = _TransporteRoteirizado(
        _redir("../z?q=1", 301),
        _redir("//b.exemplo.com/p", 302),
        _redir("  http://c.exemplo.com/final  ", 307),
        _redir("pagina.html", 308),
        _html(),
    )

    desfecho = _buscador(transporte, validar_destino=validar).buscar(
        "https://a.exemplo.com/x/y"
    )

    esperadas = [
        "https://a.exemplo.com/x/y",
        "https://a.exemplo.com/z?q=1",
        "https://b.exemplo.com/p",
        "http://c.exemplo.com/final",
        "http://c.exemplo.com/pagina.html",
    ]
    assert transporte.urls == esperadas
    assert validados == esperadas[1:]
    assert isinstance(desfecho, PaginaBaixada)
    assert desfecho.url_final == "http://c.exemplo.com/pagina.html"
    # Os cabeçalhos são sempre os mesmos objetos fixos, sem estado entre saltos.
    assert all(cab is CABECALHOS_FIXOS for _, cab, _ in transporte.chamadas)
    # O corpo dos 3xx não é lido e toda resposta é fechada.
    assert all(r.corpo.iniciado is False for r in transporte.respostas[:-1])
    assert _todas_fechadas_uma_vez(transporte)


def test_redirecionamento_para_destino_proibido_nao_e_requisitado() -> None:
    proibido = "http://127.0.0.1/admin"
    transporte = _TransporteRoteirizado(
        _redir("https://ok.exemplo.com/", 302),
        _redir(proibido, 301),
        _html(),  # nunca deve ser consumido
    )

    def validar(url: str) -> str | None:
        return "destino_nao_permitido" if url == proibido else None

    desfecho = _buscador(transporte, validar_destino=validar).buscar(URL)

    assert desfecho == FalhaBusca("redirecionamento_nao_permitido", proibido, status_http=301)
    assert transporte.urls == [URL, "https://ok.exemplo.com/"]
    assert proibido not in transporte.urls
    assert _todas_fechadas_uma_vez(transporte)


def test_cinco_redirecionamentos_sao_seguidos() -> None:
    saltos = [_redir(f"/salto{i}") for i in range(1, 6)]
    transporte = _TransporteRoteirizado(*saltos, _html())

    desfecho = _buscador(transporte).buscar("https://exemplo.com/inicio")

    assert len(transporte.chamadas) == 6
    assert isinstance(desfecho, PaginaBaixada)
    assert desfecho.url_final == "https://exemplo.com/salto5"


def test_sexto_redirecionamento_vira_redirecionamentos_excedidos() -> None:
    validados: list[str] = []

    def validar(url: str) -> str | None:
        validados.append(url)
        return None

    saltos = [_redir(f"/salto{i}") for i in range(1, 7)]
    transporte = _TransporteRoteirizado(*saltos, _html())  # 7º item nunca usado

    desfecho = _buscador(transporte, validar_destino=validar).buscar(
        "https://exemplo.com/inicio"
    )

    assert len(transporte.chamadas) == 6
    assert "https://exemplo.com/salto6" not in transporte.urls
    assert validados == [f"https://exemplo.com/salto{i}" for i in range(1, 6)]
    assert desfecho == FalhaBusca(
        "redirecionamentos_excedidos", "https://exemplo.com/salto5", status_http=302
    )
    assert _todas_fechadas_uma_vez(transporte)


def test_erro_de_transporte_no_meio_da_cadeia_registra_url_do_salto() -> None:
    transporte = _TransporteRoteirizado(_redir("/novo"), ErroTransporte("timeout"))

    desfecho = _buscador(transporte).buscar("https://exemplo.com/velho")

    assert desfecho == FalhaBusca("timeout", "https://exemplo.com/novo")
    assert _todas_fechadas_uma_vez(transporte)


# --- Content-Type (Req 5.10) ------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "text/plain; charset=utf-8", "image/png", "text/htmlx", ""],
)
def test_tipo_nao_html_vira_nao_html_com_valor_bruto(content_type: str) -> None:
    transporte = _TransporteRoteirizado(_html(content_type=content_type))

    desfecho = _buscador(transporte).buscar(URL)

    assert desfecho == FalhaBusca(
        "nao_html", URL, status_http=200, content_type=content_type
    )
    assert transporte.respostas[0].corpo.iniciado is False


def test_content_type_ausente_vira_nao_html_sem_content_type() -> None:
    transporte = _TransporteRoteirizado(_html(content_type=None))

    desfecho = _buscador(transporte).buscar(URL)

    assert desfecho == FalhaBusca("nao_html", URL, status_http=200, content_type=None)
    assert transporte.respostas[0].corpo.iniciado is False


# --- Content-Encoding (Req 5.14, 5.15) --------------------------------------


@pytest.mark.parametrize(
    "codificacao", ["gzip, br", "br", "deflate", "compress", "zstd", "identity, gzip", "x-gzip"]
)
def test_codificacao_nao_suportada(codificacao: str) -> None:
    transporte = _TransporteRoteirizado(_html(Content_Encoding=codificacao))

    desfecho = _buscador(transporte).buscar(URL)

    assert desfecho == FalhaBusca(
        "codificacao_nao_suportada", URL, status_http=200, content_type=_HTML
    )
    assert transporte.respostas[0].corpo.iniciado is False
    assert _todas_fechadas_uma_vez(transporte)


@pytest.mark.parametrize("codificacao", ["", "identity", " IDENTITY "])
def test_identity_entrega_corpo_cru(codificacao: str) -> None:
    corpo = gzip.compress(b"parece gzip mas e identity")
    transporte = _TransporteRoteirizado(_html(corpo, Content_Encoding=codificacao))
    desfecho = _buscador(transporte).buscar(URL)
    assert isinstance(desfecho, PaginaBaixada)
    assert desfecho.corpo == corpo


def test_gzip_simples_em_blocos_pequenos() -> None:
    original = b"<html>" + "peça ç ã".encode() * 500 + b"</html>"
    blocos = _em_blocos(gzip.compress(original), 7)
    transporte = _TransporteRoteirizado(_html(blocos, Content_Encoding="gzip"))

    desfecho = _buscador(transporte).buscar(URL)

    assert isinstance(desfecho, PaginaBaixada)
    assert desfecho.corpo == original


def test_gzip_com_varios_membros() -> None:
    membros = [b"<html>", b"<body>parte 2</body>", b"</html>"]
    fluxo = b"".join(gzip.compress(m) for m in membros)
    # Blocos de 5 bytes: fronteiras entre membros caem no meio dos blocos.
    transporte = _TransporteRoteirizado(_html(_em_blocos(fluxo, 5), Content_Encoding="gzip"))

    desfecho = _buscador(transporte).buscar(URL)

    assert isinstance(desfecho, PaginaBaixada)
    assert desfecho.corpo == b"".join(membros)


def test_gzip_com_preenchimento_de_zeros_apos_o_membro() -> None:
    fluxo = gzip.compress(b"<p>a</p>") + b"\x00" * 16 + gzip.compress(b"<p>b</p>") + b"\x00" * 8
    transporte = _TransporteRoteirizado(_html([fluxo], Content_Encoding="gzip"))
    desfecho = _buscador(transporte).buscar(URL)
    assert isinstance(desfecho, PaginaBaixada)
    assert desfecho.corpo == b"<p>a</p><p>b</p>"


@pytest.mark.parametrize(
    "fluxo",
    [
        gzip.compress(b"<html>truncado</html>")[:-4],  # sem o final do trailer
        gzip.compress(b"<html>truncado</html>")[:12],  # só o cabeçalho
        gzip.compress(b"<p>1</p>") + gzip.compress(b"<p>2</p>")[:-3],  # 2º membro truncado
    ],
    ids=["trailer_incompleto", "so_cabecalho", "segundo_membro_truncado"],
)
def test_gzip_truncado_vira_codificacao_invalida(fluxo: bytes) -> None:
    transporte = _TransporteRoteirizado(_html(_em_blocos(fluxo, 4), Content_Encoding="gzip"))

    desfecho = _buscador(transporte).buscar(URL)

    assert desfecho == FalhaBusca(
        "codificacao_invalida", URL, status_http=200, content_type=_HTML
    )
    assert _todas_fechadas_uma_vez(transporte)


@pytest.mark.parametrize(
    "fluxo",
    [
        b"<html>nao e gzip</html>",
        b"\x1f\x8b\x08\x00" + b"\xff" * 40,
        gzip.compress(b"<p>x</p>")[:10] + b"\x00\xff\x13" * 10,
    ],
    ids=["sem_cabecalho_gzip", "deflate_corrompido", "dados_corrompidos"],
)
def test_gzip_corrompido_vira_codificacao_invalida(fluxo: bytes) -> None:
    transporte = _TransporteRoteirizado(_html(fluxo, Content_Encoding="gzip"))
    desfecho = _buscador(transporte).buscar(URL)
    assert desfecho == FalhaBusca(
        "codificacao_invalida", URL, status_http=200, content_type=_HTML
    )


def test_gzip_com_crc_errado_vira_codificacao_invalida() -> None:
    fluxo = bytearray(gzip.compress(b"<html>crc</html>"))
    fluxo[-8] ^= 0xFF  # primeiro byte do CRC32 no trailer
    transporte = _TransporteRoteirizado(_html(bytes(fluxo), Content_Encoding="gzip"))
    desfecho = _buscador(transporte).buscar(URL)
    assert isinstance(desfecho, FalhaBusca)
    assert desfecho.motivo == "codificacao_invalida"


# --- Tamanho (Req 5.7, 5.11) ------------------------------------------------


def test_content_length_grande_falha_sem_ler_o_corpo() -> None:
    resposta = _html([b"x" * 10], Content_Length=str(LIMITE_CORPO_BYTES + 1))
    transporte = _TransporteRoteirizado(resposta)

    desfecho = _buscador(transporte).buscar(URL)

    assert desfecho == FalhaBusca(
        "tamanho_excedido", URL, status_http=200, content_type=_HTML
    )
    assert resposta.corpo.iniciado is False
    assert resposta.corpo.consumidos == 0
    assert resposta.fechamentos == 1


def test_content_length_no_limite_ou_nao_numerico_nao_bloqueia() -> None:
    for declarado in (str(LIMITE_CORPO_BYTES), "abc", "-1", "", "١٢٣"):
        transporte = _TransporteRoteirizado(_html(b"<p>x</p>", Content_Length=declarado))
        desfecho = _buscador(transporte).buscar(URL)
        assert isinstance(desfecho, PaginaBaixada), declarado


def test_corpo_identity_exatamente_no_limite_e_aceito() -> None:
    blocos: list[bytes | BaseException] = [b"a" * _MIB] * 5
    transporte = _TransporteRoteirizado(_html(blocos))

    desfecho = _buscador(transporte).buscar(URL)

    assert isinstance(desfecho, PaginaBaixada)
    assert len(desfecho.corpo) == LIMITE_CORPO_BYTES


def test_corpo_identity_acima_do_limite_para_de_ler() -> None:
    blocos: list[bytes | BaseException] = [b"a" * _MIB] * 5 + [b"b", b"c" * _MIB, b"d"]
    resposta = _html(blocos)
    transporte = _TransporteRoteirizado(resposta)

    desfecho = _buscador(transporte).buscar(URL)

    assert desfecho == FalhaBusca(
        "tamanho_excedido", URL, status_http=200, content_type=_HTML
    )
    assert resposta.corpo.consumidos == 6  # parou no bloco que ultrapassou
    assert resposta.fechamentos == 1


def test_gzip_decodificado_exatamente_no_limite_e_aceito() -> None:
    fluxo = gzip.compress(b"\0" * LIMITE_CORPO_BYTES, compresslevel=1)
    transporte = _TransporteRoteirizado(_html(fluxo, Content_Encoding="gzip"))
    desfecho = _buscador(transporte).buscar(URL)
    assert isinstance(desfecho, PaginaBaixada)
    assert len(desfecho.corpo) == LIMITE_CORPO_BYTES


def test_bomba_gzip_para_no_limite() -> None:
    bomba = gzip.compress(b"\0" * (16 * _MIB), compresslevel=9)
    blocos = _em_blocos(bomba, 1024)
    resposta = _html(blocos, Content_Encoding="gzip")
    transporte = _TransporteRoteirizado(resposta)

    desfecho = _buscador(transporte).buscar(URL)

    assert desfecho == FalhaBusca(
        "tamanho_excedido", URL, status_http=200, content_type=_HTML
    )
    # A leitura para antes do fim do fluxo comprimido.
    assert resposta.corpo.consumidos < len(blocos)
    assert resposta.fechamentos == 1


def test_bomba_gzip_nunca_produz_mais_que_limite_mais_um() -> None:
    bomba = gzip.compress(b"\0" * (16 * _MIB), compresslevel=9)
    decodificador = bp._DecodificadorCorpo(gzip=True)
    with pytest.raises(bp._TamanhoExcedido):
        decodificador.alimentar(bomba)  # um único bloco com a bomba inteira
    assert len(decodificador._saida) == LIMITE_CORPO_BYTES + 1


# --- Corpo vazio (Req 5.12) --------------------------------------------------


@pytest.mark.parametrize(
    ("blocos", "codificacao"),
    [
        ([], ""),
        ([b"", b""], ""),
        ([gzip.compress(b"")], "gzip"),
    ],
    ids=["sem_blocos", "blocos_vazios", "gzip_de_vazio"],
)
def test_corpo_vazio(blocos: list[bytes | BaseException], codificacao: str) -> None:
    transporte = _TransporteRoteirizado(_html(blocos, Content_Encoding=codificacao))

    desfecho = _buscador(transporte).buscar(URL)

    assert desfecho == FalhaBusca("corpo_vazio", URL, status_http=200, content_type=_HTML)


# --- Erros de transporte (Req 5.8) -------------------------------------------


@pytest.mark.parametrize("tipo", ["rede", "timeout"])
def test_erro_de_transporte_na_requisicao(tipo: str) -> None:
    transporte = _TransporteRoteirizado(ErroTransporte(tipo))  # type: ignore[arg-type]
    assert _buscador(transporte).buscar(URL) == FalhaBusca(tipo, URL)  # type: ignore[arg-type]
    assert len(transporte.chamadas) == 1  # sem retry


@pytest.mark.parametrize("tipo", ["rede", "timeout"])
def test_erro_de_transporte_no_meio_do_corpo(tipo: str) -> None:
    resposta = _html([b"<html>", ErroTransporte(tipo), b"nunca"])  # type: ignore[arg-type]
    transporte = _TransporteRoteirizado(resposta)

    desfecho = _buscador(transporte).buscar(URL)

    assert desfecho == FalhaBusca(tipo, URL, status_http=200)  # type: ignore[arg-type]
    assert resposta.corpo.consumidos == 1
    assert resposta.fechamentos == 1
    assert len(transporte.chamadas) == 1  # sem retry


def test_excecao_generica_do_transporte_atravessa() -> None:
    def transporte(url, cabecalhos, timeout):  # noqa: ANN001, ANN202
        raise RuntimeError("guarda de rede")

    buscador = BuscadorPaginas(validar_destino=lambda _u: None, transporte=transporte, timeout=5)
    with pytest.raises(RuntimeError, match="guarda de rede"):
        buscador.buscar(URL)


def test_falha_nao_carrega_corpo_nem_cabecalhos() -> None:
    segredo = "Set-Cookie-SEGREDO"
    resposta = _Resposta(404, {"Content-Type": _HTML, "Set-Cookie": segredo}, [b"SEGREDO"])
    desfecho = _buscador(_TransporteRoteirizado(resposta)).buscar(URL)
    assert isinstance(desfecho, FalhaBusca)
    assert "SEGREDO" not in repr(desfecho)


# --- Prazo total por gotejamento (Req 5.4) ----------------------------------


def test_timeout_por_gotejamento_com_relogio_fake() -> None:
    # O relógio avança 4 s por leitura: início em 0; blocos checados em 4, 8, 12.
    blocos: list[bytes | BaseException] = [b"<"] * 1000
    resposta = _html(blocos)
    transporte = _TransporteRoteirizado(resposta)
    relogio = _RelogioFake(passo=4.0)

    desfecho = _buscador(transporte, timeout=10.0, relogio=relogio).buscar(URL)

    assert desfecho == FalhaBusca("timeout", URL, status_http=200)
    assert resposta.corpo.consumidos == 3  # 3º bloco lido, rejeitado antes de alimentar
    assert resposta.fechamentos == 1


def test_leitura_lenta_dentro_do_prazo_e_aceita() -> None:
    blocos: list[bytes | BaseException] = [b"<p>", b"x", b"</p>"]
    transporte = _TransporteRoteirizado(_html(blocos))
    relogio = _RelogioFake(passo=2.0)  # último bloco checado em 6 s < 10 s

    desfecho = _buscador(transporte, timeout=10.0, relogio=relogio).buscar(URL)

    assert isinstance(desfecho, PaginaBaixada)
    assert desfecho.corpo == b"<p>x</p>"


def test_prazo_e_reiniciado_a_cada_salto() -> None:
    # Cada salto consome 1 leitura do relógio (início); o 200 final tem 2 blocos.
    transporte = _TransporteRoteirizado(
        _redir("/a"), _redir("/b"), _redir("/c"), _html([b"<p>", b"</p>"])
    )
    relogio = _RelogioFake(passo=3.0)

    desfecho = _buscador(transporte, timeout=10.0, relogio=relogio).buscar(URL)

    # Sem reinício o total (≥ 15 s) excederia 10 s; com reinício cada GET dura ≤ 6 s.
    assert isinstance(desfecho, PaginaBaixada)


# --- fechar em todos os caminhos --------------------------------------------


_CENARIOS_FECHAR: dict[str, Callable[[], _TransporteRoteirizado]] = {
    "sucesso": lambda: _TransporteRoteirizado(_html()),
    "redirecionamento_e_sucesso": lambda: _TransporteRoteirizado(_redir("/b"), _html()),
    "redirecionamento_proibido": lambda: _TransporteRoteirizado(_redir("http://localhost/")),
    "redirecionamentos_excedidos": lambda: _TransporteRoteirizado(
        *[_redir(f"/s{i}") for i in range(6)]
    ),
    "status_http": lambda: _TransporteRoteirizado(_html(status=500)),
    "nao_html": lambda: _TransporteRoteirizado(_html(content_type="application/pdf")),
    "codificacao_nao_suportada": lambda: _TransporteRoteirizado(_html(Content_Encoding="br")),
    "content_length": lambda: _TransporteRoteirizado(
        _html(Content_Length=str(LIMITE_CORPO_BYTES + 1))
    ),
    "codificacao_invalida": lambda: _TransporteRoteirizado(
        _html(b"lixo", Content_Encoding="gzip")
    ),
    "erro_no_corpo": lambda: _TransporteRoteirizado(_html([b"x", ErroTransporte("rede")])),
    "corpo_vazio": lambda: _TransporteRoteirizado(_html([])),
    "erro_apos_redirecionamento": lambda: _TransporteRoteirizado(
        _redir("/b"), ErroTransporte("rede")
    ),
}


def _validar_sem_localhost(url: str) -> str | None:
    return "destino_nao_permitido" if "localhost" in url else None


@pytest.mark.parametrize("cenario", list(_CENARIOS_FECHAR))
def test_fechar_chamado_uma_vez_em_todos_os_caminhos(cenario: str) -> None:
    transporte = _CENARIOS_FECHAR[cenario]()

    desfecho = _buscador(transporte, validar_destino=_validar_sem_localhost).buscar(URL)

    assert isinstance(desfecho, (PaginaBaixada, FalhaBusca))
    assert transporte.respostas, cenario
    assert _todas_fechadas_uma_vez(transporte), [r.fechamentos for r in transporte.respostas]


def test_fechar_no_timeout_por_gotejamento() -> None:
    resposta = _html([b"x"] * 10)
    transporte = _TransporteRoteirizado(resposta)
    _buscador(transporte, timeout=1.0, relogio=_RelogioFake(passo=5.0)).buscar(URL)
    assert resposta.fechamentos == 1


def test_erro_ao_fechar_nao_altera_o_desfecho() -> None:
    resposta = _html()
    resposta.erro_ao_fechar = ErroTransporte("rede")
    redirecionamento = _redir("/b")
    redirecionamento.erro_ao_fechar = ErroTransporte("timeout")
    transporte = _TransporteRoteirizado(redirecionamento, resposta)

    desfecho = _buscador(transporte).buscar(URL)

    assert isinstance(desfecho, PaginaBaixada)
    assert desfecho.url_final == "https://exemplo.com/b"
    assert _todas_fechadas_uma_vez(transporte)


# --- Resolução do timeout (Req 5.4) -----------------------------------------


@pytest.mark.parametrize(
    ("ambiente", "esperado"),
    [
        (None, 15.0),
        ("30", 30.0),
        (" 1 ", 1.0),
        ("120", 120.0),
        ("2.5", 2.5),
        ("0.5", 15.0),
        ("0", 15.0),
        ("121", 15.0),
        ("-5", 15.0),
        ("abc", 15.0),
        ("", 15.0),
        ("nan", 15.0),
        ("inf", 15.0),
    ],
)
def test_timeout_padrao_vem_do_ambiente(
    ambiente: str | None, esperado: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    if ambiente is None:
        monkeypatch.delenv("ESTAGIARIO_COLETA_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("ESTAGIARIO_COLETA_TIMEOUT", ambiente)

    assert resolver_timeout(None) == esperado
    transporte = _TransporteRoteirizado(_html())
    buscador = _buscador(transporte, timeout=None)
    assert buscador.timeout == esperado
    buscador.buscar(URL)
    assert transporte.chamadas[0][2] == esperado


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (7, 7.0),
        (1, 1.0),
        (120.0, 120.0),
        (0.99, 15.0),
        (200, 15.0),
        (0, 15.0),
        (True, 15.0),
        (float("nan"), 15.0),
        (float("inf"), 15.0),
        ("10", 15.0),
    ],
)
def test_timeout_explicito_segue_a_regra_e_ignora_o_ambiente(
    valor: object, esperado: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESTAGIARIO_COLETA_TIMEOUT", "42")
    assert resolver_timeout(valor) == esperado
    transporte = _TransporteRoteirizado(_html())
    _buscador(transporte, timeout=valor).buscar(URL)  # type: ignore[arg-type]
    assert transporte.chamadas[0][2] == esperado
