"""Testes headless do seletor "Fallback stealth" da tela "Rodar testes"
(stealth-fallback-integration, Req 11.1–11.8).

Sem rede, banco nem navegador: `executar_caso_fn` é fake e registra os
`kwargs`, `resolver_brand_id`/`sortear_grupo_aleatorio` são substituídos e a
configuração dos seletores vem de um `ler_config` fake. As funções de `config`
que leriam o ambiente falham se chamadas, provando que a tela não as usa quando
`ler_config` é injetado. Interação só por teclado (`pilot.press`) e
`Button.press()`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from textual.widgets import Button, Label, RichLog, Static

import config
import tui.screens.executar_caso_screen as modulo_tela
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.test_executar_caso_screen_seletores import (
    _aguardar,
    _ambiente,
    _AppHost,
    _ExecutarCasoFake,
    _rodar_caso,
    _stat_env,
    _sw,
    _texto,
)
from tui.seletores_execucao import ConfigSeletoresTela, TEXTO_STEALTH_DEPENDENTE

_SW_STEALTH = "switch-fallback-stealth"
_ESTADO_STEALTH = "estado-fallback-stealth"
_BLOQUEADO = " — bloqueado durante a execução"


@pytest.fixture(autouse=True)
def _isolamento(monkeypatch, tmp_path):
    """Sem banco, sem leitura de configuração pela tela, sem alterar ambiente/.env
    (Req 11.8)."""
    ambiente_antes = _ambiente()
    env_antes = _stat_env()

    def _nao_ler(*_a, **_k):
        raise AssertionError("a tela não deve ler o ambiente com ler_config injetado")

    monkeypatch.setattr(config, "web_verification_metodo", _nao_ler)
    monkeypatch.setattr(config, "coleta_habilitada", _nao_ler)
    monkeypatch.setattr(config, "coleta_stealth_habilitada", _nao_ler)
    monkeypatch.setattr(modulo_tela, "resolver_brand_id", lambda marca: 4242)
    monkeypatch.setattr(
        modulo_tela, "sortear_grupo_aleatorio",
        lambda: SimpleNamespace(search_ref="SORTEADO", brand_id=77),
    )
    monkeypatch.setattr(modulo_tela, "_LOGS_DIR", tmp_path / "logs")
    yield
    assert _ambiente() == ambiente_antes
    assert _stat_env() == env_antes


def _cfg(stealth: str = "habilitada", coleta: str = "habilitada") -> ConfigSeletoresTela:
    return ConfigSeletoresTela(
        metodo_bruto="serper", estado_chave=coleta, estado_chave_stealth=stealth
    )


async def _espaco(app, pilot, id_: str) -> None:
    _sw(app, id_).focus()
    await pilot.press("space")
    await pilot.pause()


def _primeira_linha(app) -> str:
    return app.query_one("#avisos-web", RichLog).lines[0].text.rstrip()


# ---------------------------------------------------------------- 11.1


async def test_rotulo_tooltip_e_ordem_logo_apos_coleta():
    app = _AppHost(_cfg(), _ExecutarCasoFake())
    async with app.run_test() as pilot:
        await pilot.pause()
        switch = _sw(app, _SW_STEALTH)
        assert [str(lbl.renderable) for lbl in switch.parent.query(Label)] == ["Fallback stealth"]
        assert switch.tooltip == "Fallback stealth"

        # DOM: a linha do stealth vem logo depois da linha da coleta.
        linhas = list(app.query_one("#seletores-execucao").query(".seletor-linha"))
        assert [linha.query_one("Switch").id for linha in linhas] == [
            "switch-pesquisa-web", "switch-coleta-html", _SW_STEALTH,
        ]
        ids_dom = [w.id for w in app.query("*") if w.id]
        assert ids_dom.index("switch-coleta-html") < ids_dom.index(_SW_STEALTH) < ids_dom.index("btn-aleatorio")

        # Foco: pesquisa → coleta → stealth → botões de execução.
        ids_foco = [w.id for w in app.screen.focus_chain]
        i_coleta = ids_foco.index("switch-coleta-html")
        assert ids_foco[i_coleta + 1] == _SW_STEALTH
        alvo = ["switch-pesquisa-web", "switch-coleta-html", _SW_STEALTH, "btn-aleatorio",
                "btn-caso-0", "btn-caso-1", "btn-caso-2", "btn-caso-3"]
        posicoes = [ids_foco.index(i) for i in alvo]
        assert posicoes == sorted(posicoes)


async def test_tab_foca_e_espaco_enter_alternam():
    app = _AppHost(_cfg(), _ExecutarCasoFake())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.set_focus(None)
        for _ in range(12):
            await pilot.press("tab")
            if app.focused is not None and app.focused.id == "switch-coleta-html":
                break
        assert app.focused.id == "switch-coleta-html"
        await pilot.press("tab")
        assert app.focused.id == _SW_STEALTH

        await pilot.press("space")
        await pilot.pause()
        assert _sw(app, _SW_STEALTH).value is False
        assert _texto(app, _ESTADO_STEALTH) == "desligado"

        await pilot.press("enter")
        await pilot.pause()
        assert _sw(app, _SW_STEALTH).value is True
        assert _texto(app, _ESTADO_STEALTH) == "ligado"


# ---------------------------------------------------------------- 11.2, 11.3


@pytest.mark.parametrize(
    ("estado_chave", "valor", "texto"),
    [("habilitada", True, "ligado"), ("desabilitada", False, "desligado")],
)
async def test_estado_inicial_pela_chave(estado_chave, valor, texto):
    app = _AppHost(_cfg(stealth=estado_chave), _ExecutarCasoFake())
    async with app.run_test() as pilot:
        await pilot.pause()
        switch = _sw(app, _SW_STEALTH)
        assert switch.value is valor and switch.disabled is False
        assert _texto(app, _ESTADO_STEALTH) == texto
        assert app.query_one("#aviso-config-seletores", Static).display is False


async def test_chave_invalida_comeca_desligado_com_aviso_e_pode_ligar():
    fake = _ExecutarCasoFake()
    app = _AppHost(_cfg(stealth="invalida"), fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        switch = _sw(app, _SW_STEALTH)
        assert switch.value is False and switch.disabled is False
        assert _texto(app, _ESTADO_STEALTH) == "desligado"
        aviso = app.query_one("#aviso-config-seletores", Static)
        assert aviso.display is True
        assert "ESTAGIARIO_COLETA_STEALTH_HABILITADA" in str(aviso.renderable)

        await _espaco(app, pilot, _SW_STEALTH)
        assert switch.value is True
        await _rodar_caso(app, pilot)
        assert fake.chamadas[-1][1]["fallback_stealth"] is True


# ---------------------------------------------------------------- 11.4, 11.5


@pytest.mark.parametrize("via", ["switch-coleta-html", "switch-pesquisa-web"])
@pytest.mark.parametrize("ultima_escolha", [True, False])
async def test_dependencia_da_coleta_e_restauracao(via, ultima_escolha):
    app = _AppHost(_cfg(), _ExecutarCasoFake())
    async with app.run_test() as pilot:
        await pilot.pause()
        stealth = _sw(app, _SW_STEALTH)
        if not ultima_escolha:
            await _espaco(app, pilot, _SW_STEALTH)
        assert stealth.value is ultima_escolha

        await _espaco(app, pilot, via)  # desliga coleta (direto ou via pesquisa)
        assert _sw(app, "switch-coleta-html").value is False
        assert stealth.value is False and stealth.disabled is True
        assert _texto(app, _ESTADO_STEALTH) == TEXTO_STEALTH_DEPENDENTE

        # Inoperável: alteração programática é revertida pelo modelo.
        stealth.toggle()
        await pilot.pause()
        assert stealth.value is False

        await _espaco(app, pilot, via)  # religa
        assert stealth.disabled is False
        assert stealth.value is ultima_escolha
        assert _texto(app, _ESTADO_STEALTH) == ("ligado" if ultima_escolha else "desligado")


# ---------------------------------------------------------------- 11.6, 11.7


@pytest.mark.parametrize("valor", [True, False])
async def test_bloqueado_durante_execucao_e_valor_mantido(valor):
    fake = _ExecutarCasoFake(bloquear=True)
    app = _AppHost(_cfg(stealth="habilitada" if valor else "desabilitada"), fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        stealth = _sw(app, _SW_STEALTH)
        texto_livre = "ligado" if valor else "desligado"
        try:
            app.query_one("#btn-caso-0", Button).press()
            await pilot.pause()
            assert await _aguardar(fake.iniciou)
            await pilot.pause()

            assert stealth.disabled is True and stealth.value is valor
            assert _texto(app, _ESTADO_STEALTH) == texto_livre + _BLOQUEADO

            stealth.toggle()
            await pilot.pause()
            assert stealth.value is valor
        finally:
            fake.liberar.set()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert stealth.disabled is False and stealth.value is valor
        assert _texto(app, _ESTADO_STEALTH) == texto_livre
        assert fake.chamadas[-1][1]["fallback_stealth"] is valor
        assert f"fallback stealth={texto_livre}." in _primeira_linha(app)


async def test_opcao_repassada_e_mensagem_de_configuracao():
    fake = _ExecutarCasoFake()
    app = _AppHost(_cfg(), fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _rodar_caso(app, pilot, "#btn-caso-1")
        kwargs = fake.chamadas[-1][1]
        assert (kwargs["coleta_html"], kwargs["fallback_stealth"]) == (True, True)
        assert _primeira_linha(app).endswith("fallback stealth=ligado.")

        await _espaco(app, pilot, _SW_STEALTH)
        await _rodar_caso(app, pilot, "#btn-aleatorio")
        args, kwargs = fake.chamadas[-1]
        assert args == ("SORTEADO", 77)
        assert kwargs["fallback_stealth"] is False
        assert _primeira_linha(app).endswith("fallback stealth=desligado.")

        # Religado, mas com a coleta desligada a opção sai desligada.
        await _espaco(app, pilot, _SW_STEALTH)
        await _espaco(app, pilot, "switch-coleta-html")
        await _rodar_caso(app, pilot)
        kwargs = fake.chamadas[-1][1]
        assert (kwargs["coleta_html"], kwargs["fallback_stealth"]) == (False, False)
        assert _primeira_linha(app) == (
            "Configuração da execução: pesquisa web=ligada (método serper), "
            "coleta de HTML=desligada, fallback stealth=desligado."
        )
