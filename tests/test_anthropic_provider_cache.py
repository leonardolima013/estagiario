"""AnthropicProvider: `cachear_system` envia o system como bloco com
cache_control; o default continua enviando string. Client fake, sem rede."""

from __future__ import annotations

from types import SimpleNamespace

import llm.anthropic_provider as modulo


class _MessagesFake:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        bloco = SimpleNamespace(type="tool_use", name=kwargs["tool_choice"]["name"], input={"ok": True})
        return SimpleNamespace(content=[bloco])


def _provider(monkeypatch, **kwargs):
    mensagens = _MessagesFake()
    monkeypatch.setattr(modulo, "anthropic_api_key", lambda: "chave-fake")
    monkeypatch.setattr(modulo.anthropic, "Anthropic", lambda api_key: SimpleNamespace(messages=mensagens))
    return modulo.AnthropicProvider(model="modelo-x", **kwargs), mensagens


def test_cachear_system_envia_bloco_com_cache_control(monkeypatch):
    provider, mensagens = _provider(monkeypatch, cachear_system=True)
    assert provider.gerar_json("SYSTEM FIXO", "user", {"type": "object"}, "s") == {"ok": True}
    assert mensagens.kwargs["system"] == [
        {"type": "text", "text": "SYSTEM FIXO", "cache_control": {"type": "ephemeral"}}
    ]


def test_default_envia_system_como_string(monkeypatch):
    provider, mensagens = _provider(monkeypatch)
    provider.gerar_json("SYSTEM", "user", {"type": "object"}, "s")
    assert mensagens.kwargs["system"] == "SYSTEM"
