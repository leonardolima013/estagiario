"""TUI do Estagiário — cabeçalho fixo (banner + subtítulo) no topo, com o menu
inicial (trocando entre os modos disponíveis) logo abaixo, sempre visível.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Footer

from config import rule_store_db_path
from db.rule_store import RuleStore
from llm.anthropic_provider import AnthropicProvider
from llm.provider import LLMProvider
from loop.executor import executar_loop
from pipeline import executar_caso
from tools.fk_introspection import FkDependency, introspeccao_fk
from tui.screens.cabecalho import Cabecalho
from tui.screens.menu_screen import MenuScreen

_THEME_PATH = Path(__file__).resolve().parent / "theme.tcss"


class EstagiarioApp(App):
    CSS_PATH = _THEME_PATH
    TITLE = "O Estagiário"

    def __init__(
        self,
        rule_store: RuleStore | None = None,
        llm: LLMProvider | None = None,
        dependencias_fk: list[FkDependency] | None = None,
        executar_caso_fn=executar_caso,
        executar_loop_fn=executar_loop,
        output_dir=None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._rule_store = rule_store or RuleStore(rule_store_db_path())
        self._llm = llm
        self._dependencias_fk = dependencias_fk
        self._executar_caso_fn = executar_caso_fn
        self._executar_loop_fn = executar_loop_fn
        self._output_dir = output_dir

    def compose(self) -> ComposeResult:
        yield Cabecalho()
        llm = self._llm if self._llm is not None else AnthropicProvider()
        dependencias_fk = (
            self._dependencias_fk
            if self._dependencias_fk is not None
            else introspeccao_fk("catalog_part", "id")
        )
        yield MenuScreen(
            self._rule_store, llm, dependencias_fk, executar_caso_fn=self._executar_caso_fn,
            executar_loop_fn=self._executar_loop_fn, output_dir=self._output_dir,
        )
        yield Footer()


def main() -> None:
    EstagiarioApp().run()


if __name__ == "__main__":
    main()
