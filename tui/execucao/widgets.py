"""Widgets Textual do painel de execução.

A regra mora em `tui/execucao/modelo.py`; aqui só se desenha. O worker publica
os eventos direto na fila do painel (`ExecutionPanel.publicar`, thread-safe e
sem `call_from_thread`), e um timer de 50 ms drena a fila na thread da
interface, aplica no modelo e atualiza só o que mudou (R3).

Para não montar milhares de widgets num loop longo, uma família recolhida não
mantém os itens montados: eles são recriados a partir do modelo quando o
operador expande o bloco. As tabelas de registros da "Rodar loop" ficam fora
desse corpo e continuam montadas com a família recolhida.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.widgets import DataTable, Static

from loop.models import LoopProgresso
from tui.execucao.modelo import (
    COLUNAS_REGISTRO,
    MAX_FAMILIAS,
    EstadoDigitacao,
    Familia,
    FilaEventos,
    Item,
    ModeloPainel,
    colunas_vencedor,
    legenda_registros,
    legenda_vencedor,
    linha_familia,
    linha_item,
    linha_registro,
    recolher_texto,
    valores_registro_final,
)
from tui.swatches import atribuir_cores, swatch

INTERVALO_TICK = 0.05
_ESTADOS_ITEM = ("em_curso", "ok", "erro", "escalado", "interrompido", "aviso")
_ESTADOS_FAMILIA = ("em_curso", "ok", "sinalizada", "erro", "cancelada")
_COR_SEM_ID = "#888888"


def _texto(conteudo: str) -> Text:
    # Sempre Text, nunca markup: nomes de peça, JSON e listas trazem colchetes.
    return Text(conteudo)


def _trocar_estado(widget, estados: tuple[str, ...], atual: str) -> None:
    for estado in estados:
        widget.set_class(estado == atual, f"-{estado.replace('_', '-')}")


def _tabela(colunas: Sequence[str], linhas: Sequence[Sequence[Text]], classes: str) -> DataTable:
    """DataTable só de leitura: cor da peça e id ficam fixos na rolagem horizontal."""
    tabela = DataTable(show_cursor=False, fixed_columns=2, classes=classes)
    tabela.add_columns(" ", *colunas)
    for linha in linhas:
        tabela.add_row(*linha)
    return tabela


def _linha_tabela(valores, colunas: Sequence[str], cores: dict) -> list[Text]:
    # Células sempre como Text: o DataTable interpretaria uma str como markup.
    cor = cores.get(valores.get("id"), _COR_SEM_ID)
    return [swatch(cor), *(_texto(texto) for texto in linha_registro(valores, colunas))]


def _cores(familia: Familia) -> dict:
    """Mesma cor por id nas duas tabelas, como na tela "Rodar testes"."""
    return atribuir_cores([registro.get("id") for registro in familia.registros or ()])


def secao_registros(familia: Familia) -> Vertical:
    """Legenda e tabela dos registros sorteados da família."""
    filhos: list = [Static(_texto(legenda_registros(familia)), classes="tabela-legenda")]
    if familia.registros:
        cores = _cores(familia)
        linhas = [_linha_tabela(registro, COLUNAS_REGISTRO, cores) for registro in familia.registros]
        filhos.append(_tabela(COLUNAS_REGISTRO, linhas, "tabela-registros"))
    return Vertical(*filhos, classes="familia-registros")


def secao_vencedor(familia: Familia) -> Vertical:
    """Legenda e tabela do registro vencedor de cada merge, ou o motivo de não haver."""
    filhos: list = [Static(_texto(legenda_vencedor(familia)), classes="tabela-legenda")]
    finais = familia.registros_finais
    if finais:
        cores = _cores(familia)
        colunas = colunas_vencedor(finais)
        linhas = [_linha_tabela(valores_registro_final(registro), colunas, cores) for registro in finais]
        filhos.append(_tabela(colunas, linhas, "tabela-vencedor"))
    secao = Vertical(*filhos, classes="familia-vencedor")
    secao.set_class(not finais, "-sem-vencedor")
    return secao


class _LinhaAlternavel(Static):
    """Linha de título: o clique alterna o bloco dono (família ou item)."""

    def on_click(self, event: events.Click) -> None:
        event.stop()
        dono = self.parent
        if dono is not None and hasattr(dono, "action_alternar"):
            dono.action_alternar()


class ThinkingBlock(Static):
    """Raciocínio do modelo com efeito digitando e cursor ▍ enquanto escreve.

    Depois de terminar, um texto com mais de 6 linhas recolhe para 2; o item
    dono expande com Enter ou clique.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._estado = EstadoDigitacao()
        self._aberto = True
        self._expandido = False

    @property
    def animando(self) -> bool:
        return self._aberto or not self._estado.alcancou

    @property
    def texto_visivel(self) -> str:
        return self._estado.visivel(self._aberto)

    def definir(self, texto: str, aberto: bool) -> None:
        self._estado.definir_alvo(texto)
        self._aberto = aberto
        self._redesenhar()

    def expandir(self, expandido: bool) -> None:
        self._expandido = expandido
        self._redesenhar()

    def avancar(self) -> None:
        if self._estado.avancar() or not self.animando:
            self._redesenhar()

    def _redesenhar(self) -> None:
        texto = self.texto_visivel
        self.display = bool(texto)
        if not self.animando and not self._expandido:
            largura = self.size.width or (self.app.size.width - 8 if self.is_attached else 72)
            recolhido = recolher_texto(texto, largura)
            if recolhido is not None:
                previa, ocultas = recolhido
                self.update(_texto(f"{previa}\n… mais {ocultas} linha(s) — Enter ou clique para ver tudo"))
                return
        self.update(_texto(texto))

    def on_click(self, event: events.Click) -> None:
        event.stop()
        dono = self.parent
        if dono is not None and hasattr(dono, "action_alternar"):
            dono.action_alternar()


