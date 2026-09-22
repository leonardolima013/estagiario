from concurrent.futures import ThreadPoolExecutor

import pytest

from loop.models import (
    FamiliaSorteada,
    IteracaoStatus,
    LoopConfig,
    LoopStatus,
    MotivoParada,
    RegistroIteracao,
    ResultadoLoop,
)
from loop.tracing import TraceCollector


def test_loop_config_rejeita_valor_invalido(tmp_path):
    with pytest.raises(ValueError):
        LoopConfig(iteracoes=0, output_dir=tmp_path)
    with pytest.raises(ValueError):
        LoopConfig(iteracoes=True, output_dir=tmp_path)


def test_familia_tem_chave_estavel():
    familia = FamiliaSorteada("A", 7, "MARCA")
    assert familia.chave == ("A", 7)


def test_resultado_loop_calcula_contadores():
    resultado = ResultadoLoop(
        run_id="r",
        status=LoopStatus.CONCLUIDO,
        motivo_parada=MotivoParada.ITERACOES_SOLICITADAS_ALCANCADAS,
        iteracoes_solicitadas=3,
        iteracoes=[
            RegistroIteracao(1, IteracaoStatus.SUCESSO, "a"),
            RegistroIteracao(2, IteracaoStatus.ERRO, "b"),
            RegistroIteracao(3, IteracaoStatus.CANCELADA, "c"),
        ],
    )
    assert resultado.iteracoes_executadas == 3
    assert resultado.iteracoes_concluidas == 1
    assert resultado.iteracoes_com_erro == 1
    assert resultado.iteracoes_canceladas == 1


def test_trace_redige_detalhes_sensiveis_e_trunca_texto():
    trace = TraceCollector()
    trace.registrar(
        "llm", "gerar_json",
        detalhes={"prompt": "segredo", "justificativa": "x" * 600},
    )
    evento = trace.eventos()[0]
    assert evento.detalhes["prompt"] == "[REDACTED]"
    assert len(evento.detalhes["justificativa"]) == 501


def test_trace_preserva_ordem_com_escritas_concorrentes():
    trace = TraceCollector()

    def registrar(i):
        trace.registrar("fase", f"evento-{i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(registrar, range(100)))

    eventos = trace.eventos()
    assert len(eventos) == 100
    assert {e.nome for e in eventos} == {f"evento-{i}" for i in range(100)}


def test_tracing_llm_registra_resposta_estruturada_sem_prompt():
    from loop.tracing import TraceCollector, TracingLLMProvider

    class FakeLLM:
        def gerar_json(self, system, user, json_schema, schema_name="output"):
            assert "segredo" in user
            return {"justificativa": "explicação observável", "valor": 7}

    trace = TraceCollector()
    provider = TracingLLMProvider(FakeLLM(), trace)
    resposta = provider.gerar_json("system secreto", "segredo", {"properties": {"valor": {}}}, "decisao")

    evento = trace.eventos()[0]
    assert resposta["valor"] == 7
    assert evento.nome == "decisao"
    assert evento.entrada["prompt_chars"] == len("segredo")
    assert evento.saida["resposta_estruturada"]["valor"] == 7
    assert evento.justificativa == "explicação observável"
    assert "segredo" not in str(evento.saida)
