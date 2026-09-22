"""Tradução mecânica MCP <-> Anthropic: schema de tools na listagem, tool_use ->
tool_result na execução (SPEC-verificacao-web-nomenclatura.md §7).
"""

from __future__ import annotations

from typing import Any

# browser_run_code_unsafe nunca é repassada ao modelo (SPEC §7) — execução de
# código arbitrário na página está fora de qualquer versão desta skill.
TOOLS_EXCLUIDAS = frozenset({"browser_run_code_unsafe"})


async def listar_tools_anthropic(session: Any, excluir: frozenset[str] = TOOLS_EXCLUIDAS) -> list[dict]:
    resultado = await session.list_tools()
    return [
        {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
        for t in resultado.tools
        if t.name not in excluir
    ]


async def executar_tool_call(session: Any, bloco: Any) -> dict:
    """bloco: um content block tool_use da resposta Anthropic (.id, .name, .input)."""
    resultado = await session.call_tool(bloco.name, bloco.input)

    # Só conteúdo textual (ex: snapshot de acessibilidade) volta pro modelo hoje —
    # imagens (ex: screenshot) são descartadas do tool_result; revisitar só se
    # algum passo do fluxo descrito na SPEC precisar depender de imagem.
    conteudo = [
        {"type": "text", "text": item.text}
        for item in resultado.content
        if getattr(item, "type", None) == "text"
    ]

    return {
        "type": "tool_result",
        "tool_use_id": bloco.id,
        "content": conteudo or [{"type": "text", "text": "(sem conteúdo textual)"}],
        "is_error": bool(getattr(resultado, "isError", False)),
    }
