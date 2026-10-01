"""Máquina de estados pura dos seletores da tela "Rodar testes" (Req 11).

Sem Textual: a tela (`tui/screens/executar_caso_screen.py`) só desenha o estado
daqui. As regras de dependência (pesquisa desligada → coleta desligada e
inoperável; coleta desligada → fallback stealth desligado e inoperável), de
restauração das últimas escolhas e de bloqueio durante a execução ficam
testáveis isoladamente e servem de especificação de paridade para o frontend
React + Ink.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import config
from config import (
    NOME_VAR_COLETA_HABILITADA,
    NOME_VAR_COLETA_STEALTH_HABILITADA,
    EstadoChaveColeta,
)
from verification.selector import normalizar_metodo

NOME_VAR_METODO = "ESTAGIARIO_WEB_VERIFICATION_METODO"
_METODOS_VALIDOS = frozenset({"serper", "playwright"})
_SUFIXO_BLOQUEADA = " — bloqueada durante a execução"
_SUFIXO_BLOQUEADO = " — bloqueado durante a execução"
TEXTO_COLETA_DEPENDENTE = "desligada porque a pesquisa web está desligada"
TEXTO_STEALTH_DEPENDENTE = "desligado porque a coleta de HTML está desligada"


@dataclass(frozen=True)
class ConfigSeletoresTela:
    """Configuração do ambiente lida na montagem da tela (Req 11.4–11.7 do spec
    anterior; Req 11.2, 11.3 do fallback stealth)."""

    metodo_bruto: str  # config.web_verification_metodo()
    estado_chave: EstadoChaveColeta  # config.coleta_habilitada()
    estado_chave_stealth: EstadoChaveColeta = "desabilitada"  # config.coleta_stealth_habilitada()

    @property
    def metodo_efetivo(self) -> str | None:
        """"serper"/"playwright" (via `normalizar_metodo`) ou None se inválido."""
        metodo = normalizar_metodo(self.metodo_bruto)
        return metodo if metodo in _METODOS_VALIDOS else None

    @property
    def avisos(self) -> tuple[str, ...]:
        """Avisos de configuração exibidos na tela (Req 11.5, 11.7)."""
        avisos: list[str] = []
        if self.metodo_efetivo is None:
            avisos.append(
                f"{NOME_VAR_METODO}={self.metodo_bruto} inválido; execuções com a "
                "pesquisa web ligada mostram o erro quando a verificação web for "
                "necessária."
            )
        if self.estado_chave == "invalida":
            avisos.append(
                f"{NOME_VAR_COLETA_HABILITADA} com valor não reconhecido; coleta "
                "de HTML começa desligada."
            )
        if self.estado_chave_stealth == "invalida":
            avisos.append(
                f"{NOME_VAR_COLETA_STEALTH_HABILITADA} com valor não reconhecido; "
                "fallback stealth começa desligado."
            )
        return tuple(avisos)


def ler_config_seletores() -> ConfigSeletoresTela:
    """Lê a configuração do ambiente sem escrever nada (Req 11.16; Req 11.8 do stealth)."""
    return ConfigSeletoresTela(
        metodo_bruto=config.web_verification_metodo(),
        estado_chave=config.coleta_habilitada(),
        estado_chave_stealth=config.coleta_stealth_habilitada(),
    )


@dataclass(frozen=True)
class OpcoesExecucao:
    """Opções repassadas a `executar_caso_fn` no início da execução (Req 11.12;
    `fallback_stealth` pelo Req 11.6 do stealth)."""

    pesquisa_web: bool
    coleta_html: bool
    fallback_stealth: bool = False


@dataclass(frozen=True)
class EstadoSeletores:
    """Estado dos três seletores. `coleta` e `stealth` são derivadas: com a
    pesquisa desligada, a coleta fica desligada sem perder a
    Ultima_Escolha_Coleta (Req 11.8–11.10); com a coleta desligada, o fallback
    stealth fica desligado sem perder `ultima_escolha_stealth` (Req 11.4, 11.5
    do stealth)."""

    pesquisa: bool
    ultima_escolha_coleta: bool
    em_execucao: bool = False
    ultima_escolha_stealth: bool = False

    @classmethod
    def inicial(cls, cfg: ConfigSeletoresTela) -> EstadoSeletores:
        """Pesquisa ligada; coleta ligada só com a chave habilitada (Req 11.4,
        11.6, 11.7); fallback ligado só com a Chave_Stealth habilitada (Req 11.2,
        11.3 do stealth)."""
        return cls(
            pesquisa=True,
            ultima_escolha_coleta=cfg.estado_chave == "habilitada",
            ultima_escolha_stealth=cfg.estado_chave_stealth == "habilitada",
        )

    @property
    def coleta(self) -> bool:
        return self.pesquisa and self.ultima_escolha_coleta

    @property
    def pesquisa_operavel(self) -> bool:
        return not self.em_execucao

    @property
    def coleta_operavel(self) -> bool:
        return self.pesquisa and not self.em_execucao

    @property
    def stealth(self) -> bool:
        """Segue o valor da coleta, não a operabilidade (Req 11.4, 11.6 do stealth)."""
        return self.coleta and self.ultima_escolha_stealth

    @property
    def stealth_operavel(self) -> bool:
        return self.coleta and not self.em_execucao

    def alternar_pesquisa(self, valor: bool) -> EstadoSeletores:
        """No-op se inoperável (Req 11.13)."""
        if not self.pesquisa_operavel:
            return self
        return replace(self, pesquisa=valor)

    def alternar_coleta(self, valor: bool) -> EstadoSeletores:
        """No-op se inoperável (Req 11.9, 11.13); senão grava a última escolha (Req 11.11)."""
        if not self.coleta_operavel:
            return self
        return replace(self, ultima_escolha_coleta=valor)

    def alternar_stealth(self, valor: bool) -> EstadoSeletores:
        """No-op se inoperável (Req 11.4, 11.6 do stealth); senão grava a última
        escolha (Req 11.5 do stealth)."""
        if not self.stealth_operavel:
            return self
        return replace(self, ultima_escolha_stealth=valor)

    def iniciar(self) -> tuple[EstadoSeletores, OpcoesExecucao]:
        """Bloqueia os seletores e congela as opções do instante do início (Req
        11.12, 11.13; Req 11.6 do stealth)."""
        opcoes = OpcoesExecucao(
            pesquisa_web=self.pesquisa,
            coleta_html=self.coleta,
            fallback_stealth=self.stealth,
        )
        return replace(self, em_execucao=True), opcoes

    def terminar(self) -> EstadoSeletores:
        """Reabilita os seletores preservando os valores (Req 11.14)."""
        return replace(self, em_execucao=False)


def texto_estado_pesquisa(e: EstadoSeletores, cfg: ConfigSeletoresTela) -> str:
    """Estado textual do Seletor_Pesquisa_Web (Req 11.3, 11.4, 11.5)."""
    if e.pesquisa:
        metodo = cfg.metodo_efetivo
        texto = f"ligada (método {metodo})" if metodo else "ligada (método inválido)"
    else:
        texto = "desligada"
    if not e.pesquisa_operavel:
        texto += _SUFIXO_BLOQUEADA
    return texto


def texto_estado_coleta(e: EstadoSeletores) -> str:
    """Estado textual do Seletor_Coleta_Html (Req 11.3, 11.8)."""
    if not e.pesquisa:
        texto = TEXTO_COLETA_DEPENDENTE
    else:
        texto = "ligada" if e.coleta else "desligada"
    if e.em_execucao:
        texto += _SUFIXO_BLOQUEADA
    return texto


def texto_estado_stealth(e: EstadoSeletores) -> str:
    """Estado textual do Seletor_Fallback_Stealth (Req 11.1, 11.4 do stealth)."""
    if not e.coleta:
        texto = TEXTO_STEALTH_DEPENDENTE
    else:
        texto = "ligado" if e.stealth else "desligado"
    if e.em_execucao:
        texto += _SUFIXO_BLOQUEADO
    return texto


def mensagem_configuracao(o: OpcoesExecucao, cfg: ConfigSeletoresTela) -> str:
    """Mensagem_Configuracao_Execucao, primeira linha do painel e do log (Req
    11.17; trecho do fallback pelo Req 11.7 do stealth)."""
    if o.pesquisa_web:
        metodo = cfg.metodo_efetivo
        if metodo:
            pesquisa = f"ligada (método {metodo})"
        else:
            pesquisa = f"ligada (método inválido: {cfg.metodo_bruto})"
    else:
        pesquisa = "desligada"
    coleta = "ligada" if o.coleta_html else "desligada"
    stealth = "ligado" if o.fallback_stealth else "desligado"
    return (
        f"Configuração da execução: pesquisa web={pesquisa}, coleta de HTML={coleta}, "
        f"fallback stealth={stealth}."
    )
