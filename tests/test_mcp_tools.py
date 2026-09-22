from dataclasses import dataclass, field

from verification.mcp_tools import executar_tool_call, listar_tools_anthropic


@dataclass
class ToolFake:
    name: str
    description: str | None = None
    input_schema: dict = field(default_factory=dict)


@dataclass
class ListToolsResultFake:
    tools: list


@dataclass
class ContentItemFake:
    type: str
    text: str = ""


@dataclass
class CallToolResultFake:
    content: list
    isError: bool = False


class SessionFake:
    def __init__(self, tools, call_result):
        self._tools = tools
        self._call_result = call_result
        self.chamadas = []

    async def list_tools(self):
        return ListToolsResultFake(tools=self._tools)

    async def call_tool(self, name, arguments):
        self.chamadas.append((name, arguments))
        return self._call_result


async def test_listar_tools_anthropic_mapeia_schema():
    session = SessionFake(
        tools=[ToolFake(name="browser_navigate", description="navega", input_schema={"type": "object"})],
        call_result=None,
    )

    tools = await listar_tools_anthropic(session)

    assert tools == [{"name": "browser_navigate", "description": "navega", "input_schema": {"type": "object"}}]


async def test_listar_tools_anthropic_filtra_run_code_unsafe():
    session = SessionFake(
        tools=[
            ToolFake(name="browser_navigate"),
            ToolFake(name="browser_run_code_unsafe"),
        ],
        call_result=None,
    )

    tools = await listar_tools_anthropic(session)

    assert [t["name"] for t in tools] == ["browser_navigate"]


async def test_executar_tool_call_mapeia_conteudo_textual():
    session = SessionFake(
        tools=[], call_result=CallToolResultFake(content=[ContentItemFake(type="text", text="nome: PIVO SUPERIOR")])
    )

    class BlocoFake:
        id = "t1"
        name = "browser_snapshot"
        input = {}

    resultado = await executar_tool_call(session, BlocoFake())

    assert resultado["tool_use_id"] == "t1"
    assert resultado["content"] == [{"type": "text", "text": "nome: PIVO SUPERIOR"}]
    assert resultado["is_error"] is False
    assert session.chamadas == [("browser_snapshot", {})]


async def test_executar_tool_call_marca_is_error():
    session = SessionFake(tools=[], call_result=CallToolResultFake(content=[], isError=True))

    class BlocoFake:
        id = "t2"
        name = "browser_click"
        input = {"ref": "x"}

    resultado = await executar_tool_call(session, BlocoFake())

    assert resultado["is_error"] is True
    assert resultado["content"] == [{"type": "text", "text": "(sem conteúdo textual)"}]
