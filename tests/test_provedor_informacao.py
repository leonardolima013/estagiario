"""Testes unitários da Tool_Provedor_Informacao (arbitration/provedor_informacao.py).

Usa FAKES EM MEMÓRIA (sem banco): injeta `buscar_fonte` e
`calcular_nivel_confiabilidade` como callables fake, no mesmo padrão de
tests/test_arbitrar_campo_numerico.py. Nenhum acesso a Postgres/SQLite.

_Requirements: 1.2, 1.3, 6.2, 6.4, 6.5, 7.1, 7.2, 7.3_
"""

import pytest

from arbitration.provedor_informacao import arbitrar_por_provedor
from tools.reliability import FonteCampo

_BRAND_ID = 1
_CAMPO = "name"


def _buscar_fonte_por_id(mapa: dict[int, FonteCampo]):
    """Fake de buscar_fonte: resolve a FonteCampo por part_id; ausente = sem fonte."""

    def _fn(part_id, campo):
        return mapa.get(part_id, FonteCampo(provider_id=None, owner_id=None))

    return _fn


def _nivel_por_provider(mapa_niveis: dict[int | None, str | None]):
    """Fake de calcular_nivel_confiabilidade: mapeia por fonte.provider_id.

    Segue o padrão de test_arbitrar_campo_numerico.py — cada registro usa um
    provider_id distinto como "id lógico" da fonte, salvo nos casos de mesmo
    provider, onde deliberadamente compartilham o mesmo provider_id.
    """

    def _fn(fonte, brand_id):
        return mapa_niveis.get(fonte.provider_id)

    return _fn


def test_registro_unico_retorna_sem_conflito(fazer_registro):
    registros = [fazer_registro(1, "PASTILHA DE FREIO")]

    decisao = arbitrar_por_provedor(registros, _CAMPO, _BRAND_ID)

    assert decisao.fonte == "sem_conflito"
    assert decisao.valor == "PASTILHA DE FREIO"
    assert decisao.escalado_humano is False
    assert decisao.origem_id is None


def test_consenso_todos_mesmo_valor_retorna_sem_conflito(fazer_registro):
    registros = [
        fazer_registro(1, "PASTILHA DE FREIO"),
        fazer_registro(2, "PASTILHA DE FREIO"),
        fazer_registro(3, "PASTILHA DE FREIO"),
    ]

    decisao = arbitrar_por_provedor(registros, _CAMPO, _BRAND_ID)

    assert decisao.fonte == "sem_conflito"
    assert decisao.valor == "PASTILHA DE FREIO"
    assert decisao.escalado_humano is False
    assert decisao.origem_id is None


def test_vencedor_unico_no_topo_decide_por_confiabilidade(fazer_registro):
    registros = [fazer_registro(1, "PASTILHA A"), fazer_registro(2, "PASTILHA B")]
    buscar_fonte = _buscar_fonte_por_id(
        {1: FonteCampo(provider_id=1, owner_id=None), 2: FonteCampo(provider_id=2, owner_id=None)}
    )
    calcular_nivel = _nivel_por_provider({1: "alta", 2: "baixa"})

    decisao = arbitrar_por_provedor(
        registros, _CAMPO, _BRAND_ID, buscar_fonte=buscar_fonte, calcular_nivel_confiabilidade=calcular_nivel
    )

    assert decisao.fonte == "regra_confiabilidade"
    assert decisao.valor == "PASTILHA A"
    assert decisao.origem_id == 1
    assert decisao.confianca == "alta"
    assert decisao.escalado_humano is False
    # uma entrada de evidência por registro divergente considerado
    assert len(decisao.evidencias) == len(registros)


def test_empate_no_topo_com_valores_distintos_escala(fazer_registro):
    # Dois valores distintos no mesmo nível máximo (ambos "alta") -> inconclusivo.
    registros = [fazer_registro(1, "PASTILHA A"), fazer_registro(2, "PASTILHA B")]
    buscar_fonte = _buscar_fonte_por_id(
        {1: FonteCampo(provider_id=1, owner_id=None), 2: FonteCampo(provider_id=2, owner_id=None)}
    )
    calcular_nivel = _nivel_por_provider({1: "alta", 2: "alta"})

    decisao = arbitrar_por_provedor(
        registros, _CAMPO, _BRAND_ID, buscar_fonte=buscar_fonte, calcular_nivel_confiabilidade=calcular_nivel
    )

    assert decisao.fonte == "escalado_humano"
    assert decisao.escalado_humano is True
    assert decisao.valor is None
    assert decisao.origem_id is None


