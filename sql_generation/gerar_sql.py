"""gerar_sql (SPEC.md §4.5, §6.1) — compila as decisões de merge (Fase 3) num único
script .sql revisável. Nunca aplicado automaticamente — script pra um humano revisar
e rodar quando/como decidir (SPEC.md §9).

Ordem dentro de cada merge, cada um na sua própria transação (uma falha isolada não
derruba os outros merges do script): UPDATE do vencedor -> UPDATEs de realocação de
FK (SPEC.md §4.3) -> DELETE dos perdedores.
"""

from __future__ import annotations

from psycopg import sql

from sql_generation.fk_migration import gerar_updates_fk
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from tools.fk_introspection import FkDependency


def _bloco_sinalizado(grupo: GrupoSinalizado) -> str:
    return f"-- REVISÃO MANUAL: grupo {grupo.grupo_ref} não mesclado automaticamente — {grupo.motivo}"


def _bloco_merge(decisao: DecisaoMerge, dependencias_fk: list[FkDependency]) -> str:
    linhas = [f"-- Grupo {decisao.grupo_ref}: merge {decisao.perdedor_ids} -> {decisao.vencedor_id}", "BEGIN;"]

    _SEM_SNAPSHOT = object()
    campos_set = []
    for dc in decisao.decisoes_campo:
        if dc.escalado_humano:
            linhas.append(f"-- REVISAR MANUALMENTE: campo {dc.campo} ambíguo — {dc.justificativa}")
            continue
        # Diff-only (Q-02=a): se o valor decidido já é o valor atual do vencedor,
        # não há o que atualizar — omite o campo do SET (evita UPDATE inócuo e
        # sobrescrita acidental). Quando não há snapshot pro campo (DecisaoMerge
        # montada sem valores_atuais_vencedor), mantém o comportamento legado de
        # escrever o campo.
        valor_atual = decisao.valores_atuais_vencedor.get(dc.campo, _SEM_SNAPSHOT)
        if valor_atual is not _SEM_SNAPSHOT and valor_atual == dc.valor:
            continue
        campos_set.append(
            sql.SQL("{campo} = {valor}").format(campo=sql.Identifier(dc.campo), valor=sql.Literal(dc.valor))
        )

    if campos_set:
        update_vencedor = sql.SQL("UPDATE catalog_part SET {sets} WHERE id = {vencedor};").format(
            sets=sql.SQL(", ").join(campos_set),
            vencedor=sql.Literal(decisao.vencedor_id),
        )
        linhas.append(update_vencedor.as_string(None))

    linhas.extend(gerar_updates_fk(dependencias_fk, decisao.vencedor_id, decisao.perdedor_ids))

    delete_perdedores = sql.SQL("DELETE FROM catalog_part WHERE id IN ({perdedores});").format(
        perdedores=sql.SQL(", ").join(sql.Literal(pid) for pid in decisao.perdedor_ids)
    )
    linhas.append(delete_perdedores.as_string(None))
    linhas.append("COMMIT;")

    return "\n".join(linhas)


def gerar_sql(
    decisoes: list[DecisaoMerge | GrupoSinalizado],
    dependencias_fk: list[FkDependency],
) -> str:
    blocos = []
    for decisao in decisoes:
        if isinstance(decisao, GrupoSinalizado):
            blocos.append(_bloco_sinalizado(decisao))
        else:
            blocos.append(_bloco_merge(decisao, dependencias_fk))
    return "\n\n".join(blocos) + "\n"
