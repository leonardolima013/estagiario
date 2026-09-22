"""Loop agentico do sub-agente de verificação web (SPEC-verificacao-web-nomenclatura.md
§5, §7) — controla passos/orçamento e detecção de finalização, com toda I/O
(chamada ao modelo, execução de tool) injetada como callable. Isso mantém o
controle de fluxo puro e testável com fakes simples, sem SDK/subprocess real
(mesmo espírito de partitioning/heuristics.py, sql_generation/fk_migration.py).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from verification.models import ResultadoVerificacao
from verification.validacao import validar_resultado_bruto

_NOME_TOOL_FINALIZAR = "reportar_resultado"
_MENSAGEM_NUDGE = "Chame reportar_resultado para concluir a tarefa."
_PREVIEW_MAX_CHARS = 200

# Tools de interação que a SPEC §8 exige serem precedidas de browser_highlight
# (o operador precisa ver qual elemento está sendo considerado). Não bloqueamos —
# só emitimos um aviso de observabilidade quando o highlight não veio antes, pra
# não derrubar uma verificação por causa de um passo sem destaque.
_TOOL_HIGHLIGHT = "browser_highlight"
_TOOLS_INTERACAO = frozenset({"browser_click", "browser_type", "browser_press_key", "browser_select_option"})


def _preview(texto: str) -> str:
    texto = texto.strip().replace("\n", " ")
    return texto if len(texto) <= _PREVIEW_MAX_CHARS else texto[:_PREVIEW_MAX_CHARS] + "…"


async def executar_loop_agentico(
    mensagens_iniciais: list[dict],
    gerar_resposta: Callable[[list[dict]], Awaitable[Any]],
    chamar_tool: Callable[[Any], Awaitable[dict]],
    max_passos: int,
    on_evento: Callable[[str], None] | None = None,
) -> ResultadoVerificacao:
    mensagens = list(mensagens_iniciais)
    houve_highlight_recente = False

    for passo in range(1, max_passos + 1):
        if on_evento:
            on_evento(f"passo {passo}/{max_passos}: consultando o modelo...")
        resposta = await gerar_resposta(mensagens)
        mensagens.append({"role": "assistant", "content": resposta.content})

        blocos_texto = [b for b in resposta.content if getattr(b, "type", None) == "text"]
        for bloco_texto in blocos_texto:
            if on_evento and bloco_texto.text.strip():
                on_evento(f"passo {passo}/{max_passos}: modelo disse: {_preview(bloco_texto.text)}")

        blocos_tool_use = [b for b in resposta.content if getattr(b, "type", None) == "tool_use"]

        if not blocos_tool_use:
            if on_evento:
                on_evento(f"passo {passo}/{max_passos}: modelo não chamou nenhuma tool — cobrando reportar_resultado.")
            mensagens.append({"role": "user", "content": _MENSAGEM_NUDGE})
            continue

        finalizar = next((b for b in blocos_tool_use if b.name == _NOME_TOOL_FINALIZAR), None)
        if finalizar is not None:
            if on_evento:
                on_evento(f"passo {passo}/{max_passos}: modelo chamou reportar_resultado — encerrando.")
            return validar_resultado_bruto(finalizar.input)

        resultados_tool = []
        for bloco in blocos_tool_use:
            # Observabilidade SPEC §8: interação sem browser_highlight prévio no fluxo.
            if bloco.name in _TOOLS_INTERACAO and not houve_highlight_recente and on_evento:
                on_evento(
                    f"passo {passo}/{max_passos}: AVISO — {bloco.name} sem browser_highlight "
                    "prévio (SPEC §8 pede destaque antes de interagir)."
                )
            if bloco.name == _TOOL_HIGHLIGHT:
                houve_highlight_recente = True
            elif bloco.name in _TOOLS_INTERACAO:
                houve_highlight_recente = False

            if on_evento:
                on_evento(f"passo {passo}/{max_passos}: chamando {bloco.name}({bloco.input})...")
            try:
                resultado = await chamar_tool(bloco)
            except Exception as exc:  # noqa: BLE001 — falha de uma tool não deve derrubar o loop
                if on_evento:
                    on_evento(f"passo {passo}/{max_passos}: {bloco.name} falhou: {exc}")
                resultado = {
                    "type": "tool_result",
                    "tool_use_id": bloco.id,
                    "content": [{"type": "text", "text": f"Erro ao executar {bloco.name}: {exc}"}],
                    "is_error": True,
                }
            else:
                if on_evento:
                    status = "erro" if resultado.get("is_error") else "ok"
                    trechos = [c.get("text", "") for c in resultado.get("content", []) if c.get("type") == "text"]
                    on_evento(f"passo {passo}/{max_passos}: {bloco.name} -> {status}: {_preview(' '.join(trechos))}")
            resultados_tool.append(resultado)

        mensagens.append({"role": "user", "content": resultados_tool})

    if on_evento:
        on_evento(f"Orçamento de {max_passos} passos esgotado sem confirmação clara — retornando inconclusivo.")
    return ResultadoVerificacao(
        status="inconclusivo",
        nome_sugerido=None,
        justificativa=f"Orçamento de {max_passos} passos esgotado sem confirmação clara.",
        fontes=[],
    )
