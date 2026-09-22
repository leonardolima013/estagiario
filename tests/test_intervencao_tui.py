import pytest
from threading import Event, Thread

from db.rule_store import RuleStore
from partitioning.models import Particao
from pipeline import ResultadoCaso
from tui.app import EstagiarioApp
from tui.intervencao_bridge import pedir_intervencao_bloqueante
from tui.screens.intervencao_screen import IntervencaoCanceladaError, IntervencaoScreen
from memory.models import PedidoIntervencao


class FakeLLMDestilador:
    def gerar_json(self, system, user, json_schema, schema_name="output"):
        assert schema_name == "destilar_regra_intervencao"
        return {
            "titulo": "Kit e componente",
            "condicao": "Quando um kit e uma peça avulsa aparecem no mesmo grupo",
            "resolucao": "Separar o kit da peça avulsa",
        }


@pytest.fixture
def app(tmp_path):
    return EstagiarioApp(
        rule_store=RuleStore(tmp_path / "test.db"),
        llm=FakeLLMDestilador(),
        dependencias_fk=[],
        executar_caso_fn=lambda *args, **kwargs: _resultado_vazio(),
    )


def _pedido():
    return PedidoIntervencao(
        ponto="nome",
        grupo_ref="JE4699:DRIVEWAY",
        search_ref="JE4699",
        marca="DRIVEWAY",
        nomes_conflitantes=["PIVO SUPERIOR", "PIVO INFERIOR"],
        motivo="a web foi inconclusiva",
        contexto_web="fontes conflitantes",
        membro_ids=[1, 2],
        candidatos=[(1, "PIVO SUPERIOR"), (2, "PIVO INFERIOR")],
    )


def _resultado_vazio():
    return ResultadoCaso(grupo_ref="fake", particao=Particao(grupo_ref="fake", subclusters=[]), decisoes=[], sql="")


async def test_modal_destila_edita_e_confirma_resposta(app, monkeypatch):
    # Usa a app real para garantir que ModalScreen e o tema carregam no mesmo contexto
    # dos testes da Fase 4.
    app._llm = FakeLLMDestilador()
    recebido = []

    async with app.run_test() as pilot:
        app.push_screen(IntervencaoScreen(_pedido(), app._llm), callback=recebido.append)
        await pilot.pause()

        app.query_one("#intervencao-resposta").load_text("A posição inferior é a correta.")
        await pilot.click("#btn-destilar-intervencao")
        for _ in range(100):
            await pilot.pause(0.05)
            if app.query_one("#intervencao-titulo").value:
                break

        assert app.query_one("#intervencao-titulo").value == "Kit e componente"
        app.query_one("#intervencao-valor").value = "PIVO INFERIOR"
        await pilot.click("#btn-confirmar-intervencao")
        await pilot.pause()

    assert len(recebido) == 1
    assert recebido[0].valor == "PIVO INFERIOR"
    assert recebido[0].origem_id == 2
    assert recebido[0].regra.titulo == "Kit e componente"


async def test_worker_do_pipeline_pausa_no_modal_e_retorna(app, monkeypatch):
    app._llm = FakeLLMDestilador()
    recebido = []

    def executar_fake(search_ref, brand_id, llm, dependencias_fk, pedir_intervencao=None, **kwargs):
        recebido.append(pedir_intervencao(_pedido()))
        return _resultado_vazio()

    app._executar_caso_fn = executar_fake

    async with app.run_test() as pilot:
        await pilot.click("#btn-rodar-testes")
        await pilot.pause()
        await pilot.click("#btn-caso-0")

        for _ in range(100):
            await pilot.pause(0.05)
            try:
                app.query_one("#intervencao-contexto")
                break
            except Exception:
                continue
        else:
            raise AssertionError("modal de intervenção não apareceu")

        app.query_one("#intervencao-resposta").load_text("A posição inferior é a correta.")
        await pilot.click("#btn-destilar-intervencao")
        for _ in range(100):
            await pilot.pause(0.05)
            if app.query_one("#intervencao-titulo").value:
                break
        app.query_one("#intervencao-valor").value = "PIVO INFERIOR"
        await pilot.click("#btn-confirmar-intervencao")
        await pilot.pause(0.2)

    assert len(recebido) == 1
    assert recebido[0].valor == "PIVO INFERIOR"


async def test_cancelamento_fecha_modal_e_desbloqueia_worker(app):
    cancelamento = Event()
    resultado = {}

    def worker():
        try:
            pedir_intervencao_bloqueante(
                app, app._llm, _pedido(), cancel_event=cancelamento
            )
        except Exception as exc:  # noqa: BLE001
            resultado["erro"] = exc

    async with app.run_test() as pilot:
        thread = Thread(target=worker)
        thread.start()
        for _ in range(100):
            await pilot.pause(0.05)
            try:
                app.query_one("#intervencao-contexto")
                break
            except Exception:
                continue
        else:
            raise AssertionError("modal não apareceu")

        cancelamento.set()
        for _ in range(100):
            await pilot.pause(0.05)
            if not thread.is_alive():
                break
        thread.join(timeout=1)

    assert isinstance(resultado.get("erro"), IntervencaoCanceladaError)
