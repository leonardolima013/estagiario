"""Destilação da resposta humana em regra reutilizável."""

from __future__ import annotations

from llm.provider import LLMProvider
from memory.models import PedidoIntervencao, RegraProposta

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "titulo": {
            "type": "string",
            "description": "Título curto e pesquisável, sem código ou marca específicos.",
        },
        "condicao": {
            "type": "string",
            "description": "Condição geral que pode aparecer em outras peças/grupos.",
        },
        "resolucao": {
            "type": "string",
            "description": "Ação geral a tomar quando a condição ocorrer.",
        },
    },
    "required": ["titulo", "condicao", "resolucao"],
}

_SYSTEM_PROMPT = """\
Você é o Estagiário e transforma uma explicação humana sobre um caso de catálogo
em uma regra reutilizável. Remova código, marca, ids e detalhes acidentais da
regra: ela precisa servir para outra peça com o mesmo padrão. Preserve o
conhecimento de domínio (por exemplo, a diferença entre peça avulsa, kit e
componente) e escreva uma condição e uma resolução operacionais, sem inventar
fatos que o operador não informou. Retorne somente o objeto estruturado pedido.
"""


class DestilacaoInvalidaError(ValueError):
    """A resposta estruturada não contém uma regra utilizável."""


def destilar_regra(
    caso: PedidoIntervencao,
    resposta_humana: str,
    llm: LLMProvider,
) -> RegraProposta:
    """Destila uma resposta livre em título, condição e resolução generalizáveis."""
    if not resposta_humana.strip():
        raise DestilacaoInvalidaError("resposta humana vazia")

    user_prompt = (
        f"Ponto da decisão: {caso.ponto}\n"
        f"Grupo concreto: {caso.grupo_ref}\n"
        f"Código: {caso.search_ref}\n"
        f"Marca: {caso.marca}\n"
        f"Nomes observados: {caso.nomes_conflitantes}\n"
        f"Motivo da intervenção: {caso.motivo}\n"
        f"Contexto da web: {caso.contexto_web or '(nenhum)'}\n"
        f"Resposta do operador: {resposta_humana.strip()}\n\n"
        "Converta a resposta em uma regra agnóstica de peça."
    )
    bruto = llm.gerar_json(
        system=_SYSTEM_PROMPT,
        user=user_prompt,
        json_schema=_JSON_SCHEMA,
        schema_name="destilar_regra_intervencao",
    )

    valores = {campo: bruto.get(campo) for campo in ("titulo", "condicao", "resolucao")}
    ausentes = [campo for campo, valor in valores.items() if not isinstance(valor, str) or not valor.strip()]
    if ausentes:
        raise DestilacaoInvalidaError(
            f"proposta de regra sem campos obrigatórios válidos: {', '.join(ausentes)}"
        )

    return RegraProposta(
        titulo=valores["titulo"].strip(),
        condicao=valores["condicao"].strip(),
        resolucao=valores["resolucao"].strip(),
    )