def test_sem_fonte_rastreavel_e_inconclusivo(fazer_registro):
    # Todos com nível None (fonte não rastreável) -> inconclusivo por ausência.
    registros = [fazer_registro(1, "PASTILHA A"), fazer_registro(2, "PASTILHA B")]
    buscar_fonte = _buscar_fonte_por_id(
        {1: FonteCampo(provider_id=None, owner_id=None), 2: FonteCampo(provider_id=None, owner_id=None)}
    )
    calcular_nivel = _nivel_por_provider({None: None})

    decisao = arbitrar_por_provedor(
        registros, _CAMPO, _BRAND_ID, buscar_fonte=buscar_fonte, calcular_nivel_confiabilidade=calcular_nivel
    )

    assert decisao.fonte == "escalado_humano"
    assert decisao.escalado_humano is True
    assert decisao.valor is None
    assert decisao.origem_id is None


def test_mesmo_provider_com_valores_distintos_e_inconclusivo(fazer_registro):
    # R7.3: o MESMO provider_id afirma dois valores distintos no topo -> inconclusivo.
    registros = [fazer_registro(1, "PASTILHA A"), fazer_registro(2, "PASTILHA B")]
    buscar_fonte = _buscar_fonte_por_id(
        {1: FonteCampo(provider_id=7, owner_id=None), 2: FonteCampo(provider_id=7, owner_id=None)}
    )
    calcular_nivel = _nivel_por_provider({7: "alta"})

    decisao = arbitrar_por_provedor(
        registros, _CAMPO, _BRAND_ID, buscar_fonte=buscar_fonte, calcular_nivel_confiabilidade=calcular_nivel
    )

    assert decisao.fonte == "escalado_humano"
    assert decisao.escalado_humano is True
    assert decisao.valor is None
    assert decisao.origem_id is None


def test_postgres_minima_perde_para_nivel_superior(fazer_registro):
    # POSTGRES (minima) perde para uma fonte de nível superior (baixa).
    registros = [fazer_registro(1, "PASTILHA POSTGRES"), fazer_registro(2, "PASTILHA BAIXA")]
    buscar_fonte = _buscar_fonte_por_id(
        {1: FonteCampo(provider_id=1, owner_id=None), 2: FonteCampo(provider_id=2, owner_id=None)}
    )
    calcular_nivel = _nivel_por_provider({1: "minima", 2: "baixa"})

    decisao = arbitrar_por_provedor(
        registros, _CAMPO, _BRAND_ID, buscar_fonte=buscar_fonte, calcular_nivel_confiabilidade=calcular_nivel
    )

    assert decisao.fonte == "regra_confiabilidade"
    assert decisao.valor == "PASTILHA BAIXA"
    assert decisao.origem_id == 2
    assert decisao.confianca == "baixa"


def test_postgres_minima_perde_para_alta(fazer_registro):
    # POSTGRES (minima) perde para uma fonte de nível bem superior (alta).
    registros = [fazer_registro(1, "PASTILHA POSTGRES"), fazer_registro(2, "PASTILHA ALTA")]
    buscar_fonte = _buscar_fonte_por_id(
        {1: FonteCampo(provider_id=1, owner_id=None), 2: FonteCampo(provider_id=2, owner_id=None)}
    )
    calcular_nivel = _nivel_por_provider({1: "minima", 2: "alta"})

    decisao = arbitrar_por_provedor(
        registros, _CAMPO, _BRAND_ID, buscar_fonte=buscar_fonte, calcular_nivel_confiabilidade=calcular_nivel
    )

    assert decisao.fonte == "regra_confiabilidade"
    assert decisao.valor == "PASTILHA ALTA"
    assert decisao.origem_id == 2
    assert decisao.confianca == "alta"


def test_subcluster_vazio_levanta_valueerror():
    with pytest.raises(ValueError):
        arbitrar_por_provedor([], _CAMPO, _BRAND_ID)


def test_campo_nao_suportado_levanta_valueerror(fazer_registro):
    registros = [fazer_registro(1, "PASTILHA A"), fazer_registro(2, "PASTILHA B")]

    with pytest.raises(ValueError):
        arbitrar_por_provedor(registros, "campo_inexistente", _BRAND_ID)


def test_evidencias_nao_vazam_segredos(fazer_registro):
    # Cada entrada de evidencias só pode conter part_id/provider_id/owner_id/nivel.
    registros = [fazer_registro(1, "PASTILHA A"), fazer_registro(2, "PASTILHA B")]
    buscar_fonte = _buscar_fonte_por_id(
        {1: FonteCampo(provider_id=1, owner_id=None), 2: FonteCampo(provider_id=None, owner_id=99)}
    )
    calcular_nivel = _nivel_por_provider({1: "alta", None: "baixa"})

    decisao = arbitrar_por_provedor(
        registros, _CAMPO, _BRAND_ID, buscar_fonte=buscar_fonte, calcular_nivel_confiabilidade=calcular_nivel
    )

    chaves_permitidas = {"part_id", "provider_id", "owner_id", "nivel"}
    assert decisao.evidencias, "esperava evidências para registros divergentes"
    for entrada in decisao.evidencias:
        assert set(entrada.keys()) == chaves_permitidas
