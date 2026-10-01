# Feature: html-extract-on-web-search, Property 14: Chave de habilitação e Coleta_Efetiva
"""Property 14: Chave de habilitação e Coleta_Efetiva.

Para todo texto gerado (variações de caixa e espaços dos valores reconhecidos,
vazio e ausência da variável), ``config.coleta_habilitada()`` devolve
``habilitada`` para os valores de habilitação e para vazio/ausente,
``desabilitada`` para os de desabilitação e ``invalida`` nos demais casos. Para
todo par (opção, estado da chave), ``resolver_coleta_efetiva`` devolve o estado
da opção quando ela não é ``None`` e o estado da chave quando é.

**Validates: Requirements 9.1, 9.2, 9.3**
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import config
from coleta_paginas.integracao import resolver_coleta_efetiva
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401

VAR = "ESTAGIARIO_COLETA_HABILITADA"

# Oráculo independente do código de produção (Req 9.1).
HABILITACAO = ("1", "true", "sim", "on")
DESABILITACAO = ("0", "false", "nao", "não", "off")
RECONHECIDOS = frozenset(HABILITACAO + DESABILITACAO)

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

# Textos arbitrários aceitos por os.environ (sem NUL nem surrogates) cuja forma
# normalizada não é vazia nem reconhecida.
_texto_env = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    max_size=12,
)
_invalido = _texto_env.filter(
    lambda s: s.strip() and s.strip().casefold() not in RECONHECIDOS
)


def _ler_com(valor: str | None) -> str:
    with pytest.MonkeyPatch.context() as mp:
        if valor is None:
            mp.delenv(VAR, raising=False)
        else:
            mp.setenv(VAR, valor)
        return config.coleta_habilitada()


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
def test_ausente_vazia_ou_so_espacos_e_habilitada(valor: str | None) -> None:
    assert _ler_com(valor) == "habilitada"


@_CONFIG
@given(valor=_invalido)
def test_valor_nao_reconhecido_e_invalida(valor: str) -> None:
    assert _ler_com(valor) == "invalida"


@_CONFIG
@given(
    opcao=st.one_of(st.none(), st.booleans()),
    chave=st.sampled_from(["habilitada", "desabilitada", "invalida"]),
)
def test_resolver_coleta_efetiva_opcao_prevalece(opcao: bool | None, chave: str) -> None:
    esperado = chave if opcao is None else ("habilitada" if opcao else "desabilitada")
    assert resolver_coleta_efetiva(opcao, chave) == esperado


@_CONFIG
@given(
    opcao=st.one_of(st.none(), st.booleans()),
    valor=st.one_of(
        st.none(), _ESPACOS, _habilitacao_ruidosa, _desabilitacao_ruidosa, _invalido
    ),
)
def test_coleta_efetiva_a_partir_do_ambiente(opcao: bool | None, valor: str | None) -> None:
    """Composição ponta a ponta: com opção, a chave (inclusive inválida) é ignorada."""
    chave = _ler_com(valor)
    efetiva = resolver_coleta_efetiva(opcao, chave)
    if opcao is None:
        assert efetiva == chave
    else:
        assert efetiva == ("habilitada" if opcao else "desabilitada")
