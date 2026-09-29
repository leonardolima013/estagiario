"""Tarefa 10.3 — a apresentação na TUI (Textual) precisa lidar com uma
`DecisaoCampo` cujo `confianca = "minima"` (fonte POSTGRES, vencedora por
`regra_confiabilidade`) sem quebrar e mostrando a justificativa esperada.

A tela não tem mapa nível->cor (confirmado na Tarefa 10.1): `_motivo_cell`
mostra `dc.justificativa` quando `dc.fonte != "sem_conflito"`, e `swatches.py`
mapeia id de peça -> cor, não nível de confiança. Então `confianca="minima"`
apenas flui pela renderização. Este teste fixa esse comportamento:

- teste unitário direto das funções puras de célula (`_motivo_cell`,
  `_valor_decidido`), sem banco/LLM;
- teste headless da tela (estilo dos demais em test_executar_caso_screen.py)
  renderizando a decisão `minima` de ponta a ponta.

_Requirements: 3.4_
"""

from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import DataTable

from arbitration.models import DecisaoCampo
from partitioning.models import Particao, Subcluster
from pipeline import ResultadoCaso
from sql_generation.models import DecisaoMerge
from tools.group_fetch import RegistroCatalogPart
from tui.screens.executar_caso_screen import (
    ExecutarCasoScreen,
    _motivo_cell,
    _valor_decidido,
)

_JUSTIFICATIVA_MINIMA = "Fonte POSTGRES (mínima) foi a única rastreável; venceu por confiabilidade."


def _decisao_minima(campo: str = "ncm", valor: object = "84131900") -> DecisaoCampo:
    """Decisão vinda da Tool_Provedor_Informacao com a fonte de menor nível
    rastreável (POSTGRES). É o cenário que a Tarefa 10.3 exige exercitar."""
    return DecisaoCampo(
        campo=campo,
        valor=valor,
        justificativa=_JUSTIFICATIVA_MINIMA,
        fonte="regra_confiabilidade",
        escalado_humano=False,
        origem_id=1,
        confianca="minima",
    )


# --- Testes unitários das funções puras de célula (sem TUI, sem I/O) ---------


def test_motivo_cell_mostra_justificativa_para_confianca_minima():
    dc = _decisao_minima()
    assert _motivo_cell(dc) == _JUSTIFICATIVA_MINIMA


def test_motivo_cell_sem_conflito_mostra_traco_para_contraste():
    dc = DecisaoCampo(
        campo="ncm",
        valor="84131900",
        justificativa="Sem divergência entre os registros.",
        fonte="sem_conflito",
        confianca=None,
    )
    assert _motivo_cell(dc) == "—"


def test_valor_decidido_usa_valor_da_decisao_minima_nao_escalada():
    vencedor = _registro(1, "BOMBA", ncm="00000000")
    decisao = DecisaoMerge(
        grupo_ref="83061:CITROEN",
        vencedor_id=1,
        perdedor_ids=[2],
        decisoes_campo=[_decisao_minima(campo="ncm", valor="84131900")],
    )
    assert _valor_decidido(decisao, "ncm", vencedor) == "84131900"


# --- Teste headless da tela (estilo test_executar_caso_screen.py) ------------


def _registro(id_, name, **overrides):
    campos = dict(
        id=id_, search_ref="83061", brand_id=1, brand="CITROEN", name=name,
        width=None, depth=None, height=None, gross_weight=None, net_weight=None,
        ncm=None, barcode=None, application=None, born_at=None, deprecated_at=None,
        similarity_id=None, created=datetime(2020, 1, 1),
    )
    campos.update(overrides)
    return RegistroCatalogPart(**campos)


def _resultado_com_confianca_minima() -> ResultadoCaso:
    grupo = [_registro(1, "BOMBA", ncm="84131900"), _registro(2, "BOMBA DAGUA", ncm="84131900")]
    particao = Particao(
        grupo_ref="83061:CITROEN",
        subclusters=[Subcluster(label="duplicata_real", membro_ids=[1, 2], justificativa="mesma peça")],
    )
    decisao = DecisaoMerge(
        grupo_ref="83061:CITROEN",
        vencedor_id=1,
        perdedor_ids=[2],
        decisoes_campo=[_decisao_minima(campo="ncm", valor="84131900")],
    )
    return ResultadoCaso(
        grupo_ref="83061:CITROEN", particao=particao, decisoes=[decisao],
        sql="BEGIN;\n...\nCOMMIT;\n", grupo=grupo,
    )


class _AppDeTeste(App):
    CSS_PATH = None

    def __init__(self, resultado: ResultadoCaso, **kwargs):
        super().__init__(**kwargs)
        self._resultado = resultado

    def compose(self) -> ComposeResult:
        def _executar_caso_fake(*args, **kwargs):
            return self._resultado

        yield ExecutarCasoScreen(llm=None, dependencias_fk=[], executar_caso_fn=_executar_caso_fake)


async def test_tela_renderiza_decisao_confianca_minima_sem_quebrar():
    resultado = _resultado_com_confianca_minima()
    app = _AppDeTeste(resultado)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-caso-0")
        await app.workers.wait_for_complete()
        await pilot.pause()

        container = app.query_one("#resultado-container", VerticalScroll)
        tabelas = list(container.query(DataTable))
        # tabela de peças + tabela de arbitragem + registro final
        assert tabelas[0].row_count == 2

        tabela_arbitragem = tabelas[1]
        linha_ncm = tabela_arbitragem.get_row_at(0)
        # Motivo (coluna 3) mostra a justificativa da decisão minima, não "—".
        assert linha_ncm[3] == _JUSTIFICATIVA_MINIMA
        # Origem (coluna 2) referencia o registro vencedor (id=1), não vazio.
        assert "1" in str(linha_ncm[2])
