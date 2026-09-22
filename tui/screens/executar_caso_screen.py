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
from datetime import datetime

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Collapsible, ContentSwitcher, DataTable, RichLog, Static

from arbitration.models import DecisaoCampo
from config import PROJECT_ROOT
from db.rule_store import RuleStore
from llm.provider import LLMProvider
from memory.models import PedidoIntervencao, RespostaIntervencao
from partitioning.models import Subcluster
from pipeline import ResultadoCaso, executar_caso
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from tools.fk_introspection import FkDependency
from tools.group_fetch import RegistroCatalogPart, resolver_brand_id
from tools.sortear_grupo import sortear_grupo_aleatorio
from tui.swatches import atribuir_cores, swatch
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
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._llm = llm
        self._dependencias_fk = dependencias_fk
        self._rule_store = rule_store
        self._executar_caso_fn = executar_caso_fn
        self._avisos_texto: list[str] = []

    def compose(self) -> ComposeResult:
        yield Button("← Voltar ao menu", id="btn-voltar")
        yield Static(
            "Roda o pipeline completo (particiona -> arbitra campos -> gera SQL) pra um grupo "
            "e mostra o resultado. Nunca executa o SQL contra o banco.",
            classes="panel-explicacao",
        )
        yield Button("🎲 Sortear grupo aleatório", id="btn-aleatorio", variant="primary")
        with Horizontal():
            for i, (titulo, _, _) in enumerate(_CASOS_FIXOS):
                yield Button(titulo, id=f"btn-caso-{i}")
        yield Static("", id="status", classes="status-processando")
        avisos = RichLog(id="avisos-web", classes="avisos-web-log")
        avisos.display = False
        yield avisos
        yield Button("📋 Copiar/salvar log da pesquisa web", id="btn-copiar-log", disabled=True)
        yield VerticalScroll(id="resultado-container")

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
        self.query_one("#status", Static).update("Rodando... (pode levar alguns segundos)")
        avisos = self.query_one("#avisos-web", RichLog)
        avisos.clear()
        avisos.display = False
        self._avisos_texto = []
        self.query_one("#btn-copiar-log", Button).disabled = True
        self._executar_worker(search_ref, brand)

    def _registrar_aviso(self, mensagem: str) -> None:
        self.app.call_from_thread(self._log_aviso, mensagem)

    async def _log_aviso(self, mensagem: str) -> None:
        log = self.query_one("#avisos-web", RichLog)
        log.display = True
        log.write(mensagem)
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
    def _executar_worker(self, search_ref: str | None, brand: str | None) -> None:
        try:
            if search_ref is None:
                grupo_sorteado = sortear_grupo_aleatorio()
                search_ref, brand_id = grupo_sorteado.search_ref, grupo_sorteado.brand_id
            else:
                brand_id = resolver_brand_id(brand)

            resultado = self._executar_caso_fn(
                search_ref, brand_id, self._llm, self._dependencias_fk, rule_store=self._rule_store,
                on_aviso=self._registrar_aviso, pedir_intervencao=self._pedir_intervencao,
            )
        except Exception as exc:  # noqa: BLE001 - erro mostrado inline, TUI não pode cair
            self.app.call_from_thread(self._mostrar_erro, exc)
            return

        self.app.call_from_thread(self._mostrar_resultado, resultado)

    async def _mostrar_resultado(self, resultado: ResultadoCaso) -> None:
        self.query_one("#status", Static).update("")
        container = self.query_one("#resultado-container", VerticalScroll)
        await container.remove_children()

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

    async def _mostrar_erro(self, exc: Exception) -> None:
        self.query_one("#status", Static).update("")
        container = self.query_one("#resultado-container", VerticalScroll)
        await container.remove_children()
        await container.mount(Static(f"Erro: {exc}", classes="error-message"))
