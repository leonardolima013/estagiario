"""Rastro do merge em `catalog_part`: `updated` e `catalog_partactivity`.

O backend preenche `catalog_part.updated` pelo `auto_now` do Django e grava
`catalog_partactivity` no próprio código, então um script SQL não ganha nenhum
dos dois sozinho. Sem eles, a peça inativada aparece como não alterada (achado
na execução em produção do `loop-teste-100.sql`, 2026-10-23).

Os registros seguem o formato que o merge do backend grava (conferido nas linhas
de 2026-10-16 da cópia local): para cada perdedor, `lifecycle_status`
(`ACTIVE` -> `INACTIVE`) e `merged_into` (id do vencedor); para o vencedor,
`merge` com a lista dos ids mesclados. Todos com `activity_type = 'UPD'`,
`created = now()` e os campos de owner/provider nulos: o merge não muda a fonte
de nenhum campo, e um provider aqui viraria a "fonte atual" lida por
`tools/reliability.py::buscar_fonte_atual` (que filtra por `attribute`, então
estes três atributos não interferem nela).

Precisa vir depois da migração de FK: ela move as linhas de
`catalog_partactivity` dos perdedores para o vencedor, e levaria junto as
linhas novas dos perdedores.
"""

from __future__ import annotations

from psycopg import sql

STATUS_ATIVO = "ACTIVE"


def descricao_merge(perdedor_ids: list[int]) -> str:
    """Texto de `current_value` da atividade `merge` do vencedor, igual ao do backend."""
    return f"Unificação de duplicatas (IDs mesclados: [{', '.join(str(pid) for pid in perdedor_ids)}])"


def gerar_atualizacao_updated(vencedor_id: int, perdedor_ids: list[int]) -> str:
    """`updated = now()` no vencedor e nos perdedores (o `auto_now` que o SQL não tem)."""
    return sql.SQL("UPDATE catalog_part SET {coluna} = now() WHERE id IN ({ids});").format(
        coluna=sql.Identifier("updated"),
        ids=sql.SQL(", ").join(sql.Literal(pid) for pid in [vencedor_id, *perdedor_ids]),
    ).as_string(None)


def gerar_atividades_merge(
    vencedor_id: int,
    perdedor_ids: list[int],
    status_inativo: str,
    autor_id: int,
) -> str:
    """`INSERT` em `catalog_partactivity` com as atividades do merge."""
    linhas = [(pid, "lifecycle_status", STATUS_ATIVO, status_inativo) for pid in perdedor_ids]
    linhas += [(pid, "merged_into", None, str(vencedor_id)) for pid in perdedor_ids]
    linhas.append((vencedor_id, "merge", None, descricao_merge(perdedor_ids)))

    valores = sql.SQL(",\n    ").join(
        sql.SQL("({part_id}, 'UPD', {atributo}, {anterior}, {atual}, now(), {autor})").format(
            part_id=sql.Literal(part_id),
            atributo=sql.Literal(atributo),
            anterior=sql.Literal(anterior),
            atual=sql.Literal(atual),
            autor=sql.Literal(autor_id),
        )
        for part_id, atributo, anterior, atual in linhas
    )
    return sql.SQL(
        "INSERT INTO catalog_partactivity "
        "(part_id, activity_type, attribute, previous_value, current_value, created, author_id) VALUES\n    {valores};"
    ).format(valores=valores).as_string(None)
