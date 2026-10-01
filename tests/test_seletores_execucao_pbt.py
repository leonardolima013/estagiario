"""Testes de propriedade da máquina de estados pura dos seletores da tela
"Rodar testes" (`tui/seletores_execucao.py`), sem Textual.

Organização:
- estratégias compartilhadas (configuração da tela);
- Property 18: máquina de estados dos seletores (RuleBasedStateMachine com
  oráculo independente) + verificação de que o módulo não carrega Textual;
- Property 19: mensagem de configuração da execução (tarefa 9.5).
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
configs_tela = st.builds(
    ConfigSeletoresTela, metodo_bruto=metodos_brutos, estado_chave=estados_chave
)


def _metodo_efetivo_oraculo(bruto: str) -> str | None:
    """Oráculo independente do método efetivo: vazio → playwright; só
    serper/playwright são válidos (Req 11.4, 11.5)."""
    metodo = bruto.strip().casefold() or "playwright"
    return metodo if metodo in {"serper", "playwright"} else None


# ---------------------------------------------------------------------------
# Feature: html-extract-on-web-search, Property 18: Máquina de estados dos seletores
# **Validates: Requirements 11.3, 11.4, 11.6, 11.7, 11.8, 11.9, 11.10, 11.11, 11.12, 11.13, 11.14**
# ---------------------------------------------------------------------------

_BLOQUEADA = "bloqueada durante a execução"
_DEPENDENTE = "desligada porque a pesquisa web está desligada"


@settings(max_examples=100)
class MaquinaSeletores(RuleBasedStateMachine):
    """Compara `EstadoSeletores` com um modelo mínimo mantido pelo teste."""

    @initialize(cfg=configs_tela)
    def montar(self, cfg: ConfigSeletoresTela) -> None:
        self.cfg = cfg
        self.estado = EstadoSeletores.inicial(cfg)
        # Modelo/oráculo (Req 11.4, 11.6, 11.7): pesquisa ligada; coleta ligada
        # só com a chave habilitada (desligada com chave inválida ou desabilitada).
        self.m_pesquisa = True
        self.m_escolha_coleta = cfg.estado_chave == "habilitada"
        self.m_em_execucao = False
        self.m_opcoes_inicio: OpcoesExecucao | None = None
        assert self.estado.pesquisa is True
        assert self.estado.coleta is (cfg.estado_chave == "habilitada")
        assert not self.estado.em_execucao

    # -- ações do operador -------------------------------------------------

    @rule(valor=st.booleans())
    def alternar_pesquisa(self, valor: bool) -> None:
        escolha_antes = self.m_escolha_coleta
        self.estado = self.estado.alternar_pesquisa(valor)
        if not self.m_em_execucao:  # Req 11.13: bloqueada durante a execução
            self.m_pesquisa = valor
        # A pesquisa nunca altera a Ultima_Escolha_Coleta (Req 11.8, 11.10).
        assert self.m_escolha_coleta == escolha_antes

    @rule(valor=st.booleans())
    def alternar_coleta(self, valor: bool) -> None:
        self.estado = self.estado.alternar_coleta(valor)
        # Req 11.9, 11.11, 11.13: só operável com pesquisa ligada e sem execução.
        if self.m_pesquisa and not self.m_em_execucao:
            self.m_escolha_coleta = valor

    @precondition(lambda self: not self.m_em_execucao)
    @rule()
    def iniciar(self) -> None:
        esperado = OpcoesExecucao(
            pesquisa_web=self.m_pesquisa,
            coleta_html=self.m_pesquisa and self.m_escolha_coleta,
        )
        self.estado, opcoes = self.estado.iniciar()
        # Req 11.12: opções = estado dos seletores no instante do início.
        assert opcoes == esperado
        self.m_em_execucao = True
        self.m_opcoes_inicio = opcoes

    @precondition(lambda self: self.m_em_execucao)
    @rule()
    def terminar(self) -> None:
        self.estado = self.estado.terminar()
        self.m_em_execucao = False
        # Req 11.14: os estados do início da execução são preservados.
        assert self.m_opcoes_inicio is not None
        assert self.estado.pesquisa == self.m_opcoes_inicio.pesquisa_web
        assert self.estado.coleta == self.m_opcoes_inicio.coleta_html

    # -- invariantes após cada passo ---------------------------------------

    @invariant()
    def estado_igual_ao_modelo(self) -> None:
        e = self.estado
        assert e.pesquisa == self.m_pesquisa
        assert e.em_execucao == self.m_em_execucao
        # coleta == pesquisa and ultima_escolha_coleta; religar a pesquisa
        # restaura a escolha anterior (Req 11.8, 11.10).
        assert e.coleta == (self.m_pesquisa and self.m_escolha_coleta)
        if e.pesquisa:
            assert e.ultima_escolha_coleta == self.m_escolha_coleta

    @invariant()
    def operabilidade(self) -> None:
        e = self.estado
        # Req 11.13: durante a execução, os dois seletores ficam inoperáveis.
        assert e.pesquisa_operavel == (not self.m_em_execucao)
        # Req 11.8, 11.9, 11.11: coleta operável só com pesquisa ligada.
        assert e.coleta_operavel == (self.m_pesquisa and not self.m_em_execucao)
        if not e.pesquisa:
            assert not e.coleta
            assert not e.coleta_operavel

    @invariant()
    def em_execucao_ignora_alternancias(self) -> None:
        e = self.estado
        if not e.em_execucao:
            return
        for valor in (True, False):
            assert e.alternar_pesquisa(valor) == e
            assert e.alternar_coleta(valor) == e

    @invariant()
    def textos_de_estado(self) -> None:
        # Req 11.3: estado ligado/desligado e inoperabilidade indicados em texto.
        e, cfg = self.estado, self.cfg
        tp = texto_estado_pesquisa(e, cfg)
        tc = texto_estado_coleta(e)

        if self.m_pesquisa:
            metodo = _metodo_efetivo_oraculo(cfg.metodo_bruto)
            base = f"ligada (método {metodo})" if metodo else "ligada (método inválido)"
            assert tp.startswith(base)
            assert not tp.startswith("desligada")
        else:
            assert tp.startswith("desligada")
        assert (_BLOQUEADA in tp) == self.m_em_execucao

        if not self.m_pesquisa:
            # Req 11.8: coleta desligada e inoperável, com o texto de dependência.
            assert tc.startswith(_DEPENDENTE)
        elif self.m_escolha_coleta:
            assert tc.startswith("ligada")
        else:
            assert tc.startswith("desligada")
            assert not tc.startswith(_DEPENDENTE)
        assert (_BLOQUEADA in tc) == self.m_em_execucao

        for texto in (tp, tc):
            assert texto.startswith("ligada") or texto.startswith("desligada")


TestMaquinaSeletores = MaquinaSeletores.TestCase


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
# Feature: html-extract-on-web-search, Property 19: Mensagem de configuração da execução
# **Validates: Requirements 11.17**
# ---------------------------------------------------------------------------

_PREFIXO_CONFIG = "Configuração da execução:"
opcoes_execucao = st.builds(
    OpcoesExecucao, pesquisa_web=st.booleans(), coleta_html=st.booleans()
)


def _mensagem_configuracao_oraculo(o: OpcoesExecucao, cfg: ConfigSeletoresTela) -> str:
    """Oráculo independente com os textos do design.md (Req 11.17)."""
    if o.pesquisa_web:
        metodo = _metodo_efetivo_oraculo(cfg.metodo_bruto)
        pesquisa = (
            f"ligada (método {metodo})"
            if metodo
            else f"ligada (método inválido: {cfg.metodo_bruto})"
        )
    else:
        pesquisa = "desligada"
    coleta = "ligada" if o.coleta_html else "desligada"
    stealth = "ligado" if o.fallback_stealth else "desligado"
    return (
        f"{_PREFIXO_CONFIG} pesquisa web={pesquisa}, coleta de HTML={coleta}, "
        f"fallback stealth={stealth}."
    )


@settings(max_examples=100)
@given(o=opcoes_execucao, cfg=configs_tela)
def test_mensagem_configuracao_execucao(o: OpcoesExecucao, cfg: ConfigSeletoresTela) -> None:
    msg = mensagem_configuracao(o, cfg)

    assert msg == _mensagem_configuracao_oraculo(o, cfg)
    assert msg.startswith(_PREFIXO_CONFIG)

    # Estado de cada seletor conforme as opções.
    if o.pesquisa_web:
        assert "pesquisa web=ligada" in msg
        assert "pesquisa web=desligada" not in msg
        metodo = _metodo_efetivo_oraculo(cfg.metodo_bruto)
        if metodo:
            assert f"(método {metodo})" in msg
        else:
            assert f"(método inválido: {cfg.metodo_bruto})" in msg
    else:
        assert "pesquisa web=desligada" in msg
        assert "método" not in msg
    esperado_coleta = "ligada" if o.coleta_html else "desligada"
    esperado_stealth = "ligado" if o.fallback_stealth else "desligado"
    assert msg.endswith(
        f"coleta de HTML={esperado_coleta}, fallback stealth={esperado_stealth}."
    )


def test_mensagem_configuracao_exemplo_do_design() -> None:
    cfg = ConfigSeletoresTela(metodo_bruto="serper", estado_chave="habilitada")
    assert (
        mensagem_configuracao(OpcoesExecucao(pesquisa_web=True, coleta_html=False), cfg)
        == "Configuração da execução: pesquisa web=ligada (método serper), "
        "coleta de HTML=desligada, fallback stealth=desligado."
    )
