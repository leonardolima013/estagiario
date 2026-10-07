"""Tela "Rodar loop" com o painel de execução, em `App.run_test()`.

Usa o `executar_loop` real com o caso e o LLM roteirizados de
`tests/fakes_painel.py`: nada de banco, rede ou API.
"""

from __future__ import annotations

import functools

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import ContentSwitcher, DataTable, Static

from db.rule_store import RuleStore
from loop.executor import executar_loop
from loop.models import FamiliaSorteada, GrupoCarregado, IteracaoConcluida, IteracaoIniciada, IteracaoStatus
from memory.models import RegraProposta, RespostaIntervencao
from tests.fakes_painel import CasoRoteirizado, LLMRoteirizado, sorteador
from tui.app import EstagiarioApp
from tui.execucao.modelo import CURSOR, ModeloPainel
from tui.execucao.widgets import ExecutionPanel, FamilyBlock, ItemEvento, ThinkingBlock
from tui.screens.cabecalho import Cabecalho
from tui.screens.intervencao_screen import IntervencaoScreen

FAMILIAS = [
    FamiliaSorteada("83061", 3, "CITROEN"),
    FamiliaSorteada("MB1085", 9, "AFFINIA"),
    FamiliaSorteada("JE4699", 7, "DRIVEWAY"),
]


def _app(tmp_path, *, llm=None, caso=None, familias=FAMILIAS):
    llm = llm or LLMRoteirizado()
    caso = caso or CasoRoteirizado()
    loop_fn = functools.partial(executar_loop, sortear_fn=sorteador(familias), executar_caso_fn=caso)
    return EstagiarioApp(
        rule_store=RuleStore(tmp_path / "t.db"), llm=llm, dependencias_fk=[],
        executar_loop_fn=loop_fn, output_dir=tmp_path,
    )


async def _abrir_loop(pilot, app, iteracoes: str) -> None:
    await pilot.pause()
    await pilot.click("#btn-rodar-loop")
    await pilot.pause()
    app.query_one("#input-iteracoes").value = iteracoes
    await pilot.click("#btn-iniciar-loop")


async def _esperar(pilot, condicao, vezes: int = 200) -> None:
    for _ in range(vezes):
        await pilot.pause(0.05)
        if condicao():
            return
    raise AssertionError("condição não atingida")


def _painel(app) -> ExecutionPanel:
    return app.query_one("#loop-painel", ExecutionPanel)


async def test_loop_mostra_familias_etapas_e_raciocinio_no_painel(tmp_path):
    llm = LLMRoteirizado()
    app = _app(tmp_path, llm=llm)

    async with app.run_test(size=(100, 32)) as pilot:
        await _abrir_loop(pilot, app, "3")
        await app.workers.wait_for_complete()
        await _esperar(pilot, lambda: len(_painel(app).blocos) == 3)
        await pilot.pause(0.6)

        painel = _painel(app)
        assert painel.ultimo_erro is None
        assert not app.query("#loop-log")
        assert not app.query("#loop-resultado")
        assert "Loop finalizado: concluido" in str(app.query_one("#loop-status", Static).renderable)
        assert "nunca executado" in str(app.query_one("#loop-arquivos", Static).renderable)

        estados = [bloco.familia.estado for bloco in painel.blocos]
        assert estados == ["ok", "sinalizada", "ok"]
        # Famílias sem pendência recolhem; a sinalizada continua aberta.
        assert [bloco.expandido for bloco in painel.blocos] == [False, True, False]

        sinalizada = painel.blocos[1]
        linhas = [str(w.renderable) for w in sinalizada.query(".item-linha")]
        assert linhas[0].startswith("✓ Buscar grupo · 4 registro(s)")
        assert any(l.startswith("✓ Particionar grupo · 3 subcluster(s)") for l in linhas)
        assert any("Serper #1 · Pivô de Suspensão Superior" in l for l in linhas)
        assert any("gross_weight → revisão humana" in l for l in linhas)
        assert any(l.startswith("⚠ Grupo sinalizado para revisão") for l in linhas)
        assert not any("{" in l for l in linhas), "JSON cru não pode aparecer nas linhas"

        raciocinios = list(sinalizada.query(ThinkingBlock))
        assert raciocinios, "o raciocínio do particionamento deve aparecer"
        texto = raciocinios[0].texto_visivel
        assert texto.startswith("duplicata_real: Os registros 101 e 102")
        assert "kit_componente: O 103 é o kit" in texto
        assert not texto.endswith(CURSOR)
        assert "transmitindo:particao" in llm.chamadas


