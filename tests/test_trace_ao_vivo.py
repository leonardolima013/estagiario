"""Eventos ao vivo do core: ouvinte do TraceCollector, sinais de etapa e de
resposta parcial, wrapper de intervenção e o canal ao vivo do loop.

Fakes em memória; nenhum teste acessa rede, banco ou LLM real.
"""

from __future__ import annotations

from typing import Any

import pytest

from loop.executor import executar_loop
from loop.models import (
    AvisoEmitido,
    EtapaIniciada,
    EventoExecucao,
    FamiliaSorteada,
    GrupoCarregado,
    IteracaoConcluida,
    IteracaoIniciada,
    IteracaoStatus,
    LoopConfig,
    RespostaParcial,
)
from loop.serializacao import iteracao_para_json, para_json
from loop.tracing import (
    TraceCollector,
    TracingLLMProvider,
    envolver_intervencao,
    sinalizar_grupo,
    sinalizar_inicio,
)
from memory.models import PedidoIntervencao, RegraProposta, RespostaIntervencao
from partitioning.models import Particao
from pipeline import ResultadoCaso, executar_caso
from tests.fakes import FakeVerificadorWeb
from tests.fakes_painel import CasoRoteirizado, LLMRoteirizado
from tools.sortear_grupo import NenhumGrupoDuplicadoError
from verification.models import ResultadoVerificacao

_SCHEMA = {"properties": {"modo": {}, "justificativa": {}}}


class _LLMComStreaming:
    """Fake que implementa os dois caminhos do `LLMProviderComStreaming`."""

    def __init__(self, parciais: list[dict], final: dict) -> None:
        self.parciais = parciais
        self.final = final
        self.chamadas: list[str] = []

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        self.chamadas.append("gerar_json")
        return self.final

    def gerar_json_transmitindo(self, system, user, json_schema, schema_name, ao_atualizar):
        self.chamadas.append("transmitindo")
        for parcial in self.parciais:
            ao_atualizar(parcial)
        return self.final


def _resumo(eventos: list[EventoExecucao]) -> list[tuple]:
    return [(e.fase, e.nome, e.status, e.entrada, e.saida, e.justificativa) for e in eventos]


# --- TraceCollector -------------------------------------------------------------


def test_ouvinte_recebe_registros_e_sinais_sem_mudar_eventos():
    recebidos: list[object] = []
    trace = TraceCollector(ouvinte=recebidos.append)

    trace.sinalizar_inicio("tool", "buscar_grupo", {"search_ref": "A"})
    evento = trace.registrar("tool", "buscar_grupo", saida={"quantidade": 2})
    trace.sinalizar_parcial("particao", {"subclusters": []})

    assert trace.tem_ouvinte is True
    assert trace.eventos() == [evento]
    assert [type(e) for e in recebidos] == [EtapaIniciada, EventoExecucao, RespostaParcial]
    assert recebidos[1] is evento


def test_sem_ouvinte_os_sinais_nao_fazem_nada():
    trace = TraceCollector()

    trace.sinalizar_inicio("tool", "buscar_grupo")
    trace.sinalizar_parcial("particao", {"justificativa": "x"})

    assert trace.tem_ouvinte is False
    assert trace.eventos() == []


def test_ouvinte_que_falha_nao_afeta_o_registro():
    def ouvinte(_evento):
        raise RuntimeError("a tela quebrou")

    trace = TraceCollector(ouvinte=ouvinte)
    trace.sinalizar_inicio("tool", "gerar_sql")
    evento = trace.registrar("tool", "gerar_sql")

    assert trace.eventos() == [evento]


def test_sinais_ao_vivo_passam_pela_mesma_sanitizacao_do_trace():
    recebidos: list[Any] = []
    trace = TraceCollector(ouvinte=recebidos.append)

    trace.sinalizar_inicio("llm", "particao", {"prompt": "texto do prompt", "pecas": 3})
    trace.sinalizar_parcial("particao", {"justificativa": "x" * 900, "token": "abc"})
    trace.sinalizar_parcial("particao", ["não é um objeto"])

    inicio, parcial = recebidos
    assert inicio.detalhes == {"prompt": "[REDACTED]", "pecas": 3}
    assert parcial.resposta["token"] == "[REDACTED]"
    assert parcial.resposta["justificativa"] == "x" * 500 + "…"


