"""Painel 'Rodar caso de teste': roda o pipeline completo (particionar_grupo ->
arbitrar_campo -> gerar_sql, via pipeline.executar_caso) pra um dos 4 casos fixos
(os 3 da SPEC.md §8 + Caso 4, DRIVEWAY JE4699) ou pra um grupo real sorteado, e
mostra o resultado na tela em 4
seções: tabela de peças do grupo, particionamento (collapsibles), arbitragem por
campo (tabela, sem ruído de "sem divergência" repetido) e registro final
consolidado por subcluster mesclado.

Sem confirmação/edição humana ainda (isso é a Fase 4 "oficial") — só roda e mostra.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from time import perf_counter

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Collapsible, ContentSwitcher, DataTable, Label, Static, Switch

from arbitration.models import DecisaoCampo
from config import PROJECT_ROOT
from db.rule_store import RuleStore
from llm.provider import LLMProvider
from loop.models import AvisoEmitido, FamiliaSorteada, IteracaoConcluida, IteracaoIniciada, IteracaoStatus, agora_iso
from loop.tracing import TraceCollector, TracingLLMProvider, envolver_intervencao
from memory.models import PedidoIntervencao, RespostaIntervencao
from partitioning.models import Subcluster
from pipeline import ResultadoCaso, executar_caso
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from tools.fk_introspection import FkDependency
from tools.group_fetch import RegistroCatalogPart, resolver_brand_id
from tools.sortear_grupo import sortear_grupo_aleatorio
from tui.seletores_execucao import (
    ConfigSeletoresTela,
    EstadoSeletores,
    OpcoesExecucao,
    ler_config_seletores,
    mensagem_configuracao,
    texto_estado_coleta,
    texto_estado_pesquisa,
    texto_estado_stealth,
)
from tui.swatches import atribuir_cores, swatch
from tui.execucao.widgets import ExecutionPanel
from tui.intervencao_bridge import pedir_intervencao_bloqueante

_LOGS_DIR = PROJECT_ROOT / "logs"

_CASOS_FIXOS = [
    ("Caso 1 — CITROEN 83061", "83061", "CITROEN"),
    ("Caso 2 — PEUGEOT 83062", "83062", "PEUGEOT"),
    ("Caso 3 — AFFINIA MB1085", "MB1085", "AFFINIA"),
    ("Caso 4 — DRIVEWAY JE4699", "JE4699", "DRIVEWAY"),
]

_CAMPOS_TECNICOS = ("width", "depth", "height", "gross_weight", "net_weight", "ncm", "barcode")
_LIMITE_SUBCLUSTERS_RESUMO = 15


def _colunas_com_dado(grupo: list[RegistroCatalogPart]) -> list[str]:
    """Só inclui colunas técnicas onde pelo menos um registro tem valor — evita
    coluna inteira vazia numa tabela de dezenas/centenas de peças."""
    return [c for c in _CAMPOS_TECNICOS if any(getattr(r, c) not in (None, "") for r in grupo)]


def _origem_cell(origem_id: int | None, cores: dict[int, str]):
    if origem_id is None:
        return "—"
    texto = Text()
    texto.append_text(swatch(cores.get(origem_id, "#888888")))
    texto.append(f" {origem_id}")
    return texto


def _motivo_cell(dc: DecisaoCampo) -> str:
    return dc.justificativa if dc.fonte != "sem_conflito" else "—"


def _valor_decidido(decisao: DecisaoMerge, campo: str, vencedor: RegistroCatalogPart):
    dc = next((d for d in decisao.decisoes_campo if d.campo == campo), None)
    # Campo sem decisão, ou escalado_humano (gerar_sql pula esse campo no UPDATE do
    # vencedor — ver sql_generation/gerar_sql.py) -> o valor real pós-merge é o que o
    # vencedor já tinha, nunca vazio/None só porque a arbitragem não concluiu nada.
    if dc is None or dc.escalado_humano:
        return getattr(vencedor, campo, None)
    return dc.valor


def _construir_tabela_pecas(
    grupo: list[RegistroCatalogPart], cores: dict[int, str], colunas_tecnicas: list[str]
) -> DataTable:
    dt = DataTable()
    dt.add_columns(" ", "id", "name", "search_ref", "brand", *colunas_tecnicas)
    for r in grupo:
        valores = [str(getattr(r, c)) if getattr(r, c) is not None else "" for c in colunas_tecnicas]
        dt.add_row(swatch(cores[r.id]), str(r.id), r.name, r.search_ref, r.brand, *valores)
    return dt


def _construir_linha_final(
    decisao: DecisaoMerge,
    por_id: dict[int, RegistroCatalogPart],
    colunas_tecnicas: list[str],
    cores: dict[int, str],
) -> DataTable:
    vencedor = por_id[decisao.vencedor_id]
    dt = DataTable(classes="registro-final")
    dt.add_columns(" ", "id", "name", "search_ref", "brand", *colunas_tecnicas)
    nome_final = _valor_decidido(decisao, "name", vencedor)
    valores_tecnicos = [_valor_decidido(decisao, c, vencedor) for c in colunas_tecnicas]
    dt.add_row(
        swatch(cores.get(vencedor.id, "#888888")),
        str(vencedor.id),
        str(nome_final) if nome_final is not None else "",
        vencedor.search_ref,
        vencedor.brand,
        *[str(v) if v is not None else "" for v in valores_tecnicos],
    )
    return dt


def _construir_tabela_arbitragem(decisao: DecisaoMerge, cores: dict[int, str]) -> DataTable:
    dt = DataTable()
    dt.add_columns("Campo", "Valor decidido", "Origem", "Motivo")
    for dc in decisao.decisoes_campo:
        dt.add_row(
            dc.campo,
            str(dc.valor) if dc.valor is not None else "",
            _origem_cell(dc.origem_id, cores),
            _motivo_cell(dc),
        )
    return dt


def _ids_da_decisao(decisao: DecisaoMerge | GrupoSinalizado) -> set[int]:
    if isinstance(decisao, DecisaoMerge):
        return {decisao.vencedor_id, *decisao.perdedor_ids}
    return set(decisao.membro_ids)


def _mapear_decisoes(resultado: ResultadoCaso) -> dict[int, DecisaoMerge | GrupoSinalizado | None]:
    """Correlaciona cada subcluster duplicata_real (por índice em particao.subclusters)
    com sua DecisaoMerge/GrupoSinalizado — resultado.decisoes é uma lista plana sem essa
    referência de volta, então reconstrói por conjunto de ids (vencedor+perdedores, ou
    membro_ids do sinalizado)."""
    restantes = list(resultado.decisoes)
    mapa: dict[int, DecisaoMerge | GrupoSinalizado | None] = {}
    for idx, sub in enumerate(resultado.particao.subclusters):
        if sub.label != "duplicata_real":
            continue
        ids_sub = set(sub.membro_ids)
        match = next((d for d in restantes if _ids_da_decisao(d) == ids_sub), None)
        if match is not None:
            restantes.remove(match)
        mapa[idx] = match
    return mapa


def _titulo_subcluster(sub: Subcluster, cores: dict[int, str]) -> str:
    swatches = " ".join(f"[{cores.get(i, '#888888')}]●[/]" for i in sub.membro_ids)
    return f"{swatches}  {sub.label} ({len(sub.membro_ids)} peça(s))"


def _deve_expandir(sub: Subcluster, decisao: DecisaoMerge | GrupoSinalizado | None) -> bool:
    if sub.label != "duplicata_real":
        return True
    if isinstance(decisao, GrupoSinalizado):
        return True
    if isinstance(decisao, DecisaoMerge):
        return any(dc.escalado_humano for dc in decisao.decisoes_campo)
    return False  # None -> subcluster de 1 membro, nada a revisar


def _construir_secao_particionamento(
    resultado: ResultadoCaso,
    por_id: dict[int, RegistroCatalogPart],
    colunas_tecnicas: list[str],
    cores: dict[int, str],
) -> list:
    widgets = []
    subclusters = resultado.particao.subclusters

    if len(subclusters) > _LIMITE_SUBCLUSTERS_RESUMO:
        contagem = Counter(s.label for s in subclusters)
        resumo = ", ".join(f"{v} {k}" for k, v in contagem.items())
        widgets.append(Static(f"{len(subclusters)} subclusters: {resumo}", classes="panel-explicacao"))

    mapa_decisoes = _mapear_decisoes(resultado)
    for idx, sub in enumerate(subclusters):
        decisao = mapa_decisoes.get(idx)
        conteudo = [Static(sub.justificativa)]

        if sub.label == "duplicata_real":
            if isinstance(decisao, DecisaoMerge):
                conteudo.append(Static("Arbitragem de campo:", classes="panel-title"))
                conteudo.append(_construir_tabela_arbitragem(decisao, cores))
                conteudo.append(Static("Registro final:", classes="panel-title"))
                conteudo.append(_construir_linha_final(decisao, por_id, colunas_tecnicas, cores))
            elif isinstance(decisao, GrupoSinalizado):
                conteudo.append(Static(f"⚠ {decisao.motivo}", classes="error-message"))
            else:
                conteudo.append(Static("Peça isolada — nada a mesclar."))

        widgets.append(
            Collapsible(*conteudo, title=_titulo_subcluster(sub, cores), collapsed=not _deve_expandir(sub, decisao))
        )
    return widgets


class ExecutarCasoScreen(Vertical):
    def __init__(
        self,
        llm: LLMProvider,
        dependencias_fk: list[FkDependency],
        rule_store: RuleStore | None = None,
        executar_caso_fn=executar_caso,
        ler_config: Callable[[], ConfigSeletoresTela] = ler_config_seletores,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._llm = llm
        self._dependencias_fk = dependencias_fk
        self._rule_store = rule_store
        self._executar_caso_fn = executar_caso_fn
        self._avisos_texto: list[str] = []
        # Configuração lida uma vez, só leitura (Req 11.16). O estado dos seletores
        # vive nesta instância, que continua montada no ContentSwitcher ao voltar
        # ao menu (Req 11.15).
        self._config_seletores = ler_config()
        self._seletores = EstadoSeletores.inicial(self._config_seletores)
        # Referência guardada: o worker publica no painel sem consultar o DOM.
        self._painel = ExecutionPanel(id="painel-execucao")

    def compose(self) -> ComposeResult:
        # Seletores ao lado de "Voltar ao menu", uma linha cada, antes dos botões de
        # sorteio e de casos fixos (Req 11.1): o banner fixo deixa só 12 linhas para
        # esta tela em 80x24 e os botões precisam continuar visíveis. A terceira linha
        # (Fallback stealth) vem logo depois de "Coleta de HTML" (Req 11.1 do stealth).
        with Horizontal(classes="cabecalho-execucao"):
            yield Button("← Voltar ao menu", id="btn-voltar")
            with Vertical(id="seletores-execucao", classes="seletores-execucao"):
                with Horizontal(classes="seletor-linha"):
                    yield Label("Pesquisa web", classes="seletor-rotulo")
                    yield Switch(
                        value=self._seletores.pesquisa, animate=False,
                        id="switch-pesquisa-web", tooltip="Pesquisa web",
                    )
                    yield Static("", id="estado-pesquisa-web", classes="seletor-estado")
                with Horizontal(classes="seletor-linha"):
                    yield Label("Coleta de HTML", classes="seletor-rotulo")
                    yield Switch(
                        value=self._seletores.coleta, animate=False,
                        id="switch-coleta-html", tooltip="Coleta de HTML",
                    )
                    yield Static("", id="estado-coleta-html", classes="seletor-estado")
                with Horizontal(classes="seletor-linha"):
                    yield Label("Fallback stealth", classes="seletor-rotulo")
                    yield Switch(
                        value=self._seletores.stealth, animate=False,
                        id="switch-fallback-stealth", tooltip="Fallback stealth",
                    )
                    yield Static("", id="estado-fallback-stealth", classes="seletor-estado")
                aviso_config = Static(
                    "\n".join(self._config_seletores.avisos), id="aviso-config-seletores", classes="aviso-config"
                )
                aviso_config.display = bool(self._config_seletores.avisos)
                yield aviso_config
        yield Static(
            "Roda o pipeline completo (particiona -> arbitra campos -> gera SQL) pra um grupo "
            "e mostra o resultado. Nunca executa o SQL contra o banco.",
            classes="panel-explicacao",
        )
        yield Button("🎲 Sortear grupo aleatório", id="btn-aleatorio", variant="primary")
        with Horizontal(classes="casos-fixos"):
            for i, (titulo, _, _) in enumerate(_CASOS_FIXOS):
                yield Button(titulo, id=f"btn-caso-{i}")
        yield Static("", id="status", classes="status-processando")
        yield Button("📋 Copiar/salvar log da pesquisa web", id="btn-copiar-log", disabled=True)
        # Painel de execução e resultado final na mesma rolagem: o painel mostra o
        # caso enquanto roda e, ao terminar sem pendências, recolhe para uma linha.
        painel = self._painel
        painel.display = False
        resultado_final = Vertical(id="resultado-final")
        resultado_final.styles.height = "auto"
        with VerticalScroll(id="resultado-container"):
            yield painel
            yield resultado_final

    def on_mount(self) -> None:
        self._renderizar_seletores()

    def _renderizar_seletores(self) -> None:
        """Único ponto que escreve nos seletores: valor, `disabled` e texto de estado
        vêm de `self._seletores` (Req 11.3, 11.8, 11.9, 11.13). `prevent` evita que a
        atribuição de `value` gere um `Switch.Changed` de eco."""
        estado = self._seletores
        pesquisa = self.query_one("#switch-pesquisa-web", Switch)
        coleta = self.query_one("#switch-coleta-html", Switch)
        stealth = self.query_one("#switch-fallback-stealth", Switch)
        with self.prevent(Switch.Changed):
            pesquisa.value = estado.pesquisa
            pesquisa.disabled = not estado.pesquisa_operavel
            coleta.value = estado.coleta
            coleta.disabled = not estado.coleta_operavel
            stealth.value = estado.stealth
            stealth.disabled = not estado.stealth_operavel
        self.query_one("#estado-pesquisa-web", Static).update(
            texto_estado_pesquisa(estado, self._config_seletores)
        )
        self.query_one("#estado-coleta-html", Static).update(texto_estado_coleta(estado))
        self.query_one("#estado-fallback-stealth", Static).update(texto_estado_stealth(estado))

    def on_switch_changed(self, event: Switch.Changed) -> None:
        event.stop()
        if event.switch.id == "switch-pesquisa-web":
            self._seletores = self._seletores.alternar_pesquisa(event.value)
        elif event.switch.id == "switch-coleta-html":
            self._seletores = self._seletores.alternar_coleta(event.value)
        elif event.switch.id == "switch-fallback-stealth":
            self._seletores = self._seletores.alternar_stealth(event.value)
        else:
            return
        # Se o modelo ignorou a mudança (seletor inoperável), a renderização devolve
        # o Switch ao valor do modelo.
        self._renderizar_seletores()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-voltar":
            self.app.query_one(ContentSwitcher).current = "menu-principal"
        elif button_id == "btn-aleatorio":
            self._iniciar_execucao(search_ref=None, brand=None)
        elif button_id and button_id.startswith("btn-caso-"):
            _, search_ref, brand = _CASOS_FIXOS[int(button_id.removeprefix("btn-caso-"))]
            self._iniciar_execucao(search_ref=search_ref, brand=brand)
        elif button_id == "btn-copiar-log":
            self._copiar_log_avisos()

    def _iniciar_execucao(self, search_ref: str | None, brand: str | None) -> None:
        self._seletores, opcoes = self._seletores.iniciar()
        self._renderizar_seletores()
        self.query_one("#status", Static).update("Rodando... (pode levar alguns segundos)")
        # Mensagem_Configuracao_Execucao como primeira linha do painel e do texto que
        # "Copiar/salvar" grava (Req 11.17, 11.18, 8.7). O botão de copiar continua
        # habilitado só a partir do primeiro aviso do pipeline.
        mensagem = mensagem_configuracao(opcoes, self._config_seletores)
        painel = self.query_one("#painel-execucao", ExecutionPanel)
        painel.limpar()
        painel.definir_cabecalho(mensagem)
        painel.display = True
        self._avisos_texto = [mensagem]
        self.query_one("#btn-copiar-log", Button).disabled = True
        self._executar_worker(search_ref, brand, opcoes)

    def _registrar_aviso(self, mensagem: str) -> None:
        self._painel.publicar(AvisoEmitido(timestamp=agora_iso(), mensagem=mensagem))
        self.app.call_from_thread(self._log_aviso, mensagem)

    async def _log_aviso(self, mensagem: str) -> None:
        self._avisos_texto.append(mensagem)
        self.query_one("#btn-copiar-log", Button).disabled = False

    def _copiar_log_avisos(self) -> None:
        texto = "\n".join(self._avisos_texto)

        # A área de transferência via OSC 52 (Textual copy_to_clipboard) depende do
        # terminal/multiplexador suportar e repassar a sequência corretamente — em
        # vários terminais ela é silenciosamente ignorada (o clipboard fica com o
        # que já estava lá antes, o que parece um bug mas não é). Por isso salvar
        # em arquivo é o mecanismo garantido; a cópia pra área de transferência é
        # só um bônus melhor-esforço por cima.
        try:
            self.app.copy_to_clipboard(texto)
        except Exception:  # noqa: BLE001 — melhor-esforço, nunca deve quebrar o salvamento em arquivo
            pass

        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        caminho = _LOGS_DIR / f"verificacao_web_{datetime.now():%Y%m%d_%H%M%S}.log"
        caminho.write_text(texto, encoding="utf-8")

        self.query_one("#status", Static).update(
            f"📋 Log salvo em {caminho} ({len(self._avisos_texto)} linha(s)) — também tentei "
            "copiar pra área de transferência, mas isso depende do seu terminal."
        )

    def _pedir_intervencao(self, pedido: PedidoIntervencao) -> RespostaIntervencao:
        return pedir_intervencao_bloqueante(self.app, self._llm, pedido)

    @work(thread=True, exclusive=True)
    def _executar_worker(self, search_ref: str | None, brand: str | None, opcoes: OpcoesExecucao) -> None:
        painel = self._painel
        familia: FamiliaSorteada | None = None
        inicio = perf_counter()
        try:
            if search_ref is None:
                grupo_sorteado = sortear_grupo_aleatorio()
                search_ref, brand_id = grupo_sorteado.search_ref, grupo_sorteado.brand_id
                brand = getattr(grupo_sorteado, "brand", None)
            else:
                brand_id = resolver_brand_id(brand)

            # Trace só para o painel (esta tela não grava auditoria): mesmos eventos
            # que o loop registra, com o raciocínio do modelo transmitido ao vivo.
            familia = FamiliaSorteada(search_ref, brand_id, brand or "")
            painel.publicar(IteracaoIniciada(timestamp=agora_iso(), indice=1, total=1, familia=familia))
            trace = TraceCollector(ouvinte=painel.publicar)
            resultado = self._executar_caso_fn(
                search_ref, brand_id, TracingLLMProvider(self._llm, trace), self._dependencias_fk,
                rule_store=self._rule_store, on_aviso=self._registrar_aviso,
                pedir_intervencao=envolver_intervencao(self._pedir_intervencao, trace), trace=trace,
                pesquisa_web=opcoes.pesquisa_web, coleta_html=opcoes.coleta_html,
                fallback_stealth=opcoes.fallback_stealth,
            )
        except Exception as exc:  # noqa: BLE001 - erro mostrado inline, TUI não pode cair
            if familia is not None:
                painel.publicar(IteracaoConcluida(
                    timestamp=agora_iso(), indice=1, total=1, status=IteracaoStatus.ERRO, familia=familia,
                    duracao_ms=(perf_counter() - inicio) * 1000,
                    erro={"tipo": type(exc).__name__, "mensagem": str(exc)[:1000]},
                ))
            self.app.call_from_thread(self._mostrar_erro, exc)
            return

        painel.publicar(IteracaoConcluida(
            timestamp=agora_iso(), indice=1, total=1, status=IteracaoStatus.SUCESSO, familia=familia,
            pecas=len(resultado.grupo),
            merges=sum(isinstance(d, DecisaoMerge) for d in resultado.decisoes),
            sinalizados=sum(isinstance(d, GrupoSinalizado) for d in resultado.decisoes),
            duracao_ms=(perf_counter() - inicio) * 1000,
        ))
        self.app.call_from_thread(self._mostrar_resultado, resultado)

    async def _limpar_resultado(self) -> Vertical:
        self.query_one("#painel-execucao", ExecutionPanel).drenar_agora()
        self._seletores = self._seletores.terminar()
        self._renderizar_seletores()
        self.query_one("#status", Static).update("")
        area = self.query_one("#resultado-final", Vertical)
        await area.remove_children()
        return area

    async def _mostrar_resultado(self, resultado: ResultadoCaso) -> None:
        container = await self._limpar_resultado()

        if not resultado.grupo:
            await container.mount(Static("Nenhum registro encontrado pra esse grupo."))
            return

        cores = atribuir_cores([r.id for r in resultado.grupo])
        por_id = {r.id: r for r in resultado.grupo}
        colunas_tecnicas = _colunas_com_dado(resultado.grupo)

        widgets = [
            Static(f"Grupo {resultado.grupo_ref} — {len(resultado.grupo)} peça(s)", classes="secao-titulo"),
            _construir_tabela_pecas(resultado.grupo, cores, colunas_tecnicas),
            Static("Particionamento", classes="secao-titulo"),
        ]
        widgets.extend(_construir_secao_particionamento(resultado, por_id, colunas_tecnicas, cores))

        if resultado.sql:
            widgets.append(Static("SQL gerado (revisável, não executado)", classes="secao-titulo"))
            widgets.append(Static(resultado.sql))

        await container.mount(*widgets)
        self.query_one("#resultado-container", VerticalScroll).scroll_home(animate=False)

    async def _mostrar_erro(self, exc: Exception) -> None:
        container = await self._limpar_resultado()
        await container.mount(Static(f"Erro: {exc}", classes="error-message"))
