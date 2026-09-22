from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, Collapsible, DataTable, RichLog, Static

from arbitration.models import DecisaoCampo
from partitioning.models import Particao, Subcluster
from pipeline import ResultadoCaso
from sql_generation.models import DecisaoMerge
from tui.screens.executar_caso_screen import ExecutarCasoScreen


class _AppDeTeste(App):
    CSS_PATH = None

    def __init__(self, resultado: ResultadoCaso, **kwargs):
        super().__init__(**kwargs)
        self._resultado = resultado

    def compose(self) -> ComposeResult:
        def _executar_caso_fake(*args, **kwargs):
            return self._resultado

        yield ExecutarCasoScreen(llm=None, dependencias_fk=[], executar_caso_fn=_executar_caso_fake)


def _registro(id_, name, **overrides):
    from tools.group_fetch import RegistroCatalogPart

    campos = dict(
        id=id_, search_ref="83061", brand_id=1, brand="CITROEN", name=name,
        width=None, depth=None, height=None, gross_weight=None, net_weight=None,
        ncm=None, barcode=None, application=None, born_at=None, deprecated_at=None,
        similarity_id=None, created=datetime(2020, 1, 1),
    )
    campos.update(overrides)
    return RegistroCatalogPart(**campos)


def _resultado_pequeno() -> ResultadoCaso:
    grupo = [_registro(1, "POLIA", width=3.2), _registro(2, "POLIA DA CORREIA", width=3.2)]
    particao = Particao(
        grupo_ref="83061:CITROEN",
        subclusters=[Subcluster(label="duplicata_real", membro_ids=[1, 2], justificativa="mesma peça")],
    )
    decisao = DecisaoMerge(
        grupo_ref="83061:CITROEN",
        vencedor_id=1,
        perdedor_ids=[2],
        decisoes_campo=[
            DecisaoCampo(campo="width", valor=3.2, justificativa="Sem divergência entre os registros.", fonte="sem_conflito"),
            DecisaoCampo(campo="name", valor="POLIA DA CORREIA", justificativa="mais completo", fonte="julgamento_modelo", origem_id=2),
        ],
    )
    return ResultadoCaso(grupo_ref="83061:CITROEN", particao=particao, decisoes=[decisao], sql="BEGIN;\n...\nCOMMIT;\n", grupo=grupo)


def _resultado_com_escalonamento() -> ResultadoCaso:
    grupo = [_registro(1, "POLIA", width=3.2), _registro(2, "POLIA DA CORREIA", width=9.9)]
    particao = Particao(
        grupo_ref="83061:CITROEN",
        subclusters=[Subcluster(label="duplicata_real", membro_ids=[1, 2], justificativa="mesma peça, width diverge")],
    )
    decisao = DecisaoMerge(
        grupo_ref="83061:CITROEN",
        vencedor_id=1,
        perdedor_ids=[2],
        decisoes_campo=[
            DecisaoCampo(campo="width", valor=None, justificativa="não sei", fonte="escalado_humano", escalado_humano=True),
        ],
    )
    return ResultadoCaso(grupo_ref="83061:CITROEN", particao=particao, decisoes=[decisao], sql="", grupo=grupo)


def _resultado_com_verificacao_web_inconclusiva() -> ResultadoCaso:
    grupo = [_registro(1, "PIVO"), _registro(2, "PIVO SUPERIOR")]
    particao = Particao(
        grupo_ref="83061:CITROEN",
        subclusters=[Subcluster(label="duplicata_real", membro_ids=[1, 2], justificativa="nomes divergem")],
    )
    decisao = DecisaoMerge(
        grupo_ref="83061:CITROEN",
        vencedor_id=1,
        perdedor_ids=[2],
        decisoes_campo=[
            DecisaoCampo(campo="name", valor=None, justificativa="fontes conflitantes", fonte="verificacao_web", escalado_humano=True),
        ],
    )
    return ResultadoCaso(grupo_ref="83061:CITROEN", particao=particao, decisoes=[decisao], sql="", grupo=grupo)