class ItemEvento(Vertical):
    """Uma etapa, decisão ou aviso: linha de resumo, raciocínio, detalhes e filhos."""

    DEFAULT_CSS = """
    ItemEvento { height: auto; }
    ItemEvento > .item-filhos { height: auto; padding: 0 0 0 2; }
    ItemEvento > .item-raciocinio, ItemEvento > .item-detalhes { padding: 0 0 0 2; }
    """
    BINDINGS = [Binding("enter", "alternar", "Detalhes", show=False)]

    def __init__(self, item: Item) -> None:
        super().__init__(classes=f"item-evento -{item.tipo.replace('_', '-')}")
        self.item = item
        self._versao = -1
        self._expandido = False
        # Filhos só podem ser montados depois do compose deste widget; antes disso
        # o Textual os colocaria na frente da linha de título.
        self._montado = False
        self._linha = _LinhaAlternavel(classes="item-linha")
        self._thinking = ThinkingBlock(classes="item-raciocinio") if item.tipo == "raciocinio" else None
        self._detalhes: Static | None = None
        self._filhos: Vertical | None = None
        self._widgets_filhos: dict[int, ItemEvento] = {}

    def compose(self) -> ComposeResult:
        yield self._linha
        if self._thinking is not None:
            yield self._thinking

    def on_mount(self) -> None:
        self._montado = True
        self._versao = -1
        self.sincronizar()

    def sincronizar(self, quadro: int = 0) -> None:
        item = self.item
        if item.versao != self._versao:
            self._versao = item.versao
            _trocar_estado(self, _ESTADOS_ITEM, item.estado)
            self.can_focus = item.detalhes is not None or self._thinking is not None
            if self._thinking is not None:
                self._thinking.definir(item.texto, item.aberto)
            if self._expandido:
                self._mostrar_detalhes()
            self._sincronizar_filhos()
        else:
            for filho in self._widgets_filhos.values():
                filho.sincronizar(quadro)
        self._linha.update(_texto(linha_item(item, quadro, self._expandido)))

    def animar(self, quadro: int) -> None:
        if self.item.aberto:
            self._linha.update(_texto(linha_item(self.item, quadro, self._expandido)))
        if self._thinking is not None and self._thinking.animando:
            self._thinking.avancar()
        for filho in self._widgets_filhos.values():
            filho.animar(quadro)

    def action_alternar(self) -> None:
        self._expandido = not self._expandido
        if self._thinking is not None:
            self._thinking.expandir(self._expandido)
        if self._expandido:
            self._mostrar_detalhes()
        elif self._detalhes is not None:
            self._detalhes.remove()
            self._detalhes = None
        self._linha.update(_texto(linha_item(self.item, 0, self._expandido)))

    def _mostrar_detalhes(self) -> None:
        if self.item.detalhes is None or not self._montado:
            return
        conteudo = _texto(json.dumps(self.item.detalhes, ensure_ascii=False, indent=2, default=str))
        if self._detalhes is None:
            self._detalhes = Static(conteudo, classes="item-detalhes")
            self.mount(self._detalhes, after=self._thinking or self._linha)
        else:
            self._detalhes.update(conteudo)

    def _sincronizar_filhos(self) -> None:
        if not self.item.filhos or not self._montado:
            return
        novos: list[ItemEvento] = []
        for filho in self.item.filhos:
            widget = self._widgets_filhos.get(filho.id)
            if widget is None:
                widget = ItemEvento(filho)
                self._widgets_filhos[filho.id] = widget
                novos.append(widget)
            else:
                widget.sincronizar()
        if not novos:
            return
        if self._filhos is None:
            self._filhos = Vertical(*novos, classes="item-filhos")
            self.mount(self._filhos)
        else:
            self._filhos.mount(*novos)


