"""Testes de propriedade da máquina de estados pura dos seletores da tela
"Rodar testes" com o Seletor_Fallback_Stealth (`tui/seletores_execucao.py`),
sem Textual.

Organização:
- estratégias compartilhadas (configuração da tela com a Chave_Stealth);
- Property 14: máquina de estados dos seletores com fallback
  (RuleBasedStateMachine com modelo independente) + verificação de que o
  módulo não carrega Textual;
- Property 15: mensagem de configuração com o fallback (tarefa 7.5).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from tui.seletores_execucao import (
    ConfigSeletoresTela,
    EstadoSeletores,
    OpcoesExecucao,
    mensagem_configuracao,
    texto_estado_coleta,
    texto_estado_pesquisa,
    texto_estado_stealth,
)

RAIZ = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Estratégias compartilhadas
# ---------------------------------------------------------------------------

metodos_brutos = st.one_of(
    st.sampled_from(
        ["serper", "playwright", "", "  ", " SERPER ", "Playwright", "bing", "invalido"]
    ),
    st.text(max_size=12),
)
estados_chave = st.sampled_from(["habilitada", "desabilitada", "invalida"])
configs_tela_stealth = st.builds(
    ConfigSeletoresTela,
    metodo_bruto=metodos_brutos,
    estado_chave=estados_chave,
    estado_chave_stealth=estados_chave,
)

# Textos esperados, escritos de forma independente do módulo de produção.
_VAR_STEALTH = "ESTAGIARIO_COLETA_STEALTH_HABILITADA"
_BLOQUEADA = " — bloqueada durante a execução"
_BLOQUEADO = " — bloqueado durante a execução"
_COLETA_DEPENDENTE = "desligada porque a pesquisa web está desligada"
_STEALTH_DEPENDENTE = "desligado porque a coleta de HTML está desligada"


def _metodo_efetivo_oraculo(bruto: str) -> str | None:
    """Oráculo independente do método efetivo: vazio → playwright; só
    serper/playwright são válidos."""
    metodo = bruto.strip().casefold() or "playwright"
    return metodo if metodo in {"serper", "playwright"} else None


# ---------------------------------------------------------------------------
# Feature: stealth-fallback-integration, Property 14: Máquina de estados dos seletores com fallback
# **Validates: Requirements 11.2, 11.3, 11.4, 11.5, 11.6**
# ---------------------------------------------------------------------------


@settings(max_examples=100)
class MaquinaSeletoresStealth(RuleBasedStateMachine):
    """Compara `EstadoSeletores` com um modelo mínimo mantido pelo teste:
    pesquisa, última escolha da coleta, última escolha do stealth e execução."""

    @initialize(cfg=configs_tela_stealth)
    def montar(self, cfg: ConfigSeletoresTela) -> None:
        self.cfg = cfg
        self.estado = EstadoSeletores.inicial(cfg)
        # Modelo: pesquisa ligada; coleta e fallback ligados só com as
        # respectivas chaves habilitadas (Req 11.2, 11.3).
        self.m_pesquisa = True
        self.m_escolha_coleta = cfg.estado_chave == "habilitada"
        self.m_escolha_stealth = cfg.estado_chave_stealth == "habilitada"
        self.m_em_execucao = False
        self.m_opcoes_inicio: OpcoesExecucao | None = None

        # Estado inicial: fallback ligado sse a Chave_Stealth está habilitada
        # (e a coleta está ligada, já que ele depende do valor dela).
        assert self.estado.ultima_escolha_stealth is (
            cfg.estado_chave_stealth == "habilitada"
        )
        assert self.estado.stealth is (
            cfg.estado_chave == "habilitada" and cfg.estado_chave_stealth == "habilitada"
        )
        # Aviso da chave stealth inválida sse ela é inválida (Req 11.3).
        avisos_stealth = [a for a in cfg.avisos if _VAR_STEALTH in a]
        if cfg.estado_chave_stealth == "invalida":
            assert len(avisos_stealth) == 1
            assert "fallback stealth" in avisos_stealth[0]
        else:
            assert avisos_stealth == []

    # -- modelo derivado ---------------------------------------------------

    @property
    def m_coleta(self) -> bool:
        return self.m_pesquisa and self.m_escolha_coleta

    @property
    def m_stealth(self) -> bool:
        return self.m_coleta and self.m_escolha_stealth

    # -- ações do operador -------------------------------------------------

    @rule(valor=st.booleans())
    def alternar_pesquisa(self, valor: bool) -> None:
        coleta_antes, stealth_antes = self.m_escolha_coleta, self.m_escolha_stealth
        self.estado = self.estado.alternar_pesquisa(valor)
        if not self.m_em_execucao:
            self.m_pesquisa = valor
        # A pesquisa nunca altera as últimas escolhas.
        assert self.estado.ultima_escolha_coleta == coleta_antes
        assert self.estado.ultima_escolha_stealth == stealth_antes

    @rule(valor=st.booleans())
    def alternar_coleta(self, valor: bool) -> None:
        stealth_antes = self.m_escolha_stealth
        self.estado = self.estado.alternar_coleta(valor)
        if self.m_pesquisa and not self.m_em_execucao:
            self.m_escolha_coleta = valor
        # A coleta nunca altera a última escolha do stealth (Req 11.5).
        assert self.estado.ultima_escolha_stealth == stealth_antes

    @rule(valor=st.booleans())
    def alternar_stealth(self, valor: bool) -> None:
        coleta_antes = self.m_escolha_coleta
        self.estado = self.estado.alternar_stealth(valor)
        # Operável só com o valor da coleta ligado e fora da execução (Req 11.4, 11.6).
        if self.m_coleta and not self.m_em_execucao:
            self.m_escolha_stealth = valor
        assert self.estado.ultima_escolha_coleta == coleta_antes

    @precondition(lambda self: not self.m_em_execucao)
    @rule()
    def iniciar(self) -> None:
        esperado = OpcoesExecucao(
            pesquisa_web=self.m_pesquisa,
            coleta_html=self.m_coleta,
            fallback_stealth=self.m_stealth,
        )
        self.estado, opcoes = self.estado.iniciar()
        # Opções = estado dos seletores no instante do início (Req 11.6).
        assert opcoes == esperado
        self.m_em_execucao = True
        self.m_opcoes_inicio = opcoes

    @precondition(lambda self: self.m_em_execucao)
    @rule()
    def terminar(self) -> None:
        self.estado = self.estado.terminar()
        self.m_em_execucao = False
        # Os valores do início da execução são preservados.
        o = self.m_opcoes_inicio
        assert o is not None
        assert self.estado.pesquisa == o.pesquisa_web
        assert self.estado.coleta == o.coleta_html
        assert self.estado.stealth == o.fallback_stealth

    # -- invariantes após cada passo ---------------------------------------

    @invariant()
    def estado_igual_ao_modelo(self) -> None:
        e = self.estado
        assert e.pesquisa == self.m_pesquisa
        assert e.em_execucao == self.m_em_execucao
        assert e.coleta == self.m_coleta
        if e.pesquisa:
            assert e.ultima_escolha_coleta == self.m_escolha_coleta
        # stealth == coleta and ultima_escolha_stealth; religar a coleta
        # (direta ou indiretamente) restaura a escolha anterior (Req 11.4, 11.5).
        assert e.stealth == (e.coleta and e.ultima_escolha_stealth)
        assert e.stealth == self.m_stealth
        if e.coleta:
            assert e.ultima_escolha_stealth == self.m_escolha_stealth

    @invariant()
    def operabilidade(self) -> None:
        e = self.estado
        # Invariantes da Property 18 do spec anterior.
        assert e.pesquisa_operavel == (not self.m_em_execucao)
        assert e.coleta_operavel == (self.m_pesquisa and not self.m_em_execucao)
        if not e.pesquisa:
            assert not e.coleta
            assert not e.coleta_operavel
        # Fallback: operável só com o valor da coleta ligado e fora da execução.
        assert e.stealth_operavel == (self.m_coleta and not self.m_em_execucao)
        if not e.coleta:
            assert not e.stealth
            assert not e.stealth_operavel

    @invariant()
    def em_execucao_ignora_alternancias(self) -> None:
        e = self.estado
        if not e.em_execucao:
            return
        assert self.m_opcoes_inicio is not None
        # Em execução, o fallback mantém o valor do início (não é desligado).
        assert e.stealth == self.m_opcoes_inicio.fallback_stealth
        for valor in (True, False):
            assert e.alternar_pesquisa(valor) == e
            assert e.alternar_coleta(valor) == e
            assert e.alternar_stealth(valor) == e

    @invariant()
    def textos_de_estado(self) -> None:
        e, cfg = self.estado, self.cfg
        tp = texto_estado_pesquisa(e, cfg)
        tc = texto_estado_coleta(e)
        ts = texto_estado_stealth(e)

        # Pesquisa e coleta (Property 18 do spec anterior).
        if self.m_pesquisa:
            metodo = _metodo_efetivo_oraculo(cfg.metodo_bruto)
            base = f"ligada (método {metodo})" if metodo else "ligada (método inválido)"
            assert tp == base + (_BLOQUEADA if self.m_em_execucao else "")
        else:
            assert tp == "desligada" + (_BLOQUEADA if self.m_em_execucao else "")

        if not self.m_pesquisa:
            base_c = _COLETA_DEPENDENTE
        else:
            base_c = "ligada" if self.m_escolha_coleta else "desligada"
        assert tc == base_c + (_BLOQUEADA if self.m_em_execucao else "")

        # Fallback stealth (Req 11.1, 11.4, 11.6).
        if not self.m_coleta:
            base_s = _STEALTH_DEPENDENTE
        else:
            base_s = "ligado" if self.m_escolha_stealth else "desligado"
        assert ts == base_s + (_BLOQUEADO if self.m_em_execucao else "")
        assert ts.startswith("ligado") or ts.startswith("desligado")

        for texto in (tp, tc):
            assert texto.startswith("ligada") or texto.startswith("desligada")


TestMaquinaSeletoresStealth = MaquinaSeletoresStealth.TestCase


def test_seletores_execucao_nao_carrega_textual() -> None:
    """O módulo puro dos seletores não importa Textual nem `tui.screens`."""
    codigo = (
        "import sys\n"
        "import tui.seletores_execucao\n"
        "proibidos = sorted(m for m in sys.modules\n"
        "    if m == 'textual' or m.startswith('textual.')\n"
        "    or m == 'tui.screens' or m.startswith('tui.screens.'))\n"
        "print(','.join(proibidos))\n"
    )
    saida = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert saida.stdout.strip() == "", f"módulos proibidos carregados: {saida.stdout}"


# ---------------------------------------------------------------------------
# Feature: stealth-fallback-integration, Property 15: Mensagem de configuração com o fallback
# **Validates: Requirements 11.7**
# ---------------------------------------------------------------------------

opcoes_execucao_stealth = st.builds(
    OpcoesExecucao,
    pesquisa_web=st.booleans(),
    coleta_html=st.booleans(),
    fallback_stealth=st.booleans(),
)


def _mensagem_configuracao_oraculo(o: OpcoesExecucao, cfg: ConfigSeletoresTela) -> str:
    """Oráculo independente: <P>/<C> como no spec anterior; <F> ligado/desligado."""
    if o.pesquisa_web:
        metodo = _metodo_efetivo_oraculo(cfg.metodo_bruto)
        p = f"ligada (método {metodo})" if metodo else f"ligada (método inválido: {cfg.metodo_bruto})"
    else:
        p = "desligada"
    c = "ligada" if o.coleta_html else "desligada"
    f = "ligado" if o.fallback_stealth else "desligado"
    return (
        f"Configuração da execução: pesquisa web={p}, coleta de HTML={c}, "
        f"fallback stealth={f}."
    )


@settings(max_examples=100)
@given(o=opcoes_execucao_stealth, cfg=configs_tela_stealth)
def test_mensagem_configuracao_com_fallback(
    o: OpcoesExecucao, cfg: ConfigSeletoresTela
) -> None:
    msg = mensagem_configuracao(o, cfg)
    assert msg == _mensagem_configuracao_oraculo(o, cfg)
    esperado = "ligado" if o.fallback_stealth else "desligado"
    assert msg.endswith(f", fallback stealth={esperado}.")


def test_mensagem_configuracao_exemplo_fallback_ligado() -> None:
    cfg = ConfigSeletoresTela(
        metodo_bruto="playwright",
        estado_chave="habilitada",
        estado_chave_stealth="habilitada",
    )
    opcoes = OpcoesExecucao(pesquisa_web=True, coleta_html=True, fallback_stealth=True)
    assert (
        mensagem_configuracao(opcoes, cfg)
        == "Configuração da execução: pesquisa web=ligada (método playwright), "
        "coleta de HTML=ligada, fallback stealth=ligado."
    )
