import pytest

from db.rule_store import RuleStore
from loop.models import LoopProgresso, LoopStatus, MotivoParada, ResultadoLoop
from tui.app import EstagiarioApp


class FakeLLM:
    def gerar_json(self, *args, **kwargs):
        raise AssertionError("fake loop não deveria chamar LLM")


@pytest.fixture
def app(tmp_path):
    def executar_loop_fake(config, llm, dependencias_fk, **kwargs):
        kwargs["on_progresso"](
            LoopProgresso(total=config.iteracoes, indice_atual=config.iteracoes, fase="checkpoint", mensagem="feito", concluidas=config.iteracoes)
        )
        return ResultadoLoop(
            run_id="run",
            status=LoopStatus.CONCLUIDO,
            motivo_parada=MotivoParada.ITERACOES_SOLICITADAS_ALCANCADAS,
            iteracoes_solicitadas=config.iteracoes,
            finalizada_em="fim",
            caminho_json=config.output_dir / "loop.json",
            caminho_sql=config.output_dir / "loop.sql",
        )

    return EstagiarioApp(
        rule_store=RuleStore(tmp_path / "test.db"),
        llm=FakeLLM(),
        dependencias_fk=[],
        executar_loop_fn=executar_loop_fake,
        output_dir=tmp_path,
    )


async def test_loop_invalida_numero_nao_inicia(app):
    from textual.widgets import Static

    async with app.run_test() as pilot:
        await pilot.click("#btn-rodar-loop")
        await pilot.pause()
        app.query_one("#input-iteracoes").value = "0"
        await pilot.click("#btn-iniciar-loop")
        assert "inteiro positivo" in str(app.query_one("#loop-status", Static).renderable)


async def test_loop_mostra_progresso_e_caminhos_finais(app):
    from textual.widgets import Static

    async with app.run_test() as pilot:
        await pilot.click("#btn-rodar-loop")
        await pilot.pause()
        app.query_one("#input-iteracoes").value = "3"
        await pilot.click("#btn-iniciar-loop")
        await app.workers.wait_for_complete()
        await pilot.pause()

        status = str(app.query_one("#loop-status", Static).renderable)
        arquivos = str(app.query_one("#loop-arquivos", Static).renderable)
        assert "concluido" in status
        assert "loop.json" in arquivos
        assert "loop.sql" in arquivos