def test_helper_sinalizar_inicio_ignora_none_e_trace_sem_suporte():
    class SoRegistra:
        def registrar(self, *args, **kwargs):
            raise AssertionError("sinal ao vivo não deve virar registro")

    sinalizar_inicio(None, "tool", "buscar_grupo")
    sinalizar_inicio(SoRegistra(), "tool", "buscar_grupo")


def test_sinal_do_grupo_leva_os_registros_como_o_json_sem_truncar(fazer_registro):
    registro = fazer_registro(1, "POLIA", application="GOL 1.0 1991/2001\n" * 60)
    recebidos: list[Any] = []
    trace = TraceCollector(ouvinte=recebidos.append)

    trace.sinalizar_grupo("83061:CITROEN", [registro])

    (evento,) = recebidos
    assert isinstance(evento, GrupoCarregado)
    assert evento.grupo_ref == "83061:CITROEN"
    assert evento.registros == (para_json(registro),)
    assert evento.registros[0]["application"] == registro.application  # sem o corte de 500 do trace
    assert evento.registros[0]["created"] == registro.created.isoformat()
    assert trace.eventos() == []

    sem_ouvinte = TraceCollector()
    sem_ouvinte.sinalizar_grupo("83061:CITROEN", [registro])
    assert sem_ouvinte.eventos() == []


def test_helper_sinalizar_grupo_ignora_none_e_trace_sem_suporte(fazer_registro):
    class SoRegistra:
        def registrar(self, *args, **kwargs):
            raise AssertionError("sinal ao vivo não deve virar registro")

    class QuebraNoSinal:
        def sinalizar_grupo(self, grupo_ref, registros):
            raise RuntimeError("a tela quebrou")

    sinalizar_grupo(None, "A:M", [fazer_registro(1, "POLIA")])
    sinalizar_grupo(SoRegistra(), "A:M", [fazer_registro(1, "POLIA")])
    sinalizar_grupo(QuebraNoSinal(), "A:M", [fazer_registro(1, "POLIA")])


# --- TracingLLMProvider ----------------------------------------------------------


def test_tracing_transmite_so_quando_ha_ouvinte_e_audita_igual():
    final = {"modo": "escolher", "justificativa": "Os dois nomes descrevem a mesma peça."}
    parciais = [{"justificativa": "Os"}, {"justificativa": "Os dois nomes"}]

    recebidos: list[Any] = []
    ao_vivo = TraceCollector(ouvinte=recebidos.append)
    llm_vivo = _LLMComStreaming(parciais, final)
    assert TracingLLMProvider(llm_vivo, ao_vivo).gerar_json("s", "u", _SCHEMA, "decisao_nome") == final

    sem_ouvinte = TraceCollector()
    llm_normal = _LLMComStreaming(parciais, final)
    assert TracingLLMProvider(llm_normal, sem_ouvinte).gerar_json("s", "u", _SCHEMA, "decisao_nome") == final

    assert llm_vivo.chamadas == ["transmitindo"]
    assert llm_normal.chamadas == ["gerar_json"]
    assert [type(e).__name__ for e in recebidos] == [
        "EtapaIniciada", "RespostaParcial", "RespostaParcial", "EventoExecucao",
    ]
    assert (recebidos[0].fase, recebidos[0].nome) == ("llm", "decisao_nome")
    assert "prompt_chars" in recebidos[0].detalhes
    assert [e.resposta["justificativa"] for e in recebidos[1:3]] == ["Os", "Os dois nomes"]
    assert _resumo(ao_vivo.eventos()) == _resumo(sem_ouvinte.eventos())


