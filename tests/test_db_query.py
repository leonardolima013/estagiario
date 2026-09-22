import os

import pytest

from tools.db_query import QueryValidationError, _validar_select_unico, consultar_banco

_RUN_DB_TESTS = os.environ.get("ESTAGIARIO_RUN_DB_TESTS") == "1"
_SKIP_REASON = (
    "Testes de integração contra a réplica de dev real — desligados por padrão. "
    "Rode com ESTAGIARIO_RUN_DB_TESTS=1 apontando .env para a réplica antes de habilitar."
)


# --- Validação (pura, sem tocar o banco) ---------------------------------


def test_select_simples_passa():
    _validar_select_unico("SELECT 1")


def test_select_com_where_e_join_passa():
    _validar_select_unico(
        "SELECT cp.id FROM catalog_part cp JOIN manufacturer_brand mb "
        "ON mb.id = cp.brand_id WHERE cp.id = 1"
    )


def test_rejeita_delete():
    with pytest.raises(QueryValidationError):
        _validar_select_unico("DELETE FROM catalog_part")


def test_rejeita_insert():
    with pytest.raises(QueryValidationError):
        _validar_select_unico("INSERT INTO catalog_part (name) VALUES ('x')")


def test_rejeita_drop():
    with pytest.raises(QueryValidationError):
        _validar_select_unico("DROP TABLE catalog_part")


def test_rejeita_multiplos_statements_encadeados():
    with pytest.raises(QueryValidationError):
        _validar_select_unico("SELECT * FROM catalog_part; DROP TABLE catalog_part;")


def test_rejeita_cte_com_delete_embutido():
    # Postgres aceita CTEs com DML (WITH x AS (DELETE ... RETURNING *) SELECT * FROM x)
    # como um único statement começando em SELECT — a validação bloqueia CTEs por completo
    # em vez de tentar distinguir CTEs "seguras" de "inseguras".
    with pytest.raises(QueryValidationError):
        _validar_select_unico(
            "WITH removidos AS (DELETE FROM catalog_part RETURNING *) "
            "SELECT * FROM removidos"
        )


def test_select_vazio_rejeitado():
    with pytest.raises(QueryValidationError):
        _validar_select_unico("")


# --- Integração (real, contra a réplica configurada em .env) -------------
# Desligadas por padrão para nunca rodar sem intenção explícita contra um banco real.


@pytest.mark.skipif(not _RUN_DB_TESTS, reason=_SKIP_REASON)
def test_select_1_executa_de_verdade():
    result = consultar_banco("SELECT 1")
    assert result.rows == [(1,)]
    assert not result.truncado


@pytest.mark.skipif(not _RUN_DB_TESTS, reason=_SKIP_REASON)
def test_max_rows_trunca_de_verdade():
    result = consultar_banco("SELECT * FROM generate_series(1, 10) AS s", max_rows=3)
    assert len(result.rows) == 3
    assert result.truncado


@pytest.mark.skipif(not _RUN_DB_TESTS, reason=_SKIP_REASON)
def test_delete_e_rejeitado_antes_de_conectar():
    with pytest.raises(QueryValidationError):
        consultar_banco("DELETE FROM catalog_part")
