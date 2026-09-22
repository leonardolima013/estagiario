"""Cascata de arbitragem pra campos numéricos/discretos (SPEC.md §4.2, passo a passo):

1. Lookup de confiabilidade (provider, fallback owner).
2. Empate/sem dado + divergência acima do threshold: julgamento contextual do modelo.
3. Ainda ambíguo (modelo com baixa confiança): escalado pra humano (retorna marcado,
   não bloqueia — TUI de sessão é Fase 4).
"""

from __future__ import annotations

from itertools import combinations

from arbitration.models import DecisaoCampo
from db.rule_store import RuleStore
from llm.provider import LLMProvider
from partitioning.heuristics import sinal_item_distinto
from tools.group_fetch import RegistroCatalogPart
from tools.reliability import FonteCampo, buscar_fonte_atual, nivel_confiabilidade

_CAMPOS_MAGNITUDE = frozenset({"width", "depth", "height", "net_weight", "gross_weight"})
_CAMPOS_CATEGORICOS = frozenset({"ncm", "barcode", "born_at", "deprecated_at"})
_NIVEL_PARA_ORDEM = {"alta": 3, "media": 2, "baixa": 1, None: 0}

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "part_id_escolhido": {"type": "integer"},
        "justificativa": {"type": "string"},
        "confianca": {"type": "string", "enum": ["alta", "baixa"]},
    },
    "required": ["part_id_escolhido", "justificativa", "confianca"],
}

_SYSTEM_PROMPT = """\
Você é o Estagiário, um assistente de dados que decide o valor correto de um \
campo entre registros duplicados de uma peça de autopeças. A confiabilidade \
por fonte não foi suficiente pra decidir sozinha — use julgamento sobre qual \
valor faz mais sentido pro campo e contexto da peça. Se não tiver informação \
suficiente pra decidir com segurança, responda confianca="baixa" — isso \
escala a decisão pra um humano, é a resposta certa quando você não sabe.
"""


class ArbitragemInvalidaError(ValueError):
    """A resposta do modelo aponta um part_id que não está entre os candidatos."""


def _ha_divergencia(campo: str, valores: list, threshold: float) -> bool:
    unicos = list(dict.fromkeys(v for v in valores if v is not None))
    if len(unicos) <= 1:
        return False
    if campo in _CAMPOS_MAGNITUDE:
        return any(sinal_item_distinto(a, b, threshold) for a, b in combinations(unicos, 2))
    return True


def _vencedor_por_confiabilidade(
    registros: list[RegistroCatalogPart],
    campo: str,
    brand_id: int,
    buscar_fonte,
    calcular_nivel,
) -> tuple[object, FonteCampo, str, int] | None:
    niveis = []
    for registro in registros:
        fonte = buscar_fonte(registro.id, campo)
        nivel = calcular_nivel(fonte, brand_id)
        niveis.append((registro, nivel))

    melhor_ordem = max(_NIVEL_PARA_ORDEM[nivel] for _, nivel in niveis)
    if melhor_ordem == 0:
        return None

    candidatos_no_topo = [(r, n) for r, n in niveis if _NIVEL_PARA_ORDEM[n] == melhor_ordem]
    valores_no_topo = {getattr(r, campo) for r, _ in candidatos_no_topo if getattr(r, campo) is not None}
    if len(valores_no_topo) != 1:
        return None

    registro_vencedor, nivel_vencedor = candidatos_no_topo[0]
    return getattr(registro_vencedor, campo), nivel_vencedor, registro_vencedor.name, registro_vencedor.id


def _julgamento_modelo(
    registros: list[RegistroCatalogPart],
    campo: str,
    llm: LLMProvider,
    rule_store: RuleStore | None,
) -> DecisaoCampo:
    linhas = [f"- id={r.id}, name={r.name!r}, {campo}={getattr(r, campo)!r}" for r in registros]
    regras = rule_store.consultar("arbitragem_campo", campo=campo) if rule_store else []
    contexto_regras = (
        "\n".join(f"- {reg.condicao} => {reg.resolucao}" for reg in regras) if regras else ""
    )
    prompt = f"Campo em conflito: {campo}\n\nRegistros candidatos:\n" + "\n".join(linhas)
    if contexto_regras:
        prompt += f"\n\nRegras aprendidas em sessões anteriores:\n{contexto_regras}"
    prompt += "\n\nDecida qual registro tem o valor correto pra esse campo."

    resposta = llm.gerar_json(
        system=_SYSTEM_PROMPT, user=prompt, json_schema=_JSON_SCHEMA, schema_name="decisao_campo"
    )

    ids_validos = {r.id for r in registros}
    if resposta["part_id_escolhido"] not in ids_validos:
        raise ArbitragemInvalidaError(
            f"Modelo escolheu part_id {resposta['part_id_escolhido']!r}, fora do subcluster {sorted(ids_validos)}"
        )

    if resposta["confianca"] == "baixa":
        return DecisaoCampo(
            campo=campo, valor=None, justificativa=resposta["justificativa"],
            fonte="escalado_humano", escalado_humano=True, confianca="baixa",
        )

    registro_escolhido = next(r for r in registros if r.id == resposta["part_id_escolhido"])
    return DecisaoCampo(
        campo=campo,
        valor=getattr(registro_escolhido, campo),
        justificativa=resposta["justificativa"],
        fonte="julgamento_modelo",
        origem_id=registro_escolhido.id,
        confianca=resposta.get("confianca"),
    )


def arbitrar_campo_numerico(
    registros: list[RegistroCatalogPart],
    campo: str,
    llm: LLMProvider,
    rule_store: RuleStore | None,
    brand_id: int,
    threshold_divergencia: float = 0.15,
    buscar_fonte=buscar_fonte_atual,
    calcular_nivel_confiabilidade=nivel_confiabilidade,
) -> DecisaoCampo:
    valores = [getattr(r, campo) for r in registros]

    if not _ha_divergencia(campo, valores, threshold_divergencia):
        valor_unico = next((v for v in valores if v is not None), None)
        return DecisaoCampo(
            campo=campo, valor=valor_unico, justificativa="Sem divergência entre os registros.",
            fonte="sem_conflito", confianca="alta",
        )

    vencedor = _vencedor_por_confiabilidade(
        registros, campo, brand_id, buscar_fonte, calcular_nivel_confiabilidade
    )
    if vencedor is not None:
        valor, nivel, nome_fonte, id_fonte = vencedor
        return DecisaoCampo(
            campo=campo, valor=valor,
            justificativa=f"Fonte de confiabilidade {nivel!r} ({nome_fonte}) decide sozinha.",
            fonte="regra_confiabilidade",
            origem_id=id_fonte,
            confianca=nivel,
        )

    return _julgamento_modelo(registros, campo, llm, rule_store)
