import os

import pytest

from arbitration.arbitrar import arbitrar_campo
from partitioning.particionar import particionar_grupo
from tools.group_fetch import buscar_grupo, resolver_brand_id

_RUN_LLM_TESTS = (
    os.environ.get("ESTAGIARIO_RUN_LLM_TESTS") == "1"
    and bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
)
_RUN_DB_TESTS = os.environ.get("ESTAGIARIO_RUN_DB_TESTS") == "1"
_SKIP_REASON = (
    "Teste de integração real (DB + chamada de API à Anthropic) — desligado por padrão, "
    "custa dinheiro e depende de rede. Rode com ESTAGIARIO_RUN_LLM_TESTS=1 e "
    "ESTAGIARIO_RUN_DB_TESTS=1, com ANTHROPIC_API_KEY configurada."
)

pytestmark = pytest.mark.skipif(not (_RUN_LLM_TESTS and _RUN_DB_TESTS), reason=_SKIP_REASON)

_CAMPOS_A_ARBITRAR = [
    "width", "depth", "height", "gross_weight", "net_weight",
    "ncm", "barcode", "born_at", "deprecated_at", "application", "name",
]

_GRUPOS_SPEC = [
    ("83061", "CITROEN"),
    ("83062", "PEUGEOT"),
    ("MB1085", "AFFINIA"),
]


def test_arbitragem_end_to_end_num_subcluster_duplicata_real():
    # O particionamento (Fase 1, já validado em tests/test_particionar_grupo_integracao.py)
    # não é determinístico — o modelo pode não render nenhum duplicata_real num grupo
    # específico numa rodada. Tenta os 3 grupos da SPEC.md §8 e usa o primeiro que
    # render algo pra arbitrar; isso testa arbitrar_campo, não a classificação da Fase 1.
    from llm.anthropic_provider import AnthropicProvider

    llm = AnthropicProvider()

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
            "rodada (o particionamento não é determinístico) — rode de novo."
        )

    por_id = {r.id: r for r in grupo}
    for subcluster in subclusters_duplicata:
        registros_subcluster = [por_id[i] for i in subcluster.membro_ids]
        for campo in _CAMPOS_A_ARBITRAR:
            decisao = arbitrar_campo(registros_subcluster, campo, llm=llm, rule_store=None, brand_id=brand_id)
            assert decisao.fonte in (
                "sem_conflito", "regra_confiabilidade", "julgamento_modelo", "normalizacao", "escalado_humano",
            )
