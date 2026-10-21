"""Gera os UPDATEs de realocação de FK (SPEC.md §4.3) — reaproveita a lista de
FkDependency já descoberta por tools.fk_introspection.introspeccao_fk (uma vez
por sessão, não por merge). Pura: não abre conexão nenhuma, só monta texto SQL
com escaping seguro via psycopg.sql (Identifier/Literal funcionam sem conexão
viva — `.as_string(None)`).

Índices únicos com a coluna da FK (ex.: catalog_fit (part_id, version_id)):
quando vencedor e perdedor já têm a mesma linha, o UPDATE simples viola o
índice e derruba a transação do merge inteiro. Para essas tabelas, antes do
primeiro UPDATE, sai um DELETE que remove as linhas que ficariam repetidas
depois da realocação, sempre mantendo a do vencedor quando ela existe.
"""

from __future__ import annotations

from psycopg import sql

from tools.fk_introspection import FkDependency, IndiceUnico


def gerar_updates_fk(
    dependencias: list[FkDependency],
    vencedor_id: int,
    perdedor_ids: list[int],
) -> list[str]:
    """Uma UPDATE por (tabela, coluna) FK, redirecionando FKs dos perdedores pro vencedor.

    Deduplica por (tabela_dependente, coluna_fk) preservando a ordem de primeira
    aparição (F-06): duas constraints distintas na mesma coluna gerariam UPDATEs
    idênticas — uma só basta. Antes do primeiro UPDATE de uma tabela, emite um
    DELETE de deduplicação por índice único que contenha alguma coluna de FK.
    """
    colunas_fk_por_tabela: dict[str, list[str]] = {}
    indices_por_tabela: dict[str, dict[str, IndiceUnico]] = {}
    for dep in dependencias:
        colunas = colunas_fk_por_tabela.setdefault(dep.tabela_dependente, [])
        if dep.coluna_fk not in colunas:
            colunas.append(dep.coluna_fk)
        indices = indices_por_tabela.setdefault(dep.tabela_dependente, {})
        for idx in dep.indices_unicos:
            indices.setdefault(idx.nome, idx)

    statements = []
    vistos: set[tuple[str, str]] = set()
    tabelas_deduplicadas: set[str] = set()
    for dep in dependencias:
        chave = (dep.tabela_dependente, dep.coluna_fk)
        if chave in vistos:
            continue
        vistos.add(chave)
        if dep.tabela_dependente not in tabelas_deduplicadas:
            tabelas_deduplicadas.add(dep.tabela_dependente)
            for idx in indices_por_tabela[dep.tabela_dependente].values():
                statements.append(
                    _delete_duplicatas(
                        dep.tabela_dependente,
                        idx,
                        colunas_fk_por_tabela[dep.tabela_dependente],
                        vencedor_id,
                        perdedor_ids,
                    )
                )
        statement = sql.SQL("UPDATE {tabela} SET {coluna} = {vencedor} WHERE {coluna} IN ({perdedores});").format(
            tabela=sql.Identifier(dep.tabela_dependente),
            coluna=sql.Identifier(dep.coluna_fk),
            vencedor=sql.Literal(vencedor_id),
            perdedores=_lista(perdedor_ids),
        )
        statements.append(statement.as_string(None))
    return statements


def _lista(ids: list[int]) -> sql.Composable:
    return sql.SQL(", ").join(sql.Literal(i) for i in ids)


def _delete_duplicatas(
    tabela: str,
    indice: IndiceUnico,
    colunas_fk_tabela: list[str],
    vencedor_id: int,
    perdedor_ids: list[int],
) -> str:
    """DELETE das linhas que colidiriam em `indice` depois da realocação.

    Agrupa as linhas tocadas pelo merge pela chave do índice como ela fica
    depois dos UPDATEs (cada coluna de FK com perdedor vira o vencedor) e
    mantém uma por grupo: a que tem menos colunas apontando para perdedores
    (a do vencedor, quando existe), depois a de menor `ctid`. Linhas com NULL
    em alguma coluna do índice não colidem (NULLs distintos) e ficam de fora,
    assim como as que não atendem ao predicado de um índice parcial.
    """
    fks = [c for c in indice.colunas if c in colunas_fk_tabela]
    perdedores = _lista(perdedor_ids)
    todos = _lista([vencedor_id, *perdedor_ids])

    chave_final = sql.SQL(", ").join(
        sql.SQL("CASE WHEN {c} IN ({perdedores}) THEN {vencedor} ELSE {c} END").format(
            c=sql.Identifier(c), perdedores=perdedores, vencedor=sql.Literal(vencedor_id)
        )
        if c in fks
        else sql.Identifier(c)
        for c in indice.colunas
    )
    prioridade = sql.SQL(" + ").join(
        sql.SQL("CASE WHEN {c} IN ({perdedores}) THEN 1 ELSE 0 END").format(
            c=sql.Identifier(c), perdedores=perdedores
        )
        for c in fks
    )
    tocadas = sql.SQL(" OR ").join(
        sql.SQL("{c} IN ({todos})").format(c=sql.Identifier(c), todos=todos) for c in fks
    )
    condicoes = [sql.SQL("({})").format(tocadas)]
    condicoes += [sql.SQL("{} IS NOT NULL").format(sql.Identifier(c)) for c in indice.colunas]
    if indice.predicado:
        condicoes.append(sql.SQL("({})").format(sql.SQL(indice.predicado)))

    statement = sql.SQL(
        "DELETE FROM {tabela} WHERE ctid IN ("
        "SELECT ctid FROM ("
        "SELECT ctid, row_number() OVER (PARTITION BY {chave} ORDER BY {prioridade}, ctid) AS rn "
        "FROM {tabela} WHERE {condicoes}"
        ") AS dup WHERE dup.rn > 1);"
    ).format(
        tabela=sql.Identifier(tabela),
        chave=chave_final,
        prioridade=prioridade,
        condicoes=sql.SQL(" AND ").join(condicoes),
    )
    return statement.as_string(None)