async def test_titulo_da_etapa_vem_antes_dos_filhos(tmp_path):
    app = _app(tmp_path, familias=FAMILIAS[1:2])

    async with app.run_test(size=(100, 32)) as pilot:
        await _abrir_loop(pilot, app, "1")
        await app.workers.wait_for_complete()
        await _esperar(pilot, lambda: _painel(app).blocos and _painel(app).blocos[0].query(ThinkingBlock))
        await pilot.pause(0.3)

        particionar = next(
            w for w in app.query(ItemEvento) if w.item.nome == "particionar_grupo"
        )
        filhos = list(particionar.children)
        assert "item-linha" in filhos[0].classes
        assert "item-filhos" in filhos[-1].classes


async def test_raciocinio_aparece_enquanto_e_gerado(tmp_path):
    llm = LLMRoteirizado(tamanho=8, pausar_em={"particao": 16})
    app = _app(tmp_path, llm=llm, familias=FAMILIAS[:1])

    async with app.run_test(size=(100, 32)) as pilot:
        try:
            await _abrir_loop(pilot, app, "1")
            await _esperar(pilot, llm.pausado.is_set)
            await _esperar(
                pilot,
                lambda: "registros 101" in "".join(t.texto_visivel for t in _painel(app).query(ThinkingBlock)),
            )

            parcial = _painel(app).query_one(ThinkingBlock).texto_visivel
            assert parcial.endswith(CURSOR)
            assert parcial.startswith("duplicata_real: Os registros 101")
            assert "kit_componente" not in parcial
        finally:
            llm.continuar.set()
        await app.workers.wait_for_complete()
        await pilot.pause(0.6)
        (familia,) = _painel(app).modelo.familias
        particionar = next(i for i in familia.itens if i.nome == "particionar_grupo")
        (raciocinio,) = [f for f in particionar.filhos if f.tipo == "raciocinio"]
        assert raciocinio.estado == "ok"
        assert "variante_dimensional: O 104 é a versão reforçada" in raciocinio.texto


async def test_detalhes_ficam_recolhidos_e_abrem_com_enter(tmp_path):
    app = _app(tmp_path, familias=FAMILIAS[1:2])

    async with app.run_test(size=(100, 40)) as pilot:
        await _abrir_loop(pilot, app, "1")
        await app.workers.wait_for_complete()
        await _esperar(pilot, lambda: any(w.item.nome == "verificar_nomenclatura_peca" for w in app.query(ItemEvento)))

        verificacao = next(w for w in app.query(ItemEvento) if w.item.nome == "verificar_nomenclatura_peca")
        assert not verificacao.query(".item-detalhes")
        verificacao.focus()
        await pilot.press("enter")
        await pilot.pause()

        (detalhes,) = verificacao.query(".item-detalhes")
        conteudo = str(detalhes.renderable)
        assert '"nome_sugerido": "PIVO SUPERIOR"' in conteudo
        assert str(verificacao.query_one(".item-linha").renderable).endswith(" ▾")


async def test_operador_expande_familia_recolhida(tmp_path):
    app = _app(tmp_path, familias=FAMILIAS[:1])

    async with app.run_test(size=(100, 32)) as pilot:
        await _abrir_loop(pilot, app, "1")
        await app.workers.wait_for_complete()
        await _esperar(pilot, lambda: _painel(app).blocos and not _painel(app).blocos[0].expandido)

        bloco = _painel(app).blocos[0]
        assert not bloco.query(ItemEvento)
        bloco.focus()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert bloco.expandido
        assert bloco.query(ItemEvento)