def test_tracing_usa_gerar_json_quando_o_delegate_nao_transmite():
    class SoGerarJson:
        def gerar_json(self, system, user, json_schema, schema_name="output"):
            return {"justificativa": "ok"}

    recebidos: list[Any] = []
    trace = TraceCollector(ouvinte=recebidos.append)

    assert TracingLLMProvider(SoGerarJson(), trace).gerar_json("s", "u", _SCHEMA, "decisao_campo") == {
        "justificativa": "ok"
    }
    assert [type(e).__name__ for e in recebidos] == ["EtapaIniciada", "EventoExecucao"]


def test_tracing_registra_erro_quando_o_stream_cai_no_meio():
    class StreamQueCai(_LLMComStreaming):
        def gerar_json_transmitindo(self, system, user, json_schema, schema_name, ao_atualizar):
            ao_atualizar({"justificativa": "parcial"})
            raise ConnectionError("queda do stream")

    recebidos: list[Any] = []
    trace = TraceCollector(ouvinte=recebidos.append)

    with pytest.raises(ConnectionError):
        TracingLLMProvider(StreamQueCai([], {}), trace).gerar_json("s", "u", _SCHEMA, "particao")

    (evento,) = trace.eventos()
    assert (evento.status, evento.saida) == ("erro", {"erro": "ConnectionError"})
    assert [type(e).__name__ for e in recebidos] == ["EtapaIniciada", "RespostaParcial", "EventoExecucao"]


# --- Intervenção --------------------------------------------------------------------


def _pedido() -> PedidoIntervencao:
    return PedidoIntervencao(
        ponto="nome",
        grupo_ref="JE4699:DRIVEWAY",
        search_ref="JE4699",
        marca="DRIVEWAY",
        nomes_conflitantes=["PIVO INFERIOR", "PIVO SUPERIOR"],
        motivo="a web foi inconclusiva",
        membro_ids=[1, 2],
        candidatos=[(1, "PIVO SUPERIOR"), (2, "PIVO INFERIOR")],
    )


def test_envolver_intervencao_sinaliza_espera_e_registra_a_resposta():
    resposta = RespostaIntervencao(
        resposta_humana="A posição inferior é a correta.",
        regra=RegraProposta(titulo="Pivô", condicao="quando", resolucao="usar inferior"),
        valor="PIVO INFERIOR",
        origem_id=2,
    )
    recebidos: list[Any] = []
    trace = TraceCollector(ouvinte=recebidos.append)

    pedir = envolver_intervencao(lambda pedido: resposta, trace)

    assert pedir(_pedido()) is resposta
    assert isinstance(recebidos[0], EtapaIniciada)
    assert (recebidos[0].fase, recebidos[0].nome) == ("intervencao", "intervencao_humana")
    (evento,) = trace.eventos()
    assert (evento.fase, evento.nome) == ("intervencao", "intervencao_humana")
    assert evento.entrada["motivo"] == "a web foi inconclusiva"
    assert evento.saida["valor"] == "PIVO INFERIOR"
    assert evento.justificativa == "A posição inferior é a correta."
    assert envolver_intervencao(None, trace) is None


# --- Loop ---------------------------------------------------------------------------


def _sorter(familias):
    restantes = list(familias)

    def sortear(*, excluir):
        for familia in restantes:
            if familia.chave not in excluir:
                return familia
        raise NenhumGrupoDuplicadoError("esgotado")

    return sortear


def _caso_instrumentado(search_ref, brand_id, llm, dependencias_fk, *, trace=None, on_aviso=None, **kwargs):
    sinalizar_inicio(trace, "tool", "buscar_grupo", {"search_ref": search_ref})
    trace.registrar("tool", "buscar_grupo", saida={"quantidade": 2, "ids": [1, 2]})
    llm.gerar_json("s", "u", _SCHEMA, "decisao_nome")
    if on_aviso is not None:
        on_aviso(f"aviso de {search_ref}")
    if search_ref == "B":
        raise RuntimeError("falha isolada")
    return ResultadoCaso(
        grupo_ref=f"{search_ref}:M",
        particao=Particao(grupo_ref=f"{search_ref}:M", subclusters=[]),
        decisoes=[],
        sql="",
        grupo=[],
    )


