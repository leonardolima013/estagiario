"""Fakes reutilizáveis entre arquivos de teste (sem custo de API/DB)."""

from __future__ import annotations


class FakeLLMProvider:
    def __init__(self, resposta: dict):
        self._resposta = resposta

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        return self._resposta


class FakeVerificadorWeb:
    """Fake de verificar_nomenclatura_peca (verification/mcp_playwright_agent.py) —
    mesma forma de FakeLLMProvider, zero custo de API/subprocess/rede."""

    def __init__(self, resultado):
        self._resultado = resultado
        self.chamadas: list[tuple] = []

    def __call__(self, codigo, marca, nomes_conflitantes, on_evento=None):
        self.chamadas.append((codigo, marca, nomes_conflitantes))
        return self._resultado
