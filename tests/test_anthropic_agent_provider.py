"""T-05: cobre a abstração AgentLLMProvider (llm/provider.py) e sua implementação
Anthropic (llm/anthropic_agent_provider.py), e confirma que o caminho do loop de
verificação web (verification/mcp_playwright_agent.py) consome a abstração em vez
de instanciar o SDK da Anthropic diretamente (F-02, Q-01=a). Puro: nenhum SDK,
rede ou subprocess real.
"""

import asyncio

from llm.anthropic_agent_provider import AnthropicAgentProvider


class _FakeMessages:
    def __init__(self, registro):
        self._registro = registro

    def create(self, **kwargs):
        self._registro.append(kwargs)
        return {"content": [], "echo": kwargs}


class _FakeAnthropicClient:
    """Imita anthropic.Anthropic o suficiente pro provider — só .messages.create."""

    def __init__(self):
        self.chamadas = []
        self.messages = _FakeMessages(self.chamadas)


def test_provider_passa_system_mensagens_e_tools_pro_sdk():
    client = _FakeAnthropicClient()
    provider = AnthropicAgentProvider(client=client, model="modelo-x", max_tokens=1234)

    mensagens = [{"role": "user", "content": "oi"}]
    tools = [{"name": "reportar_resultado"}]

    resposta = asyncio.run(
        provider.gerar_resposta_com_tools(system="sys", mensagens=mensagens, tools=tools)
    )

    assert len(client.chamadas) == 1
    chamada = client.chamadas[0]
    assert chamada["model"] == "modelo-x"
    assert chamada["max_tokens"] == 1234
    assert chamada["system"] == "sys"
    assert chamada["messages"] == mensagens
    assert chamada["tools"] == tools
    assert resposta["echo"]["model"] == "modelo-x"


def test_provider_uma_chamada_de_sdk_por_rodada():
    # Restrição de custo/contexto (Q-01=a): o provider não faz nenhum loop próprio —
    # é uma rodada de messages.create por chamada. O orçamento de passos vive no
    # executar_loop_agentico, não aqui.
    client = _FakeAnthropicClient()
    provider = AnthropicAgentProvider(client=client, model="m")

    asyncio.run(provider.gerar_resposta_com_tools(system="s", mensagens=[], tools=[]))
    asyncio.run(provider.gerar_resposta_com_tools(system="s", mensagens=[], tools=[]))

    assert len(client.chamadas) == 2


def test_mcp_playwright_agent_nao_importa_anthropic_no_caminho_do_loop():
    # F-02: o wiring da verificação web não deve mais falar direto com o SDK da
    # Anthropic — quem faz isso é o AnthropicAgentProvider (única fronteira com o SDK).
    import verification.mcp_playwright_agent as agent

    assert not hasattr(agent, "anthropic")
    fonte = __import__("inspect").getsource(agent)
    assert "anthropic.Anthropic" not in fonte
    assert "AgentLLMProvider" in fonte


def test_comando_com_headless_e_rejeitado():
    # T-11 (SPEC §8): modo headed obrigatório — comando com --headless deve ser barrado.
    import pytest

    from verification.mcp_playwright_agent import ModoHeadlessProibidoError, verificar_nomenclatura_peca

    with pytest.raises(ModoHeadlessProibidoError):
        verificar_nomenclatura_peca(
            "JE4699", "DRIVEWAY", ["PIVO", "PIVO SUPERIOR"],
            mcp_command=["npx", "-y", "@playwright/mcp@0.0.81", "--headless"],
        )


def test_guard_headed_aceita_comando_default():
    # O default (sem --headless) passa pelo guard sem erro. Chamamos só o guard
    # diretamente pra não subir o servidor MCP real.
    from verification.mcp_playwright_agent import _garantir_modo_headed

    _garantir_modo_headed(["npx", "-y", "@playwright/mcp@0.0.81"])  # não levanta