def _llm_loop() -> _LLMComStreaming:
    return _LLMComStreaming([{"justificativa": "pa"}], {"modo": "escolher", "justificativa": "parcial completa"})


def _familias():
    return [FamiliaSorteada("A", 1, "M"), FamiliaSorteada("B", 2, "M")]


def test_loop_emite_familias_trace_e_avisos_ao_vivo(tmp_path):
    recebidos: list[Any] = []
    avisos: list[str] = []

    executar_loop(
        LoopConfig(2, tmp_path), llm=_llm_loop(), dependencias_fk=[],
        sortear_fn=_sorter(_familias()), executar_caso_fn=_caso_instrumentado,
        on_aviso=avisos.append, on_evento_ao_vivo=recebidos.append,
    )

    iniciadas = [e for e in recebidos if isinstance(e, IteracaoIniciada)]
    concluidas = [e for e in recebidos if isinstance(e, IteracaoConcluida)]
    assert [(e.indice, e.total, e.familia.search_ref) for e in iniciadas] == [(1, 2, "A"), (2, 2, "B")]
    assert [c.status for c in concluidas] == [IteracaoStatus.SUCESSO, IteracaoStatus.ERRO]
    assert concluidas[1].erro["tipo"] == "RuntimeError"
    assert concluidas[0].duracao_ms is not None
    assert recebidos.index(iniciadas[0]) == 0
    assert recebidos.index(concluidas[0]) < recebidos.index(iniciadas[1])
    assert [e.mensagem for e in recebidos if isinstance(e, AvisoEmitido)] == ["aviso de A", "aviso de B"]
    assert avisos == ["aviso de A", "aviso de B"]
    assert any(isinstance(e, RespostaParcial) for e in recebidos)
    nomes_trace = [e.nome for e in recebidos if isinstance(e, EventoExecucao)]
    assert nomes_trace[:3] == ["sortear_grupo_aleatorio", "buscar_grupo", "decisao_nome"]


def test_loop_ao_vivo_tolera_callback_que_falha(tmp_path):
    def callback(_evento):
        raise RuntimeError("a tela quebrou")

    resultado = executar_loop(
        LoopConfig(1, tmp_path), llm=_llm_loop(), dependencias_fk=[],
        sortear_fn=_sorter(_familias()), executar_caso_fn=_caso_instrumentado,
        on_evento_ao_vivo=callback,
    )

    assert resultado.iteracoes[0].status == IteracaoStatus.SUCESSO


_VOLATEIS = frozenset({"timestamp", "iniciada_em", "finalizada_em", "duracao_ms"})


def _sem_volateis(valor: Any) -> Any:
    if isinstance(valor, dict):
        return {k: _sem_volateis(v) for k, v in valor.items() if k not in _VOLATEIS}
    if isinstance(valor, list):
        return [_sem_volateis(v) for v in valor]
    return valor


def test_auditoria_do_loop_nao_muda_com_o_canal_ao_vivo(tmp_path):
    kwargs = dict(
        dependencias_fk=[], executar_caso_fn=_caso_instrumentado, on_aviso=lambda _m: None,
    )
    sem = executar_loop(
        LoopConfig(2, tmp_path / "sem"), llm=_llm_loop(), sortear_fn=_sorter(_familias()), **kwargs,
    )
    com = executar_loop(
        LoopConfig(2, tmp_path / "com"), llm=_llm_loop(), sortear_fn=_sorter(_familias()),
        on_evento_ao_vivo=lambda _e: None, **kwargs,
    )

    assert [_sem_volateis(iteracao_para_json(i)) for i in com.iteracoes] == [
        _sem_volateis(iteracao_para_json(i)) for i in sem.iteracoes
    ]


def test_erro_de_sorteio_vira_iteracao_concluida_com_erro(tmp_path):
    def sortear(*, excluir):
        if not excluir:
            raise RuntimeError("réplica fora do ar")
        raise NenhumGrupoDuplicadoError("esgotado")

    recebidos: list[Any] = []
    executar_loop(
        LoopConfig(1, tmp_path), llm=_llm_loop(), dependencias_fk=[],
        sortear_fn=sortear, executar_caso_fn=_caso_instrumentado, on_evento_ao_vivo=recebidos.append,
    )

    (concluida,) = [e for e in recebidos if isinstance(e, IteracaoConcluida)]
    assert concluida.status == IteracaoStatus.ERRO
    assert concluida.familia is None
    assert concluida.erro["tipo"] == "RuntimeError"


