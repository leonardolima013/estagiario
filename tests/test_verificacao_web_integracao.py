"""Teste de integração real da skill de verificação web
(SPEC-verificacao-web-nomenclatura.md) — desligado por padrão: abre um
navegador Chromium real e visível, faz busca real no Google e chamadas reais à
API da Anthropic. Custa dinheiro, depende de rede e requer Node.js/npx com os
binários de navegador do Playwright já baixados.

Roda com ESTAGIARIO_RUN_WEB_TESTS=1 e ANTHROPIC_API_KEY configurada. Só
verifica o contrato estrutural do resultado (SPEC §6) — nunca um texto
específico, já que resultados de busca real mudam com o tempo.
"""

import os

import pytest

from verification.mcp_playwright_agent import verificar_nomenclatura_peca

_RUN_WEB_TESTS = os.environ.get("ESTAGIARIO_RUN_WEB_TESTS") == "1"
_SKIP_REASON = (
    "Teste de integração real (navegador real + chamada de API à Anthropic) — desligado "
    "por padrão, custa dinheiro e depende de rede. Rode com ESTAGIARIO_RUN_WEB_TESTS=1, "
    "com ANTHROPIC_API_KEY configurada e Node.js/npx disponíveis."
)

pytestmark = pytest.mark.skipif(not _RUN_WEB_TESTS, reason=_SKIP_REASON)


def test_verificacao_web_retorna_contrato_estrutural_valido():
    resultado = verificar_nomenclatura_peca(
        codigo="MB1085", marca="AFFINIA", nomes_conflitantes=["PIVO", "PIVO SUPERIOR", "PIVO INFERIOR"],
        on_evento=print,
    )

    assert resultado.status in ("confirmado", "inconclusivo")
    if resultado.status == "confirmado":
        assert resultado.nome_sugerido
    else:
        assert resultado.nome_sugerido is None
    assert isinstance(resultado.justificativa, str) and resultado.justificativa