class FamilyBlock(Vertical):
    """Uma família do loop. Expandida enquanto roda; ao terminar, recolhe sozinha,
    a não ser que tenha erro, sinalização ou campo para revisão.

    Com `mostrar_registros`, a tabela dos registros sorteados fica logo abaixo do
    título e a do vencedor no fim, ambas fora do corpo recolhível: continuam
    visíveis com a família recolhida.
    """

    DEFAULT_CSS = """
    FamilyBlock { height: auto; }
    FamilyBlock > .familia-corpo { height: auto; padding: 0 0 0 2; }
    FamilyBlock > .familia-registros, FamilyBlock > .familia-vencedor { height: auto; padding: 0 0 0 2; }
    """
    BINDINGS = [Binding("enter", "alternar", "Expandir/recolher", show=False)]
    can_focus = True

    def __init__(self, familia: Familia, *, mostrar_registros: bool = False) -> None:
        super().__init__(classes="family-block")
        self.familia = familia
        self._versao = -1
        self._expandido = True
        self._escolha_do_operador = False
        self._montado = False
        self._mostrar_registros = mostrar_registros
        self._titulo = _LinhaAlternavel(classes="familia-titulo")
        self._corpo: Vertical | None = None
        self._secao_registros: Vertical | None = None
        self._secao_vencedor: Vertical | None = None
        self._widgets: dict[int, ItemEvento] = {}

    @property
    def expandido(self) -> bool:
        return self._expandido

    def compose(self) -> ComposeResult:
        yield self._titulo

    def on_mount(self) -> None:
        self._montado = True
        # O painel pode ter sincronizado o bloco antes deste mount (família que
        # termina no tick seguinte, sob carga); refaz tudo agora que dá para montar.
        self._versao = -1
        self.sincronizar()

    def sincronizar(self, quadro: int = 0) -> None:
        familia = self.familia
        if familia.versao != self._versao:
            self._versao = familia.versao
            _trocar_estado(self, _ESTADOS_FAMILIA, familia.estado)
            if not familia.em_curso and not self._escolha_do_operador:
                self._definir_expandido(familia.precisa_atencao)
            self._sincronizar_tabelas()
        if self._expandido:
            self._sincronizar_itens(quadro)
        self._titulo.update(_texto(linha_familia(familia, quadro)))

    def animar(self, quadro: int) -> None:
        if self.familia.em_curso:
            self._titulo.update(_texto(linha_familia(self.familia, quadro)))
        for widget in self._widgets.values():
            widget.animar(quadro)

    def action_alternar(self) -> None:
        self._escolha_do_operador = True
        self._definir_expandido(not self._expandido)

    def _definir_expandido(self, expandido: bool) -> None:
        if expandido == self._expandido:
            return
        self._expandido = expandido
        self.set_class(not expandido, "-recolhida")
        if expandido:
            self._sincronizar_itens(0)
        elif self._corpo is not None:
            self._corpo.remove()
            self._corpo = None
            self._widgets.clear()

    def _sincronizar_itens(self, quadro: int) -> None:
        if not self._montado:
            return
        novos: list[ItemEvento] = []
        for item in self.familia.itens:
            widget = self._widgets.get(item.id)
            if widget is None:
                widget = ItemEvento(item)
                self._widgets[item.id] = widget
                novos.append(widget)
            else:
                widget.sincronizar(quadro)
        if not novos:
            return
        if self._corpo is None:
            self._corpo = Vertical(*novos, classes="familia-corpo")
            # Ao reabrir uma família já terminada, as etapas voltam entre as duas tabelas.
            if self._secao_vencedor is not None:
                self.mount(self._corpo, before=self._secao_vencedor)
            else:
                self.mount(self._corpo)
        else:
            self._corpo.mount(*novos)

    def _sincronizar_tabelas(self) -> None:
        if not self._mostrar_registros or not self._montado:
            return
        familia = self.familia
        if self._secao_registros is None and familia.registros is not None:
            self._secao_registros = secao_registros(familia)
            self.mount(self._secao_registros, after=self._titulo)
        # Família sem registros e sem merge (erro no sorteio) não ganha a seção do vencedor.
        if (
            self._secao_vencedor is None
            and not familia.em_curso
            and (familia.registros is not None or familia.registros_finais)
        ):
            self._secao_vencedor = secao_vencedor(familia)
            self.mount(self._secao_vencedor)


