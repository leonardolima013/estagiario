"""Modal bloqueante da intervenção humana.

A tela é mostrada no event loop do Textual, mas a destilação roda em worker
próprio. O worker do pipeline aguarda o resultado através do callback de
`push_screen`; portanto a UI continua responsiva enquanto o operador decide.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual import work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TextArea

from llm.provider import LLMProvider
from memory.destilar_regra import destilar_regra
from memory.models import PedidoIntervencao, RegraProposta, RespostaIntervencao
from verification.divergencia import tokens_normalizados


class IntervencaoCanceladaError(RuntimeError):
    """O operador fechou a intervenção sem confirmar uma decisão."""


class IntervencaoScreen(ModalScreen[RespostaIntervencao | None]):
    """Pergunta o contexto faltante e confirma a regra que será persistida."""

    def __init__(self, pedido: PedidoIntervencao, llm: LLMProvider, **kwargs) -> None:
        super().__init__(**kwargs)
        self._pedido = pedido
        self._llm = llm
        self._resposta_humana = ""
        self._proposta: RegraProposta | None = None
        self._acao: str | None = None

    def compose(self) -> ComposeResult:
        contexto = (
            f"Ponto: {self._pedido.ponto}\n"
            f"Grupo: {self._pedido.grupo_ref}\n"
            f"Código/marca: {self._pedido.search_ref} / {self._pedido.marca}\n"
            f"Nomes: {', '.join(self._pedido.nomes_conflitantes)}\n"
            f"Motivo: {self._pedido.motivo}"
        )
        if self._pedido.contexto_web:
            contexto += f"\nContexto web: {self._pedido.contexto_web}"

        with Vertical(id="intervencao-modal"):
            with VerticalScroll(id="intervencao-conteudo"):
                yield Static("Intervenção humana necessária", classes="intervencao-titulo")
                yield Static(contexto, id="intervencao-contexto", classes="intervencao-contexto")
                yield Static(
                    "Explique o contexto de domínio que faltou. A resposta será transformada "
                    "em uma regra reutilizável e ficará auditada junto deste caso.",
                    classes="panel-explicacao",
                )
                yield TextArea(id="intervencao-resposta", language=None)
                yield Input(placeholder="Nome final para este caso (opcional)", id="intervencao-valor")
                yield Input(value="operador", placeholder="Identificação do operador", id="intervencao-criado-por")
                yield Static("", id="intervencao-status", classes="intervencao-status")
                with Vertical(id="intervencao-regra-editor"):
                    yield Static("Regra proposta — revise antes de confirmar", classes="panel-title")
                    yield Input(placeholder="Título", id="intervencao-titulo")
                    yield TextArea(id="intervencao-condicao", language=None)
                    yield TextArea(id="intervencao-resolucao", language=None)
                    if self._pedido.ponto == "particionamento":
                        yield Static("Decisão para o caso atual", classes="panel-title")
                        with Horizontal():
                            yield Button("Permitir merge", id="btn-permitir-merge", disabled=True)
                            yield Button("Manter sinalizado", id="btn-manter-sinalizado", disabled=True)
            with Horizontal(id="intervencao-acoes"):
                yield Button("Destilar proposta", id="btn-destilar-intervencao", variant="primary")
                yield Button("Confirmar intervenção", id="btn-confirmar-intervencao", disabled=True, variant="primary")
                yield Button("Cancelar", id="btn-cancelar-intervencao")

    def on_mount(self) -> None:
        self.query_one("#intervencao-regra-editor").display = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-destilar-intervencao":
            self._iniciar_destilacao()
        elif button_id == "btn-permitir-merge":
            self._selecionar_acao("permitir_merge")
        elif button_id == "btn-manter-sinalizado":
            self._selecionar_acao("manter_sinalizado")
        elif button_id == "btn-confirmar-intervencao":
            self._confirmar()
        elif button_id == "btn-cancelar-intervencao":
            self.dismiss(None)

    def _iniciar_destilacao(self) -> None:
        resposta = self.query_one("#intervencao-resposta", TextArea).text.strip()
        if not resposta:
            self.query_one("#intervencao-status", Static).update("Escreva uma resposta antes de destilar.")
            return
        self._resposta_humana = resposta
        self.query_one("#btn-destilar-intervencao", Button).disabled = True
        self.query_one("#intervencao-status", Static).update("Destilando regra com o modelo...")
        self._destilar_worker(resposta)

    @work(thread=True, exclusive=True)
    def _destilar_worker(self, resposta: str) -> None:
        try:
            proposta = destilar_regra(self._pedido, resposta, self._llm)
        except Exception as exc:  # noqa: BLE001 — erro é mostrado no próprio modal
            self.app.call_from_thread(self._mostrar_erro, exc)
            return
        self.app.call_from_thread(self._mostrar_proposta, proposta)

    def _mostrar_erro(self, exc: Exception) -> None:
        self.query_one("#btn-destilar-intervencao", Button).disabled = False
        self.query_one("#intervencao-status", Static).update(f"Não foi possível destilar a regra: {exc}")

    def _mostrar_proposta(self, proposta: RegraProposta) -> None:
        self._proposta = proposta
        self.query_one("#intervencao-titulo", Input).value = proposta.titulo
        self.query_one("#intervencao-condicao", TextArea).load_text(proposta.condicao)
        self.query_one("#intervencao-resolucao", TextArea).load_text(proposta.resolucao)
        self.query_one("#intervencao-regra-editor").display = True
        self.query_one("#intervencao-status", Static).update(
            "Revise/edite a regra. Para particionamento, escolha a decisão do caso atual."
        )
        self.query_one("#btn-confirmar-intervencao", Button).disabled = False
        if self._pedido.ponto == "particionamento":
            self.query_one("#btn-permitir-merge", Button).disabled = False
            self.query_one("#btn-manter-sinalizado", Button).disabled = False

    def _selecionar_acao(self, acao: str) -> None:
        self._acao = acao
        self.query_one("#intervencao-status", Static).update(f"Ação selecionada: {acao}.")

    def _confirmar(self) -> None:
        if self._proposta is None:
            self.query_one("#intervencao-status", Static).update("Gere uma proposta antes de confirmar.")
            return
        if self._pedido.ponto == "particionamento" and self._acao is None:
            self.query_one("#intervencao-status", Static).update(
                "Escolha Permitir merge ou Manter sinalizado antes de confirmar."
            )
            return

        titulo = self.query_one("#intervencao-titulo", Input).value.strip()
        condicao = self.query_one("#intervencao-condicao", TextArea).text.strip()
        resolucao = self.query_one("#intervencao-resolucao", TextArea).text.strip()
        if not titulo or not condicao or not resolucao:
            self.query_one("#intervencao-status", Static).update(
                "Título, condição e resolução são obrigatórios."
            )
            return

        valor = self.query_one("#intervencao-valor", Input).value.strip() or None
        origem_id = None
        if valor is not None:
            tokens_valor = tokens_normalizados(valor)
            origem_id = next(
                (rid for rid, nome in self._pedido.candidatos
                 if tokens_normalizados(nome) == tokens_valor),
                None,
            )
        criado_por = self.query_one("#intervencao-criado-por", Input).value.strip() or "operador"
        self.dismiss(
            RespostaIntervencao(
                resposta_humana=self._resposta_humana,
                regra=RegraProposta(titulo=titulo, condicao=condicao, resolucao=resolucao),
                valor=valor,
                origem_id=origem_id,
                acao=self._acao,
                criado_por=criado_por,
            )
        )
