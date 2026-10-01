"""Testes headless dos seletores "Pesquisa web" e "Coleta de HTML" da tela
"Rodar testes" (html-extract-on-web-search, Req 11.21).

Sem rede nem banco: `executar_caso_fn` é fake e registra os `kwargs`,
`resolver_brand_id`/`sortear_grupo_aleatorio` são substituídos e a configuração
dos seletores vem de um `ler_config` fake. As funções de `config` que leriam o
ambiente são trocadas por funções que falham, provando que a tela não as usa
quando `ler_config` é injetado.
"""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, ContentSwitcher, Label, RichLog, Static, Switch

import config
import tui.screens.executar_caso_screen as modulo_tela
from partitioning.models import Particao
from pipeline import ResultadoCaso
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tui.screens.executar_caso_screen import ExecutarCasoScreen
from tui.screens.menu_screen import MenuPrincipal
from tui.seletores_execucao import ConfigSeletoresTela, TEXTO_COLETA_DEPENDENTE

_ENV_PATH = config.PROJECT_ROOT / ".env"


# ---------------------------------------------------------------- fixtures


def _stat_env() -> tuple[int, int] | None:
    if not _ENV_PATH.exists():
        return None
    st = _ENV_PATH.stat()
    return (st.st_size, st.st_mtime_ns)


def _ambiente() -> dict[str, str]:
    # PYTEST_CURRENT_TEST é escrito pelo próprio pytest entre setup e teardown.
    return {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}


@pytest.fixture(autouse=True)
def _isolamento(monkeypatch, tmp_path):
    """Sem banco, sem leitura de configuração pela tela, sem alterar ambiente/.env
    (Req 11.16, 11.21)."""
    ambiente_antes = _ambiente()
    env_antes = _stat_env()

    def _nao_ler(*_a, **_k):
        raise AssertionError("a tela não deve ler o ambiente com ler_config injetado")

    monkeypatch.setattr(config, "web_verification_metodo", _nao_ler)
    monkeypatch.setattr(config, "coleta_habilitada", _nao_ler)
    monkeypatch.setattr(modulo_tela, "resolver_brand_id", lambda marca: 4242)
    monkeypatch.setattr(
        modulo_tela, "sortear_grupo_aleatorio",
        lambda: SimpleNamespace(search_ref="SORTEADO", brand_id=77),
    )
    monkeypatch.setattr(modulo_tela, "_LOGS_DIR", tmp_path / "logs")
    yield
    assert _ambiente() == ambiente_antes
    assert _stat_env() == env_antes


def _resultado_vazio(search_ref="X", brand_id=0) -> ResultadoCaso:
    return ResultadoCaso(
        grupo_ref=f"{search_ref}:{brand_id}",
        particao=Particao(grupo_ref="fake", subclusters=[]),
        decisoes=[],
        sql="",
    )


class _ExecutarCasoFake:
    """Registra cada chamada; opcionalmente emite um aviso e/ou bloqueia até `liberar`."""

    def __init__(self, *, aviso: str | None = None, bloquear: bool = False) -> None:
        self.chamadas: list[tuple[tuple, dict]] = []
        self.aviso = aviso
        self.iniciou = threading.Event()
        self.liberar = threading.Event()
        if not bloquear:
            self.liberar.set()

    def __call__(self, search_ref, brand_id, llm, dependencias_fk, **kwargs):
        self.chamadas.append(((search_ref, brand_id), kwargs))
        self.iniciou.set()
        if self.aviso and kwargs.get("on_aviso"):
            kwargs["on_aviso"](self.aviso)
        assert self.liberar.wait(timeout=10), "fake não foi liberado"
        return _resultado_vazio(search_ref, brand_id)


class _FakeComErro(_ExecutarCasoFake):
    def __call__(self, *args, **kwargs):
        super().__call__(*args, **kwargs)
        raise RuntimeError("falhou")


class _AppHost(App):
    """Host mínimo: ContentSwitcher com menu e a tela, como `MenuScreen`."""

    CSS_PATH = None

    def __init__(self, cfg: ConfigSeletoresTela, fake: _ExecutarCasoFake, **kwargs) -> None:
        super().__init__(**kwargs)
        self._cfg = cfg
        self._fake = fake

    def compose(self) -> ComposeResult:
        with ContentSwitcher(initial="executar-caso"):
            yield MenuPrincipal(id="menu-principal")
            yield ExecutarCasoScreen(
                llm=None, dependencias_fk=[], executar_caso_fn=self._fake,
                ler_config=lambda: self._cfg, id="executar-caso",
            )


