"""Saída SQL acumulada do loop — revisável, nunca executada automaticamente.

O loop grava com `EscritorSqlLoop`: cada iteração acrescenta só o próprio bloco
ao arquivo (com `fsync`), sem remontar nem regravar o que já foi escrito. O
conteúdo final é idêntico ao de `gerar_sql_loop` sobre as mesmas iterações. Um
bloco cortado por uma queda no meio da escrita fica sem `COMMIT;` e não é
aplicado parcialmente (cada merge tem a própria transação).
"""

from __future__ import annotations

import os
from pathlib import Path

from loop.models import RegistroIteracao


def _comentario(texto: str) -> str:
    return "\n".join(f"-- {linha}" for linha in str(texto).splitlines())


def sql_da_iteracao(iteracao: RegistroIteracao, total: int) -> str:
    familia = iteracao.familia
    familia_texto = (
        f"search_ref={familia.search_ref}, brand_id={familia.brand_id}, brand={familia.brand}"
        if familia is not None else "família não obtida"
    )
    linhas = [
        f"-- ITERAÇÃO {iteracao.indice}/{total}",
        f"-- Família: {familia_texto}",
        f"-- Resultado: {iteracao.status.value}",
    ]
    if iteracao.erro:
        linhas.append(_comentario(f"Erro: {iteracao.erro.get('tipo')}: {iteracao.erro.get('mensagem')}"))
    if iteracao.sql:
        linhas.append(iteracao.sql.rstrip())
    elif not iteracao.erro:
        linhas.append("-- Nenhum SQL executável gerado para esta iteração.")
    return "\n".join(linhas)


def gerar_sql_loop(iteracoes: list[RegistroIteracao], total: int) -> str:
    if not iteracoes:
        return "-- Nenhuma iteração concluída.\n"
    return "\n\n".join(sql_da_iteracao(iteracao, total) for iteracao in iteracoes) + "\n"


def salvar_sql_atomico(caminho: Path, conteudo: str) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")
    temporario.write_text(conteudo, encoding="utf-8")
    os.replace(temporario, caminho)


class EscritorSqlLoop:
    """Escreve o SQL do loop em append, uma iteração por vez."""

    def __init__(self, caminho: Path, total: int) -> None:
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._total = total
        self._blocos = 0
        self.caminho.write_text("", encoding="utf-8")

    @property
    def blocos(self) -> int:
        return self._blocos

    def registrar(self, iteracao: RegistroIteracao) -> None:
        bloco = sql_da_iteracao(iteracao, self._total)
        # Mesmo resultado de gerar_sql_loop: blocos separados por linha em branco
        # e uma quebra de linha no fim.
        texto = ("\n" if self._blocos else "") + bloco + "\n"
        with self.caminho.open("a", encoding="utf-8") as arquivo:
            arquivo.write(texto)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        self._blocos += 1

    def finalizar(self) -> Path:
        if not self._blocos:
            self.caminho.write_text(gerar_sql_loop([], self._total), encoding="utf-8")
        return self.caminho
