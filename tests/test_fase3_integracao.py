import os

import pytest

from sql_generation.gerar_sql import gerar_sql
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from sql_generation.montar_decisao import montar_decisao_merge
from tools.fk_introspection import introspeccao_fk
from tools.group_fetch import buscar_grupo, resolver_brand_id

_RUN_LLM_TESTS = (
    os.environ.get("ESTAGIARIO_RUN_LLM_TESTS") == "1"
    and bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
)
_RUN_DB_TESTS = os.environ.get("ESTAGIARIO_RUN_DB_TESTS") == "1"
_SKIP_REASON = (
    "Teste de integração real (DB + chamada de API à Anthropic) — desligado por padrão, "
    "custa dinheiro e depende de rede. Rode com ESTAGIARIO_RUN_LLM_TESTS=1 e "
    "ESTAGIARIO_RUN_DB_TESTS=1, com ANTHROPIC_API_KEY configurada. Só gera e inspeciona "
    "o texto do SQL — nunca executa nada contra o banco."
)

pytestmark = pytest.mark.skipif(not (_RUN_LLM_TESTS and _RUN_DB_TESTS), reason=_SKIP_REASON)

_GRUPOS_SPEC = [
    ("83061", "CITROEN"),
    ("83062", "PEUGEOT"),
    ("MB1085", "AFFINIA"),
]


def test_pipeline_completo_gera_sql_sintaticamente_coerente():
    from llm.anthropic_provider import AnthropicProvider
    from partitioning.particionar import particionar_grupo

    llm = AnthropicProvider()
    dependencias_fk = introspeccao_fk("catalog_part", "id")

    for search_ref, brand in _GRUPOS_SPEC:
        brand_id = resolver_brand_id(brand)
        grupo = buscar_grupo(search_ref, brand_id)
        particao = particionar_grupo(grupo, llm=llm)
        subclusters_duplicata = [s for s in particao.subclusters if s.label == "duplicata_real"]
        if subclusters_duplicata:
            break
    else:
        pytest.skip(
            "nenhum dos 3 grupos da SPEC.md §8 rendeu um subcluster duplicata_real nesta "
            "rodada (particionamento não é determinístico) — rode de novo."
        )

    por_id = {r.id: r for r in grupo}
    decisoes = [
        montar_decisao_merge(
            f"{search_ref}:{brand}",
            [por_id[i] for i in subcluster.membro_ids],
            llm=llm, rule_store=None, brand_id=brand_id,
        )
        for subcluster in subclusters_duplicata
    ]
    decisoes = [d for d in decisoes if d is not None]
    if not decisoes:
        pytest.skip("todos os subclusters duplicata_real tinham 1 membro só — nada pra mesclar nesta rodada.")

    script = gerar_sql(decisoes, dependencias_fk)

    assert script.count("BEGIN;") == script.count("COMMIT;")
    assert script.count("BEGIN;") == sum(1 for d in decisoes if isinstance(d, DecisaoMerge))
    if any(isinstance(d, GrupoSinalizado) for d in decisoes):
        assert "REVISÃO MANUAL" in script

    for merge in (d for d in decisoes if isinstance(d, DecisaoMerge)):
        assert f"WHERE id = {merge.vencedor_id}" in script or "-- REVISAR MANUALMENTE" in script
        posicao_delete = script.index("DELETE FROM catalog_part WHERE id IN")
        for dep in dependencias_fk:
            trecho_update_fk = f'UPDATE "{dep.tabela_dependente}" SET "{dep.coluna_fk}" = {merge.vencedor_id}'
            if trecho_update_fk in script:
                assert script.index(trecho_update_fk) < posicao_delete
