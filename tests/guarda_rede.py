"""Guarda de rede para os testes da coleta de páginas (html-extract-save, Req 9.6).

A suíte padrão não pode abrir conexões de rede. Esta fixture troca, via
``monkeypatch``, ``socket.socket.connect``, ``socket.socket.connect_ex`` e
``socket.create_connection`` por funções que falham com ``RedeBloqueadaError``.

``RedeBloqueadaError`` deriva de ``RuntimeError`` (e não de ``OSError``) de
propósito: o transporte HTTP converte ``OSError`` em desfecho ``rede``, o que
esconderia uma conexão acidental. Com ``RuntimeError`` a tentativa atravessa o
código de produção e derruba o teste de forma visível.

Sockets ``AF_UNIX`` continuam permitidos: não são rede e podem ser usados por
infraestrutura local do interpretador.

Uso em um módulo de teste da coleta (ativa a guarda em todos os testes do módulo)::

    from tests.guarda_rede import guarda_rede_autouse  # noqa: F401

Ou, se o teste precisar inspecionar as tentativas bloqueadas, peça a fixture
``bloquear_rede`` explicitamente; ela devolve a lista de destinos recusados::

    from tests.guarda_rede import bloquear_rede  # noqa: F401

    def test_x(bloquear_rede):
        ...
        assert bloquear_rede == []
"""

from __future__ import annotations

import socket

import pytest


class RedeBloqueadaError(RuntimeError):
    """Tentativa de conexão de rede durante a suíte padrão."""

    def __init__(self, destino: object) -> None:
        super().__init__(f"conexão de rede bloqueada nos testes: {destino!r}")
        self.destino = destino


def _eh_unix(sock: socket.socket) -> bool:
    af_unix = getattr(socket, "AF_UNIX", None)
    return af_unix is not None and sock.family == af_unix


def instalar_guarda_rede(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Instala o bloqueio e devolve a lista (mutável) de destinos recusados."""
    tentativas: list[object] = []
    connect_original = socket.socket.connect
    connect_ex_original = socket.socket.connect_ex

    def connect_bloqueado(self: socket.socket, endereco: object) -> None:
        if _eh_unix(self):
            return connect_original(self, endereco)
        tentativas.append(endereco)
        raise RedeBloqueadaError(endereco)

    def connect_ex_bloqueado(self: socket.socket, endereco: object) -> int:
        if _eh_unix(self):
            return connect_ex_original(self, endereco)
        tentativas.append(endereco)
        raise RedeBloqueadaError(endereco)

    def create_connection_bloqueado(endereco: object, *args: object, **kwargs: object):
        tentativas.append(endereco)
        raise RedeBloqueadaError(endereco)

    monkeypatch.setattr(socket.socket, "connect", connect_bloqueado)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex_bloqueado)
    monkeypatch.setattr(socket, "create_connection", create_connection_bloqueado)
    return tentativas


@pytest.fixture
def bloquear_rede(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Bloqueia a rede durante o teste; devolve os destinos recusados."""
    return instalar_guarda_rede(monkeypatch)


@pytest.fixture(autouse=True)
def guarda_rede_autouse(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Versão ``autouse``: basta importá-la no módulo de teste para ativar a guarda.

    Não depende de ``bloquear_rede`` para funcionar mesmo quando só ela é importada
    (pytest só registra as fixtures presentes no namespace do módulo).
    """
    return instalar_guarda_rede(monkeypatch)
