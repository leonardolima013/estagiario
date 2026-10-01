# Feature: stealth-fallback-integration, Property 1: Chave_Stealth e Stealth_Efetivo
"""Property 1 (parte de ``config``): Chave_Stealth.

Para todo texto gerado da variável ``ESTAGIARIO_COLETA_STEALTH_HABILITADA``
(variações de caixa e espaços dos valores reconhecidos, vazio, só espaços,
valores arbitrários e ausência), ``config.coleta_stealth_habilitada()`` devolve
``habilitada`` para 1/true/sim/on, ``desabilitada`` para 0/false/nao/não/off,
vazio ou ausente, e ``invalida`` nos demais casos.

A parte do pipeline (Req 1.4, 1.6) fica em ``tests/test_pipeline_stealth_pbt.py``.

**Validates: Requirements 1.1, 1.2**
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import config
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401

VAR = "ESTAGIARIO_COLETA_STEALTH_HABILITADA"

# Oráculo independente do código de produção (Req 1.1, 1.2).
HABILITACAO = ("1", "true", "sim", "on")
DESABILITACAO = ("0", "false", "nao", "não", "off")


def _oraculo(valor: str | None) -> str:
    if valor is None:
        return "desabilitada"
    normalizado = valor.strip().casefold()
    if normalizado in HABILITACAO:
        return "habilitada"
    if not normalizado or normalizado in DESABILITACAO:
        return "desabilitada"
    return "invalida"


_ESPACOS = st.text(alphabet=" \t\n\r\x0b\x0c", max_size=4)

_CONFIG = settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def _caixa_variada(valor: str) -> st.SearchStrategy[str]:
    """Cada caractere em minúscula ou maiúscula, escolhido pelo Hypothesis."""
    return st.lists(st.booleans(), min_size=len(valor), max_size=len(valor)).map(
        lambda flags: "".join(c.upper() if f else c for c, f in zip(valor, flags))
    )


def _com_ruido(base: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    return st.tuples(_ESPACOS, base.flatmap(_caixa_variada), _ESPACOS).map(
        lambda t: t[0] + t[1] + t[2]
    )


_habilitacao_ruidosa = _com_ruido(st.sampled_from(HABILITACAO))
_desabilitacao_ruidosa = _com_ruido(st.sampled_from(DESABILITACAO))

# Textos aceitos por os.environ (sem NUL nem surrogates).
_texto_env = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    max_size=12,
)
_invalido = _texto_env.filter(lambda s: _oraculo(s) == "invalida")


def _ler_com(valor: str | None) -> str:
    with pytest.MonkeyPatch.context() as mp:
        if valor is None:
            mp.delenv(VAR, raising=False)
        else:
            mp.setenv(VAR, valor)
        return config.coleta_stealth_habilitada()


@_CONFIG
@given(valor=_habilitacao_ruidosa)
def test_valores_de_habilitacao_com_caixa_e_espacos(valor: str) -> None:
    assert _ler_com(valor) == "habilitada"


@_CONFIG
@given(valor=_desabilitacao_ruidosa)
def test_valores_de_desabilitacao_com_caixa_e_espacos(valor: str) -> None:
    assert _ler_com(valor) == "desabilitada"


@_CONFIG
@given(valor=st.one_of(st.none(), _ESPACOS))
def test_ausente_vazia_ou_so_espacos_e_desabilitada(valor: str | None) -> None:
    """Diferente da Chave_Habilitacao da coleta: ausente/vazia desliga (Req 1.2)."""
    assert _ler_com(valor) == "desabilitada"


@_CONFIG
@given(valor=_invalido)
def test_valor_nao_reconhecido_e_invalida(valor: str) -> None:
    assert _ler_com(valor) == "invalida"


@_CONFIG
@given(
    valor=st.one_of(
        st.none(),
        _ESPACOS,
        _habilitacao_ruidosa,
        _desabilitacao_ruidosa,
        _texto_env,
    )
)
def test_chave_stealth_igual_ao_oraculo(valor: str | None) -> None:
    assert _ler_com(valor) == _oraculo(valor)


@_CONFIG
@given(valor=st.one_of(st.none(), _habilitacao_ruidosa, _texto_env))
def test_chave_stealth_independe_da_chave_de_coleta(valor: str | None) -> None:
    """A Chave_Stealth não herda o padrão "habilitada" da Chave_Habilitacao."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("ESTAGIARIO_COLETA_HABILITADA", "1")
        if valor is None:
            mp.delenv(VAR, raising=False)
        else:
            mp.setenv(VAR, valor)
        assert config.coleta_stealth_habilitada() == _oraculo(valor)