def test_loop_ao_vivo_manda_os_registros_e_o_registro_final_iguais_ao_json(tmp_path):
    recebidos: list[Any] = []
    familias = [FamiliaSorteada("83061", 3, "CITROEN"), FamiliaSorteada("MB1085", 9, "AFFINIA")]

    resultado = executar_loop(
        LoopConfig(2, tmp_path), llm=LLMRoteirizado(), dependencias_fk=[],
        sortear_fn=_sorter(familias), executar_caso_fn=CasoRoteirizado(), on_evento_ao_vivo=recebidos.append,
    )

    documentos = [iteracao_para_json(iteracao) for iteracao in resultado.iteracoes]
    grupos = [e for e in recebidos if isinstance(e, GrupoCarregado)]
    concluidas = [e for e in recebidos if isinstance(e, IteracaoConcluida)]
    assert [list(g.registros) for g in grupos] == [d["pecas"] for d in documentos]
    assert [g.grupo_ref for g in grupos] == ["83061:CITROEN", "MB1085:AFFINIA"]
    assert [c.registros_finais for c in concluidas] == [
        tuple(r for r in d["registro_final"] if r["status"] == "merge") for d in documentos
    ]

    (final,) = concluidas[0].registros_finais
    assert (final["id_mantido"], final["ids_removidos"], final["nome"]) == (101, [102], "PIVO SUPERIOR")
    assert final["campos"]["application"] == "GOL 1.0 1991/2001\nPARATI 1.6 1996/2001"
    assert final["campos"]["gross_weight"] == 0.41  # sem decisão: fica o valor do vencedor
    assert concluidas[1].registros_finais == ()  # sinalizada, sem merge
    # Os registros chegam antes do particionamento, não só no fim da família.
    assert recebidos.index(grupos[0]) < next(
        i for i, e in enumerate(recebidos) if isinstance(e, EtapaIniciada) and e.nome == "particionar_grupo"
    )


def test_familia_com_erro_ou_sem_merge_conclui_sem_registro_final(tmp_path):
    recebidos: list[Any] = []

    executar_loop(
        LoopConfig(2, tmp_path), llm=_llm_loop(), dependencias_fk=[],
        sortear_fn=_sorter(_familias()), executar_caso_fn=_caso_instrumentado, on_evento_ao_vivo=recebidos.append,
    )

    concluidas = [e for e in recebidos if isinstance(e, IteracaoConcluida)]
    assert [c.status for c in concluidas] == [IteracaoStatus.SUCESSO, IteracaoStatus.ERRO]
    assert [c.registros_finais for c in concluidas] == [(), ()]


# --- Pipeline -----------------------------------------------------------------------


class _LLMPipeline:
    def __init__(self, ids: list[int]) -> None:
        self._ids = ids

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        if schema_name == "particao":
            return {"subclusters": [{"label": "duplicata_real", "membro_ids": self._ids, "justificativa": "mesma peça"}]}
        if schema_name == "decisao_nome":
            return {"modo": "escolher", "part_id_escolhido": self._ids[0], "justificativa": "ok"}
        raise AssertionError(f"chamada inesperada: {schema_name}")


