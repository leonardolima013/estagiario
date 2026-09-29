import os

import pytest

from partitioning.heuristics import sinal_variante_dimensional
from partitioning.particionar import particionar_grupo
from tools.group_fetch import buscar_grupo, resolver_brand_id

_RUN_DB_TESTS = os.environ.get("ESTAGIARIO_RUN_DB_TESTS") == "1"
_RUN_LLM_TESTS = (
    os.environ.get("ESTAGIARIO_RUN_LLM_TESTS") == "1"
    and bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
)
_SKIP_REASON = (
    "Teste de integração real (DB + chamada de API à Anthropic) — desligado por padrão, "
    "custa dinheiro e depende de rede. Rode com ESTAGIARIO_RUN_DB_TESTS=1, "
    "ESTAGIARIO_RUN_LLM_TESTS=1 e ANTHROPIC_API_KEY configurada."
)

pytestmark = pytest.mark.skipif(
    not (_RUN_DB_TESTS and _RUN_LLM_TESTS),
    reason=_SKIP_REASON,
)


def _provider():
    from llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider()


def test_caso_1_citroen_nao_faz_merge_n_a_1():
    # SPEC.md §8, Caso 1: espera-se pelo menos 4 a 6 subclusters, nunca um único
    # subcluster cobrindo o grupo inteiro.
    grupo = buscar_grupo("83061", resolver_brand_id("CITROEN"))
    particao = particionar_grupo(grupo, llm=_provider())

    assert len(particao.subclusters) >= 4
    assert not any(len(s.membro_ids) == len(grupo) for s in particao.subclusters)


def test_caso_2_peugeot_nao_faz_merge_n_a_1():
    # SPEC.md §8, Caso 2: mesmo padrão do Caso 1, marca diferente.
    grupo = buscar_grupo("83062", resolver_brand_id("PEUGEOT"))
    particao = particionar_grupo(grupo, llm=_provider())

    assert len(particao.subclusters) >= 4
    assert not any(len(s.membro_ids) == len(grupo) for s in particao.subclusters)


def test_caso_3_affinia_nao_mescla_sobremedidas_diferentes():
    # SPEC.md §8, Caso 3: nenhum MERGE entre sufixos de sobremedida diferentes.
    # A garantia de segurança real é sobre subclusters duplicata_real (os únicos
    # que seguem para arbitragem/merge na Fase 2, SPEC.md §4.2) — um subcluster
    # variante_dimensional pode legitimamente conter vários sufixos juntos,
    # porque esse rótulo inteiro já é excluído do merge (SPEC.md §2.3). Misturar
    # sufixos ali é uma perda de granularidade (não separa os 2 STD pra virarem
    # duplicata_real entre si), não um erro de segurança de dados.
    grupo = buscar_grupo("MB1085", resolver_brand_id("AFFINIA"))
    particao = particionar_grupo(grupo, llm=_provider())

    por_id = {r.id: r for r in grupo}
    for sub in particao.subclusters:
        if sub.label != "duplicata_real":
            continue
        sufixos = {
            sinal_variante_dimensional(por_id[i].name)
            for i in sub.membro_ids
            if sinal_variante_dimensional(por_id[i].name) is not None
        }
        assert len(sufixos) <= 1, (
            f"subcluster duplicata_real {sub.membro_ids} mistura sobremedidas: {sufixos} "
            "— isso mescla peças fisicamente diferentes."
        )



def test_caso_gb1052_affinia_colapsa_em_um_unico_duplicata_real():
    # Requirement 9 (9.1): registros de mesmo search_ref + Marca que só diferem
    # em especificidade de nome ("jogo de bucha de eixo de comando SPA",
    # "bucha do eixo de comando", "kit de buchas de eixo de comando") devem
    # colapsar em UM ÚNICO subcluster duplicata_real, sem sufixo de variante
    # dimensional divergente nem divergência técnica objetiva. Assim a
    # Arbitragem_De_Nome decide o registro final em vez de o Particionamento
    # desistir separando-os em kit_componente/distinto_nao_classificado.
    #
    # Comportamento esperado do Requirement 9. Como o particionamento por LLM
    # não é determinístico, uma execução isolada pode falhar e exigir rerun;
    # deliberadamente NÃO há lógica de retry aqui (mantém-se simples).
    grupo = buscar_grupo("GB1052", resolver_brand_id("AFFINIA"))
    particao = particionar_grupo(grupo, llm=_provider())

    duplicata_real = [s for s in particao.subclusters if s.label == "duplicata_real"]
    assert len(duplicata_real) == 1, (
        "esperado exatamente um subcluster duplicata_real cobrindo o grupo inteiro, "
        f"obtido: {[(s.label, s.membro_ids) for s in particao.subclusters]}"
    )
    assert len(duplicata_real[0].membro_ids) == len(grupo), (
        "o único subcluster duplicata_real deve cobrir todos os ids do grupo "
        f"(esperado {len(grupo)}, obtido {len(duplicata_real[0].membro_ids)})"
    )
