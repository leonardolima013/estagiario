"""Aba 'FK introspection': roda introspeccao_fk contra a réplica configurada."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Static

from tools.fk_introspection import introspeccao_fk

_COLUMNS = ("tabela_dependente", "coluna_fk", "nome_constraint")


class FkIntrospectionScreen(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(
            "Descobre, direto no banco, quais tabelas têm uma coluna apontando (chave "
            "estrangeira) pra uma tabela/coluna dada — ou seja, 'se eu apagar isso, quem mais "
            "precisa ser atualizado antes'. É a mesma introspecção que a geração de SQL já usa "
            "internamente pra migrar dependências antes de excluir um registro; aqui é só pra "
            "inspecionar manualmente.",
            classes="panel-explicacao",
        )
        with Horizontal():
            yield Input(placeholder="tabela (ex: catalog_part)", id="input-tabela", value="catalog_part")
            yield Input(placeholder="coluna (ex: id)", id="input-coluna", value="id")
            yield Button("Rodar introspecção", id="btn-fk-run", variant="primary")
        yield Static("", id="fk-error", classes="error-message")
        yield DataTable(id="fk-table")

    def on_mount(self) -> None:
        table = self.query_one("#fk-table", DataTable)
        table.add_columns(*_COLUMNS)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-fk-run":
            self._run_introspection()

    def _run_introspection(self) -> None:
        error_box = self.query_one("#fk-error", Static)
        table = self.query_one("#fk-table", DataTable)
        tabela = self.query_one("#input-tabela", Input).value.strip()
        coluna = self.query_one("#input-coluna", Input).value.strip()

        if not tabela or not coluna:
            error_box.update("Informe tabela e coluna.")
            return

        table.clear()
        try:
            dependencias = introspeccao_fk(tabela, coluna)
        except Exception as exc:  # noqa: BLE001 - erro mostrado inline, TUI não pode cair
            error_box.update(f"Erro: {exc}")
            return

        if not dependencias:
            error_box.update("Nenhuma dependência encontrada.")
            return
        error_box.update("")
        for dep in dependencias:
            table.add_row(dep.tabela_dependente, dep.coluna_fk, dep.nome_constraint)
