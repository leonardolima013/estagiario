from datetime import datetime, timedelta

import pytest

from tools.group_fetch import RegistroCatalogPart


@pytest.fixture
def fazer_registro():
    """Factory de RegistroCatalogPart com defaults sensatos — só sobrescreve o que o teste precisa."""

    def _fazer(id_, name, **overrides) -> RegistroCatalogPart:
        campos = dict(
            id=id_,
            search_ref="83061",
            brand_id=1,
            brand="CITROEN",
            name=name,
            width=None,
            depth=None,
            height=None,
            gross_weight=None,
            net_weight=None,
            ncm=None,
            barcode=None,
            application=None,
            born_at=None,
            deprecated_at=None,
            similarity_id=None,
            # cada registro nasce num instante distinto por padrão (id maior = mais novo),
            # sobrescreva explicitamente pra testar empate de desempate por idade.
            created=datetime(2020, 1, 1) + timedelta(seconds=id_),
        )
        campos.update(overrides)
        return RegistroCatalogPart(**campos)

    return _fazer


@pytest.fixture
def sem_banco(monkeypatch):
    """Impede qualquer chamada real ao banco a partir de tools.reliability.

    Sem isso, buscar_fonte_atual/nivel_confiabilidade (usados com seus defaults reais
    por arbitrar_application e arbitrar_campo_numerico) bateriam no Postgres de .env
    mesmo em testes "puros" — qualquer lookup passa a retornar "sem dado" (linhas
    vazias), o que nivel_confiabilidade já trata como confiabilidade desconhecida.
    """
    from tools.db_query import QueryResult

    def _sem_resultado(*args, **kwargs):
        return QueryResult(columns=[], rows=[], truncado=False)

    monkeypatch.setattr("tools.reliability.consultar_banco", _sem_resultado)
