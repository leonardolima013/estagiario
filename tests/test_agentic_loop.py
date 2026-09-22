from dataclasses import dataclass, field

from verification.agentic_loop import executar_loop_agentico


@dataclass
class BlocoFake:
    type: str
    name: str = ""
    input: dict = field(default_factory=dict)
    id: str = "tool_1"


@dataclass
class RespostaFake:
    content: list


def _finalizar(status="confirmado", nome_sugerido="X", fontes=None, justificativa="ok"):
    return BlocoFake(
        type="tool_use", name="reportar_resultado", id="finalizar",
        input={"status": status, "nome_sugerido": nome_sugerido, "fontes": fontes or [], "justificativa": justificativa},
    )


async def test_finaliza_imediatamente_ao_chamar_reportar_resultado():
    async def gerar_resposta(mensagens):
        return RespostaFake(content=[_finalizar()])

    async def chamar_tool(bloco):
        raise AssertionError("não deveria chamar nenhuma tool além de reportar_resultado")

    resultado = await executar_loop_agentico([{"role": "user", "content": "oi"}], gerar_resposta, chamar_tool, max_passos=5)

    assert resultado.status == "confirmado"
    assert resultado.nome_sugerido == "X"


async def test_executa_tools_intermediarias_antes_de_finalizar():
    chamadas = []
    respostas = [
        RespostaFake(content=[BlocoFake(type="tool_use", name="browser_navigate", input={"url": "google.com"})]),
        RespostaFake(content=[_finalizar()]),
    ]

    async def gerar_resposta(mensagens):
        return respostas.pop(0)

    async def chamar_tool(bloco):
        chamadas.append(bloco.name)
        return {"type": "tool_result", "tool_use_id": bloco.id, "content": [{"type": "text", "text": "ok"}]}

    resultado = await executar_loop_agentico([{"role": "user", "content": "oi"}], gerar_resposta, chamar_tool, max_passos=5)

    assert chamadas == ["browser_navigate"]
    assert resultado.status == "confirmado"


async def test_multiplos_tool_use_no_mesmo_turno_recebem_tool_result():
    chamadas = []
    respostas = [
        RespostaFake(content=[
            BlocoFake(type="tool_use", name="browser_navigate", id="t1"),
            BlocoFake(type="tool_use", name="browser_highlight", id="t2"),
        ]),
        RespostaFake(content=[_finalizar()]),
    ]

    async def gerar_resposta(mensagens):
        return respostas.pop(0)

    async def chamar_tool(bloco):
        chamadas.append(bloco.id)
        return {"type": "tool_result", "tool_use_id": bloco.id, "content": [{"type": "text", "text": "ok"}]}

    await executar_loop_agentico([{"role": "user", "content": "oi"}], gerar_resposta, chamar_tool, max_passos=5)

    assert chamadas == ["t1", "t2"]


async def test_esgota_orcamento_sem_finalizar_retorna_inconclusivo():
    chamadas_gerar = []

    async def gerar_resposta(mensagens):
        chamadas_gerar.append(1)
        return RespostaFake(content=[BlocoFake(type="tool_use", name="browser_navigate", id=f"t{len(chamadas_gerar)}")])

    async def chamar_tool(bloco):
        return {"type": "tool_result", "tool_use_id": bloco.id, "content": [{"type": "text", "text": "ok"}]}

    resultado = await executar_loop_agentico([{"role": "user", "content": "oi"}], gerar_resposta, chamar_tool, max_passos=3)

    assert len(chamadas_gerar) == 3
    assert resultado.status == "inconclusivo"
    assert resultado.nome_sugerido is None


async def test_falha_de_tool_vira_tool_result_de_erro_sem_derrubar_loop():
    respostas = [
        RespostaFake(content=[BlocoFake(type="tool_use", name="browser_navigate", id="t1")]),
        RespostaFake(content=[_finalizar()]),
    ]
    mensagens_capturadas = []

    async def gerar_resposta(mensagens):
        mensagens_capturadas.append([m for m in mensagens])
        return respostas.pop(0)

    async def chamar_tool(bloco):
        raise RuntimeError("navegação falhou")

    resultado = await executar_loop_agentico([{"role": "user", "content": "oi"}], gerar_resposta, chamar_tool, max_passos=5)

    assert resultado.status == "confirmado"
    ultima_mensagem_usuario = mensagens_capturadas[-1][-1]
    assert ultima_mensagem_usuario["content"][0]["is_error"] is True


async def test_turno_sem_tool_use_consome_um_passo_e_recebe_nudge():
    respostas = [
        RespostaFake(content=[BlocoFake(type="text")]),
        RespostaFake(content=[_finalizar()]),
    ]

    async def gerar_resposta(mensagens):
        return respostas.pop(0)

    async def chamar_tool(bloco):
        raise AssertionError("não deveria chamar tool nenhuma")

    resultado = await executar_loop_agentico([{"role": "user", "content": "oi"}], gerar_resposta, chamar_tool, max_passos=5)

    assert resultado.status == "confirmado"


async def test_interacao_sem_highlight_previo_emite_aviso():
    # T-11/SPEC §8: browser_click sem browser_highlight antes -> aviso de observabilidade.
    respostas = [
        RespostaFake(content=[BlocoFake(type="tool_use", name="browser_click", id="t1", input={"ref": "a"})]),
        RespostaFake(content=[_finalizar()]),
    ]
    eventos = []

    async def gerar_resposta(mensagens):
        return respostas.pop(0)

    async def chamar_tool(bloco):
        return {"type": "tool_result", "tool_use_id": bloco.id, "content": [{"type": "text", "text": "ok"}]}

    await executar_loop_agentico(
        [{"role": "user", "content": "oi"}], gerar_resposta, chamar_tool, max_passos=5, on_evento=eventos.append
    )

    assert any("sem browser_highlight" in e for e in eventos)


async def test_highlight_antes_de_interacao_nao_emite_aviso():
    # highlight seguido de click no mesmo turno -> sem aviso.
    respostas = [
        RespostaFake(content=[
            BlocoFake(type="tool_use", name="browser_highlight", id="h1", input={"ref": "a"}),
            BlocoFake(type="tool_use", name="browser_click", id="t1", input={"ref": "a"}),
        ]),
        RespostaFake(content=[_finalizar()]),
    ]
    eventos = []

    async def gerar_resposta(mensagens):
        return respostas.pop(0)

    async def chamar_tool(bloco):
        return {"type": "tool_result", "tool_use_id": bloco.id, "content": [{"type": "text", "text": "ok"}]}

    await executar_loop_agentico(
        [{"role": "user", "content": "oi"}], gerar_resposta, chamar_tool, max_passos=5, on_evento=eventos.append
    )

    assert not any("sem browser_highlight" in e for e in eventos)
