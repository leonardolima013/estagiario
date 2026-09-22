import os

import pytest

from tools.fk_introspection import FkDependency, introspeccao_fk

_RUN_DB_TESTS = os.environ.get("ESTAGIARIO_RUN_DB_TESTS") == "1"
_SKIP_REASON = (
    "Teste de integração contra a réplica de dev real — desligado por padrão. "
    "Rode com ESTAGIARIO_RUN_DB_TESTS=1 apontando .env para a réplica antes de habilitar."
)


def test_fk_dependency_e_um_dataclass_imutavel():
    dep = FkDependency(
        tabela_dependente="catalog_partactivity",
        coluna_fk="part_id",
        nome_constraint="fk_catalog_partactivity_part_id",
    )
    assert dep.tabela_dependente == "catalog_partactivity"
    with pytest.raises(AttributeError):
        dep.coluna_fk = "outra"  # type: ignore[misc]


@pytest.mark.skipif(not _RUN_DB_TESTS, reason=_SKIP_REASON)
def test_introspeccao_fk_catalog_part_id_contra_replica_real():
    # Critério de aceite da Fase 0 (SPEC.md §7): validar manualmente contra o
    # schema real da réplica de dev que as tabelas dependentes retornadas
    # batem com o que se espera do catálogo.
    dependencias = introspeccao_fk("catalog_part", "id")
    assert isinstance(dependencias, list)
    for dep in dependencias:
        assert isinstance(dep, FkDependency)
