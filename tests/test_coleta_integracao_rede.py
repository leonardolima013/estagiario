"""Integração opt-in do transporte HTTP real da coleta de páginas (html-extract-save).

Exercita ``tools.buscador_paginas.transporte_urllib`` (urllib real, sockets reais)
contra um ``http.server`` local, em thread, ligado a ``127.0.0.1`` numa porta
efêmera. Não acessa rede externa, não gera custo e não grava arquivos (Req 5.2, 9.6).

Só roda com ``ESTAGIARIO_RUN_COLETA_WEB_TESTS=1``. Este módulo, de propósito,
NÃO usa ``tests.guarda_rede``: ele precisa de sockets locais reais.

Observação sobre ``validar_destino``: em produção o ``BuscadorPaginas`` recebe
``coleta_paginas.url.motivo_recusa_url``, que recusa ``127.0.0.1`` (Req 4.5).
Para os testes ponta a ponta do buscador contra o servidor local, um validador
permissivo (``_permitir_tudo``) é injetado explicitamente; o teste
``test_validador_real_recusa_redirecionamento_para_loopback`` usa o validador de
produção e confirma que o salto para loopback é recusado sem ser requisitado.
"""

from __future__ import annotations

import gzip
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from coleta_paginas.url import motivo_recusa_url
from tools.buscador_paginas import (
    CABECALHOS_FIXOS,
    LIMITE_CORPO_BYTES,
    BuscadorPaginas,
    FalhaBusca,
    PaginaBaixada,
    transporte_urllib,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("ESTAGIARIO_RUN_COLETA_WEB_TESTS") != "1",
    reason="integração HTTP real da coleta; habilite com ESTAGIARIO_RUN_COLETA_WEB_TESTS=1",
)

TIMEOUT_S = 5.0
HTML = "<html><body><h1>Peça JE-4699 — ação</h1></body></html>".encode("utf-8")
HTML_DESTINO = b"<html><body>destino</body></html>"
TAMANHO_DECLARADO_GRANDE = 2 * LIMITE_CORPO_BYTES

# Nomes de cabeçalho que o urllib acrescenta por conta própria.
_CABECALHOS_DO_URLLIB = {"host", "connection"}

_VARIAVEIS_PROXY = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)


def _permitir_tudo(_url: str) -> None:
    """Validador permissivo, só para apontar o buscador ao servidor local."""
    return None


@dataclass
class _Registro:
    trava: threading.Lock = field(default_factory=threading.Lock)
    requisicoes: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def adicionar(self, caminho: str, cabecalhos: dict[str, str]) -> None:
        with self.trava:
            self.requisicoes.append((caminho, cabecalhos))

    def caminhos(self) -> list[str]:
        with self.trava:
            return [caminho for caminho, _ in self.requisicoes]

    def cabecalhos_de(self, caminho: str) -> dict[str, str]:
        with self.trava:
            for c, cabecalhos in self.requisicoes:
                if c == caminho:
                    return cabecalhos
        raise AssertionError(f"caminho não requisitado: {caminho}")


def _criar_handler(registro: _Registro) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return None

        def _html(self, corpo: bytes, extras: dict[str, str] | None = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            for nome, valor in (extras or {}).items():
                self.send_header(nome, valor)
            self.end_headers()
            self.wfile.write(corpo)

        def do_GET(self) -> None:  # noqa: N802
            registro.adicionar(
                self.path, {nome.lower(): valor for nome, valor in self.headers.items()}
            )
            try:
                if self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "/destino")
                    self.send_header("Set-Cookie", "sessao=segredo; Path=/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/destino":
                    self._html(HTML_DESTINO)
                elif self.path == "/gzip":
                    comprimido = gzip.compress(HTML)
                    self._html(comprimido, {"Content-Encoding": "gzip"})
                elif self.path == "/grande":
                    # Declara muito mais do que envia: se o cliente tentasse ler o
                    # corpo, obteria leitura incompleta (falha "rede"), não
                    # "tamanho_excedido".
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(TAMANHO_DECLARADO_GRANDE))
                    self.end_headers()
                    self.wfile.write(b"<html>")
                elif self.path == "/cookie":
                    self._html(HTML, {"Set-Cookie": "sessao=segredo; Path=/"})
                elif self.path == "/eco":
                    self._html(HTML)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

    return _Handler


@dataclass(frozen=True)
class _Servidor:
    base: str
    registro: _Registro


@pytest.fixture
def servidor(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Servidor]:
    # Sem proxy do ambiente: 127.0.0.1 nunca pode ser enviado a um proxy.
    for nome in _VARIAVEIS_PROXY:
        monkeypatch.delenv(nome, raising=False)

    registro = _Registro()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _criar_handler(registro))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, porta = httpd.server_address[:2]
        yield _Servidor(base=f"http://{host}:{porta}", registro=registro)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# transporte_urllib direto
