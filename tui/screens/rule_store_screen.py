"""Aba 'Rule store': lista regras existentes e permite registrar novas manualmente.

Implementada como Widget (não `Screen` modal) porque vive dentro de uma aba do
`TabbedContent` da Fase 0 — a Fase 4 reaproveita este mesmo widget/App, apenas
adicionando novos screens/telas para o fluxo de sessão completo.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Static

from db.rule_store import RuleStore

_COLUMNS = (
    "id", "categoria", "campo", "condicao", "resolucao",
    "criado_por", "criado_em", "ativo",
)

_INPUT_IDS = (
    "#input-categoria", "#input-campo", "#input-criado-por",
    "#input-condicao", "#input-resolucao", "#input-grupo-exemplo",
)


class RuleStoreScreen(Vertical):
    def __init__(self, rule_store: RuleStore, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rule_store = rule_store

    def compose(self) -> ComposeResult:
        yield Static(
            "É a memória persistente do Estagiário: regras aprendidas (ex: 'o provider X é "
            "confiável pro campo largura', 'nomes de kit começam com KIT') que o pipeline já "
            "consulta de verdade. Hoje nada escreve aqui sozinho — cadastre manualmente se já "
            "souber de algo; a escrita automática (humano corrigindo uma decisão) chega numa "
            "fase futura.",
            classes="panel-explicacao",
        )
        yield Static("Regras cadastradas", classes="panel-title")
        yield DataTable(id="regras-table")
        yield Static("Registrar nova regra", classes="panel-title")
        with Horizontal():
            yield Input(placeholder="categoria*", id="input-categoria")
            yield Input(placeholder="campo", id="input-campo")
            yield Input(placeholder="criado_por*", id="input-criado-por")
        with Horizontal():
            yield Input(placeholder="condicao*", id="input-condicao")
            yield Input(placeholder="resolucao*", id="input-resolucao")
            yield Input(placeholder="grupo_exemplo_ref", id="input-grupo-exemplo")
        yield Button("Registrar regra", id="btn-registrar", variant="primary")
        yield Static("", id="rule-store-error", classes="error-message")

    def on_mount(self) -> None:
        table = self.query_one("#regras-table", DataTable)
        table.add_columns(*_COLUMNS)
        self._reload_table()

    def _reload_table(self) -> None:
        table = self.query_one("#regras-table", DataTable)
        table.clear()
        for regra in self._rule_store.listar_todas():
            table.add_row(
                str(regra.id), regra.categoria, regra.campo or "", regra.condicao,
                regra.resolucao, regra.criado_por, regra.criado_em, str(regra.ativo),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-registrar":
            self._registrar_regra()

    def _registrar_regra(self) -> None:
        error_box = self.query_one("#rule-store-error", Static)
        categoria = self.query_one("#input-categoria", Input).value.strip()
        campo = self.query_one("#input-campo", Input).value.strip() or None
        criado_por = self.query_one("#input-criado-por", Input).value.strip()
        condicao = self.query_one("#input-condicao", Input).value.strip()
        resolucao = self.query_one("#input-resolucao", Input).value.strip()
        grupo_exemplo_ref = self.query_one("#input-grupo-exemplo", Input).value.strip() or None

        if not categoria or not criado_por or not condicao or not resolucao:
            error_box.update("categoria, criado_por, condicao e resolucao são obrigatórios.")
            return

        self._rule_store.registrar(
            categoria=categoria,
            condicao=condicao,
            resolucao=resolucao,
            criado_por=criado_por,
            campo=campo,
            grupo_exemplo_ref=grupo_exemplo_ref,
        )
        error_box.update("")
        for input_id in _INPUT_IDS:
            self.query_one(input_id, Input).value = ""
        self._reload_table()
