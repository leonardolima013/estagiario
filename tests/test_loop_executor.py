import json
from threading import Event

from loop.executor import executar_loop
from loop.models import FamiliaSorteada, LoopConfig, LoopStatus, MotivoParada, LoopProgresso
from partitioning.models import Particao
from pipeline import ResultadoCaso
from tools.sortear_grupo import NenhumGrupoDuplicadoError


def _caso_fake(search_ref, brand_id, llm, dependencias_fk, **kwargs):
    return ResultadoCaso(
        grupo_ref=f"{search_ref}:MARCA",
        particao=Particao(grupo_ref=f"{search_ref}:MARCA", subclusters=[]),
        decisoes=[],
        sql="",
        grupo=[],
    )


def _sorter_sequencial(familias):
    restantes = list(familias)

    def sortear(*, excluir):
        for familia in restantes:
            if familia.chave not in excluir:
                return familia
        raise NenhumGrupoDuplicadoError("esgotado")

    return sortear


def test_executa_n_iteracoes_e_gera_os_dois_arquivos(tmp_path):
    familias = [FamiliaSorteada("A", 1, "M"), FamiliaSorteada("B", 2, "M")]
    resultado = executar_loop(
        LoopConfig(2, tmp_path), llm=object(), dependencias_fk=[],
        sortear_fn=_sorter_sequencial(familias), executar_caso_fn=_caso_fake,
    )

    assert resultado.status == LoopStatus.CONCLUIDO
    assert resultado.motivo_parada == MotivoParada.ITERACOES_SOLICITADAS_ALCANCADAS
    assert [i.familia.chave for i in resultado.iteracoes] == [("A", 1), ("B", 2)]
    assert resultado.caminho_json.exists()
    assert resultado.caminho_sql.exists()
    documento = json.loads(resultado.caminho_json.read_text(encoding="utf-8"))
    assert len(documento["iteracoes"]) == 2


def test_erro_de_uma_iteracao_e_loop_continua(tmp_path):
    familias = [FamiliaSorteada("A", 1, "M"), FamiliaSorteada("B", 2, "M"), FamiliaSorteada("C", 3, "M")]

    def caso_com_erro(search_ref, brand_id, llm, dependencias_fk, **kwargs):
        if search_ref == "B":
            raise RuntimeError("falha isolada")
        return _caso_fake(search_ref, brand_id, llm, dependencias_fk, **kwargs)

    resultado = executar_loop(
        LoopConfig(3, tmp_path), llm=object(), dependencias_fk=[],
        sortear_fn=_sorter_sequencial(familias), executar_caso_fn=caso_com_erro,
    )

    assert resultado.status == LoopStatus.FINALIZADO_COM_ERROS
    assert len(resultado.iteracoes) == 3
    assert resultado.iteracoes[1].erro["tipo"] == "RuntimeError"
    documento = json.loads(resultado.caminho_json.read_text(encoding="utf-8"))
    assert documento["iteracoes"]["3"]["familia"]["codigo"] == "C"


def test_esgotamento_encerra_com_parcial(tmp_path):
    familias = [FamiliaSorteada("A", 1, "M")]
    resultado = executar_loop(
        LoopConfig(3, tmp_path), llm=object(), dependencias_fk=[],
        sortear_fn=_sorter_sequencial(familias), executar_caso_fn=_caso_fake,
    )

    assert resultado.status == LoopStatus.PARCIAL
    assert resultado.motivo_parada == MotivoParada.FAMILIAS_DUPLICADAS_ESGOTADAS
    assert len(resultado.iteracoes) == 1


def test_cancelamento_apos_checkpoint_salva_parcial(tmp_path):
    cancelamento = Event()
    familias = [FamiliaSorteada("A", 1, "M"), FamiliaSorteada("B", 2, "M")]

    def progresso(evento: LoopProgresso):
        if evento.fase == "checkpoint" and evento.indice_atual == 1:
            cancelamento.set()

    resultado = executar_loop(
        LoopConfig(2, tmp_path), llm=object(), dependencias_fk=[],
        sortear_fn=_sorter_sequencial(familias), executar_caso_fn=_caso_fake,
        cancel_event=cancelamento, on_progresso=progresso,
    )

    assert resultado.status == LoopStatus.CANCELADO
    assert len(resultado.iteracoes) == 1
    documento = json.loads(resultado.caminho_json.read_text(encoding="utf-8"))
    assert documento["execucao"]["status"] == "cancelado"


def test_excecao_de_intervencao_apos_cancelamento_nao_vira_merge(tmp_path):
    cancelamento = Event()
    familias = [FamiliaSorteada("A", 1, "M")]

    def caso_cancelado(search_ref, brand_id, llm, dependencias_fk, **kwargs):
        cancelamento.set()
        raise RuntimeError("modal cancelada")

    resultado = executar_loop(
        LoopConfig(1, tmp_path), llm=object(), dependencias_fk=[],
        sortear_fn=_sorter_sequencial(familias), executar_caso_fn=caso_cancelado,
        cancel_event=cancelamento,
    )

    assert resultado.status == LoopStatus.CANCELADO
    assert resultado.iteracoes[0].status.value == "cancelada"
    assert resultado.iteracoes[0].sql == ""
