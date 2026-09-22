import os
from datetime import datetime

import pytest

from tools.db_query import QueryResult
from tools.group_fetch import (
    GrupoTruncadoError,
    RegistroCatalogPart,
    buscar_grupo,
    resolver_brand_id,
)
import tools.group_fetch as group_fetch

_RUN_DB_TESTS = os.environ.get("ESTAGIARIO_RUN_DB_TESTS") == "1"
_SKIP_REASON = (
    "Teste de integração contra a réplica de dev real — desligado por padrão. "
    "Rode com ESTAGIARIO_RUN_DB_TESTS=1 apontando .env para a réplica antes de habilitar."
)

_COLUNAS = [
    "id", "search_ref", "brand_id", "brand", "name",
    "width", "depth", "height", "gross_weight", "net_weight",
    "ncm", "barcode", "application", "born_at", "deprecated_at", "similarity_id", "created",
]


def _linha(id_, name):
    return (
        id_, "83061", 1, "CITROEN", name,
        None, None, None, None, None, None, None, None, None, None, None, datetime(2020, 1, 1),
    )


def test_registro_catalog_part_e_dataclass_imutavel():
    registro = RegistroCatalogPart(
        id=1, search_ref="83061", brand_id=1, brand="CITROEN", name="POLIA",
        width=None, depth=None, height=None, gross_weight=None, net_weight=None,
        ncm=None, barcode=None,
        application=None, born_at=None, deprecated_at=None, similarity_id=None,
        created=datetime(2020, 1, 1),
    )
    assert registro.id == 1
    with pytest.raises(AttributeError):
        registro.name = "outro"  # type: ignore[misc]


def test_buscar_grupo_resultado_truncado_levanta_erro(monkeypatch):
    # F-08: grupo grande volta truncado -> falhar explícito, nunca particionar subconjunto.
    def _fake(*args, **kwargs):
        return QueryResult(columns=_COLUNAS, rows=[_linha(1, "POLIA")], truncado=True)

    monkeypatch.setattr(group_fetch, "consultar_banco", _fake)

    with pytest.raises(GrupoTruncadoError):
        buscar_grupo("83061", 1)


def test_buscar_grupo_nao_truncado_retorna_registros(monkeypatch):
    def _fake(*args, **kwargs):
        return QueryResult(columns=_COLUNAS, rows=[_linha(1, "POLIA"), _linha(2, "POLIA DA CORREIA")], truncado=False)

    monkeypatch.setattr(group_fetch, "consultar_banco", _fake)

    grupo = buscar_grupo("83061", 1)

    assert [r.id for r in grupo] == [1, 2]
    assert grupo[0].name == "POLIA"


@pytest.mark.skipif(not _RUN_DB_TESTS, reason=_SKIP_REASON)
def test_buscar_grupo_caso_1_citroen():
    # Caso 1 da SPEC.md §8 — validar manualmente que os 8 ids batem com o esperado.
    brand_id = resolver_brand_id("CITROEN")
    grupo = buscar_grupo("83061", brand_id)
    ids = {r.id for r in grupo}
    assert ids == {3161876, 3291920, 3474053, 3896742, 3903224, 4579664, 4579866, 4583782}