async def test_cabecalho_compacto_nas_telas_de_execucao_e_grande_no_menu(tmp_path):
    app = _app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        cabecalho = app.query_one(Cabecalho)
        assert not cabecalho.compacto
        assert app.query_one("#cabecalho-subtitulo").display

        await pilot.click("#btn-rodar-loop")
        await pilot.pause()
        assert cabecalho.compacto and cabecalho.display
        assert cabecalho.region.height == 2
        assert str(app.query_one("#cabecalho-compacto", Static).renderable) == "ESTAGIÁRIO · Data validation agent"

        app.query_one(ContentSwitcher).current = "executar-caso"
        await pilot.pause()
        assert cabecalho.compacto

        app.query_one(ContentSwitcher).current = "menu-principal"
        await pilot.pause()
        assert not cabecalho.compacto


async def test_painel_ocupa_a_maior_parte_da_tela_em_80x24(tmp_path):
    app = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.click("#btn-rodar-loop")
        await pilot.pause()
        rolagem = app.query_one("#loop-rolagem")
        assert rolagem.region.y <= 5
        assert rolagem.region.height >= 15


async def test_cancelar_e_voltar_continuam_funcionando(tmp_path):
    llm = LLMRoteirizado(pausar_em={"particao": 3})
    app = _app(tmp_path, llm=llm)

    async with app.run_test(size=(100, 32)) as pilot:
        try:
            await _abrir_loop(pilot, app, "3")
            await _esperar(pilot, llm.pausado.is_set)

            await pilot.click("#btn-voltar-loop")
            await pilot.pause()
            assert "Cancele o loop antes de voltar." in str(app.query_one("#loop-status", Static).renderable)
            assert app.query_one(ContentSwitcher).current == "rodar-loop"

            await pilot.click("#btn-cancelar-loop")
            await pilot.pause()
        finally:
            llm.continuar.set()
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)
        assert "cancelado" in str(app.query_one("#loop-status", Static).renderable)
        assert len(_painel(app).blocos) == 1

        await pilot.click("#btn-voltar-loop")
        await pilot.pause()
        assert app.query_one(ContentSwitcher).current == "menu-principal"