class ExecutionPanel(Vertical):
    """Painel de eventos de uma execução (R4).

    `publicar` pode ser chamado de qualquer thread. `LoopProgresso` não vira item:
    vai para `ao_progresso`, na thread da interface, na ordem em que chegou.
    `definir_cabecalho` mostra uma linha fixa acima das famílias (a tela "Rodar
    testes" usa para a configuração da execução). `mostrar_registros` liga as
    tabelas de registros sorteados e do vencedor em cada família (só a "Rodar
    loop" usa; a "Rodar testes" mostra as dela no resultado final). Uma falha ao
    desenhar fica em `ultimo_erro` e numa linha do painel, sem derrubar a TUI.
    """

    DEFAULT_CSS = """
    ExecutionPanel { height: auto; }
    """

    def __init__(
        self,
        *,
        vazio: str = "",
        ao_progresso: Callable[[LoopProgresso], None] | None = None,
        max_familias: int = MAX_FAMILIAS,
        mostrar_registros: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._fila = FilaEventos()
        self._max_familias = max_familias
        self._mostrar_registros = mostrar_registros
        self._modelo = ModeloPainel(max_familias)
        self._blocos: dict[int, FamilyBlock] = {}
        self._ao_progresso = ao_progresso
        self._quadro = 0
        self._cabecalho = Static("", classes="painel-cabecalho")
        self._vazio = Static(_texto(vazio), classes="painel-vazio")
        self._ocultas = Static("", classes="painel-ocultas")
        self._falha = Static("", classes="painel-falha")
        self._tem_vazio = bool(vazio)
        self._fim_seguido: float | None = None
        self.ultimo_erro: Exception | None = None

    @property
    def modelo(self) -> ModeloPainel:
        return self._modelo

    @property
    def blocos(self) -> list[FamilyBlock]:
        return list(self._blocos.values())

    def compose(self) -> ComposeResult:
        yield self._cabecalho
        yield self._vazio
        yield self._ocultas
        yield self._falha

    def on_mount(self) -> None:
        self._cabecalho.display = False
        self._vazio.display = self._tem_vazio
        self._ocultas.display = False
        self._falha.display = False
        self.set_interval(INTERVALO_TICK, self._tick)

    def publicar(self, evento: object) -> None:
        self._fila.publicar(evento)

    def definir_cabecalho(self, texto: str | None) -> None:
        self._cabecalho.update(_texto(texto or ""))
        self._cabecalho.display = bool(texto)

    def linhas_visiveis(self) -> list[str]:
        """Texto das linhas de título montadas, na ordem da tela (cabeçalho,
        famílias e itens), sem o raciocínio e os detalhes."""
        linhas = [str(self._cabecalho.renderable)] if self._cabecalho.display else []
        for widget in self.query(".familia-titulo, .item-linha"):
            linhas.append(str(widget.renderable))
        return linhas

    def drenar_agora(self) -> None:
        """Processa já o que estiver na fila (usado antes de mostrar o resultado final)."""
        self._tick()

    def limpar(self) -> None:
        self._fila.drenar()
        for bloco in self._blocos.values():
            bloco.remove()
        self._blocos.clear()
        self._modelo = ModeloPainel(self._max_familias)
        self.definir_cabecalho(None)
        self._vazio.display = self._tem_vazio
        self._ocultas.display = False
        self._falha.display = False
        self._fim_seguido = None
        self.ultimo_erro = None

    def _rolagem(self) -> ScrollableContainer | None:
        pai = self.parent
        return pai if isinstance(pai, ScrollableContainer) else None

    def _acompanha_o_fim(self, rolagem: ScrollableContainer) -> bool:
        """O operador está no fim da rolagem, ou estava quando o painel rolou até
        lá e o painel cresceu depois sem ele subir (uma tabela só ganha altura um
        refresh depois de montada).

        A segunda regra só vale quando o painel é o último conteúdo da rolagem. Na
        "Rodar testes" o resultado final vem abaixo dele e a tela volta ao topo ao
        mostrá-lo, então lá vale só a primeira, como antes.
        """
        if rolagem.scroll_y >= rolagem.max_scroll_y - 1:
            return True
        filhos = rolagem.children
        return (
            self._fim_seguido is not None
            and bool(filhos) and filhos[-1] is self
            and rolagem.scroll_y >= self._fim_seguido - 1
        )

    def _registrar_fim(self) -> None:
        rolagem = self._rolagem()
        if rolagem is not None:
            self._fim_seguido = rolagem.scroll_y

    def _tick(self) -> None:
        try:
            self._processar()
        except Exception as exc:  # noqa: BLE001 — falha de desenho não derruba a TUI
            self.ultimo_erro = exc
            self._falha.update(_texto(f"Falha ao desenhar o painel: {type(exc).__name__}: {exc}"))
            self._falha.display = True

    def _processar(self) -> None:
        eventos = self._fila.drenar()
        rolagem = self._rolagem()
        no_fim = rolagem is not None and self._acompanha_o_fim(rolagem)
        for evento in eventos:
            if isinstance(evento, LoopProgresso):
                if self._ao_progresso is not None:
                    self._ao_progresso(evento)
                continue
            self._modelo.aplicar(evento)

        removidas, alteradas = self._modelo.consumir_alteracoes()
        for familia_id in removidas:
            bloco = self._blocos.pop(familia_id, None)
            if bloco is not None:
                bloco.remove()
        novos: list[FamilyBlock] = []
        for familia in self._modelo.familias:
            bloco = self._blocos.get(familia.id)
            if bloco is None:
                bloco = FamilyBlock(familia, mostrar_registros=self._mostrar_registros)
                self._blocos[familia.id] = bloco
                novos.append(bloco)
            elif familia.id in alteradas:
                bloco.sincronizar(self._quadro)
        if novos:
            self.mount(*novos)

        self._vazio.display = self._tem_vazio and not self._modelo.familias
        if self._modelo.ocultas:
            self._ocultas.update(_texto(
                f"… {self._modelo.ocultas} família(s) anterior(es) fora da tela — detalhes no JSON do loop"
            ))
            self._ocultas.display = True

        self._quadro += 1
        for bloco in list(self._blocos.values())[-2:]:
            bloco.animar(self._quadro)

        if rolagem is not None and no_fim:
            # Estrito: a tolerância de uma linha do `no_fim` deixaria a última linha fora.
            cresceu = rolagem.scroll_y < rolagem.max_scroll_y
            if eventos or self._animando() or cresceu:
                self.call_after_refresh(rolagem.scroll_end, animate=False, on_complete=self._registrar_fim)

    def _animando(self) -> bool:
        atual = self._modelo.atual
        return atual is not None and atual.em_curso