def _resultado_grande(n: int = 320, n_subclusters: int = 40) -> ResultadoCaso:
    grupo = [_registro(i, f"PEÇA {i}") for i in range(1, n + 1)]
    ids_por_sub = [list(range((i * n) // n_subclusters + 1, ((i + 1) * n) // n_subclusters + 1)) for i in range(n_subclusters)]
    ids_por_sub = [ids for ids in ids_por_sub if ids]
    subclusters = [
        Subcluster(label="duplicata_real" if i % 2 == 0 else "distinto_nao_classificado", membro_ids=ids, justificativa="x")
        for i, ids in enumerate(ids_por_sub)
    ]
    particao = Particao(grupo_ref="GRANDE:TESTE", subclusters=subclusters)
    decisoes = []
    for sub in subclusters:
        if sub.label != "duplicata_real" or len(sub.membro_ids) < 2:
            continue
        vencedor, *perdedores = sub.membro_ids
        decisoes.append(
            DecisaoMerge(
                grupo_ref="GRANDE:TESTE", vencedor_id=vencedor, perdedor_ids=perdedores,
                decisoes_campo=[DecisaoCampo(campo="name", valor="X", justificativa="ok", fonte="sem_conflito")],
            )
        )
    return ResultadoCaso(grupo_ref="GRANDE:TESTE", particao=particao, decisoes=decisoes, sql="", grupo=grupo)


async def test_tabela_de_pecas_e_motivo_sem_ruido():
    resultado = _resultado_pequeno()
    app = _AppDeTeste(resultado)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-caso-0")
        await app.workers.wait_for_complete()
        await pilot.pause()

        container = app.query_one("#resultado-container", VerticalScroll)
        tabelas = list(container.query(DataTable))
        tabela_pecas = tabelas[0]
        assert tabela_pecas.row_count == 2

        collapsibles = list(container.query(Collapsible))
        assert len(collapsibles) == 1
        assert collapsibles[0].collapsed is True  # duplicata_real sem campo escalado -> fechado

        tabela_arbitragem = tabelas[1]
        linha_width = tabela_arbitragem.get_row_at(0)
        assert linha_width[3] == "—"  # Motivo em branco pro campo sem_conflito
        linha_name = tabela_arbitragem.get_row_at(1)
        assert linha_name[3] == "mais completo"


async def test_subcluster_com_escalado_humano_comeca_expandido():
    resultado = _resultado_com_escalonamento()
    app = _AppDeTeste(resultado)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-caso-0")
        await app.workers.wait_for_complete()
        await pilot.pause()

        container = app.query_one("#resultado-container", VerticalScroll)
        collapsibles = list(container.query(Collapsible))
        assert collapsibles[0].collapsed is False


async def test_subcluster_com_verificacao_web_inconclusiva_comeca_expandido():
    resultado = _resultado_com_verificacao_web_inconclusiva()
    app = _AppDeTeste(resultado)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-caso-0")
        await app.workers.wait_for_complete()
        await pilot.pause()

        container = app.query_one("#resultado-container", VerticalScroll)
        collapsibles = list(container.query(Collapsible))
        assert collapsibles[0].collapsed is False


async def test_campo_escalado_no_registro_final_mostra_valor_atual_do_vencedor_nao_vazio():
    # Regressão: name=None (verificação web inconclusiva) não pode aparecer como
    # nome vazio no "Registro final" — o SQL gerado pula esse campo (fica com o
    # que o vencedor já tinha), então a prévia da tela precisa refletir isso, não
    # sugerir que o nome vai ficar vazio depois do merge.
    resultado = _resultado_com_verificacao_web_inconclusiva()
    app = _AppDeTeste(resultado)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-caso-0")
        await app.workers.wait_for_complete()
        await pilot.pause()

        container = app.query_one("#resultado-container", VerticalScroll)
        tabela_final = container.query_one(".registro-final", DataTable)
        linha = tabela_final.get_row_at(0)
        assert linha[2] == "PIVO"  # nome do vencedor (id=1), não vazio


class _AppComAviso(App):
    CSS_PATH = None

    def compose(self) -> ComposeResult:
        def _executar_caso_com_aviso(*args, on_aviso=None, **kwargs):
            if on_aviso:
                on_aviso("Nomes divergentes para 83061 (CITROEN): acionando verificação web.")
            return _resultado_pequeno()

        yield ExecutarCasoScreen(llm=None, dependencias_fk=[], executar_caso_fn=_executar_caso_com_aviso)


async def test_aviso_de_verificacao_web_aparece_no_log_live():
    app = _AppComAviso()
    async with app.run_test() as pilot:
        await pilot.pause()

        log = app.query_one("#avisos-web", RichLog)
        assert log.display is False

        await pilot.click("#btn-caso-0")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert log.display is True
        linhas = [str(line) for line in log.lines]
        assert any("acionando verificação web" in linha for linha in linhas)


async def test_botao_copiar_log_desabilitado_sem_avisos():
    resultado = _resultado_pequeno()
    app = _AppDeTeste(resultado)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-caso-0")
        await app.workers.wait_for_complete()
        await pilot.pause()

        botao = app.query_one("#btn-copiar-log", Button)
        assert botao.disabled is True


async def test_botao_copiar_log_salva_arquivo_e_tenta_clipboard(tmp_path, monkeypatch):
    import tui.screens.executar_caso_screen as modulo

    monkeypatch.setattr(modulo, "_LOGS_DIR", tmp_path)

    app = _AppComAviso()
    textos_copiados = []
    async with app.run_test() as pilot:
        app.copy_to_clipboard = textos_copiados.append
        await pilot.pause()
        await pilot.click("#btn-caso-0")
        await app.workers.wait_for_complete()
        await pilot.pause()

        botao = app.query_one("#btn-copiar-log", Button)
        assert botao.disabled is False

        await pilot.click("#btn-copiar-log")
        await pilot.pause()

        # tentativa de clipboard é best-effort, mas ainda deve ser chamada
        assert textos_copiados == ["Nomes divergentes para 83061 (CITROEN): acionando verificação web."]

        arquivos = list(tmp_path.glob("verificacao_web_*.log"))
        assert len(arquivos) == 1
        assert arquivos[0].read_text(encoding="utf-8") == "Nomes divergentes para 83061 (CITROEN): acionando verificação web."

        status = app.query_one("#status", Static)
        assert str(arquivos[0]) in str(status.renderable)


async def test_botao_copiar_log_salva_arquivo_mesmo_se_clipboard_falhar(tmp_path, monkeypatch):
    import tui.screens.executar_caso_screen as modulo

    monkeypatch.setattr(modulo, "_LOGS_DIR", tmp_path)

    app = _AppComAviso()
    async with app.run_test() as pilot:
        def _clipboard_quebrado(texto):
            raise RuntimeError("terminal não suporta OSC 52")

        app.copy_to_clipboard = _clipboard_quebrado
        await pilot.pause()
        await pilot.click("#btn-caso-0")
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.click("#btn-copiar-log")
        await pilot.pause()

        arquivos = list(tmp_path.glob("verificacao_web_*.log"))
        assert len(arquivos) == 1


async def test_grupo_grande_nao_quebra_e_mostra_resumo():
    resultado = _resultado_grande()
    app = _AppDeTeste(resultado)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-caso-0")
        await app.workers.wait_for_complete()
        await pilot.pause()

        container = app.query_one("#resultado-container", VerticalScroll)
        tabelas = list(container.query(DataTable))
        assert tabelas[0].row_count == len(resultado.grupo)

        textos = [str(s.renderable) for s in container.query(Static)]
        assert any("subclusters:" in t for t in textos)


async def test_mesmo_id_mesma_cor_em_tabela_de_pecas_e_arbitragem():
    resultado = _resultado_pequeno()
    app = _AppDeTeste(resultado)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-caso-0")
        await app.workers.wait_for_complete()
        await pilot.pause()

        container = app.query_one("#resultado-container", VerticalScroll)
        tabelas = list(container.query(DataTable))
        tabela_pecas = tabelas[0]
        swatch_peca_2 = tabela_pecas.get_row_at(1)[0]

        tabela_arbitragem = tabelas[1]
        origem_name = tabela_arbitragem.get_row_at(1)[2]  # campo name, origem_id=2

        assert swatch_peca_2.style == origem_name.spans[0].style