async def test_intervencao_humana_aparece_no_painel_e_continua_o_loop(tmp_path):
    caso = CasoRoteirizado(intervir=frozenset({"JE4699"}))
    app = _app(tmp_path, caso=caso, familias=FAMILIAS[2:])
    resposta = RespostaIntervencao(
        resposta_humana="É o superior.", regra=RegraProposta(titulo="Pivô", condicao="c", resolucao="r"),
        valor="PIVO SUPERIOR", origem_id=201,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        painel = None
        try:
            await pilot.pause()
            painel = _painel(app)  # app.query só enxerga a tela ativa, que vira a modal
            await _abrir_loop(pilot, app, "1")
            await _esperar(pilot, lambda: isinstance(app.screen, IntervencaoScreen))
            await _esperar(pilot, lambda: any(w.item.tipo == "intervencao" for w in painel.query(ItemEvento)))
            espera = next(w for w in painel.query(ItemEvento) if w.item.tipo == "intervencao")
            assert "aguardando o operador" in str(espera.query_one(".item-linha").renderable)

            app.screen.dismiss(resposta)
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
        finally:
            if isinstance(app.screen, IntervencaoScreen):
                app.screen.dismiss(None)
                await app.workers.wait_for_complete()

        assert caso.respostas_intervencao == [resposta]
        assert espera.item.estado == "ok"
        assert "regra 'Pivô'" in espera.item.resumo
        assert painel.blocos[0].familia.estado == "ok"


@pytest.mark.parametrize(("tamanho", "com_familia"), [((80, 24), False), ((120, 40), True)])
async def test_status_do_loop_cabe_na_barra(tmp_path, tamanho, com_familia):
    llm = LLMRoteirizado(pausar_em={"particao": 3})
    app = _app(tmp_path, llm=llm, familias=FAMILIAS[:1])

    async with app.run_test(size=tamanho) as pilot:
        try:
            await _abrir_loop(pilot, app, "1")
            await _esperar(pilot, llm.pausado.is_set)
            status = app.query_one("#loop-status", Static)
            await _esperar(pilot, lambda: str(status.renderable).startswith("executando 1/1"))

            texto = str(status.renderable)
            assert cell_len(texto) <= status.size.width
            assert ("83061/CITROEN" in texto) is com_familia
        finally:
            llm.continuar.set()
        await app.workers.wait_for_complete()


async def test_mensagens_longas_do_status_quebram_em_vez_de_cortar(tmp_path):
    llm = LLMRoteirizado(pausar_em={"particao": 3})
    app = _app(tmp_path, llm=llm, familias=FAMILIAS[:1])

    def _cabe_inteira(status: Static) -> bool:
        texto = str(status.renderable)
        return status.size.height > 1 and status.size.height * status.size.width >= cell_len(texto)

    async with app.run_test(size=(80, 24)) as pilot:
        await _abrir_loop(pilot, app, "0")
        # O Button do Textual ignora um clique durante o efeito de "ativo" (~0,2 s).
        await pilot.pause(0.3)
        status = app.query_one("#loop-status", Static)
        assert str(status.renderable) == "Informe um inteiro positivo para o número de iterações."
        assert _cabe_inteira(status)

        try:
            app.query_one("#input-iteracoes").value = "1"
            await pilot.click("#btn-iniciar-loop")
            await _esperar(pilot, llm.pausado.is_set)
            await _esperar(pilot, lambda: str(status.renderable).startswith("executando"))
            assert status.size.height == 1

            await pilot.click("#btn-cancelar-loop")
            await pilot.pause()
            assert str(status.renderable).startswith("Cancelamento solicitado")
            assert _cabe_inteira(status)
            assert app.query_one("#loop-rolagem").region.height >= 14
        finally:
            llm.continuar.set()
        await app.workers.wait_for_complete()


@pytest.mark.parametrize("tamanho", [(80, 24), (120, 40)])
async def test_nenhuma_falha_de_desenho_em_tamanhos_diferentes(tmp_path, tamanho):
    app = _app(tmp_path)

    async with app.run_test(size=tamanho) as pilot:
        await _abrir_loop(pilot, app, "3")
        await app.workers.wait_for_complete()
        await _esperar(pilot, lambda: len(_painel(app).blocos) == 3)
        assert _painel(app).ultimo_erro is None
        assert app.query(FamilyBlock)


# --- tabelas de registros sorteados e do vencedor ---------------------------------------

COLUNAS_SORTEADOS = [
    " ", "id", "name", "born_at", "deprecated_at", "width", "depth", "height",
    "gross_weight", "net_weight", "ncm", "barcode", "similarity_id",
]


def _secoes(bloco: FamilyBlock) -> list[str]:
    """Ordem dos filhos do bloco: título, sorteados, corpo (etapas) e vencedor."""
    return [next(c for c in w.classes if c.startswith("familia-")) for w in bloco.children]


def _colunas(tabela: DataTable) -> list[str]:
    return [str(coluna.label) for coluna in tabela.columns.values()]


def _celulas(tabela: DataTable, linha: int) -> list[str]:
    return [str(celula) for celula in tabela.get_row_at(linha)]


def _todos_terminaram(app) -> bool:
    blocos = _painel(app).blocos
    return len(blocos) == 3 and all(bloco.query(".familia-vencedor") for bloco in blocos)


@pytest.mark.parametrize("tamanho", [(80, 24), (120, 40)])
async def test_tabelas_de_registros_ficam_visiveis_com_a_familia_recolhida(tmp_path, tamanho):
    app = _app(tmp_path)

    async with app.run_test(size=tamanho) as pilot:
        await _abrir_loop(pilot, app, "3")
        await app.workers.wait_for_complete()
        await _esperar(pilot, lambda: _todos_terminaram(app))
        await pilot.pause(0.3)

        primeira, sinalizada, terceira = _painel(app).blocos
        assert _painel(app).ultimo_erro is None
        assert not primeira.expandido and not terceira.expandido
        assert _secoes(primeira) == ["familia-titulo", "familia-registros", "familia-vencedor"]

        sorteados = primeira.query_one(".tabela-registros", DataTable)
        assert str(primeira.query_one(".familia-registros > .tabela-legenda", Static).renderable) == (
            "Registros sorteados · 4"
        )
        assert _colunas(sorteados) == COLUNAS_SORTEADOS
        assert [_celulas(sorteados, i)[1] for i in range(sorteados.row_count)] == ["101", "102", "103", "104"]
        assert _celulas(sorteados, 1)[2:] == [
            "PIVO DA SUSPENSAO SUPERIOR", "1996", "2001", "16.0", "", "42.0", "0.62", "", "", "", "977",
        ]
        assert sorteados.max_scroll_y == 0, "as 4 linhas cabem sem rolagem vertical"

        legenda = primeira.query_one(".familia-vencedor > .tabela-legenda", Static)
        assert str(legenda.renderable) == "Registro vencedor · mantém 101 (remove 102) · valores depois do merge"
        vencedor = primeira.query_one(".tabela-vencedor", DataTable)
        colunas = _colunas(vencedor)
        assert colunas == [*COLUNAS_SORTEADOS, "search_ref", "brand", "brand_id", "created", "application"]
        assert vencedor.row_count == 1
        valores = dict(zip(colunas, _celulas(vencedor, 0)))
        # Valores depois do merge: nome e aplicação decididos; peso e código de barras do próprio 101.
        assert {k: valores[k] for k in ("id", "name", "ncm", "gross_weight", "barcode", "similarity_id")} == {
            "id": "101", "name": "PIVO SUPERIOR", "ncm": "87088000", "gross_weight": "0.41",
            "barcode": "7891234500101", "similarity_id": "812",
        }
        assert {k: valores[k] for k in ("search_ref", "brand", "brand_id", "created", "application")} == {
            "search_ref": "83061", "brand": "CITROEN", "brand_id": "3", "created": "2019-03-04 10:15",
            "application": "GOL 1.0 1991/2001 (+1 linha(s))",
        }
        assert vencedor.max_scroll_y == 0
        # Mesma cor do 101 nas duas tabelas.
        assert vencedor.get_row_at(0)[0].style == sorteados.get_row_at(0)[0].style

        assert sinalizada.expandido
        assert _secoes(sinalizada) == ["familia-titulo", "familia-registros", "familia-corpo", "familia-vencedor"]
        assert not sinalizada.query(".tabela-vencedor")
        assert str(sinalizada.query_one(".familia-vencedor > .tabela-legenda", Static).renderable) == (
            "Nenhum registro vencedor: 1 grupo(s) sinalizado(s) para revisão, sem merge automático."
        )


async def test_tabela_dos_sorteados_aparece_enquanto_a_familia_roda(tmp_path):
    llm = LLMRoteirizado(pausar_em={"particao": 3})
    app = _app(tmp_path, llm=llm, familias=FAMILIAS[:1])

    async with app.run_test(size=(100, 32)) as pilot:
        try:
            await _abrir_loop(pilot, app, "1")
            await _esperar(pilot, llm.pausado.is_set)
            await _esperar(pilot, lambda: _painel(app).query(".tabela-registros"))
            await pilot.pause(0.2)

            (bloco,) = _painel(app).blocos
            assert bloco.familia.em_curso
            assert bloco.query_one(".tabela-registros", DataTable).row_count == 4
            assert _secoes(bloco) == ["familia-titulo", "familia-registros", "familia-corpo"]
            assert not bloco.query(".familia-vencedor")
        finally:
            llm.continuar.set()
        await app.workers.wait_for_complete()
        await _esperar(pilot, lambda: _painel(app).query(".tabela-vencedor"))


async def test_reabrir_familia_recolhida_poe_as_etapas_entre_as_tabelas(tmp_path):
    app = _app(tmp_path, familias=FAMILIAS[:1])

    async with app.run_test(size=(100, 32)) as pilot:
        await _abrir_loop(pilot, app, "1")
        await app.workers.wait_for_complete()
        await _esperar(pilot, lambda: _painel(app).query(".tabela-vencedor") and not _painel(app).blocos[0].expandido)

        bloco = _painel(app).blocos[0]
        bloco.focus()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert bloco.expandido
        assert _secoes(bloco) == ["familia-titulo", "familia-registros", "familia-corpo", "familia-vencedor"]

        await pilot.press("enter")
        await pilot.pause(0.2)
        assert _secoes(bloco) == ["familia-titulo", "familia-registros", "familia-vencedor"]


class _AppPainel(App):
    def __init__(self, painel) -> None:
        super().__init__()
        self.painel = painel

    def compose(self) -> ComposeResult:
        yield self.painel


async def test_bloco_sincronizado_antes_de_montar_ainda_ganha_as_tabelas():
    """Sob carga, o painel pode sincronizar um bloco novo antes do `on_mount` dele."""
    familia = FamiliaSorteada("83061", 3, "CITROEN")
    ts = "2026-10-06T14:00:00.000+00:00"
    final = {"status": "merge", "id_mantido": 101, "ids_removidos": [102], "nome": "PIVO", "campos": {}}
    modelo = ModeloPainel()
    for evento in (
        IteracaoIniciada(timestamp=ts, indice=1, total=1, familia=familia),
        GrupoCarregado(timestamp=ts, grupo_ref="83061:CITROEN", registros=({"id": 101}, {"id": 102})),
        IteracaoConcluida(timestamp=ts, indice=1, total=1, status=IteracaoStatus.SUCESSO, familia=familia,
                          merges=1, registros_finais=(final,)),
    ):
        modelo.aplicar(evento)
    bloco = FamilyBlock(modelo.familias[0], mostrar_registros=True)
    bloco.sincronizar()  # ainda fora da árvore

    app = _AppPainel(bloco)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        assert _secoes(bloco) == ["familia-titulo", "familia-registros", "familia-vencedor"]


@pytest.mark.parametrize("mostrar", [False, True])
async def test_so_o_painel_com_mostrar_registros_desenha_as_tabelas(mostrar):
    familia = FamiliaSorteada("83061", 3, "CITROEN")
    ts = "2026-10-06T14:00:00.000+00:00"
    # Colchetes num nome não podem virar markup na célula.
    registro = {"id": 101, "name": "PIVO [bold]SUPERIOR[/bold]", "born_at": 1991}
    app = _AppPainel(ExecutionPanel(mostrar_registros=mostrar))

    async with app.run_test() as pilot:
        for evento in (
            IteracaoIniciada(timestamp=ts, indice=1, total=1, familia=familia),
            GrupoCarregado(timestamp=ts, grupo_ref="83061:CITROEN", registros=(registro,)),
            IteracaoConcluida(timestamp=ts, indice=1, total=1, status=IteracaoStatus.SUCESSO, familia=familia),
        ):
            app.painel.publicar(evento)
        app.painel.drenar_agora()
        await pilot.pause(0.2)

        assert app.painel.ultimo_erro is None
        assert app.painel.modelo.familias[0].registros == (registro,)
        assert bool(app.painel.query(".familia-registros")) is mostrar
        assert bool(app.painel.query(".familia-vencedor")) is mostrar
        if mostrar:
            celula = app.painel.query_one(".tabela-registros", DataTable).get_row_at(0)[2]
            assert isinstance(celula, Text) and celula.plain == "PIVO [bold]SUPERIOR[/bold]"


@pytest.mark.parametrize("tamanho", [(80, 24), (120, 40)])
async def test_loop_termina_com_a_rolagem_no_fim(tmp_path, tamanho):
    app = _app(tmp_path)

    async with app.run_test(size=tamanho) as pilot:
        await _abrir_loop(pilot, app, "3")
        await app.workers.wait_for_complete()
        await _esperar(pilot, lambda: _todos_terminaram(app))
        rolagem = app.query_one("#loop-rolagem")
        # A tabela do vencedor só ganha altura um refresh depois de montada.
        await _esperar(pilot, lambda: rolagem.max_scroll_y > 0 and rolagem.scroll_y >= rolagem.max_scroll_y)


class _AppRolagem(App):
    def __init__(self, conteudo_abaixo: int = 0) -> None:
        super().__init__()
        self.painel = ExecutionPanel(mostrar_registros=True)
        self._conteudo_abaixo = conteudo_abaixo

    def compose(self) -> ComposeResult:
        from textual.containers import VerticalScroll

        with VerticalScroll(id="rolagem"):
            yield self.painel
            if self._conteudo_abaixo:
                yield Static("\n".join(f"resultado {i}" for i in range(self._conteudo_abaixo)), id="abaixo")


def _publicar_familia(painel: ExecutionPanel, indice: int) -> None:
    ts = "2026-10-06T14:00:00.000+00:00"
    familia = FamiliaSorteada(f"REF{indice}", indice, "MARCA")
    registros = tuple({"id": indice * 10 + n, "name": f"PECA {n}"} for n in range(4))
    final = {
        "status": "merge", "id_mantido": indice * 10, "ids_removidos": [indice * 10 + 1], "nome": "PECA 0",
        "campos": {},
    }
    for evento in (
        IteracaoIniciada(timestamp=ts, indice=indice, total=9, familia=familia),
        GrupoCarregado(timestamp=ts, grupo_ref=f"REF{indice}:MARCA", registros=registros),
        IteracaoConcluida(
            timestamp=ts, indice=indice, total=9, status=IteracaoStatus.SUCESSO, familia=familia,
            pecas=4, merges=1, registros_finais=(final,),
        ),
    ):
        painel.publicar(evento)


async def test_painel_segue_o_fim_mas_nao_puxa_quem_subiu_a_rolagem():
    app = _AppRolagem()

    async with app.run_test(size=(80, 24)) as pilot:
        rolagem = app.query_one("#rolagem")
        for indice in range(1, 4):
            _publicar_familia(app.painel, indice)
        await _esperar(pilot, lambda: rolagem.max_scroll_y > 0 and rolagem.scroll_y >= rolagem.max_scroll_y)

        rolagem.scroll_home(animate=False)
        await pilot.pause(0.2)
        _publicar_familia(app.painel, 4)
        await pilot.pause(0.5)
        assert rolagem.scroll_y == 0, "o operador subiu: o painel não pode puxá-lo para o fim"

        rolagem.scroll_end(animate=False)
        await pilot.pause(0.2)
        _publicar_familia(app.painel, 5)
        await _esperar(pilot, lambda: len(app.painel.blocos) == 5 and rolagem.scroll_y >= rolagem.max_scroll_y)


async def test_com_conteudo_abaixo_do_painel_o_crescimento_dele_nao_puxa_a_rolagem():
    """Caso da "Rodar testes": o resultado final fica abaixo do painel e a tela volta ao topo."""
    app = _AppRolagem(conteudo_abaixo=60)

    async with app.run_test(size=(80, 24)) as pilot:
        rolagem = app.query_one("#rolagem")
        await pilot.pause(0.2)
        app.painel._fim_seguido = 0  # o painel acompanhou o fim enquanto ainda cabia na tela
        assert rolagem.scroll_y == 0

        _publicar_familia(app.painel, 1)
        await _esperar(pilot, lambda: app.painel.query(".tabela-vencedor"))
        await pilot.pause(0.5)

        assert rolagem.max_scroll_y > 0
        assert rolagem.scroll_y == 0
