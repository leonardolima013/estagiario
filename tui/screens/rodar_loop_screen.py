"""Tela Textual para executar o loop autônomo de famílias duplicadas.

Uma barra de controle de uma linha (voltar, iterações, iniciar, cancelar e
status), uma linha de progresso e, no resto da tela, o painel de execução, que
mostra cada família enquanto ela roda: etapas, raciocínio do modelo, decisões
por campo e avisos (docs/refactor-painel/DESIGN.md). Cada família tem também a
tabela dos registros sorteados e, ao terminar, a do registro vencedor, que
continuam visíveis com a família recolhida.
"""

from __future__ import annotations

from pathlib import Path
from threading import Event
from time import monotonic

from rich.cells import cell_len
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, ContentSwitcher, Input, ProgressBar, Static

from config import PROJECT_ROOT
from db.rule_store import RuleStore
from llm.provider import LLMProvider
from loop.executor import executar_loop
from loop.models import LoopConfig, LoopProgresso, MotivoParada, ResultadoLoop
from tools.fk_introspection import FkDependency
from tui.execucao.widgets import ExecutionPanel
from tui.intervencao_bridge import pedir_intervencao_bloqueante

TEXTO_VAZIO = (
    "Executa famílias duplicadas sem repetir search_ref + brand_id. Cada família aparece aqui "
    "enquanto roda. Erros são registrados e o SQL nunca é executado automaticamente."
)
_MOTIVOS = {
    MotivoParada.ITERACOES_SOLICITADAS_ALCANCADAS: "iterações solicitadas concluídas",
    MotivoParada.FAMILIAS_DUPLICADAS_ESGOTADAS: "famílias duplicadas esgotadas",
    MotivoParada.CANCELADO_PELO_USUARIO: "cancelado pelo operador",
    MotivoParada.ERRO_FATAL: "erro fatal",
}
_SEGUNDOS_STATUS_TEMPORARIO = 4.0


def _caminho_curto(caminho: Path | None) -> str:
    if caminho is None:
        return "—"
    try:
        return str(Path(caminho).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(caminho)


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
        self._status_fixo_ate = 0.0
        self._painel = ExecutionPanel(
            vazio=TEXTO_VAZIO, ao_progresso=self._atualizar_progresso, mostrar_registros=True,
            id="loop-painel",
        )

    def compose(self) -> ComposeResult:
        with Horizontal(id="loop-barra", classes="barra-controle"):
            yield Button("← Menu", id="btn-voltar-loop")
            yield Input(placeholder="Iterações", id="input-iteracoes")
            yield Button("▶ Iniciar", id="btn-iniciar-loop", variant="primary")
            yield Button("■ Cancelar", id="btn-cancelar-loop", disabled=True)
            yield Static("Aguardando número de iterações.", id="loop-status", classes="status-processando")
        with Horizontal(id="loop-linha-progresso"):
            yield ProgressBar(total=1, show_percentage=True, show_eta=False, id="loop-progress")
            yield Static("", id="loop-contadores")
        with VerticalScroll(id="loop-rolagem"):
            yield self._painel
        yield Static("", id="loop-arquivos")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-voltar-loop":
            executando = self._worker is not None and not self._worker.is_finished
            if self._cancel_event is not None and not self._cancel_event.is_set() and executando:
                self._mostrar_status_temporario("Cancele o loop antes de voltar.")
            else:
                self.app.query_one(ContentSwitcher).current = "menu-principal"
        elif button_id == "btn-iniciar-loop":
            self._iniciar_loop()
        elif button_id == "btn-cancelar-loop":
            self._cancelar_loop()

    def _mostrar_status_temporario(self, mensagem: str) -> None:
        """Mensagem para o operador que o progresso não sobrescreve por alguns segundos."""
        self._status_fixo_ate = monotonic() + _SEGUNDOS_STATUS_TEMPORARIO
        self.query_one("#loop-status", Static).update(mensagem)

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
        self._painel.limpar()
        self.query_one("#loop-progress", ProgressBar).update(total=iteracoes, progress=0)
        self.query_one("#loop-contadores", Static).update("")
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

    def _atualizar_progresso(self, progresso: LoopProgresso) -> None:
        """Chamado pelo painel, na thread da interface, na ordem dos eventos."""
        barra = self.query_one("#loop-progress", ProgressBar)
        barra.update(total=progresso.total, progress=min(progresso.indice_atual, progresso.total))
        cancelando = self._cancel_event is not None and self._cancel_event.is_set()
        if not cancelando and monotonic() >= self._status_fixo_ate:
            status = self.query_one("#loop-status", Static)
            texto = f"{progresso.fase} {progresso.indice_atual}/{progresso.total}"
            if progresso.familia:
                # A família só entra se couber: a barra tem uma linha, e em 80 colunas
                # o texto seria cortado no meio. O bloco da família no painel já a mostra.
                familia = progresso.familia
                completo = f"{texto} · {familia.search_ref}/{familia.brand or familia.brand_id}"
                if cell_len(completo) <= status.size.width:
                    texto = completo
            status.update(texto)
        self.query_one("#loop-contadores", Static).update(
            f"concl {progresso.concluidas} · erros {progresso.erros} · sinal {progresso.sinalizadas}"
        )

    def _pedir_intervencao(self, pedido):
        return pedir_intervencao_bloqueante(
            self.app, self._llm, pedido, cancel_event=self._cancel_event
        )

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _executar_worker(self, iteracoes: int) -> None:
        painel = self._painel
        try:
            resultado = self._executar_loop_fn(
                LoopConfig(iteracoes=iteracoes, output_dir=self._output_dir),
                self._llm,
                self._dependencias_fk,
                rule_store=self._rule_store,
                cancel_event=self._cancel_event,
                on_progresso=painel.publicar,
                pedir_intervencao=self._pedir_intervencao,
                on_evento_ao_vivo=painel.publicar,
            )
        except Exception as exc:  # noqa: BLE001 — mostra erro sem derrubar a TUI
            self.app.call_from_thread(self._mostrar_erro, exc)
            return
        self.app.call_from_thread(self._mostrar_final, resultado)

    def _reabilitar_controles(self) -> None:
        self.query_one("#btn-iniciar-loop", Button).disabled = False
        self.query_one("#btn-cancelar-loop", Button).disabled = True
        self.query_one("#input-iteracoes", Input).disabled = False

    def _mostrar_erro(self, exc: Exception) -> None:
        self._painel.drenar_agora()
        self.query_one("#loop-status", Static).update(f"Erro no loop: {exc}")
        self._reabilitar_controles()

    def _mostrar_final(self, resultado: ResultadoLoop) -> None:
        self._painel.drenar_agora()
        self.query_one("#loop-status", Static).update(f"Loop finalizado: {resultado.status.value}")
        self.query_one("#loop-contadores", Static).update(
            f"exec {resultado.iteracoes_executadas}/{resultado.iteracoes_solicitadas} · "
            f"concl {resultado.iteracoes_concluidas} · erros {resultado.iteracoes_com_erro}"
        )
        self.query_one("#loop-arquivos", Static).update(
            f"Parada: {_MOTIVOS.get(resultado.motivo_parada, resultado.motivo_parada.value)} · "
            f"JSON: {_caminho_curto(resultado.caminho_json)} · "
            f"SQL: {_caminho_curto(resultado.caminho_sql)} (nunca executado)"
        )
        self._reabilitar_controles()
