"""Testes de propriedade (Hypothesis) das features de nome e particionamento.

Cobrem as Correctness Properties 12 e 13 do design `information-provider-tool`
(design.md, seção "Correctness Properties"):

- Property 13 (Tarefa 13.3) — calibração de sinais heurísticos de
  `partitioning/particionar.py::_sinais_heuristicos`: diferença de especificidade
  de nome NÃO instrui separação; sufixo de sobremedida divergente e divergência
  técnica objetiva CONTINUAM emitindo os sinais de proteção.
- Property 12 (Tarefa 14.1) — `arbitration/nome.py::arbitrar_nome` com a
  `Tool_Provedor_Informacao` como 1º desempate: vencedor do provedor decide
  `name` sem tocar a cascata; inconclusivo cai na cascata (juiz textual / web).

Toda a lógica é exercitada com FAKES EM MEMÓRIA (provedor injetável, LLM e
verificador web instrumentados) — nenhum acesso a banco ou LLM real. Os
`RegistroCatalogPart` são construídos diretamente (fixtures function-scoped não
compõem com `@given` do Hypothesis).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from arbitration.models import DecisaoCampo
from arbitration.nome import arbitrar_nome
from partitioning.particionar import _sinais_heuristicos
from tools.group_fetch import RegistroCatalogPart

_MAX_EXAMPLES = 200
_BRAND_ID = 7
_THRESHOLD = 0.15

# Marcadores de separação emitidos por _sinais_heuristicos. Property 13 afirma
# que NENHUM deles aparece quando a diferença é só de especificidade de nome.
_MARCADOR_VARIANTE = "[variante_dimensional?]"
_MARCADOR_ITEM_DISTINTO = "[item_distinto?]"
# Rótulo antigo que NÃO deve mais ser emitido como motivo de separação.
_MARCADOR_KIT_ANTIGO = "[kit_componente?]"
# Indício fraco recalibrado — PODE aparecer, não é separação.
_MARCADOR_MESMA_PECA = "[mesma_peca_especificidade?]"


def _fazer_registro(
    part_id: int,
    name: str,
    *,
    width: float | None = None,
    gross_weight: float | None = None,
) -> RegistroCatalogPart:
    """Constrói um RegistroCatalogPart diretamente (sem fixture, pra compor com @given)."""
    return RegistroCatalogPart(
        id=part_id,
        search_ref="GB1052",
        brand_id=_BRAND_ID,
        brand="AFFINIA",
        name=name,
        width=width,
        depth=None,
        height=None,
        gross_weight=gross_weight,
        net_weight=None,
        ncm=None,
        barcode=None,
        application=None,
        born_at=None,
        deprecated_at=None,
        similarity_id=None,
        created=datetime(2020, 1, 1) + timedelta(seconds=part_id),
    )


# ---------------------------------------------------------------------------
# Property 13: Calibração de sinais heurísticos
# ---------------------------------------------------------------------------

# Peça-base neutra: sem sufixo de sobremedida (STD, 0,25...) e sem número que o
# regex de variante (`\b(STD|\d+,\d{2})\s*$`) capturaria no fim do nome.
_BASES_PECA = [
    "bucha do eixo de comando",
    "pivo de suspensao",
    "polia da correia dentada",
    "rolamento da roda dianteira",
    "junta do cabecote",
    "retentor do virabrequim",
]

# Prefixos/qualificadores que só mudam a ESPECIFICIDADE do nome — todos fazem o
# nome-base aparecer como substring (gatilho de sinal_kit_componente quando há
# 'KIT'), sem sufixo de sobremedida nem divergência técnica.
_PREFIXOS_ESPECIFICIDADE = [
    "kit de ",
    "kit ",
    "jogo de ",
    "jogo ",
    "",  # o próprio nome-base, avulso
]


@st.composite
def _grupo_so_especificidade(draw):
    """Gera 2+ registros de mesmo search_ref+marca cujos nomes só diferem em
    especificidade/prefixo KIT/JOGO, SEM sufixo de sobremedida e com campos
    técnicos (width/gross_weight) IGUAIS ou None — sem divergência técnica."""
    base = draw(st.sampled_from(_BASES_PECA))
    n = draw(st.integers(min_value=2, max_value=4))
    prefixos = draw(
        st.lists(st.sampled_from(_PREFIXOS_ESPECIFICIDADE), min_size=n, max_size=n)
    )

    # width/gross_weight IGUAIS entre todos (ou None) — garante ausência de
    # Divergencia_Tecnica_Objetiva independentemente do threshold.
    width_comum = draw(st.one_of(st.none(), st.floats(min_value=1.0, max_value=500.0)))
    peso_comum = draw(st.one_of(st.none(), st.floats(min_value=0.1, max_value=50.0)))

    registros = []
    for i, prefixo in enumerate(prefixos, start=1):
        nome = f"{prefixo}{base}"
        # pelo menos um par precisa de nome-base como substring de outro pra
        # disparar sinal_kit_componente; prefixo "" garante isso quando há 'kit'.
        registros.append(
            _fazer_registro(i, nome, width=width_comum, gross_weight=peso_comum)
        )
    return registros


# Feature: information-provider-tool, Property 13: Para todo grupo cujos
# registros compartilham search_ref+marca e cuja única diferença observável é de
# especificidade de nome (sem sufixo de variante dimensional e sem divergência
# técnica), _sinais_heuristicos NÃO emite sinal de separação — nem
# [variante_dimensional?] nem [item_distinto?], e o antigo [kit_componente?] não
# aparece; complementarmente, sufixo de sobremedida divergente emite
# [variante_dimensional?] e divergência técnica acima do threshold emite
# [item_distinto?] (proteções preservadas).
@settings(max_examples=_MAX_EXAMPLES)
@given(grupo=_grupo_so_especificidade())
def test_property_13_calibracao_so_especificidade_nao_separa(grupo):
    """**Property 13: Calibração de sinais heurísticos preserva proteções e não instrui separação por especificidade**

    **Validates: Requirements 9.1, 9.5**
    """
    sinais = _sinais_heuristicos(grupo, _THRESHOLD)

    # NENHUM sinal instrui separação: sem variante dimensional, sem item distinto,
    # e o rótulo antigo [kit_componente?] não aparece mais como motivo de separar.
    for sinal in sinais:
        assert not sinal.startswith(_MARCADOR_VARIANTE), sinal
        assert not sinal.startswith(_MARCADOR_ITEM_DISTINTO), sinal
        assert _MARCADOR_KIT_ANTIGO not in sinal, sinal


# Feature: information-provider-tool, Property 13 (controle): sufixo de
# sobremedida divergente DEVE emitir [variante_dimensional?] — proteção
# preservada.
@settings(max_examples=_MAX_EXAMPLES)
@given(data=st.data())
def test_property_13_controle_variante_dimensional_ainda_emitida(data):
    """**Property 13: Calibração de sinais heurísticos preserva proteções e não instrui separação por especificidade**

    **Validates: Requirements 9.1, 9.5**
    """
    base = data.draw(st.sampled_from(_BASES_PECA))
    # dois sufixos de sobremedida DISTINTOS entre si (ex.: "STD" vs "0,50").
    # gera sobremedidas numéricas arbitrárias no formato d+,dd além dos rótulos
    # canônicos, ampliando o espaço de entrada da propriedade.
    sufixo_st = st.one_of(
        st.just("STD"),
        st.builds(
            lambda inteiro, frac: f"{inteiro},{frac:02d}",
            st.integers(min_value=0, max_value=9),
            st.integers(min_value=0, max_value=99),
        ),
    )
    sufixos = data.draw(st.lists(sufixo_st, min_size=2, max_size=2, unique=True))
    grupo = [
        _fazer_registro(1, f"{base} {sufixos[0]}"),
        _fazer_registro(2, f"{base} {sufixos[1]}"),
    ]

    sinais = _sinais_heuristicos(grupo, _THRESHOLD)

    assert any(s.startswith(_MARCADOR_VARIANTE) for s in sinais), sinais


# Feature: information-provider-tool, Property 13 (controle): divergência técnica
# objetiva (width com diferença relativa > threshold) DEVE emitir
# [item_distinto?] — proteção preservada.
@settings(max_examples=_MAX_EXAMPLES)
@given(data=st.data())
def test_property_13_controle_item_distinto_ainda_emitido(data):
    """**Property 13: Calibração de sinais heurísticos preserva proteções e não instrui separação por especificidade**

    **Validates: Requirements 9.1, 9.5**
    """
    base = data.draw(st.sampled_from(_BASES_PECA))
    width_a = data.draw(st.floats(min_value=10.0, max_value=100.0))
    # width_b com diferença relativa > threshold em relação a width_a. Escolhe o
    # maior lado como base: |a-b|/max(a,b) > threshold. Usando b = a*(1+fator)
    # com fator > threshold: |a-b|/max = fator/(1+fator); resolvemos pra garantir
    # a razão acima do threshold usando um fator amplo.
    fator = data.draw(st.floats(min_value=0.5, max_value=5.0))
    width_b = width_a * (1.0 + fator)
    # sanity: garante que a divergência relativa realmente excede o threshold.
    maior = max(width_a, width_b)
    assume(abs(width_a - width_b) / maior > _THRESHOLD)

    grupo = [
        _fazer_registro(1, base, width=width_a),
        _fazer_registro(2, base, width=width_b),
    ]

    sinais = _sinais_heuristicos(grupo, _THRESHOLD)

    assert any(s.startswith(_MARCADOR_ITEM_DISTINTO) for s in sinais), sinais


# ---------------------------------------------------------------------------
# Property 12: Tool_Provedor_Informacao como 1º desempate de `name`
# ---------------------------------------------------------------------------


class _LLMInstrumentado:
    """LLM fake que registra se foi chamado. Responde de forma válida ao juiz
    textual de nome (schema `decisao_nome`) escolhendo o 1º id oferecido."""

    def __init__(self, ids_validos: list[int]):
        self.chamado = False
        self._ids_validos = ids_validos

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        self.chamado = True
        return {
            "modo": "escolher",
            "part_id_escolhido": self._ids_validos[0],
            "justificativa": "juiz textual escolheu",
        }


class _VerificadorWebInstrumentado:
    """verificar_web fake que registra se foi chamado e confirma um nome."""

    def __init__(self, nome_sugerido: str):
        self.chamado = False
        self._nome = nome_sugerido

    def __call__(self, codigo, marca, nomes_conflitantes, on_evento=None):
        from verification.models import ResultadoVerificacao

        self.chamado = True
        return ResultadoVerificacao(
            status="confirmado",
            nome_sugerido=self._nome,
            justificativa="verificação web confirmou",
            fontes=[],
        )


def _decisao_provedor_conclusiva(caso: str, valor: str, origem_id: int) -> DecisaoCampo:
    """Monta a DecisaoCampo que o fake `arbitrar_por_provedor` devolve num caso
    conclusivo — vencedor por regra_confiabilidade (R10.2) ou consenso/único
    sem_conflito (R10.5)."""
    if caso == "regra_confiabilidade":
        return DecisaoCampo(
            campo="name",
            valor=valor,
            justificativa="fonte de confiabilidade decide",
            fonte="regra_confiabilidade",
            escalado_humano=False,
            origem_id=origem_id,
            confianca="alta",
        )
    # sem_conflito (único / consenso)
    return DecisaoCampo(
        campo="name",
        valor=valor,
        justificativa="Todos os registros concordam no valor.",
        fonte="sem_conflito",
        escalado_humano=False,
    )


def _decisao_provedor_inconclusiva() -> DecisaoCampo:
    """DecisaoCampo de Arbitragem_Inconclusiva (R10.3): escala sem valor."""
    return DecisaoCampo(
        campo="name",
        valor=None,
        justificativa="Arbitragem por confiabilidade inconclusiva: empate no topo.",
        fonte="escalado_humano",
        escalado_humano=True,
        origem_id=None,
    )


# Nomes convergentes (só diferem em caixa/espaço) → juiz textual (LLM). Nomes
# divergentes de verdade (qualificadores conflitantes) → verificação web.
_NOMES_CONVERGENTES = ["polia da correia", "POLIA DA CORREIA", "polia  da  correia"]
_PARES_DIVERGENTES = [
    ("PIVO SUPERIOR", "PIVO INFERIOR"),
    ("BRACO DIANTEIRO DIREITO", "BRACO DIANTEIRO ESQUERDO"),
    ("SENSOR DE ENTRADA", "SENSOR DE SAIDA"),
]


# Feature: information-provider-tool, Property 12: Para todo campo name em
# subcluster duplicata_real com brand_id resolvível: quando a
# Tool_Provedor_Informacao produz vencedor (regra_confiabilidade) ou consenso/
# único (sem_conflito), arbitrar_nome devolve exatamente essa DecisaoCampo e NÃO
# aciona juiz textual nem verificação web; quando a tool é inconclusiva, a
# cascata existente roda (juiz textual p/ nomes convergentes, verificação web p/
# nomes divergentes).
@settings(max_examples=_MAX_EXAMPLES)
@given(data=st.data())
def test_property_12_provedor_primeiro_desempate_de_name(data):
    """**Property 12: Tool_Provedor_Informacao decide `name` quando há vencedor; senão cai na cascata**

    **Validates: Requirements 10.1, 10.2, 10.3, 10.5**
    """
    resultado_provedor = data.draw(
        st.sampled_from(["regra_confiabilidade", "sem_conflito", "inconclusivo"])
    )

    llm = None  # definido por ramo abaixo
    verificador = None
    if resultado_provedor in ("regra_confiabilidade", "sem_conflito"):
        # Caso CONCLUSIVO: nomes/registros podem ser quaisquer — a cascata não
        # deve rodar de forma alguma. Usamos nomes divergentes de propósito pra
        # provar que nem a web é chamada mesmo com divergência.
        nome_a, nome_b = data.draw(st.sampled_from(_PARES_DIVERGENTES))
        registros = [_fazer_registro(1, nome_a), _fazer_registro(2, nome_b)]
        valor_vencedor = data.draw(st.sampled_from([nome_a, nome_b]))
        origem_id = 1 if valor_vencedor == nome_a else 2

        decisao_fake = _decisao_provedor_conclusiva(
            resultado_provedor, valor_vencedor, origem_id
        )

        llm = _LLMInstrumentado([r.id for r in registros])
        verificador = _VerificadorWebInstrumentado("QUALQUER")

        def fake_arbitrar_por_provedor(regs, campo, brand_id, *a, **k):
            return decisao_fake

        decisao = arbitrar_nome(
            registros,
            llm=llm,
            brand_id=_BRAND_ID,
            verificar_web=verificador,
            arbitrar_por_provedor=fake_arbitrar_por_provedor,
        )

        # A decisão do provedor é a decisão final, sem tocar cascata (R10.2/R10.5).
        assert decisao == decisao_fake
        assert decisao.fonte == resultado_provedor
        assert decisao.escalado_humano is False
        assert llm.chamado is False
        assert verificador.chamado is False
        return

    # Caso INCONCLUSIVO (R10.3): a cascata existente roda. O ramo da cascata é
    # determinístico via escolha dos nomes: convergentes → juiz textual (LLM);
    # divergentes → verificação web.
    ramo = data.draw(st.sampled_from(["juiz_textual", "verificacao_web"]))
    decisao_fake = _decisao_provedor_inconclusiva()

    def fake_arbitrar_por_provedor(regs, campo, brand_id, *a, **k):
        return decisao_fake

    if ramo == "juiz_textual":
        # Nomes convergentes (só caixa/espaço) → NÃO divergem → juiz textual.
        nomes = data.draw(
            st.lists(st.sampled_from(_NOMES_CONVERGENTES), min_size=2, max_size=3)
        )
        registros = [_fazer_registro(i, nome) for i, nome in enumerate(nomes, start=1)]
        llm = _LLMInstrumentado([r.id for r in registros])
        verificador = _VerificadorWebInstrumentado("QUALQUER")

        decisao = arbitrar_nome(
            registros,
            llm=llm,
            brand_id=_BRAND_ID,
            verificar_web=verificador,
            arbitrar_por_provedor=fake_arbitrar_por_provedor,
        )

        # Cascata rodou pelo juiz textual do modelo, não pela web.
        assert llm.chamado is True
        assert verificador.chamado is False
        assert decisao.fonte == "julgamento_modelo"
    else:
        # Nomes divergentes de verdade → verificação web.
        nome_a, nome_b = data.draw(st.sampled_from(_PARES_DIVERGENTES))
        registros = [_fazer_registro(1, nome_a), _fazer_registro(2, nome_b)]
        llm = _LLMInstrumentado([r.id for r in registros])
        verificador = _VerificadorWebInstrumentado(nome_a)

        decisao = arbitrar_nome(
            registros,
            llm=llm,
            brand_id=_BRAND_ID,
            verificar_web=verificador,
            arbitrar_por_provedor=fake_arbitrar_por_provedor,
        )

        # Cascata rodou pela verificação web (nomes divergentes).
        assert verificador.chamado is True
        assert decisao.fonte == "verificacao_web"
