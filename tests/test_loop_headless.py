"""Runner headless (`loop.headless`) e opção não interativa da tela "Rodar loop".

Todas as dependências do runner são injetadas: sem banco, sem LLM, sem rede.
"""

from __future__ import annotations

import json

import pytest

from db.rule_store import RuleStore
from loop import headless
from loop.fila import FilaFamilias
from loop.models import LoopProgresso, LoopStatus, MotivoParada, ResultadoLoop
from partitioning.models import Particao
from pipeline import ResultadoCaso, executar_caso
from tui.app import EstagiarioApp


def _fila(tmp_path):
    return FilaFamilias(tmp_path / "fila.db", ler_familias=lambda: [[("A", 1, "M", 2), ("B", 2, "M", 2)]])


def _main(tmp_path, argv, *, executar_loop_fn=None, fila=None):
    return headless.main(
        argv + ["--output-dir", str(tmp_path / "output")],
        executar_loop_fn=executar_loop_fn,
        fila=fila if fila is not None else _fila(tmp_path),
        criar_llm=object,
        introspeccao_fk_fn=lambda tabela, coluna: [],
        criar_rule_store=lambda: RuleStore(tmp_path / "regras.db"),
        criar_verificador=lambda: "verificador-serper",
    )


def test_headless_e_nao_interativo_com_todas_as_opcoes_ligadas(tmp_path, capsys):
    recebido = {}

    def executar_loop_fake(config, llm, deps, **kwargs):
        recebido.update(kwargs)
        recebido["iteracoes"] = config.iteracoes
        return ResultadoLoop(
            run_id="r", status=LoopStatus.CONCLUIDO,
            motivo_parada=MotivoParada.ITERACOES_SOLICITADAS_ALCANCADAS,
            iteracoes_solicitadas=config.iteracoes,
        )

    assert _main(tmp_path, ["--iteracoes", "7"], executar_loop_fn=executar_loop_fake) == headless.SAIDA_OK

    assert recebido["iteracoes"] == 7
    assert recebido["pedir_intervencao"] is None
    assert recebido["pesquisa_web"] is True
    assert recebido["coleta_html"] is True
    assert recebido["fallback_stealth"] is True
    assert recebido["verificar_web"] == "verificador-serper"
    assert isinstance(recebido["fonte_familias"], FilaFamilias)
    assert "não interativo" in capsys.readouterr().out


def test_headless_roda_o_loop_real_sobre_a_fila(tmp_path, capsys):
    def caso(search_ref, brand_id, llm, deps, **kwargs):
        assert kwargs["pedir_intervencao"] is None
        ref = f"{search_ref}:M"
        return ResultadoCaso(grupo_ref=ref, particao=Particao(ref, []), decisoes=[], sql="", grupo=[])

    from functools import partial

    from loop.executor import executar_loop

    fila = _fila(tmp_path)
    codigo = _main(tmp_path, ["--iteracoes", "5"], executar_loop_fn=partial(executar_loop, executar_caso_fn=caso), fila=fila)

    assert codigo == headless.SAIDA_OK
    saida = capsys.readouterr().out
    assert "[1/5] A/M: ok" in saida and "[2/5] B/M: ok" in saida
    assert "familias_duplicadas_esgotadas" in saida
    assert fila.contagem()["concluida"] == 2
    assert list((tmp_path / "output").glob("loop_*.json"))


@pytest.mark.parametrize(
    ("status", "motivo", "codigo"),
    [
        (LoopStatus.FINALIZADO_COM_ERROS, MotivoParada.ITERACOES_SOLICITADAS_ALCANCADAS, headless.SAIDA_COM_ERROS),
        (LoopStatus.CANCELADO, MotivoParada.CANCELADO_PELO_USUARIO, headless.SAIDA_CANCELADO),
        (LoopStatus.FINALIZADO_COM_ERROS, MotivoParada.ERRO_FATAL, headless.SAIDA_FATAL),
        (LoopStatus.PARCIAL, MotivoParada.FAMILIAS_DUPLICADAS_ESGOTADAS, headless.SAIDA_OK),
    ],
)
def test_codigo_de_saida(status, motivo, codigo):
    resultado = ResultadoLoop(run_id="r", status=status, motivo_parada=motivo, iteracoes_solicitadas=1)
    assert headless.codigo_saida(resultado) == codigo