# ---------------------------------------------------------------------------


def test_302_nao_e_seguido_automaticamente(servidor: _Servidor) -> None:
    resposta = transporte_urllib(f"{servidor.base}/redir", CABECALHOS_FIXOS, TIMEOUT_S)
    try:
        assert resposta.status == 302
        assert resposta.cabecalhos["location"] == "/destino"
    finally:
        resposta.fechar()
    assert servidor.registro.caminhos() == ["/redir"]


def test_gzip_entregue_cru_pelo_transporte(servidor: _Servidor) -> None:
    resposta = transporte_urllib(f"{servidor.base}/gzip", CABECALHOS_FIXOS, TIMEOUT_S)
    try:
        corpo = b"".join(resposta.blocos)
    finally:
        resposta.fechar()
    assert resposta.status == 200
    assert resposta.cabecalhos["content-encoding"] == "gzip"
    assert corpo.startswith(b"\x1f\x8b")
    assert gzip.decompress(corpo) == HTML


def test_content_length_grande_exposto_sem_ler_corpo(servidor: _Servidor) -> None:
    resposta = transporte_urllib(f"{servidor.base}/grande", CABECALHOS_FIXOS, TIMEOUT_S)
    try:
        assert resposta.status == 200
        assert resposta.cabecalhos["content-length"] == str(TAMANHO_DECLARADO_GRANDE)
    finally:
        resposta.fechar()


def test_cabecalhos_enviados_sao_so_os_fixos_e_sem_cookie(servidor: _Servidor) -> None:
    for caminho in ("/cookie", "/eco"):
        resposta = transporte_urllib(f"{servidor.base}{caminho}", CABECALHOS_FIXOS, TIMEOUT_S)
        try:
            b"".join(resposta.blocos)
        finally:
            resposta.fechar()

    esperados = {nome.lower() for nome in CABECALHOS_FIXOS} | _CABECALHOS_DO_URLLIB
    for caminho in ("/cookie", "/eco"):
        recebidos = servidor.registro.cabecalhos_de(caminho)
        assert set(recebidos) == esperados
        assert recebidos["user-agent"] == CABECALHOS_FIXOS["User-Agent"]
        assert recebidos["accept-encoding"] == CABECALHOS_FIXOS["Accept-Encoding"]
        assert "cookie" not in recebidos
        assert "authorization" not in recebidos


# ---------------------------------------------------------------------------
# BuscadorPaginas ponta a ponta com o transporte real
# ---------------------------------------------------------------------------


def test_buscador_decodifica_gzip(servidor: _Servidor) -> None:
    buscador = BuscadorPaginas(validar_destino=_permitir_tudo, timeout=TIMEOUT_S)
    desfecho = buscador.buscar(f"{servidor.base}/gzip")
    assert isinstance(desfecho, PaginaBaixada)
    assert desfecho.corpo == HTML
    assert desfecho.charset == "utf-8"
    assert desfecho.status_http == 200


def test_buscador_content_length_grande_vira_tamanho_excedido(servidor: _Servidor) -> None:
    buscador = BuscadorPaginas(validar_destino=_permitir_tudo, timeout=TIMEOUT_S)
    desfecho = buscador.buscar(f"{servidor.base}/grande")
    assert isinstance(desfecho, FalhaBusca)
    assert desfecho.motivo == "tamanho_excedido"
    assert servidor.registro.caminhos() == ["/grande"]


def test_buscador_segue_redirecionamento_validado_sem_reenviar_cookie(
    servidor: _Servidor,
) -> None:
    buscador = BuscadorPaginas(validar_destino=_permitir_tudo, timeout=TIMEOUT_S)
    desfecho = buscador.buscar(f"{servidor.base}/redir")
    assert isinstance(desfecho, PaginaBaixada)
    assert desfecho.url_final == f"{servidor.base}/destino"
    assert desfecho.corpo == HTML_DESTINO
    assert servidor.registro.caminhos() == ["/redir", "/destino"]
    assert "cookie" not in servidor.registro.cabecalhos_de("/destino")


def test_validador_real_recusa_redirecionamento_para_loopback(servidor: _Servidor) -> None:
    buscador = BuscadorPaginas(validar_destino=motivo_recusa_url, timeout=TIMEOUT_S)
    desfecho = buscador.buscar(f"{servidor.base}/redir")
    assert isinstance(desfecho, FalhaBusca)
    assert desfecho.motivo == "redirecionamento_nao_permitido"
    assert desfecho.url == f"{servidor.base}/destino"
    assert servidor.registro.caminhos() == ["/redir"]
