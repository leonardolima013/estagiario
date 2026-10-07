"""Tela de menu, logo abaixo do cabeçalho fixo. Por enquanto só tem a opção
"Rodar testes" — Rule store e FK introspection saem da navegação por ora (o
código continua existindo em tui/screens/rule_store_screen.py e
fk_introspection_screen.py, só não está encaixado aqui; ver CLAUDE.md).

Troca de conteúdo via ContentSwitcher, mesma ideia de sempre (um App só,
trocando o que é mostrado) — só a navegação de entrada mudou de Select pra
botão, já que agora só há uma opção.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, ContentSwitcher

from db.rule_store import RuleStore
from loop.executor import executar_loop
from llm.provider import LLMProvider
from pipeline import executar_caso
from tools.fk_introspection import FkDependency
from tui.screens.cabecalho import Cabecalho
from tui.screens.executar_caso_screen import ExecutarCasoScreen
from tui.screens.rodar_loop_screen import RodarLoopScreen

# Telas que usam o cabeçalho compacto para dar espaço ao painel de execução.
TELAS_EXECUCAO = frozenset({"executar-caso", "rodar-loop"})


class MenuPrincipal(Vertical):
    def compose(self) -> ComposeResult:
        yield Button("Rodar testes", id="btn-rodar-testes", variant="primary")
        yield Button("Rodar loop", id="btn-rodar-loop", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-rodar-testes":
            self.app.query_one(ContentSwitcher).current = "executar-caso"
        elif event.button.id == "btn-rodar-loop":
            self.app.query_one(ContentSwitcher).current = "rodar-loop"


class MenuScreen(Vertical):
    def __init__(
        self,
        rule_store: RuleStore,
        llm: LLMProvider,
        dependencias_fk: list[FkDependency],
        executar_caso_fn=executar_caso,
        executar_loop_fn=executar_loop,
        output_dir=None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._rule_store = rule_store
        self._llm = llm
        self._dependencias_fk = dependencias_fk
        self._executar_caso_fn = executar_caso_fn
        self._executar_loop_fn = executar_loop_fn
        self._output_dir = output_dir

    def compose(self) -> ComposeResult:
        with ContentSwitcher(initial="menu-principal"):
            yield MenuPrincipal(id="menu-principal")
            yield ExecutarCasoScreen(
                self._llm, self._dependencias_fk, rule_store=self._rule_store,
                executar_caso_fn=self._executar_caso_fn, id="executar-caso",
            )
            yield RodarLoopScreen(
                self._llm, self._dependencias_fk, rule_store=self._rule_store,
                executar_loop_fn=self._executar_loop_fn, output_dir=self._output_dir, id="rodar-loop",
            )

    def on_mount(self) -> None:
        self.watch(self.query_one(ContentSwitcher), "current", self._ao_trocar_tela)

    def _ao_trocar_tela(self, atual: str | None) -> None:
        for cabecalho in self.app.query(Cabecalho):
            cabecalho.compacto = atual in TELAS_EXECUCAO
