"""Fila de famílias do loop (`loop.fila`): SQLite local, sem Postgres.

A leitura do Postgres é injetada (`ler_familias`), então nada aqui abre conexão.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

from loop.fila import FilaFamilias
from loop.models import FamiliaSorteada, IteracaoStatus


def _leitor(*lotes):
    chamadas = []

    def ler():
        chamadas.append(1)
        return [list(lote) for lote in lotes]

    ler.chamadas = chamadas
    return ler


def _fila(tmp_path, *lotes):
    return FilaFamilias(tmp_path / "fila.db", ler_familias=_leitor(*lotes))


def _drenar(fila):
    vistas = []
    while (familia := fila.proxima()) is not None:
        vistas.append(familia)
        fila.concluir(familia, IteracaoStatus.SUCESSO)
    return vistas


def test_construir_nao_faz_io(tmp_path):
    FilaFamilias(tmp_path / "fila.db", ler_familias=_leitor())
    assert not (tmp_path / "fila.db").exists()


def test_preparar_monta_uma_vez_e_entrega_das_menores_para_as_maiores(tmp_path):
    ler = _leitor(
        [("B", 2, "MARCA B", 5), ("A", 1, "MARCA A", 3)],
        [("C", 3, "MARCA C", 2), ("A", 9, "MARCA X", 3)],
    )
    fila = FilaFamilias(tmp_path / "fila.db", ler_familias=ler)

    fila.preparar()
    fila.preparar()  # já montada: não relê o banco

    assert len(ler.chamadas) == 1
    assert [f.chave for f in _drenar(fila)] == [("C", 3), ("A", 1), ("A", 9), ("B", 2)]
    assert fila.contagem() == {"pendente": 0, "em_andamento": 0, "concluida": 4, "erro": 0}
    assert fila.proxima() is None


def test_proxima_marca_em_andamento_e_concluir_grava_o_desfecho(tmp_path):
    fila = _fila(tmp_path, [("A", 1, "M", 2), ("B", 2, "M", 2), ("C", 3, "M", 2)])
    fila.preparar()

    a = fila.proxima()
    assert a == FamiliaSorteada("A", 1, "M")
    assert fila.contagem()["em_andamento"] == 1
    fila.concluir(a, IteracaoStatus.SUCESSO)
    b = fila.proxima()
    fila.concluir(b, IteracaoStatus.ERRO, {"tipo": "RuntimeError", "mensagem": "falhou"})
    c = fila.proxima()
    fila.concluir(c, IteracaoStatus.CANCELADA)

    assert fila.contagem() == {"pendente": 1, "em_andamento": 0, "concluida": 1, "erro": 1}
    # Cancelada volta para a fila e é a próxima.
    assert fila.proxima() == c
    with sqlite3.connect(fila.caminho) as conn:
        erro = conn.execute(
            "SELECT ultimo_erro, tentativas FROM fila_familias WHERE search_ref = 'B'"
        ).fetchone()
    assert erro == ("RuntimeError: falhou", 1)


def test_reabrir_erros_devolve_a_fila(tmp_path):
    fila = _fila(tmp_path, [("A", 1, "M", 2)])
    fila.preparar()
    a = fila.proxima()
    fila.concluir(a, IteracaoStatus.ERRO, {"tipo": "X", "mensagem": "y"})
    assert fila.proxima() is None

    assert fila.reabrir_erros() == 1
    assert fila.proxima() == a


def test_encerrar_devolve_o_que_ficou_em_andamento(tmp_path):
    fila = _fila(tmp_path, [("A", 1, "M", 2), ("B", 2, "M", 2)])
    fila.preparar()
    fila.proxima()

    fila.encerrar()

    assert fila.contagem()["em_andamento"] == 0
    assert fila.contagem()["pendente"] == 2


def test_em_andamento_de_processo_morto_volta_a_pendente(tmp_path):
    fila = _fila(tmp_path, [("A", 1, "M", 2), ("B", 2, "M", 2)])
    fila.preparar()
    morto = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                           capture_output=True, text=True, check=True)
    pid_morto = int(morto.stdout)
    with sqlite3.connect(fila.caminho) as conn:
        conn.execute(
            "UPDATE fila_familias SET status = 'em_andamento', pid = ?, execucao = 'outra' "
            "WHERE search_ref = 'A'", (pid_morto,),
        )
        conn.execute(
            "UPDATE fila_familias SET status = 'em_andamento', pid = ?, execucao = 'viva' "
            "WHERE search_ref = 'B'", (os.getppid(),),
        )

    nova = FilaFamilias(fila.caminho, ler_familias=_leitor())
    nova.preparar()

    # A (processo morto) voltou; B pertence a um processo vivo e fica onde está.
    assert nova.contagem() == {"pendente": 1, "em_andamento": 1, "concluida": 0, "erro": 0}
    assert nova.proxima().chave == ("A", 1)


def test_montar_de_novo_mantem_concluidas_remove_pendentes_que_sairam_e_adiciona_novas(tmp_path):
    fila = _fila(tmp_path, [("A", 1, "M", 2), ("B", 2, "M", 3), ("C", 3, "M", 4)])
    fila.preparar()
    a = fila.proxima()
    fila.concluir(a, IteracaoStatus.SUCESSO)

    # Nova foto: B deixou de ser duplicada; A continua (SQL ainda não aplicado); D é nova.
    fila._ler_familias = _leitor([("A", 1, "M", 2), ("C", 3, "M", 4), ("D", 4, "M", 2)])
    assert fila.montar() == 3

    assert fila.contagem() == {"pendente": 2, "em_andamento": 0, "concluida": 1, "erro": 0}
    assert [f.chave for f in _drenar(fila)] == [("D", 4), ("C", 3)]


def test_falha_na_leitura_do_banco_nao_deixa_fila_meio_montada(tmp_path):
    def ler_quebrado():
        yield [("A", 1, "M", 2)]
        raise RuntimeError("banco caiu no meio")

    fila = FilaFamilias(tmp_path / "fila.db", ler_familias=ler_quebrado)
    with pytest.raises(RuntimeError):
        fila.preparar()

    assert not fila.montada
    assert fila.contagem()["pendente"] == 0
