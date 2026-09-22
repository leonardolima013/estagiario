"""particionar_grupo (SPEC.md §4.1, §6.1) — divide um grupo em subclusters rotulados
ANTES de qualquer arbitragem de campo (Fase 2).
"""

from __future__ import annotations

from itertools import combinations

from db.rule_store import RuleStore
from llm.provider import LLMProvider
from partitioning.heuristics import (
    sinal_item_distinto,
    sinal_kit_componente,
    sinal_variante_dimensional,
)
from partitioning.models import ROTULOS_VALIDOS, Particao, Subcluster
from tools.group_fetch import RegistroCatalogPart

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "subclusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "enum": sorted(ROTULOS_VALIDOS),
                    },
                    "membro_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                    },
                    "justificativa": {"type": "string"},
                },
                "required": ["label", "membro_ids", "justificativa"],
            },
            "minItems": 1,
        }
    },
    "required": ["subclusters"],
}

_SYSTEM_PROMPT = """\
Você é o Estagiário, um assistente de dados que particiona grupos de peças \
candidatas a duplicata no catálogo de uma autopeças. A chave de agrupamento \
(search_ref + marca) produz falsos positivos: peças genuinamente distintas, \
kits junto com seus próprios componentes, e variantes dimensionais \
(sobremedidas) que não são intercambiáveis.

Sua tarefa: dividir os registros do grupo em subclusters, cada um rotulado \
como exatamente um destes valores:
- duplicata_real: registros que são a mesma peça, apenas cadastrada mais de uma vez.
- kit_componente: um kit e um componente que já faz parte desse kit.
- variante_dimensional: sobremedidas/variantes fisicamente diferentes e não \
substituíveis entre si (ex: STD, 0,25, 0,50, 0,75, 1,00).
- distinto_nao_classificado: peça(s) diferente(s) que não se encaixam nos rótulos acima.

Regras importantes:
- TODO id do grupo precisa aparecer em exatamente um subcluster — nunca repita \
o mesmo id em dois subclusters, nunca esqueça um id.
- Nunca coloque registros com sufixos de sobremedida diferentes no mesmo \
subcluster duplicata_real — isso seria um erro de dados grave.
- Os sinais heurísticos e regras aprendidas fornecidos são apoio, não verdade \
absoluta — use seu julgamento sobre o conteúdo real dos nomes.
- Cadastros de peças frequentemente têm erros de nomenclatura (nome incompleto, \
genérico demais, ou até um qualificador posicional errado como 'superior' em vez \
de 'inferior'). Quando os registros do grupo NÃO têm nenhuma evidência técnica \
objetiva de serem peças diferentes (sem divergência de width/depth/height/peso \
acima do sinal heurístico, sem sufixo de sobremedida) e a única diferença entre \
eles é o nome variar em especificidade ou em algum qualificador (ex: 'PIVO' vs \
'PIVO SUPERIOR' vs 'PIVÔ DE SUSPENSÃO INFERIOR DIREITO/ESQUERDO'), prefira \
duplicata_real — mesmo que um dos nomes pareça contradizer outro nesse \
qualificador — em vez de presumir que são peças fisicamente diferentes só pelo \
texto do nome. A arbitragem de nome (Fase 2, com verificação externa quando \
necessário) é o lugar certo pra resolver qual nome está correto, não o \
particionamento. Reserve distinto_nao_classificado pra quando os nomes descrevem \
categorias de produto claramente diferentes (ex: uma polia avulsa vs um kit de \
reparo vs uma guia) ou quando HÁ divergência técnica objetiva entre os registros.
"""


class ParticaoInvalidaError(ValueError):
    """A resposta do modelo não forma uma partição válida do grupo original."""


def _formatar_registro(registro: RegistroCatalogPart) -> str:
    campos = [
        f"id={registro.id}",
        f"name={registro.name!r}",
        f"width={registro.width}",
        f"depth={registro.depth}",
        f"height={registro.height}",
        f"gross_weight={registro.gross_weight}",
        f"net_weight={registro.net_weight}",
    ]
    return "- " + ", ".join(campos)


