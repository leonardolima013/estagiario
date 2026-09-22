import os

import pytest

from tools.group_fetch import buscar_grupo
from tools.sortear_grupo import GrupoSorteado, NenhumGrupoDuplicadoError, sortear_grupo_aleatorio
from tools.db_query import QueryResult

_RUN_DB_TESTS = os.environ.get("ESTAGIARIO_RUN_DB_TESTS") == "1"
_SKIP_REASON = (
    "Teste de integração contra a réplica de dev real — desligado por padrão. "
    "Rode com ESTAGIARIO_RUN_DB_TESTS=1 apontando .env para a réplica antes de habilitar."
)


@pytest.mark.skipif(not _RUN_DB_TESTS, reason=_SKIP_REASON)
def test_sortear_grupo_aleatorio_retorna_grupo_real_com_duplicatas():
    grupo = sortear_grupo_aleatorio()

    assert isinstance(grupo, GrupoSorteado)
    registros = buscar_grupo(grupo.search_ref, grupo.brand_id)
    assert len(registros) > 1
    assert all(r.brand == grupo.brand for r in registros)


def test_sortear_grupo_aplica_exclusoes_parametrizadas(monkeypatch):
    capturado = {}

    def fake_consultar(query, params=None, timeout_seconds=10, max_rows=500):
        capturado.update(query=query, params=params, timeout_seconds=timeout_seconds)
        return QueryResult(columns=["search_ref", "brand_id", "brand"], rows=[("B", 2, "MARCA")], truncado=False)

    monkeypatch.setattr("tools.sortear_grupo.consultar_banco", fake_consultar)

    grupo = sortear_grupo_aleatorio(excluir={("A", 1), ("C", 3)})

    assert grupo == GrupoSorteado("B", 2, "MARCA")
    assert "NOT IN" in capturado["query"]
    assert capturado["params"] == {
        "ex_search_0": "A", "ex_brand_0": 1,
        "ex_search_1": "C", "ex_brand_1": 3,
    }
    assert capturado["timeout_seconds"] == 20


def test_sortear_grupo_sem_linhas_sinaliza_exaustao(monkeypatch):
    monkeypatch.setattr(
        "tools.sortear_grupo.consultar_banco",
        lambda *args, **kwargs: QueryResult(columns=[], rows=[], truncado=False),
    )

    with pytest.raises(NenhumGrupoDuplicadoError):
        sortear_grupo_aleatorio(excluir={("A", 1)})


def test_erro_de_banco_nao_e_confundido_com_exaustao(monkeypatch):
    def falhar(*args, **kwargs):
        raise ConnectionError("banco indisponível")

    monkeypatch.setattr("tools.sortear_grupo.consultar_banco", falhar)

    with pytest.raises(ConnectionError, match="banco indisponível"):
        sortear_grupo_aleatorio()
