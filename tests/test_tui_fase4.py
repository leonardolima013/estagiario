import pytest

from pipeline import ResultadoCaso
from tui.app import EstagiarioApp


class FakeLLM:
    def gerar_json(self, system, user, json_schema, schema_name="output"):
        raise AssertionError("LLM não deveria ser chamado diretamente pela TUI nestes testes")


def _executar_caso_fake(search_ref, brand_id, llm, dependencias_fk, rule_store=None, **kwargs):
    return ResultadoCaso(grupo_ref=f"{search_ref}:{brand_id}", particao=_particao_vazia(), decisoes=[], sql="")


def _particao_vazia():
    from partitioning.models import Particao

    return Particao(grupo_ref="fake", subclusters=[])


@pytest.fixture
def app(tmp_path):
    from db.rule_store import RuleStore

    return EstagiarioApp(
        rule_store=RuleStore(tmp_path / "test.db"),
        llm=FakeLLM(),
        dependencias_fk=[],
        executar_caso_fn=_executar_caso_fake,
    )


async def test_app_abre_no_menu_principal_com_botao_rodar_testes(app):
    from textual.widgets import Button, ContentSwitcher

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(ContentSwitcher).current == "menu-principal"
        assert app.query_one("#btn-rodar-testes", Button)


async def test_cabecalho_e_menu_visiveis_ao_mesmo_tempo(app):
    from textual.widgets import Button, Static

    from tui.screens.cabecalho import Cabecalho

    async with app.run_test() as pilot:
        await pilot.pause()
        # Nada de splash/navegação: cabeçalho e menu coexistem desde o primeiro frame.
        assert app.query_one(Cabecalho)
        assert app.query_one("#cabecalho-subtitulo", Static)
        assert app.query_one("#btn-rodar-testes", Button)


async def test_rodar_testes_troca_pro_painel_de_execucao_e_cabecalho_permanece(app):
    from textual.widgets import ContentSwitcher

    from tui.screens.cabecalho import Cabecalho

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#btn-rodar-loop")
        await pilot.click("#btn-rodar-testes")
        await pilot.pause()

        assert app.query_one(ContentSwitcher).current == "executar-caso"
        assert app.query_one(Cabecalho)  # continua lá, não é uma tela que se navega pra longe


async def test_rodar_loop_abre_painel_com_controles(app):
    from textual.widgets import ContentSwitcher

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-rodar-loop")
        await pilot.pause()

        assert app.query_one(ContentSwitcher).current == "rodar-loop"
        assert app.query_one("#input-iteracoes")
        assert app.query_one("#btn-iniciar-loop")
        assert app.query_one("#btn-cancelar-loop")


async def test_voltar_ao_menu_a_partir_da_execucao(app):
    from textual.widgets import ContentSwitcher

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-rodar-testes")
        await pilot.pause()
        await pilot.click("#btn-voltar")
        await pilot.pause()

        assert app.query_one(ContentSwitcher).current == "menu-principal"


async def test_rodar_caso_fixo_mostra_resultado(app):
    from textual.containers import VerticalScroll

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-rodar-testes")
        await pilot.pause()
        await pilot.click("#btn-caso-0")
        await app.workers.wait_for_complete()
        await pilot.pause()

        container = app.query_one("#resultado-container", VerticalScroll)
        assert len(list(container.children)) > 0


async def test_rodar_caso_aleatorio_chama_pipeline_sem_search_ref_fixo(app, monkeypatch):
    from tools.sortear_grupo import GrupoSorteado

    monkeypatch.setattr(
        "tui.screens.executar_caso_screen.sortear_grupo_aleatorio",
        lambda: GrupoSorteado(search_ref="99999", brand_id=42, brand="TESTE"),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-rodar-testes")
        await pilot.pause()
        await pilot.click("#btn-aleatorio")
        await app.workers.wait_for_complete()
        await pilot.pause()

        from textual.containers import VerticalScroll

        container = app.query_one("#resultado-container", VerticalScroll)
        assert len(list(container.children)) > 0
