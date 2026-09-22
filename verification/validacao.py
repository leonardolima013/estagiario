"""Schema + validação da resposta final do sub-agente de verificação web
(SPEC-verificacao-web-nomenclatura.md §6) — o sub-agente só encerra a tarefa
chamando a tool reportar_resultado, forçando o contrato estruturado."""

from __future__ import annotations

from verification.models import FonteWeb, ResultadoVerificacao

REPORTAR_RESULTADO_TOOL = {
    "name": "reportar_resultado",
    "description": (
        "Encerra a tarefa e reporta a conclusão da verificação de nomenclatura. "
        "É a ÚNICA forma de terminar a tarefa — sempre chame esta tool ao final, "
        "mesmo quando a conclusão for inconclusiva."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["confirmado", "inconclusivo"]},
            "nome_sugerido": {"type": ["string", "null"]},
            "fontes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "nome_encontrado": {"type": "string"},
                    },
                    "required": ["url", "nome_encontrado"],
                },
            },
            "justificativa": {"type": "string"},
        },
        "required": ["status", "fontes", "justificativa"],
    },
}


class VerificacaoInvalidaError(ValueError):
    """A resposta do sub-agente não forma um resultado de verificação válido."""


def validar_resultado_bruto(bruto: dict) -> ResultadoVerificacao:
    status = bruto.get("status")
    if status not in ("confirmado", "inconclusivo"):
        raise VerificacaoInvalidaError(f"status inválido: {status!r}")

    nome_sugerido = bruto.get("nome_sugerido")
    if status == "confirmado" and not nome_sugerido:
        raise VerificacaoInvalidaError("status='confirmado' sem nome_sugerido.")
    if status == "inconclusivo":
        # nunca um palpite: descarta qualquer nome_sugerido perdido junto de um
        # status inconclusivo em vez de derrubar o pipeline por isso.
        nome_sugerido = None

    fontes_brutas = bruto.get("fontes") or []
    fontes = []
    for f in fontes_brutas:
        if "url" not in f or "nome_encontrado" not in f:
            raise VerificacaoInvalidaError(f"fonte malformada: {f!r}")
        fontes.append(FonteWeb(url=f["url"], nome_encontrado=f["nome_encontrado"]))

    return ResultadoVerificacao(
        status=status,
        nome_sugerido=nome_sugerido,
        justificativa=bruto.get("justificativa", ""),
        fontes=fontes,
    )
