"""AnthropicProvider.gerar_json_transmitindo com cliente falso, sem rede."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import pytest

import llm.anthropic_provider as modulo

_SCHEMA = {"type": "object", "properties": {"modo": {}, "justificativa": {}}}
_TRECHOS = ['{"modo": "esco', 'lher", "justif', 'icativa": "Os dois ', 'nomes descrevem a mesma peça."}']
_FINAL = {"modo": "escolher", "justificativa": "Os dois nomes descrevem a mesma peça."}


class _Recusa400(anthropic.BadRequestError):
    """BadRequestError sem resposta HTTP real (o construtor do SDK exige uma)."""

    def __init__(self, mensagem: str) -> None:
        Exception.__init__(self, mensagem)


class _Stream:
    def __init__(self, trechos, final, erro_ao_abrir=None):
        self._trechos = trechos
        self._final = final
        self._erro = erro_ao_abrir

    def __enter__(self):
        if self._erro is not None:
            raise self._erro
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield SimpleNamespace(type="message_start")
        for trecho in self._trechos:
            yield SimpleNamespace(type="input_json", partial_json=trecho, snapshot={})
        yield SimpleNamespace(type="content_block_stop")

    def get_final_message(self):
        return self._final


def _mensagem(input_final: dict, nome: str = "decisao_nome", stop_reason: str = "tool_use"):
    bloco = SimpleNamespace(type="tool_use", name=nome, input=input_final)
    return SimpleNamespace(content=[bloco], stop_reason=stop_reason)


class _Messages:
    def __init__(self, trechos=_TRECHOS, final=None, erro_ao_abrir=None):
        self.stream_kwargs = None
        self.create_kwargs = None
        self._trechos = trechos
        self._final = final if final is not None else _mensagem(_FINAL)
        self._erro = erro_ao_abrir
        self.chamadas: list[str] = []

    def stream(self, **kwargs):
        self.chamadas.append("stream")
        self.stream_kwargs = kwargs
        return _Stream(self._trechos, self._final, self._erro)

    def create(self, **kwargs):
        self.chamadas.append("create")
        self.create_kwargs = kwargs
        bloco = SimpleNamespace(type="tool_use", name=kwargs["tool_choice"]["name"], input=_FINAL)
        return SimpleNamespace(content=[bloco])


def _provider(monkeypatch, mensagens: _Messages, **kwargs):
    monkeypatch.setattr(modulo, "anthropic_api_key", lambda: "chave-fake")
    monkeypatch.setattr(modulo.anthropic, "Anthropic", lambda api_key: SimpleNamespace(messages=mensagens))
    return modulo.AnthropicProvider(model="modelo-x", **kwargs)


def test_transmite_parciais_com_a_string_aberta_e_devolve_o_json_final(monkeypatch):
    mensagens = _Messages()
    provider = _provider(monkeypatch, mensagens)
    parciais: list[dict] = []

    resposta = provider.gerar_json_transmitindo("SYS", "user", _SCHEMA, "decisao_nome", ao_atualizar=parciais.append)

    assert resposta == _FINAL
    assert parciais[0] == {"modo": "esco"}
    assert parciais[2]["justificativa"] == "Os dois "
    assert parciais[-1] == _FINAL
    justificativas = [p.get("justificativa", "") for p in parciais]
    assert all(_FINAL["justificativa"].startswith(j) for j in justificativas)


def test_mesmos_parametros_do_create_mais_eager_input_streaming(monkeypatch):
    mensagens = _Messages()
    provider = _provider(monkeypatch, mensagens, cachear_system=True)

    provider.gerar_json_transmitindo("SYS", "user", _SCHEMA, "decisao_nome", ao_atualizar=lambda _p: None)
    provider.gerar_json("SYS", "user", _SCHEMA, "decisao_nome")

    stream_kwargs, create_kwargs = mensagens.stream_kwargs, mensagens.create_kwargs
    assert stream_kwargs["tools"][0].pop("eager_input_streaming") is True
    assert stream_kwargs == create_kwargs
    assert "eager_input_streaming" not in create_kwargs["tools"][0]
    assert create_kwargs["tool_choice"] == {"type": "tool", "name": "decisao_nome"}


def test_sem_observador_usa_o_create(monkeypatch):
    mensagens = _Messages()
    provider = _provider(monkeypatch, mensagens)

    assert provider.gerar_json_transmitindo("SYS", "user", _SCHEMA, "decisao_nome") == _FINAL
    assert mensagens.chamadas == ["create"]


def test_observador_que_falha_nao_interrompe_a_geracao(monkeypatch):
    provider = _provider(monkeypatch, _Messages())

    def ao_atualizar(_parcial):
        raise RuntimeError("a tela quebrou")

    assert provider.gerar_json_transmitindo("SYS", "user", _SCHEMA, "decisao_nome", ao_atualizar=ao_atualizar) == _FINAL


def test_json_incompleto_no_fim_do_stream_vira_erro(monkeypatch):
    mensagens = _Messages(trechos=_TRECHOS[:2], final=_mensagem({"modo": "escolher"}, stop_reason="max_tokens"))
    provider = _provider(monkeypatch, mensagens)

    with pytest.raises(RuntimeError, match="JSON incompleto.*max_tokens"):
        provider.gerar_json_transmitindo("SYS", "user", _SCHEMA, "decisao_nome", ao_atualizar=lambda _p: None)


def test_resposta_sem_tool_use_vira_erro(monkeypatch):
    sem_tool = SimpleNamespace(content=[SimpleNamespace(type="text", text="oi")], stop_reason="end_turn")
    provider = _provider(monkeypatch, _Messages(trechos=[], final=sem_tool))

    with pytest.raises(RuntimeError, match="não trouxe um tool_use"):
        provider.gerar_json_transmitindo("SYS", "user", _SCHEMA, "decisao_nome", ao_atualizar=lambda _p: None)


def test_recusa_do_eager_streaming_cai_para_o_create_e_desliga_o_stream(monkeypatch):
    erro = _Recusa400("tools.0.custom.eager_input_streaming: Extra inputs are not permitted")
    mensagens = _Messages(erro_ao_abrir=erro)
    provider = _provider(monkeypatch, mensagens)

    assert provider.gerar_json_transmitindo("SYS", "user", _SCHEMA, "decisao_nome", ao_atualizar=lambda _p: None) == _FINAL
    assert provider.gerar_json_transmitindo("SYS", "user", _SCHEMA, "decisao_nome", ao_atualizar=lambda _p: None) == _FINAL
    assert mensagens.chamadas == ["stream", "create", "create"]


def test_outra_recusa_400_sobe_como_no_create(monkeypatch):
    mensagens = _Messages(erro_ao_abrir=_Recusa400("prompt is too long"))
    provider = _provider(monkeypatch, mensagens)

    with pytest.raises(anthropic.BadRequestError):
        provider.gerar_json_transmitindo("SYS", "user", _SCHEMA, "decisao_nome", ao_atualizar=lambda _p: None)
    assert mensagens.chamadas == ["stream"]