def test_status_e_atualizacao_da_fila(tmp_path, capsys):
    fila = _fila(tmp_path)

    assert _main(tmp_path, ["--atualizar-fila"], fila=fila) == headless.SAIDA_OK
    assert _main(tmp_path, ["--status-fila"], fila=fila) == headless.SAIDA_OK

    saida = capsys.readouterr().out
    assert "2 famílias duplicadas lidas." in saida
    assert "pendente=2" in saida


def test_iteracoes_obrigatorias(tmp_path):
    with pytest.raises(SystemExit):
        _main(tmp_path, [])


def test_recuperar_json(tmp_path, capsys):
    journal = tmp_path / ".loop_20260101_000000_000001.jsonl"
    journal.write_text(json.dumps({"indice": 1, "status": "sucesso", "iniciada_em": "a", "finalizada_em": "b"}) + "\n")

    assert _main(tmp_path, ["--recuperar-json", str(journal)]) == headless.SAIDA_OK
    gerado = tmp_path / "loop_20260101_000000_000001.json"
    assert json.loads(gerado.read_text())["iteracoes"]["1"]["status"] == "sucesso"
    assert "JSON gerado" in capsys.readouterr().out


# -- tela "Rodar loop" ------------------------------------------------------


class _LLMProibido:
    def gerar_json(self, *args, **kwargs):
        raise AssertionError("o fake não chama o LLM")


async def test_tela_do_loop_alterna_para_nao_interativo(tmp_path):
    from textual.widgets import Button

    recebidos = []

    def executar_loop_fake(config, llm, dependencias_fk, **kwargs):
        recebidos.append(kwargs["pedir_intervencao"])
        kwargs["on_progresso"](LoopProgresso(total=1, indice_atual=1, fase="checkpoint", mensagem="ok"))
        return ResultadoLoop(
            run_id="run", status=LoopStatus.CONCLUIDO,
            motivo_parada=MotivoParada.ITERACOES_SOLICITADAS_ALCANCADAS, iteracoes_solicitadas=1,
            caminho_json=config.output_dir / "loop.json", caminho_sql=config.output_dir / "loop.sql",
        )

    app = EstagiarioApp(
        rule_store=RuleStore(tmp_path / "t.db"), llm=_LLMProibido(), dependencias_fk=[],
        executar_loop_fn=executar_loop_fake, output_dir=tmp_path,
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#btn-rodar-loop")
        await pilot.pause()
        botao = app.query_one("#btn-intervencao-loop", Button)
        assert "interativa" in str(botao.label) and "não" not in str(botao.label)

        app.query_one("#input-iteracoes").value = "1"
        await pilot.click("#btn-iniciar-loop")
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.click("#btn-intervencao-loop")
        await pilot.pause()
        assert "não interativa" in str(botao.label)
        await pilot.click("#btn-iniciar-loop")
        await app.workers.wait_for_complete()
        await pilot.pause()
        # O painel continua com a altura que tinha (a opção fica na linha de progresso).
        assert app.query_one("#loop-rolagem").region.y <= 5

    assert callable(recebidos[0])
    assert recebidos[1] is None


# -- pipeline -----------------------------------------------------------------


def test_familia_com_menos_de_duas_pecas_nao_chama_o_llm(fazer_registro):
    from loop.tracing import TraceCollector

    trace = TraceCollector()
    resultado = executar_caso(
        "A", 1, _LLMProibido(), [], buscar_grupo=lambda s, b: [fazer_registro(1, "PIVO")], trace=trace,
    )

    assert resultado.decisoes == [] and resultado.sql == ""
    assert [e.nome for e in trace.eventos()] == ["buscar_grupo", "grupo_sem_duplicatas"]
