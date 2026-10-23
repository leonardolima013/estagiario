"""`tools.db_query.sessao_banco`: uma conexão por sessão, aberta só no primeiro uso.

`psycopg.connect` é trocado por um fake; nada aqui abre conexão de verdade.
"""

from __future__ import annotations

import threading

import psycopg
import pytest

import tools.db_query as db_query
import tools.reliability as reliability


class _CursorFalso:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._linhas = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        texto = query if isinstance(query, str) else "SET"
        self._conn.executadas.append(texto)
        if texto.lstrip().upper().startswith("SELECT"):
            if self._conn.falhar_com is not None:
                erro, self._conn.falhar_com = self._conn.falhar_com, None
                self._conn.broken = True
                raise erro
            self.description = [("valor",)]
            self._linhas = [(params,)]

    def fetchmany(self, n):
        return self._linhas[:n]


class _ConexaoFalsa:
    def __init__(self):
        self.closed = False
        self.broken = False
        self.read_only = False
        self.executadas = []
        self.rollbacks = 0
        self.falhar_com = None

    def cursor(self):
        return _CursorFalso(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def conexoes(monkeypatch):
    monkeypatch.setattr(db_query, "replica_connection_params", lambda: {})
    abertas: list[_ConexaoFalsa] = []

    def conectar(**kwargs):
        conn = _ConexaoFalsa()
        abertas.append(conn)
        return conn

    monkeypatch.setattr(psycopg, "connect", conectar)
    return abertas


def test_fora_de_sessao_cada_consulta_abre_e_fecha_uma_conexao(conexoes):
    db_query.consultar_banco("SELECT 1")
    db_query.consultar_banco("SELECT 2")

    assert len(conexoes) == 2
    assert all(c.closed and c.read_only for c in conexoes)


def test_sessao_reaproveita_uma_conexao_e_encerra_cada_transacao(conexoes):
    with db_query.sessao_banco() as sessao:
        for i in range(5):
            assert db_query.consultar_banco("SELECT %(x)s", {"x": i}).rows == [({"x": i},)]
        assert len(conexoes) == 1
        assert not conexoes[0].closed

    conn = conexoes[0]
    assert conn.closed
    assert conn.read_only
    assert conn.rollbacks == 5  # uma transação curta por consulta
    assert sessao.conexoes_abertas == 1


def test_sessao_sem_consulta_nao_abre_conexao(conexoes):
    with db_query.sessao_banco():
        pass
    assert conexoes == []


def test_conexao_perdida_e_substituida_na_proxima_consulta(conexoes):
    with db_query.sessao_banco():
        db_query.consultar_banco("SELECT 1")
        conexoes[0].falhar_com = psycopg.OperationalError("servidor caiu")
        with pytest.raises(psycopg.OperationalError):
            db_query.consultar_banco("SELECT 2")
        db_query.consultar_banco("SELECT 3")

    assert len(conexoes) == 2
    assert all(c.closed for c in conexoes)


def test_outra_thread_nao_usa_a_conexao_da_sessao(conexoes):
    with db_query.sessao_banco():
        db_query.consultar_banco("SELECT 1")
        thread = threading.Thread(target=lambda: db_query.consultar_banco("SELECT 2"))
        thread.start()
        thread.join()
        db_query.consultar_banco("SELECT 3")

    # A thread abriu (e fechou) a própria conexão; a da sessão atendeu as outras duas.
    assert len(conexoes) == 2
    assert conexoes[0].executadas.count("SELECT 1") == 1
    assert "SELECT 2" in conexoes[1].executadas


def test_sessoes_aninhadas_reaproveitam_a_de_fora(conexoes):
    with db_query.sessao_banco() as externa:
        with db_query.sessao_banco() as interna:
            db_query.consultar_banco("SELECT 1")
        assert interna is externa
        assert not conexoes[0].closed
        db_query.consultar_banco("SELECT 2")
    assert len(conexoes) == 1


def test_memo_sessao_so_guarda_dentro_da_sessao():
    chamadas = []

    def calcular():
        chamadas.append(1)
        return len(chamadas)

    assert db_query.memo_sessao("k", calcular) == 1
    assert db_query.memo_sessao("k", calcular) == 2  # sem sessão, sem cache
    with db_query.sessao_banco():
        assert db_query.memo_sessao("k", calcular) == 3
        assert db_query.memo_sessao("k", calcular) == 3
    with db_query.sessao_banco():
        assert db_query.memo_sessao("k", calcular) == 4  # cache não passa de uma sessão à outra


def test_cache_da_sessao_tem_teto(monkeypatch):
    monkeypatch.setattr(db_query, "MAX_CACHE_SESSAO", 3)
    with db_query.sessao_banco() as sessao:
        for i in range(5):
            db_query.memo_sessao(i, lambda i=i: i)
        assert list(sessao._cache) == [2, 3, 4]


def test_lookups_de_referencia_da_confiabilidade_ficam_em_cache_na_sessao(monkeypatch):
    consultas = []

    def consultar(query, params=None, **kwargs):
        consultas.append(params)
        return db_query.QueryResult(columns=["manufacturer_id"], rows=[(77,)], truncado=False)

    monkeypatch.setattr(reliability, "consultar_banco", consultar)

    with db_query.sessao_banco():
        assert [reliability.buscar_manufacturer_da_marca(5) for _ in range(4)] == [77] * 4
        reliability.buscar_manufacturer_do_owner(9)
        reliability.buscar_manufacturer_do_owner(9)
    assert consultas == [{"brand_id": 5}, {"owner_id": 9}]

    reliability.buscar_manufacturer_da_marca(5)
    assert len(consultas) == 3  # fora da sessão, consulta de novo
