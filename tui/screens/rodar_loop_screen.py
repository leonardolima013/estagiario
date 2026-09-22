"""Tela Textual para executar o loop autônomo de famílias duplicadas."""

from __future__ import annotations

from threading import Event

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, ContentSwitcher, Input, ProgressBar, RichLog, Static

from config import PROJECT_ROOT
from db.rule_store import RuleStore
from llm.provider import LLMProvider
from loop.executor import executar_loop
from loop.models import LoopConfig, LoopProgresso, ResultadoLoop
from tools.fk_introspection import FkDependency
from tui.intervencao_bridge import pedir_intervencao_bloqueante


class RodarLoopScreen(Vertical):
    def __init__(
        self,
        llm: LLMProvider,
        dependencias_fk: list[FkDependency],
        rule_store: RuleStore | None = None,
        executar_loop_fn=executar_loop,
        output_dir=None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._llm = llm
        self._dependencias_fk = dependencias_fk
        self._rule_store = rule_store
        self._executar_loop_fn = executar_loop_fn
        self._output_dir = output_dir or (PROJECT_ROOT / "output")
        self._cancel_event: Event | None = None
        self._worker = None

    def compose(self) -> ComposeResult:
        yield Button("← Voltar ao menu", id="btn-voltar-loop")
        yield Static(
            "Executa famílias duplicadas sem repetir search_ref + brand_id. "
            "Erros são registrados e o SQL nunca é executado automaticamente.",
            classes="panel-explicacao",
        )
        with Horizontal(classes="loop-controles"):
            yield Input(placeholder="Número de iterações", id="input-iteracoes")
            yield Button("Iniciar loop", id="btn-iniciar-loop", variant="primary")
            yield Button("Cancelar", id="btn-cancelar-loop", disabled=True)
        yield ProgressBar(total=1, show_percentage=True, id="loop-progress")
        yield Static("Aguardando número de iterações.", id="loop-status", classes="status-processando")
        yield Static("", id="loop-contadores", classes="panel-explicacao")
        yield Static("", id="loop-arquivos", classes="panel-explicacao")
        yield RichLog(id="loop-log", classes="loop-log")
        yield VerticalScroll(id="loop-resultado")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-voltar-loop":
            executando = self._worker is not None and not self._worker.is_finished
            if self._cancel_event is not None and not self._cancel_event.is_set() and executando:
                self.query_one("#loop-status", Static).update("Cancele o loop antes de voltar.")
            else:
                self.app.query_one(ContentSwitcher).current = "menu-principal"
        elif button_id == "btn-iniciar-loop":
            self._iniciar_loop()
        elif button_id == "btn-cancelar-loop":
            self._cancelar_loop()

    def _iniciar_loop(self) -> None:
        campo = self.query_one("#input-iteracoes", Input)
        try:
            iteracoes = int(campo.value.strip())
            LoopConfig(iteracoes=iteracoes, output_dir=self._output_dir)
        except (ValueError, TypeError):
            self.query_one("#loop-status", Static).update(
                "Informe um inteiro positivo para o número de iterações."
            )
            return

        self._cancel_event = Event()
        self.query_one("#btn-iniciar-loop", Button).disabled = True
        self.query_one("#btn-cancelar-loop", Button).disabled = False
        self.query_one("#input-iteracoes", Input).disabled = True
        self.query_one("#loop-log", RichLog).clear()
        self.query_one("#loop-arquivos", Static).update("")
        self.query_one("#loop-status", Static).update("Iniciando loop...")
        self._worker = self._executar_worker(iteracoes)

    def _cancelar_loop(self) -> None:
        if self._cancel_event is None:
            return
        self._cancel_event.set()
        self.query_one("#btn-cancelar-loop", Button).disabled = True
        self.query_one("#loop-status", Static).update(
            "Cancelamento solicitado; a chamada atual terminará de forma cooperativa..."
        )

    def _registrar_progresso(self, progresso: LoopProgresso) -> None:
        try:
            self.app.call_from_thread(self._atualizar_progresso, progresso)
        except RuntimeError:
            # A tela pode ter sido desmontada enquanto a thread terminava.
            pass

    def _atualizar_progresso(self, progresso: LoopProgresso) -> None:
        barra = self.query_one("#loop-progress", ProgressBar)
        barra.update(total=progresso.total, progress=min(progresso.indice_atual, progresso.total))
        familia = (
            f"{progresso.familia.search_ref}/{progresso.familia.brand_id}"
            if progresso.familia else "—"
        )
        self.query_one("#loop-status", Static).update(
            f"{progresso.fase}: {progresso.mensagem} Família: {familia}"
        )
        self.query_one("#loop-contadores", Static).update(
            f"Concluídas: {progresso.concluidas} | Erros: {progresso.erros} | "
            f"Sinalizadas: {progresso.sinalizadas}"
        )
        if progresso.mensagem:
            self.query_one("#loop-log", RichLog).write(progresso.mensagem)

    def _registrar_aviso(self, mensagem: str) -> None:
        try:
            self.app.call_from_thread(self._mostrar_aviso, mensagem)
        except RuntimeError:
            pass

    def _mostrar_aviso(self, mensagem: str) -> None:
        self.query_one("#loop-log", RichLog).write(mensagem)

    def _pedir_intervencao(self, pedido):
        return pedir_intervencao_bloqueante(
            self.app, self._llm, pedido, cancel_event=self._cancel_event
        )

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _executar_worker(self, iteracoes: int) -> None:
        try:
            resultado = self._executar_loop_fn(
                LoopConfig(iteracoes=iteracoes, output_dir=self._output_dir),
                self._llm,
                self._dependencias_fk,
                rule_store=self._rule_store,
                cancel_event=self._cancel_event,
                on_progresso=self._registrar_progresso,
                on_aviso=self._registrar_aviso,
                pedir_intervencao=self._pedir_intervencao,
            )
        except Exception as exc:  # noqa: BLE001 — mostra erro sem derrubar a TUI
            self.app.call_from_thread(self._mostrar_erro, exc)
            return
        self.app.call_from_thread(self._mostrar_final, resultado)

    def _mostrar_erro(self, exc: Exception) -> None:
        self.query_one("#loop-status", Static).update(f"Erro no loop: {exc}")
        self.query_one("#btn-iniciar-loop", Button).disabled = False
        self.query_one("#btn-cancelar-loop", Button).disabled = True
        self.query_one("#input-iteracoes", Input).disabled = False

    def _mostrar_final(self, resultado: ResultadoLoop) -> None:
        self.query_one("#loop-status", Static).update(
            f"Loop finalizado: {resultado.status.value} — {resultado.motivo_parada.value}"
        )
        self.query_one("#loop-contadores", Static).update(
            f"Executadas: {resultado.iteracoes_executadas}/{resultado.iteracoes_solicitadas} | "
            f"Concluídas: {resultado.iteracoes_concluidas} | "
            f"Erros: {resultado.iteracoes_com_erro}"
        )
        self.query_one("#loop-arquivos", Static).update(
            f"JSON: {resultado.caminho_json}\nSQL: {resultado.caminho_sql}"
        )
        self.query_one("#btn-iniciar-loop", Button).disabled = False
        self.query_one("#btn-cancelar-loop", Button).disabled = True
        self.query_one("#input-iteracoes", Input).disabled = False
