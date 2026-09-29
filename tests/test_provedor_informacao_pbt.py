"""Testes de propriedade (Hypothesis) da Tool_Provedor_Informacao.

Cobrem as Correctness Properties 1–11 do design `information-provider-tool`
(design.md, seção "Correctness Properties"). Todas as propriedades operam sobre
a LÓGICA PURA — `ORDEM_CONFIABILIDADE`, `nivel_confiabilidade` (com lookups
fake), `resolver_fonte_de_linhas` e `arbitrar_por_provedor` (com `buscar_fonte`/
`calcular_nivel_confiabilidade` fake) — usando FAKES EM MEMÓRIA construídos a
partir dos dados gerados. Nenhum acesso a banco ou LLM.

Separado dos unit tests em tests/test_provedor_informacao.py: aqui só moram as
propriedades universais (≥100 iterações cada, via @settings(max_examples=...)).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from arbitration.provedor_informacao import arbitrar_por_provedor
from tools.reliability import (
    ORDEM_CONFIABILIDADE,
    FonteCampo,
    InfoProvider,
    LinhaAtividade,
    nivel_confiabilidade,
    resolver_fonte_de_linhas,
)

_MAX_EXAMPLES = 200
_CAMPO = "name"
_BRAND_ID = 1
_MANUFACTURER_DA_MARCA = 100

# Níveis classificados (sem o `None`/desconhecido).
_NIVEIS = ["alta", "media", "baixa", "minima"]
# Inclui desconhecido (None) para as propriedades de ordem/ranking.
_NIVEIS_COM_NONE = [*_NIVEIS, None]


# ---------------------------------------------------------------------------
# Fakes puros reaproveitados dos unit tests (test_provedor_informacao.py):
# resolvem fonte e nível a partir de mapas construídos dos dados gerados.
# ---------------------------------------------------------------------------


def _buscar_fonte_por_id(mapa: dict[int, FonteCampo]):
    def _fn(part_id, campo):
        return mapa.get(part_id, FonteCampo(provider_id=None, owner_id=None))

    return _fn


def _nivel_por_provider(mapa_niveis: dict[int | None, str | None]):
    def _fn(fonte, brand_id):
        return mapa_niveis.get(fonte.provider_id)

    return _fn


def _fakes_reliability(*, manufacturer_provider, nome_provider, manufacturer_owner):
    """Constrói os três lookups injetáveis de `nivel_confiabilidade`.

    `nome_provider=None` e `manufacturer_provider=None` juntos => provider não
    resolve (`buscar_info_provider` -> None), como no helper de test_reliability.
    """

    def buscar_info_provider(provider_id):
        if manufacturer_provider is None and nome_provider is None:
            return None
        return InfoProvider(nome=nome_provider, manufacturer_id=manufacturer_provider)

    def buscar_manufacturer_do_owner(owner_id):
        return manufacturer_owner

    def buscar_manufacturer_da_marca(brand_id):
        return _MANUFACTURER_DA_MARCA

    return buscar_info_provider, buscar_manufacturer_do_owner, buscar_manufacturer_da_marca


# ---------------------------------------------------------------------------
# Property 1: Ordem total de confiabilidade
# ---------------------------------------------------------------------------

# Feature: information-provider-tool, Property 1: Para toda dupla de níveis de
# confiabilidade n1, n2 em {alta, media, baixa, minima, desconhecido}, a
# comparação por ORDEM_CONFIABILIDADE é uma ordem total estrita consistente com
# alta > media > baixa > minima > desconhecido; toda fonte minima pesa
# estritamente menos que baixa/media/alta, e qualquer nível classificado pesa
# estritamente mais que desconhecido.
@settings(max_examples=_MAX_EXAMPLES)
@given(
    n1=st.sampled_from(_NIVEIS_COM_NONE),
    n2=st.sampled_from(_NIVEIS_COM_NONE),
)
def test_property_1_ordem_total_de_confiabilidade(n1, n2):
    """**Property 1: Ordem total de confiabilidade**

    **Validates: Requirements 3.5**
    """
    peso1 = ORDEM_CONFIABILIDADE[n1]
    peso2 = ORDEM_CONFIABILIDADE[n2]

    # Ordem total estrita: tricotomia (exatamente uma das relações vale) e
    # anti-simetria consistente com igualdade só quando os níveis são iguais.
    assert (peso1 < peso2) or (peso1 > peso2) or (peso1 == peso2)
    assert (peso1 == peso2) == (n1 == n2)

    # Consistência com a cadeia alta > media > baixa > minima > desconhecido.
    ordem_esperada = {"alta": 4, "media": 3, "baixa": 2, "minima": 1, None: 0}
    assert (peso1 > peso2) == (ordem_esperada[n1] > ordem_esperada[n2])

    # minima estritamente abaixo de baixa/media/alta.
    if n1 == "minima" and n2 in ("baixa", "media", "alta"):
        assert peso1 < peso2
    # qualquer nível classificado > desconhecido (None).
    if n1 is not None and n2 is None:
        assert peso1 > peso2


# ---------------------------------------------------------------------------
# Property 2: Classificação de provider por nome normalizado
# ---------------------------------------------------------------------------

_NOMES_MEDIO = ["FRAGA", "SUIV"]
_NOME_MINIMA = "POSTGRES"
_NOMES_BAIXA = ["ALGUM PROVIDER", "BOSCH", "X", "MARELLI", "acme"]


def _envolver_com_ruido(draw, base: str) -> str:
    """Aplica variações de caixa e espaços nas extremidades preservando o token."""
    espaco_esq = draw(st.text(alphabet=" \t", max_size=3))
    espaco_dir = draw(st.text(alphabet=" \t", max_size=3))
    # varia a caixa caractere a caractere
    caixa = "".join(
        c.upper() if draw(st.booleans()) else c.lower() for c in base
    )
    return f"{espaco_esq}{caixa}{espaco_dir}"


# Feature: information-provider-tool, Property 2: Para todo provider que não é o
# Provedor_Da_Marca, cujo name após strip().upper() é FRAGA ou SUIV o nível é
# media; POSTGRES -> minima; qualquer outro nome não vazio -> baixa. Invariante a
# espaços nas extremidades e caixa.
@settings(max_examples=_MAX_EXAMPLES)
@given(data=st.data())
def test_property_2_classificacao_provider_por_nome_normalizado(data):
    """**Property 2: Classificação de provider por nome normalizado**

    **Validates: Requirements 3.2, 3.3, 3.4**
    """
    categoria = data.draw(st.sampled_from(["media", "minima", "baixa"]))
    if categoria == "media":
        base = data.draw(st.sampled_from(_NOMES_MEDIO))
        esperado = "media"
    elif categoria == "minima":
        base = _NOME_MINIMA
        esperado = "minima"
    else:
        base = data.draw(st.sampled_from(_NOMES_BAIXA))
        esperado = "baixa"

    nome_com_ruido = _envolver_com_ruido(data.draw, base)

    # provider de OUTRA manufacturer (não a da marca), para não cair em `alta`.
    info_fn, owner_fn, marca_fn = _fakes_reliability(
        manufacturer_provider=999, nome_provider=nome_com_ruido, manufacturer_owner=None
    )
    fonte = FonteCampo(provider_id=42, owner_id=None)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == esperado


# ---------------------------------------------------------------------------
# Property 3: Provedor da marca é sempre o mais confiável
# ---------------------------------------------------------------------------

# Feature: information-provider-tool, Property 3: Para toda fonte cujo
# manufacturer_id (provider ou owner) é igual ao manufacturer_id da marca
# resolvível, o nível é alta, independentemente do name normalizado do provider.
@settings(max_examples=_MAX_EXAMPLES)
@given(
    via_provider=st.booleans(),
    nome_provider=st.sampled_from(
        [*_NOMES_MEDIO, _NOME_MINIMA, *_NOMES_BAIXA, "  postgres  ", "FrAgA"]
    ),
)
def test_property_3_provedor_da_marca_e_sempre_alta(via_provider, nome_provider):
    """**Property 3: Provedor da marca é sempre o mais confiável**

    **Validates: Requirements 3.1, 5.2**
    """
    if via_provider:
        # provider cujo manufacturer == manufacturer da marca -> alta,
        # independentemente do nome.
        info_fn, owner_fn, marca_fn = _fakes_reliability(
            manufacturer_provider=_MANUFACTURER_DA_MARCA,
            nome_provider=nome_provider,
            manufacturer_owner=None,
        )
        fonte = FonteCampo(provider_id=42, owner_id=None)
    else:
        # owner cujo manufacturer == manufacturer da marca -> alta (R5.2).
        info_fn, owner_fn, marca_fn = _fakes_reliability(
            manufacturer_provider=None,
            nome_provider=None,
            manufacturer_owner=_MANUFACTURER_DA_MARCA,
        )
        fonte = FonteCampo(provider_id=None, owner_id=7)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == "alta"


# ---------------------------------------------------------------------------
# Property 4: Fonte sem rastreio é desconhecido
# ---------------------------------------------------------------------------

# Feature: information-provider-tool, Property 4: Para toda fonte sem
# current_provider_id e sem current_owner_id rastreável, ou cujo provider não
# resolve e não há owner, ou cujo owner não resolve / não tem manufacturer, ou
# cuja marca não resolve, o nível é desconhecido (None).
@settings(max_examples=_MAX_EXAMPLES)
@given(data=st.data())
def test_property_4_fonte_sem_rastreio_e_desconhecido(data):
    """**Property 4: Fonte sem rastreio é desconhecido**

    **Validates: Requirements 3.6, 5.6**
    """
    caso = data.draw(
        st.sampled_from(
            [
                "sem_provider_sem_owner",
                "provider_nao_resolve_sem_owner",
                "owner_sem_manufacturer",
                "marca_nao_resolve",
            ]
        )
    )

    def marca_resolve(brand_id):
        return _MANUFACTURER_DA_MARCA

    def marca_nao_resolve(brand_id):
        return None

    if caso == "sem_provider_sem_owner":
        # R3.6: nem provider nem owner.
        def info_fn(pid):
            return None

        def owner_fn(oid):
            return None

        fonte = FonteCampo(provider_id=None, owner_id=None)
        marca_fn = marca_resolve
    elif caso == "provider_nao_resolve_sem_owner":
        # provider presente que não resolve, sem owner -> None.
        def info_fn(pid):
            return None

        def owner_fn(oid):
            return None

        fonte = FonteCampo(provider_id=42, owner_id=None)
        marca_fn = marca_resolve
    elif caso == "owner_sem_manufacturer":
        # R5.6: owner presente sem manufacturer resolvível.
        def info_fn(pid):
            return None

        def owner_fn(oid):
            return None

        fonte = FonteCampo(provider_id=None, owner_id=7)
        marca_fn = marca_resolve
    else:  # marca_nao_resolve
        # R5.6: owner com manufacturer conhecido, mas marca não resolve.
        def info_fn(pid):
            return None

        def owner_fn(oid):
            return data.draw(st.integers(min_value=1, max_value=10_000))

        fonte = FonteCampo(provider_id=None, owner_id=7)
        marca_fn = marca_nao_resolve

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel is None


# ---------------------------------------------------------------------------
# Estratégias para linhas de atividade (Properties 5 e 6).
# ---------------------------------------------------------------------------


def _linha(provider_id, owner_id, activity_type, attribute, created) -> LinhaAtividade:
    return LinhaAtividade(
        current_provider_id=provider_id,
        current_owner_id=owner_id,
        activity_type=activity_type,
        attribute=attribute,
        created=created,
    )


@st.composite
def _fontes_ids(draw):
    """Gera um par (provider_id, owner_id) com ao menos um não-nulo."""
    provider_id = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=1000)))
    if provider_id is None:
        owner_id = draw(st.integers(min_value=1, max_value=1000))
    else:
        owner_id = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=1000)))
    return provider_id, owner_id


# ---------------------------------------------------------------------------
# Property 5: Seleção de fonte pela linha mais recente (CRE/UPD)
# ---------------------------------------------------------------------------

# Feature: information-provider-tool, Property 5: Para toda lista de linhas de
# atividade aplicáveis a (part_id, campo) com um único máximo de created, a
# fonte selecionada é exatamente a fonte dessa linha máxima; se essa linha é um
# UPD do campo, é a fonte do UPD, senão a fonte do CRE.
@settings(max_examples=_MAX_EXAMPLES)
@given(data=st.data())
def test_property_5_selecao_pela_linha_mais_recente(data):
    """**Property 5: Seleção de fonte pela linha mais recente (CRE/UPD)**

    **Validates: Requirements 4.1, 4.2, 4.3**
    """
    n = data.draw(st.integers(min_value=1, max_value=6))
    # createds distintos garantem um único máximo (sem empate no topo).
    createds = data.draw(
        st.lists(
            st.integers(min_value=0, max_value=10_000),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    linhas: list[LinhaAtividade] = []
    for created in createds:
        provider_id, owner_id = data.draw(_fontes_ids())
        # o topo (created máximo) modelado como CRE ou UPD do campo; ambos são
        # linhas aplicáveis já filtradas pela query.
        tipo = data.draw(st.sampled_from(["CRE", "UPD"]))
        attribute = _CAMPO if tipo == "UPD" else None
        linhas.append(_linha(provider_id, owner_id, tipo, attribute, created))

    # A query entrega ordenado por created DESC.
    linhas.sort(key=lambda linha: linha.created, reverse=True)
    linha_max = linhas[0]

    fonte = resolver_fonte_de_linhas(linhas)

    assert fonte == FonteCampo(
        provider_id=linha_max.current_provider_id, owner_id=linha_max.current_owner_id
    )


# ---------------------------------------------------------------------------
# Property 6: Empate de timestamp no topo anula o rastreio
# ---------------------------------------------------------------------------

# Feature: information-provider-tool, Property 6: Para toda lista de linhas
# aplicáveis em que duas ou mais linhas compartilham o created máximo, a fonte
# resolvida tem provider e owner ambos ausentes; lista vazia idem.
@settings(max_examples=_MAX_EXAMPLES)
@given(data=st.data())
def test_property_6_empate_no_topo_ou_vazio_anula_rastreio(data):
    """**Property 6: Empate de timestamp no topo anula o rastreio**

    **Validates: Requirements 4.4, 4.5**
    """
    if data.draw(st.booleans()):
        # Caso vazio -> sem fonte (R4.5).
        fonte = resolver_fonte_de_linhas([])
        assert fonte == FonteCampo(provider_id=None, owner_id=None)
        return

    # Empate no topo: 2+ linhas com o mesmo created máximo (R4.4).
    n_topo = data.draw(st.integers(min_value=2, max_value=4))
    created_max = data.draw(st.integers(min_value=100, max_value=10_000))
    linhas: list[LinhaAtividade] = []
    for _ in range(n_topo):
        provider_id, owner_id = data.draw(_fontes_ids())
        tipo = data.draw(st.sampled_from(["CRE", "UPD"]))
        attribute = _CAMPO if tipo == "UPD" else None
        linhas.append(_linha(provider_id, owner_id, tipo, attribute, created_max))

    # linhas mais antigas opcionais, sempre com created menor que o máximo.
    n_antigas = data.draw(st.integers(min_value=0, max_value=3))
    for _ in range(n_antigas):
        created = data.draw(st.integers(min_value=0, max_value=99))
        provider_id, owner_id = data.draw(_fontes_ids())
        linhas.append(_linha(provider_id, owner_id, "CRE", None, created))

    linhas.sort(key=lambda linha: linha.created, reverse=True)

    fonte = resolver_fonte_de_linhas(linhas)

    assert fonte == FonteCampo(provider_id=None, owner_id=None)


# ---------------------------------------------------------------------------
# Estratégias/ajudantes para as propriedades da tool (7–11).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RegSpec:
    """Especificação gerada de um registro: id, valor do campo, e como resolver
    fonte/nível para ele (via provider_id lógico + nível associado)."""

    part_id: int
    valor: str
    provider_id: int | None
    nivel: str | None


def _valor(indice: int) -> str:
    return f"VALOR_{indice}"


def _montar_fakes(specs: list[_RegSpec]):
    """A partir das specs, monta os fakes buscar_fonte e calcular_nivel.

    Cada part_id mapeia para uma FonteCampo(provider_id=...); o nível é resolvido
    por provider_id. Specs com o mesmo provider_id devem ter o mesmo nível (o
    caller garante isso), refletindo que o nível é uma função da fonte.
    """
    mapa_fonte = {
        spec.part_id: FonteCampo(provider_id=spec.provider_id, owner_id=None) for spec in specs
    }
    mapa_nivel: dict[int | None, str | None] = {}
    for spec in specs:
        mapa_nivel[spec.provider_id] = spec.nivel
    return _buscar_fonte_por_id(mapa_fonte), _nivel_por_provider(mapa_nivel)


def _fazer_registro_puro(part_id: int, valor: str):
    """Constrói um RegistroCatalogPart sem depender de fixture de função —
    fixtures function-scoped não compõem com @given do Hypothesis."""
    from datetime import datetime, timedelta

    from tools.group_fetch import RegistroCatalogPart

    return RegistroCatalogPart(
        id=part_id,
        search_ref="83061",
        brand_id=_BRAND_ID,
        brand="CITROEN",
        name=valor,
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
        created=datetime(2020, 1, 1) + timedelta(seconds=part_id),
    )


def _fazer_registros(specs: list[_RegSpec]):
    return [_fazer_registro_puro(spec.part_id, spec.valor) for spec in specs]


# ---------------------------------------------------------------------------
# Property 7: Consenso e registro único são decisão sem conflito idempotente
# ---------------------------------------------------------------------------

# Feature: information-provider-tool, Property 7: Para todo subcluster com um
# único registro, ou em que todos têm o mesmo valor do campo, a tool retorna
# fonte=sem_conflito, escalado_humano=False, com o valor consensual/único, nunca
# inconclusiva; reaplicar produz o mesmo DecisaoCampo (idempotência).
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(data=st.data())
def test_property_7_consenso_e_unico_sem_conflito_idempotente(data):
    """**Property 7: Consenso e registro único são decisão sem conflito idempotente**

    **Validates: Requirements 1.2, 1.3, 7.5**
    """
    n = data.draw(st.integers(min_value=1, max_value=6))
    valor_consensual = _valor(data.draw(st.integers(min_value=0, max_value=50)))
    ids = data.draw(
        st.lists(st.integers(min_value=1, max_value=10_000), min_size=n, max_size=n, unique=True)
    )

    registros = [_fazer_registro_puro(part_id, valor_consensual) for part_id in ids]

    # buscar_fonte/calcular_nivel não devem sequer ser necessários para consenso;
    # passamos fakes que explodiriam se chamados, provando que a decisão é
    # tomada sem ranquear fontes.
    def _fonte_proibida(part_id, campo):  # pragma: no cover - não deve ser chamado
        raise AssertionError("consenso não deve ranquear fontes")

    def _nivel_proibido(fonte, brand_id):  # pragma: no cover
        raise AssertionError("consenso não deve calcular nível")

    decisao = arbitrar_por_provedor(
        registros,
        _CAMPO,
        _BRAND_ID,
        buscar_fonte=_fonte_proibida,
        calcular_nivel_confiabilidade=_nivel_proibido,
    )

    assert decisao.fonte == "sem_conflito"
    assert decisao.escalado_humano is False
    assert decisao.valor == valor_consensual

    # Idempotência: reaplicar sobre o mesmo subcluster dá o mesmo DecisaoCampo.
    decisao2 = arbitrar_por_provedor(
        registros,
        _CAMPO,
        _BRAND_ID,
        buscar_fonte=_fonte_proibida,
        calcular_nivel_confiabilidade=_nivel_proibido,
    )
    assert decisao2 == decisao


# ---------------------------------------------------------------------------
# Property 8: Vencedor único no topo é escolhido e rastreado
# ---------------------------------------------------------------------------

# Feature: information-provider-tool, Property 8: Para todo subcluster divergente
# em que existe exatamente um valor distinto entre as fontes de maior nível
# rastreável, a tool retorna fonte=regra_confiabilidade, escalado_humano=False,
# valor igual a esse valor, origem_id de um registro que o afirma, e confianca
# igual ao nível vencedor.
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(data=st.data())
def test_property_8_vencedor_unico_no_topo(data):
    """**Property 8: Vencedor único no topo é escolhido e rastreado**

    **Validates: Requirements 6.2, 6.3**
    """
    # Constrói um subcluster divergente onde EXATAMENTE UM valor distinto ocupa o
    # nível máximo rastreável.
    nivel_topo = data.draw(st.sampled_from(_NIVEIS))
    ordem_topo = ORDEM_CONFIABILIDADE[nivel_topo]

    valor_vencedor = _valor(data.draw(st.integers(min_value=0, max_value=10)))

    specs: list[_RegSpec] = []
    proximo_id = 1
    proximo_provider = 1

    # 1+ registros no topo, todos afirmando o MESMO valor vencedor. Podem ter
    # o mesmo provider (mesmo provider + mesmo valor não é ambíguo) ou providers
    # distintos.
    n_topo = data.draw(st.integers(min_value=1, max_value=3))
    provider_topo_compartilhado = data.draw(st.booleans())
    pid_topo = proximo_provider
    proximo_provider += 1
    for _ in range(n_topo):
        pid = pid_topo if provider_topo_compartilhado else proximo_provider
        if not provider_topo_compartilhado:
            proximo_provider += 1
        specs.append(_RegSpec(part_id=proximo_id, valor=valor_vencedor, provider_id=pid, nivel=nivel_topo))
        proximo_id += 1

    # registros abaixo do topo (nível estritamente menor, ou desconhecido),
    # afirmando valores DIFERENTES do vencedor — não devem influenciar.
    niveis_abaixo = [n for n in _NIVEIS_COM_NONE if ORDEM_CONFIABILIDADE[n] < ordem_topo]
    if niveis_abaixo:
        n_abaixo = data.draw(st.integers(min_value=0, max_value=4))
        for i in range(n_abaixo):
            nivel_i = data.draw(st.sampled_from(niveis_abaixo))
            valor_i = _valor(100 + i)  # distinto do vencedor
            pid = None if nivel_i is None else proximo_provider
            if nivel_i is not None:
                proximo_provider += 1
            specs.append(_RegSpec(part_id=proximo_id, valor=valor_i, provider_id=pid, nivel=nivel_i))
            proximo_id += 1

    # Garante divergência real (senão vira sem_conflito, fora do escopo desta prop).
    assume(len({spec.valor for spec in specs}) >= 2)

    registros = _fazer_registros(specs)
    buscar_fonte, calcular_nivel = _montar_fakes(specs)

    decisao = arbitrar_por_provedor(
        registros,
        _CAMPO,
        _BRAND_ID,
        buscar_fonte=buscar_fonte,
        calcular_nivel_confiabilidade=calcular_nivel,
    )

    assert decisao.fonte == "regra_confiabilidade"
    assert decisao.escalado_humano is False
    assert decisao.valor == valor_vencedor
    assert decisao.confianca == nivel_topo
    # origem_id deve ser de um registro que afirma o valor vencedor.
    ids_vencedores = {spec.part_id for spec in specs if spec.valor == valor_vencedor}
    assert decisao.origem_id in ids_vencedores


# ---------------------------------------------------------------------------
# Property 9: A tool nunca inventa vencedor quando o topo discorda
# ---------------------------------------------------------------------------

# Feature: information-provider-tool, Property 9: Para todo subcluster divergente
# em que as fontes de maior nível rastreável afirmam 2+ valores distintos, ou
# nenhuma fonte é rastreável, ou o mesmo provider afirma valores distintos, a
# tool retorna escalado_humano=True, valor=None, origem_id=None, preservando os
# valores originais dos registros, e NÃO retorna fonte=regra_confiabilidade.
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(data=st.data())
def test_property_9_nunca_inventa_vencedor_quando_topo_discorda(data):
    """**Property 9: A tool nunca inventa vencedor quando o topo discorda**

    **Validates: Requirements 6.4, 6.5, 7.1, 7.2, 7.3, 7.4**
    """
    caso = data.draw(st.sampled_from(["empate_topo", "sem_fonte", "mesmo_provider_ambiguo"]))
    specs: list[_RegSpec] = []
    proximo_id = 1

    if caso == "empate_topo":
        # 2+ valores DISTINTOS no mesmo nível máximo, providers distintos.
        nivel_topo = data.draw(st.sampled_from(_NIVEIS))
        n_topo = data.draw(st.integers(min_value=2, max_value=4))
        for i in range(n_topo):
            specs.append(
                _RegSpec(part_id=proximo_id, valor=_valor(i), provider_id=100 + i, nivel=nivel_topo)
            )
            proximo_id += 1
    elif caso == "sem_fonte":
        # Divergente mas nenhuma fonte rastreável (todos nível None).
        n = data.draw(st.integers(min_value=2, max_value=4))
        for i in range(n):
            specs.append(_RegSpec(part_id=proximo_id, valor=_valor(i), provider_id=None, nivel=None))
            proximo_id += 1
    else:  # mesmo_provider_ambiguo
        # O MESMO provider_id afirma 2+ valores distintos no topo (R7.3).
        nivel_topo = data.draw(st.sampled_from(_NIVEIS))
        pid = 7
        n = data.draw(st.integers(min_value=2, max_value=4))
        for i in range(n):
            specs.append(_RegSpec(part_id=proximo_id, valor=_valor(i), provider_id=pid, nivel=nivel_topo))
            proximo_id += 1

    valores_originais = {spec.part_id: spec.valor for spec in specs}
    registros = _fazer_registros(specs)
    buscar_fonte, calcular_nivel = _montar_fakes(specs)

    decisao = arbitrar_por_provedor(
        registros,
        _CAMPO,
        _BRAND_ID,
        buscar_fonte=buscar_fonte,
        calcular_nivel_confiabilidade=calcular_nivel,
    )

    assert decisao.escalado_humano is True
    assert decisao.valor is None
    assert decisao.origem_id is None
    assert decisao.fonte != "regra_confiabilidade"
    # valores originais dos registros preservados (a tool não os altera).
    assert {reg.id: reg.name for reg in registros} == valores_originais


# ---------------------------------------------------------------------------
# Property 10: POSTGRES perde para qualquer fonte de nível superior
# ---------------------------------------------------------------------------

# Feature: information-provider-tool, Property 10: Para todo subcluster
# divergente contendo uma fonte POSTGRES (minima) e ao menos uma fonte
# baixa/media/alta com valor distinto, o valor vencedor nunca é o afirmado
# exclusivamente pela fonte minima quando há um nível superior rastreável.
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(data=st.data())
def test_property_10_postgres_perde_para_nivel_superior(data):
    """**Property 10: POSTGRES perde para qualquer fonte de nível superior**

    **Validates: Requirements 3.4, 3.5, 6.1**
    """
    specs: list[_RegSpec] = []
    proximo_id = 1
    proximo_provider = 1

    valor_minima = _valor(0)  # valor afirmado exclusivamente pela fonte minima
    # 1+ fonte(s) minima (POSTGRES) afirmando valor_minima.
    n_minima = data.draw(st.integers(min_value=1, max_value=2))
    for _ in range(n_minima):
        specs.append(
            _RegSpec(part_id=proximo_id, valor=valor_minima, provider_id=proximo_provider, nivel="minima")
        )
        proximo_id += 1
        proximo_provider += 1

    # 1+ fonte(s) de nível superior (baixa/media/alta) com valor DISTINTO.
    nivel_superior = data.draw(st.sampled_from(["baixa", "media", "alta"]))
    n_superior = data.draw(st.integers(min_value=1, max_value=3))
    for i in range(n_superior):
        specs.append(
            _RegSpec(
                part_id=proximo_id,
                valor=_valor(10 + i) if data.draw(st.booleans()) else _valor(10),
                provider_id=proximo_provider,
                nivel=nivel_superior,
            )
        )
        proximo_id += 1
        proximo_provider += 1

    assume(len({spec.valor for spec in specs}) >= 2)

    registros = _fazer_registros(specs)
    buscar_fonte, calcular_nivel = _montar_fakes(specs)

    decisao = arbitrar_por_provedor(
        registros,
        _CAMPO,
        _BRAND_ID,
        buscar_fonte=buscar_fonte,
        calcular_nivel_confiabilidade=calcular_nivel,
    )

    # O valor exclusivo da fonte minima nunca vence quando há nível superior.
    if decisao.fonte == "regra_confiabilidade":
        assert decisao.valor != valor_minima
        assert decisao.confianca == nivel_superior
    else:
        # Se inconclusiva (topo do nível superior discorda), o valor minima
        # tampouco vence — não há vencedor.
        assert decisao.valor is None


# ---------------------------------------------------------------------------
# Property 11: Evidências não vazam segredos e cobrem os divergentes
# ---------------------------------------------------------------------------

_CHAVES_EVIDENCIA = {"part_id", "provider_id", "owner_id", "nivel"}
_TERMOS_PROIBIDOS = ["prompt", "senha", "password", "api_key", "apikey", "chave", "raciocín", "secret", "token"]


# Feature: information-provider-tool, Property 11: Para toda DecisaoCampo
# produzida pela tool, evidencias contém uma entrada por registro divergente
# considerado (fonte+nivel), com chaves exatamente {part_id, provider_id,
# owner_id, nivel}; nenhuma entrada de justificativa/evidencias contém prompt,
# raciocínio, chave ou senha.
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(data=st.data())
def test_property_11_evidencias_nao_vazam_e_cobrem_divergentes(data):
    """**Property 11: Evidências não vazam segredos e cobrem os divergentes**

    **Validates: Requirements 8.5, 8.6**
    """
    # Gera um subcluster divergente arbitrário (qualquer combinação de níveis e
    # valores), garantindo divergência real para que a tool ranqueie fontes.
    n = data.draw(st.integers(min_value=2, max_value=6))
    specs: list[_RegSpec] = []
    for i in range(n):
        nivel = data.draw(st.sampled_from(_NIVEIS_COM_NONE))
        # valor distinto por registro para forçar divergência.
        valor = _valor(i)
        pid = None if nivel is None else 100 + i
        specs.append(_RegSpec(part_id=i + 1, valor=valor, provider_id=pid, nivel=nivel))

    assume(len({spec.valor for spec in specs}) >= 2)

    registros = _fazer_registros(specs)
    buscar_fonte, calcular_nivel = _montar_fakes(specs)

    decisao = arbitrar_por_provedor(
        registros,
        _CAMPO,
        _BRAND_ID,
        buscar_fonte=buscar_fonte,
        calcular_nivel_confiabilidade=calcular_nivel,
    )

    # Uma entrada por registro divergente considerado; chaves exatamente as
    # permitidas — nenhum campo extra que pudesse vazar dado sensível.
    assert len(decisao.evidencias) == len(registros)
    ids_evidencia = set()
    for entrada in decisao.evidencias:
        assert set(entrada.keys()) == _CHAVES_EVIDENCIA
        ids_evidencia.add(entrada["part_id"])
    assert ids_evidencia == {spec.part_id for spec in specs}

    # Nenhum termo sensível na justificativa nem nos valores das evidências.
    justificativa_lower = decisao.justificativa.lower()
    for termo in _TERMOS_PROIBIDOS:
        assert termo not in justificativa_lower
    for entrada in decisao.evidencias:
        texto = " ".join(str(v) for v in entrada.values()).lower()
        for termo in _TERMOS_PROIBIDOS:
            assert termo not in texto