_CFG_PADRAO = ConfigSeletoresTela(metodo_bruto="serper", estado_chave="habilitada")


def _sw(app, id_: str) -> Switch:
    return app.query_one(f"#{id_}", Switch)


def _texto(app, id_: str) -> str:
    return str(app.query_one(f"#{id_}", Static).renderable)


async def _rodar_caso(app, pilot, botao: str = "#btn-caso-0") -> None:
    app.query_one(botao, Button).press()
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


# ---------------------------------------------------------------- 11.1, 11.2


async def test_rotulos_e_ordem_antes_dos_botoes_de_execucao():
    app = _AppHost(_CFG_PADRAO, _ExecutarCasoFake())
    async with app.run_test() as pilot:
        await pilot.pause()
        for id_switch, rotulo in (("switch-pesquisa-web", "Pesquisa web"), ("switch-coleta-html", "Coleta de HTML")):
            switch = _sw(app, id_switch)
            linha = switch.parent
            labels = [str(lbl.renderable) for lbl in linha.query(Label)]
            assert labels == [rotulo]
            assert switch.tooltip == rotulo

        ids_foco = [w.id for w in app.screen.focus_chain]
        alvo = ["switch-pesquisa-web", "switch-coleta-html", "btn-aleatorio",
                "btn-caso-0", "btn-caso-1", "btn-caso-2", "btn-caso-3"]
        posicoes = [ids_foco.index(i) for i in alvo]
        assert posicoes == sorted(posicoes)

        ids_dom = [w.id for w in app.query("*") if w.id]
        assert ids_dom.index("seletores-execucao") < ids_dom.index("btn-aleatorio")


async def test_tab_foca_seletores_e_espaco_enter_alternam():
    app = _AppHost(_CFG_PADRAO, _ExecutarCasoFake())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.set_focus(None)
        for _ in range(10):
            await pilot.press("tab")
            if app.focused is not None and app.focused.id == "switch-pesquisa-web":
                break
        assert app.focused.id == "switch-pesquisa-web"

        await pilot.press("tab")
        assert app.focused.id == "switch-coleta-html"
        await pilot.press("space")
        await pilot.pause()
        assert _sw(app, "switch-coleta-html").value is False
        assert _texto(app, "estado-coleta-html") == "desligada"
        await pilot.press("enter")
        await pilot.pause()
        assert _sw(app, "switch-coleta-html").value is True

        await pilot.press("shift+tab")
        assert app.focused.id == "switch-pesquisa-web"
        await pilot.press("enter")
        await pilot.pause()
        assert _sw(app, "switch-pesquisa-web").value is False
        assert _texto(app, "estado-pesquisa-web") == "desligada"


async def test_mouse_alterna_seletor():
    app = _AppHost(_CFG_PADRAO, _ExecutarCasoFake())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#switch-coleta-html")
        await pilot.pause()
        assert _sw(app, "switch-coleta-html").value is False


# ---------------------------------------------------------------- 11.4–11.7


@pytest.mark.parametrize(
    ("cfg", "coleta", "texto_pesquisa", "texto_coleta"),
    [
        (ConfigSeletoresTela("serper", "habilitada"), True, "ligada (método serper)", "ligada"),
        (ConfigSeletoresTela("", "habilitada"), True, "ligada (método playwright)", "ligada"),
        (ConfigSeletoresTela(" PlayWright ", "desabilitada"), False, "ligada (método playwright)", "desligada"),
    ],
)
async def test_estados_iniciais_validos(cfg, coleta, texto_pesquisa, texto_coleta):
    app = _AppHost(cfg, _ExecutarCasoFake())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _sw(app, "switch-pesquisa-web").value is True
        assert _sw(app, "switch-pesquisa-web").disabled is False
        assert _sw(app, "switch-coleta-html").value is coleta
        assert _sw(app, "switch-coleta-html").disabled is False
        assert _texto(app, "estado-pesquisa-web") == texto_pesquisa
        assert _texto(app, "estado-coleta-html") == texto_coleta
        assert app.query_one("#aviso-config-seletores", Static).display is False