def _sinais_heuristicos(grupo: list[RegistroCatalogPart], threshold_divergencia: float) -> list[str]:
    sinais: list[str] = []
    for a, b in combinations(grupo, 2):
        sufixo_a = sinal_variante_dimensional(a.name)
        sufixo_b = sinal_variante_dimensional(b.name)
        if sufixo_a and sufixo_b and sufixo_a != sufixo_b:
            sinais.append(
                f"[variante_dimensional?] id={a.id} (sufixo {sufixo_a}) vs "
                f"id={b.id} (sufixo {sufixo_b}) — sobremedidas diferentes, provavelmente não intercambiáveis"
            )
        if sinal_kit_componente(a.name, b.name):
            sinais.append(f"[kit_componente?] id={a.id} vs id={b.id} — nome de um contém o do outro sem 'KIT'")
        for campo, va, vb in (
            ("width", a.width, b.width),
            ("gross_weight", a.gross_weight, b.gross_weight),
        ):
            if sinal_item_distinto(va, vb, threshold_divergencia):
                sinais.append(
                    f"[item_distinto?] id={a.id} vs id={b.id} — divergência de {campo} "
                    f"acima de {threshold_divergencia:.0%} ({va} vs {vb})"
                )
    return sinais


def _regras_aplicaveis(rule_store: RuleStore | None) -> list[str]:
    if rule_store is None:
        return []
    regras = rule_store.consultar("kit_deteccao") + rule_store.consultar("particionamento")
    return [f"- [{r.categoria}] {r.condicao} => {r.resolucao}" for r in regras]


def _montar_prompt(
    grupo: list[RegistroCatalogPart], sinais: list[str], regras: list[str]
) -> str:
    partes = ["Registros do grupo:", *[_formatar_registro(r) for r in grupo]]
    if sinais:
        partes.append("\nSinais heurísticos (apoio, não decisivos):")
        partes.extend(f"- {s}" for s in sinais)
    if regras:
        partes.append("\nRegras aprendidas em sessões anteriores:")
        partes.extend(regras)
    partes.append("\nProduza a partição completa do grupo acima.")
    return "\n".join(partes)


def _validar_particao(particao_bruta: dict, ids_esperados: set[int]) -> None:
    ids_vistos: list[int] = []
    for sub in particao_bruta.get("subclusters", []):
        if sub.get("label") not in ROTULOS_VALIDOS:
            raise ParticaoInvalidaError(f"Rótulo inválido: {sub.get('label')!r}")
        ids_vistos.extend(sub.get("membro_ids", []))

    ids_faltando = ids_esperados - set(ids_vistos)
    if ids_faltando:
        raise ParticaoInvalidaError(f"ids do grupo ausentes na partição: {sorted(ids_faltando)}")

    ids_desconhecidos = set(ids_vistos) - ids_esperados
    if ids_desconhecidos:
        raise ParticaoInvalidaError(f"ids que não pertencem ao grupo: {sorted(ids_desconhecidos)}")

    duplicados = {i for i in ids_vistos if ids_vistos.count(i) > 1}
    if duplicados:
        raise ParticaoInvalidaError(f"ids duplicados entre subclusters: {sorted(duplicados)}")


def particionar_grupo(
    grupo: list[RegistroCatalogPart],
    llm: LLMProvider,
    rule_store: RuleStore | None = None,
    threshold_divergencia: float = 0.15,
    max_tentativas: int = 3,
) -> Particao:
    if not grupo:
        raise ValueError("grupo vazio")

    grupo_ref = f"{grupo[0].search_ref}:{grupo[0].brand}"
    ids_esperados = {r.id for r in grupo}

    sinais = _sinais_heuristicos(grupo, threshold_divergencia)
    regras = _regras_aplicaveis(rule_store)
    prompt = _montar_prompt(grupo, sinais, regras)

    # Modelos baratos (ex: Haiku) ocasionalmente erram a validação estrutural
    # (id duplicado/faltando) sem errar o julgamento em si — vale re-perguntar
    # apontando o erro antes de desistir, mais barato que subir pra um modelo maior.
    resposta = None
    ultimo_erro: ParticaoInvalidaError | None = None
    for _tentativa in range(max_tentativas):
        user_prompt = prompt
        if ultimo_erro is not None:
            user_prompt = (
                f"{prompt}\n\nSua resposta anterior era inválida: {ultimo_erro}\n"
                "Corrija e responda de novo — cada id do grupo precisa aparecer em "
                "exatamente um subcluster, nenhum a mais, nenhum a menos."
            )
        resposta = llm.gerar_json(
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            json_schema=_JSON_SCHEMA,
            schema_name="particao",
        )
        try:
            _validar_particao(resposta, ids_esperados)
            break
        except ParticaoInvalidaError as exc:
            ultimo_erro = exc
    else:
        raise ultimo_erro

    subclusters = [
        Subcluster(
            label=sub["label"],
            membro_ids=list(sub["membro_ids"]),
            justificativa=sub["justificativa"],
        )
        for sub in resposta["subclusters"]
    ]
    return Particao(
        grupo_ref=grupo_ref,
        subclusters=subclusters,
        sinais_heuristicos=sinais,
        regras_aplicaveis=regras,
    )
