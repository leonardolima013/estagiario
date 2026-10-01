"""Autoteste da guarda de rede usada pelos testes da coleta (Req 9.6)."""

from __future__ import annotations

import socket
import urllib.request

import pytest

from tests.guarda_rede import RedeBloqueadaError, bloquear_rede  # noqa: F401

pytest_plugins = ["pytester"]

# Capturados na importação do módulo, quando nenhuma guarda está instalada.
_CONNECT_ORIGINAL = socket.socket.connect
_CREATE_CONNECTION_ORIGINAL = socket.create_connection


@pytest.fixture
def porta_local():
    """Socket ouvindo em 127.0.0.1: sem a guarda, conectar nele funcionaria."""
    servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    servidor.bind(("127.0.0.1", 0))
    servidor.listen(1)
    try:
        yield servidor.getsockname()[1]
    finally:
        servidor.close()


def test_socket_connect_bloqueado(bloquear_rede, porta_local):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as cliente:
        with pytest.raises(RedeBloqueadaError):
            cliente.connect(("127.0.0.1", porta_local))
        with pytest.raises(RedeBloqueadaError):
            cliente.connect_ex(("127.0.0.1", porta_local))
    assert bloquear_rede == [("127.0.0.1", porta_local)] * 2


def test_create_connection_bloqueado(bloquear_rede, porta_local):
    with pytest.raises(RedeBloqueadaError):
        socket.create_connection(("127.0.0.1", porta_local), timeout=1)
    assert bloquear_rede == [("127.0.0.1", porta_local)]


def test_urllib_nao_mascara_como_oserror(bloquear_rede, porta_local):
    # RedeBloqueadaError não é OSError: atravessa o urllib em vez de virar URLError.
    assert not issubclass(RedeBloqueadaError, OSError)
    with pytest.raises(RedeBloqueadaError):
        urllib.request.urlopen(f"http://127.0.0.1:{porta_local}/", timeout=1)


def test_af_unix_permitido(bloquear_rede):
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        a.sendall(b"ok")
        assert b.recv(2) == b"ok"
    finally:
        a.close()
        b.close()
    assert bloquear_rede == []


def test_sem_fixture_socket_original_restaurado():
    # Este teste não pede a fixture: o monkeypatch dos testes anteriores foi desfeito.
    assert socket.socket.connect is _CONNECT_ORIGINAL
    assert socket.create_connection is _CREATE_CONNECTION_ORIGINAL


def test_import_da_versao_autouse_ativa_a_guarda(pytester):
    pytester.makepyfile(
        test_modulo_coleta="""
        import socket

        import pytest

        from tests.guarda_rede import RedeBloqueadaError, guarda_rede_autouse  # noqa: F401


        def test_rede_bloqueada_sem_pedir_fixture():
            with pytest.raises(RedeBloqueadaError):
                socket.create_connection(("127.0.0.1", 9), timeout=1)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                with pytest.raises(RedeBloqueadaError):
                    s.connect(("127.0.0.1", 9))
        """
    )
    resultado = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider", "-p", "no:asyncio")
    resultado.assert_outcomes(passed=1)