async def test_estado_inicial_com_metodo_e_chave_invalidos():
    cfg = ConfigSeletoresTela(metodo_bruto="bingo", estado_chave="invalida")
    fake = _ExecutarCasoFake()
    app = _AppHost(cfg, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _sw(app, "switch-pesquisa-web").value is True
        assert _texto(app, "estado-pesquisa-web") == "ligada (método inválido)"
        assert _sw(app, "switch-coleta-html").value is False
        assert _texto(app, "estado-coleta-html") == "desligada"

        aviso = app.query_one("#aviso-config-seletores", Static)
        assert aviso.display is True
        texto_aviso = str(aviso.renderable)
        assert "ESTAGIARIO_WEB_VERIFICATION_METODO=bingo" in texto_aviso
        assert "ESTAGIARIO_COLETA_HABILITADA" in texto_aviso

        # Req 11.7: o operador pode ligar a coleta mesmo com a chave inválida.
        _sw(app, "switch-coleta-html").focus()
        await pilot.press("space")
        await pilot.pause()
        assert _sw(app, "switch-coleta-html").value is True

        await _rodar_caso(app, pilot)
        _, kwargs = fake.chamadas[-1]
        assert kwargs["pesquisa_web"] is True and kwargs["coleta_html"] is True
        primeira = app.query_one("#avisos-web", RichLog).lines[0].text.rstrip()
        assert "pesquisa web=ligada (método inválido: bingo)" in primeira


# ---------------------------------------------------------------- 11.8–11.11


async def test_dependencia_e_restauracao_da_ultima_escolha():
    app = _AppHost(_CFG_PADRAO, _ExecutarCasoFake())
    async with app.run_test() as pilot:
        await pilot.pause()
        coleta = _sw(app, "switch-coleta-html")
        pesquisa = _sw(app, "switch-pesquisa-web")

        # Última escolha = desligada; desligar e religar a pesquisa a restaura.
        coleta.focus()
        await pilot.press("space")
        await pilot.pause()
        assert coleta.value is False

        pesquisa.focus()
        await pilot.press("space")
        await pilot.pause()
        assert pesquisa.value is False
        assert coleta.value is False and coleta.disabled is True
        assert _texto(app, "estado-coleta-html") == TEXTO_COLETA_DEPENDENTE

        # Inoperável: tentativa programática é revertida pelo modelo (Req 11.9).
        coleta.toggle()
        await pilot.pause()
        assert coleta.value is False

        await pilot.press("space")
        await pilot.pause()
        assert pesquisa.value is True
        assert coleta.disabled is False and coleta.value is False

        # Agora última escolha = ligada; o ciclo restaura "ligada".
        coleta.focus()
        await pilot.press("space")
        await pilot.pause()
        assert coleta.value is True
        pesquisa.focus()
        await pilot.press("space")
        await pilot.pause()
        assert coleta.value is False and coleta.disabled is True
        await pilot.press("space")
        await pilot.pause()
        assert coleta.value is True and coleta.disabled is False
        assert _texto(app, "estado-coleta-html") == "ligada"


# ---------------------------------------------------------------- 11.12


async def test_opcoes_repassadas_por_caso_fixo_e_sorteio():
    fake = _ExecutarCasoFake()
    app = _AppHost(_CFG_PADRAO, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _rodar_caso(app, pilot, "#btn-caso-2")
        args, kwargs = fake.chamadas[-1]
        assert args == ("MB1085", 4242)
        assert (kwargs["pesquisa_web"], kwargs["coleta_html"]) == (True, True)

        _sw(app, "switch-coleta-html").focus()
        await pilot.press("space")
        await pilot.pause()
        await _rodar_caso(app, pilot, "#btn-aleatorio")
        args, kwargs = fake.chamadas[-1]
        assert args == ("SORTEADO", 77)
        assert (kwargs["pesquisa_web"], kwargs["coleta_html"]) == (True, False)

        _sw(app, "switch-coleta-html").focus()
        await pilot.press("space")  # última escolha volta a ligada
        _sw(app, "switch-pesquisa-web").focus()
        await pilot.press("space")
        await pilot.pause()
        await _rodar_caso(app, pilot, "#btn-caso-0")
        _, kwargs = fake.chamadas[-1]
        assert (kwargs["pesquisa_web"], kwargs["coleta_html"]) == (False, False)


# ---------------------------------------------------------------- 11.13, 11.14


@pytest.mark.parametrize("com_erro", [False, True])
async def test_seletores_bloqueados_durante_execucao(com_erro):
    fake = (_FakeComErro if com_erro else _ExecutarCasoFake)(bloquear=True)
    app = _AppHost(_CFG_PADRAO, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        pesquisa = _sw(app, "switch-pesquisa-web")
        coleta = _sw(app, "switch-coleta-html")
        coleta.focus()
        await pilot.press("space")  # coleta desligada no início
        await pilot.pause()
        try:
            app.query_one("#btn-caso-0", Button).press()
            await pilot.pause()
            assert await _aguardar(fake.iniciou)
            await pilot.pause()

            assert pesquisa.disabled is True and coleta.disabled is True
            assert "bloqueada durante a execução" in _texto(app, "estado-pesquisa-web")
            assert "bloqueada durante a execução" in _texto(app, "estado-coleta-html")

            # Teclado e alteração programática não mudam nada durante a execução.
            await pilot.press("space")
            pesquisa.toggle()
            coleta.toggle()
            await pilot.pause()
            assert pesquisa.value is True and coleta.value is False
        finally:
            fake.liberar.set()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert pesquisa.disabled is False and coleta.disabled is False
        assert pesquisa.value is True and coleta.value is False
        assert _texto(app, "estado-pesquisa-web") == "ligada (método serper)"
        assert _texto(app, "estado-coleta-html") == "desligada"
        _, kwargs = fake.chamadas[-1]
        assert (kwargs["pesquisa_web"], kwargs["coleta_html"]) == (True, False)
        if com_erro:
            textos = [str(s.renderable) for s in app.query("#resultado-container Static")]
            assert any("Erro: falhou" in t for t in textos)


async def _aguardar(evento: threading.Event, timeout: float = 5.0) -> bool:
    import asyncio

    return await asyncio.to_thread(evento.wait, timeout)


# ---------------------------------------------------------------- 11.17, 11.18, 8.7


async def test_mensagem_configuracao_primeira_linha_do_painel_e_do_arquivo(tmp_path):
    aviso = "Coleta de páginas: {\"desfecho\": \"nao_executada\", \"motivo\": \"desabilitada\"}"
    fake = _ExecutarCasoFake(aviso=aviso)
    app = _AppHost(_CFG_PADRAO, fake)
    async with app.run_test() as pilot:
        app.copy_to_clipboard = lambda _texto: None
        await pilot.pause()
        _sw(app, "switch-coleta-html").focus()
        await pilot.press("space")
        await pilot.pause()
        await _rodar_caso(app, pilot)

        esperado = (
            "Configuração da execução: pesquisa web=ligada (método serper), "
            "coleta de HTML=desligada, fallback stealth=desligado."
        )
        log = app.query_one("#avisos-web", RichLog)
        linhas = [linha.text.rstrip() for linha in log.lines]
        assert linhas[0] == esperado
        assert any(aviso in linha for linha in linhas[1:])

        app.query_one("#btn-copiar-log", Button).press()
        await pilot.pause()
        arquivos = list((tmp_path / "logs").glob("verificacao_web_*.log"))
        assert len(arquivos) == 1
        assert arquivos[0].read_text(encoding="utf-8").split("\n") == [esperado, aviso]

        # Nova execução: painel limpo, nova mensagem de configuração como primeira linha.
        _sw(app, "switch-pesquisa-web").focus()
        await pilot.press("space")
        await pilot.pause()
        await _rodar_caso(app, pilot)
        linhas = [linha.text.rstrip() for linha in log.lines]
        assert linhas[0] == (
            "Configuração da execução: pesquisa web=desligada, coleta de HTML=desligada, "
            "fallback stealth=desligado."
        )
        assert sum(linha.startswith("Configuração da execução:") for linha in linhas) == 1


# ---------------------------------------------------------------- 11.15


async def test_estado_preservado_ao_voltar_ao_menu():
    fake = _ExecutarCasoFake()
    app = _AppHost(_CFG_PADRAO, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        _sw(app, "switch-coleta-html").focus()
        await pilot.press("space")
        _sw(app, "switch-pesquisa-web").focus()
        await pilot.press("space")
        await pilot.pause()

        app.query_one("#btn-voltar", Button).press()
        await pilot.pause()
        assert app.query_one(ContentSwitcher).current == "menu-principal"
        app.query_one("#btn-rodar-testes", Button).press()
        await pilot.pause()
        assert app.query_one(ContentSwitcher).current == "executar-caso"

        assert _sw(app, "switch-pesquisa-web").value is False
        assert _sw(app, "switch-coleta-html").value is False
        assert _sw(app, "switch-coleta-html").disabled is True

        _sw(app, "switch-pesquisa-web").focus()
        await pilot.press("space")
        await pilot.pause()
        assert _sw(app, "switch-coleta-html").value is False  # última escolha preservada

        await _rodar_caso(app, pilot)
        _, kwargs = fake.chamadas[-1]
        assert (kwargs["pesquisa_web"], kwargs["coleta_html"]) == (True, False)