def test_pipeline_sinaliza_o_inicio_de_cada_etapa(fazer_registro, sem_banco):
    def buscar_grupo(search_ref, brand_id):
        return [
            fazer_registro(1, "POLIA", application="GOL 1.0 1991/2001"),
            fazer_registro(2, "POLIA DA CORREIA", application="GOL 1.0 1991/2001"),
        ]

    recebidos: list[Any] = []
    trace = TraceCollector(ouvinte=recebidos.append)
    verificador = FakeVerificadorWeb(
        ResultadoVerificacao(status="confirmado", nome_sugerido="POLIA DA CORREIA", justificativa="ok", fontes=[])
    )

    executar_caso(
        "83061", 1, llm=TracingLLMProvider(_LLMPipeline([1, 2]), trace), dependencias_fk=[],
        buscar_grupo=buscar_grupo, verificar_web=verificador, trace=trace, coleta_html=False,
    )

    inicios = [e.nome for e in recebidos if isinstance(e, EtapaIniciada)]
    assert inicios == [
        "buscar_grupo", "particionar_grupo", "particao", "montar_decisao_merge", "decisao_nome", "gerar_sql",
    ]
    for posicao, evento in enumerate(recebidos):
        if isinstance(evento, EtapaIniciada):
            assert any(
                isinstance(depois, EventoExecucao) and depois.nome == evento.nome
                for depois in recebidos[posicao + 1:]
            ), evento.nome
    assert [e for e in recebidos if isinstance(e, EventoExecucao)] == trace.eventos()


def test_pipeline_publica_os_registros_logo_depois_de_buscar_grupo(fazer_registro, sem_banco):
    grupo = [
        fazer_registro(1, "POLIA", application="GOL 1.0 1991/2001", born_at=1991, deprecated_at=2001),
        fazer_registro(2, "POLIA DA CORREIA", application="GOL 1.0 1991/2001", ncm="84833090"),
    ]
    recebidos: list[Any] = []
    trace = TraceCollector(ouvinte=recebidos.append)
    verificador = FakeVerificadorWeb(
        ResultadoVerificacao(status="confirmado", nome_sugerido="POLIA DA CORREIA", justificativa="ok", fontes=[])
    )

    executar_caso(
        "83061", 1, llm=TracingLLMProvider(_LLMPipeline([1, 2]), trace), dependencias_fk=[],
        buscar_grupo=lambda search_ref, brand_id: grupo, verificar_web=verificador, trace=trace,
        coleta_html=False,
    )

    (carregado,) = [e for e in recebidos if isinstance(e, GrupoCarregado)]
    posicao = recebidos.index(carregado)
    anterior = recebidos[posicao - 1]
    assert isinstance(anterior, EventoExecucao) and anterior.nome == "buscar_grupo"
    assert (type(recebidos[posicao + 1]), recebidos[posicao + 1].nome) == (EtapaIniciada, "particionar_grupo")
    assert carregado.grupo_ref == "83061:CITROEN"
    assert carregado.registros == tuple(para_json(registro) for registro in grupo)
    assert [e for e in recebidos if isinstance(e, EventoExecucao)] == trace.eventos()


def test_verificacao_web_e_coleta_sinalizam_inicio_antes_do_registro(fazer_registro):
    from arbitration.nome import arbitrar_nome
    from arbitration.pesquisa_web import ContextoPesquisaWeb

    class PortaQueRegistra:
        def processar(self, ativacao, *, contexto, trace):
            trace.registrar("tool", "coletar_paginas", saida={"desfecho": "nao_executada"})

    class LLMProibido:
        def gerar_json(self, *args, **kwargs):
            raise AssertionError("o LLM não deveria ser chamado neste caminho")

    inconclusivo = ResultadoVerificacao(
        status="inconclusivo", nome_sugerido=None, justificativa="fontes conflitantes", fontes=[]
    )
    recebidos: list[Any] = []
    trace = TraceCollector(ouvinte=recebidos.append)

    arbitrar_nome(
        [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")],
        llm=LLMProibido(), verificar_web=FakeVerificadorWeb(inconclusivo), trace=trace,
        contexto_web=ContextoPesquisaWeb(coleta=PortaQueRegistra()),
    )

    sequencia = [
        (type(e).__name__, e.nome) for e in recebidos
        if getattr(e, "nome", "") in {"verificar_nomenclatura_peca", "coletar_paginas"}
    ]
    assert sequencia == [
        ("EtapaIniciada", "verificar_nomenclatura_peca"),
        ("EventoExecucao", "verificar_nomenclatura_peca"),
        ("EtapaIniciada", "coletar_paginas"),
        ("EventoExecucao", "coletar_paginas"),
    ]
