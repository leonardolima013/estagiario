"""Gera o SQL que corrige um script de merge já executado sem `updated` e sem
`catalog_partactivity` (scripts gerados antes de sql_generation/atividade.py).

Lê os blocos de merge do .sql do loop (`-- Grupo <ref>: merge [perdedores] -> vencedor`,
conferidos contra a linha de inativação do mesmo bloco) e escreve um script com
uma transação única que, só para os merges efetivados no banco de destino
(vencedor `ACTIVE` e todos os perdedores `INACTIVE`):

- insere em `catalog_partactivity` os mesmos registros que a pipeline passa a
  gravar (`lifecycle_status` e `merged_into` nos perdedores, `merge` no vencedor),
  pulando os que já existem;
- marca `catalog_part.updated = now()` nas peças que ganharam um registro.

Rodar duas vezes não duplica nada nem muda o `updated` de novo. O script gerado
nunca é executado aqui.

Uso:
    venv/bin/python scripts/gerar_correcao_atividade_merge.py <loop.sql> [-o saida.sql] [--autor-id 8]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from sql_generation.atividade import STATUS_ATIVO, descricao_merge  # noqa: E402
from sql_generation.gerar_sql import STATUS_INATIVO  # noqa: E402

_CABECALHO_MERGE = re.compile(r"^-- Grupo (?P<ref>.+): merge \[(?P<perdedores>[\d, ]+)\] -> (?P<vencedor>\d+)$")
_INATIVACAO = re.compile(r"""^UPDATE catalog_part SET "lifecycle_status" = 'INACTIVE' WHERE id IN \((?P<ids>[\d, ]+)\);$""")


def _ids(texto: str) -> list[int]:
    return [int(parte) for parte in texto.split(",")]


def ler_merges(texto_sql: str) -> list[tuple[str, int, list[int]]]:
    """(grupo_ref, vencedor_id, perdedor_ids) de cada bloco de merge, na ordem do script."""
    merges: list[tuple[str, int, list[int]]] = []
    aberto: tuple[str, int, list[int]] | None = None
    for linha in texto_sql.splitlines():
        if cabecalho := _CABECALHO_MERGE.match(linha):
            if aberto is not None:
                raise ValueError(f"merge sem inativação: {aberto[0]}")
            aberto = (cabecalho["ref"], int(cabecalho["vencedor"]), _ids(cabecalho["perdedores"]))
        elif inativacao := _INATIVACAO.match(linha):
            if aberto is None or _ids(inativacao["ids"]) != aberto[2]:
                raise ValueError(f"inativação sem cabeçalho correspondente: {linha}")
            merges.append(aberto)
            aberto = None
    if aberto is not None:
        raise ValueError(f"merge sem inativação: {aberto[0]}")
    vistos = [pid for _, vencedor, perdedores in merges for pid in (vencedor, *perdedores)]
    if len(vistos) != len(set(vistos)):
        raise ValueError("a mesma peça aparece em mais de um merge")
    return merges


def _literal(texto: str) -> str:
    return "'" + texto.replace("'", "''") + "'"


def gerar_correcao(merges: list[tuple[str, int, list[int]]], autor_id: int, origem: str, sha256: str) -> str:
    valores = ",\n    ".join(
        f"({vencedor}, ARRAY[{', '.join(map(str, perdedores))}]::integer[], {_literal(descricao_merge(perdedores))}, {_literal(ref)})"
        for ref, vencedor, perdedores in merges
    )
    pecas = sum(1 + len(perdedores) for _, _, perdedores in merges)
    return f"""-- Correção do rastro dos merges de {origem}
-- SHA-256 do script de origem: {sha256}
-- {len(merges)} merges, {pecas} peças. Gerado por scripts/gerar_correcao_atividade_merge.py.
--
-- O script de origem inativou os perdedores sem atualizar catalog_part.updated e sem
-- gravar catalog_partactivity. Esta correção, numa transação única e só para os merges
-- efetivados (vencedor ACTIVE e todos os perdedores INACTIVE):
--   * insere em catalog_partactivity, com author_id = {autor_id} e created = now():
--       perdedor: lifecycle_status ({STATUS_ATIVO} -> {STATUS_INATIVO}) e merged_into (id do vencedor)
--       vencedor: merge ('Unificação de duplicatas (IDs mesclados: [...])')
--     pulando o registro que já existe com o mesmo part_id, attribute e current_value;
--   * faz catalog_part.updated = now() nas peças que ganharam algum registro.
-- Rodar de novo não duplica registros nem muda o updated outra vez.
--
-- Antes de rodar, confira que o autor existe:
--   SELECT id, email FROM authentication_user WHERE id = {autor_id};
-- Para só pré-visualizar as contagens, troque o COMMIT do fim por ROLLBACK.

BEGIN;

CREATE TEMP TABLE estagiario_correcao_merge (
    vencedor_id integer NOT NULL,
    perdedor_ids integer[] NOT NULL,
    descricao text NOT NULL,
    grupo_ref text NOT NULL
) ON COMMIT DROP;

INSERT INTO estagiario_correcao_merge (vencedor_id, perdedor_ids, descricao, grupo_ref) VALUES
    {valores};

CREATE TEMP TABLE estagiario_correcao_efetivada ON COMMIT DROP AS
SELECT m.*
FROM estagiario_correcao_merge m
JOIN catalog_part v ON v.id = m.vencedor_id AND v.lifecycle_status = '{STATUS_ATIVO}'
WHERE NOT EXISTS (
    SELECT 1 FROM unnest(m.perdedor_ids) AS u(perdedor_id)
    LEFT JOIN catalog_part p ON p.id = u.perdedor_id
    WHERE p.lifecycle_status IS DISTINCT FROM '{STATUS_INATIVO}'
);

-- Merges do script que NÃO estão efetivados no banco (devem ser zero; são ignorados).
SELECT m.grupo_ref, m.vencedor_id, m.perdedor_ids
FROM estagiario_correcao_merge m
WHERE m.vencedor_id NOT IN (SELECT vencedor_id FROM estagiario_correcao_efetivada)
ORDER BY m.grupo_ref;

CREATE TEMP TABLE estagiario_correcao_atividade ON COMMIT DROP AS
SELECT u.perdedor_id AS part_id, 'lifecycle_status'::text AS attribute,
    '{STATUS_ATIVO}'::text AS previous_value, '{STATUS_INATIVO}'::text AS current_value
FROM estagiario_correcao_efetivada e, unnest(e.perdedor_ids) AS u(perdedor_id)
UNION ALL
SELECT u.perdedor_id, 'merged_into', NULL, e.vencedor_id::text
FROM estagiario_correcao_efetivada e, unnest(e.perdedor_ids) AS u(perdedor_id)
UNION ALL
SELECT e.vencedor_id, 'merge', NULL, e.descricao
FROM estagiario_correcao_efetivada e;

CREATE TEMP TABLE estagiario_correcao_inserida ON COMMIT DROP AS
WITH inseridas AS (
    INSERT INTO catalog_partactivity (part_id, activity_type, attribute, previous_value, current_value, created, author_id)
    SELECT a.part_id, 'UPD', a.attribute, a.previous_value, a.current_value, now(), {autor_id}
    FROM estagiario_correcao_atividade a
    WHERE NOT EXISTS (
        SELECT 1 FROM catalog_partactivity x
        WHERE x.part_id = a.part_id AND x.attribute = a.attribute AND x.current_value = a.current_value
    )
    ORDER BY a.part_id, a.attribute
    RETURNING id, part_id, attribute
)
SELECT * FROM inseridas;

UPDATE catalog_part SET "updated" = now()
WHERE id IN (SELECT part_id FROM estagiario_correcao_inserida);

-- Resumo: esperado na primeira execução = {len(merges)} merges efetivados, {pecas} peças com updated,
-- e um registro por perdedor em lifecycle_status e merged_into e um por vencedor em merge.
SELECT
    (SELECT count(*) FROM estagiario_correcao_merge) AS merges_no_script,
    (SELECT count(*) FROM estagiario_correcao_efetivada) AS merges_efetivados,
    (SELECT count(DISTINCT part_id) FROM estagiario_correcao_inserida) AS pecas_com_updated,
    (SELECT count(*) FROM estagiario_correcao_inserida WHERE attribute = 'lifecycle_status') AS atividades_lifecycle_status,
    (SELECT count(*) FROM estagiario_correcao_inserida WHERE attribute = 'merged_into') AS atividades_merged_into,
    (SELECT count(*) FROM estagiario_correcao_inserida WHERE attribute = 'merge') AS atividades_merge;

COMMIT;
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("script_merge", type=Path, help="o .sql do loop que já foi executado")
    parser.add_argument("-o", "--saida", type=Path, help="arquivo de saída (padrão: stdout)")
    parser.add_argument("--autor-id", type=int, default=None, help="author_id das atividades (padrão: config)")
    args = parser.parse_args()

    conteudo = args.script_merge.read_bytes()
    merges = ler_merges(conteudo.decode("utf-8"))
    autor_id = args.autor_id if args.autor_id is not None else config.atividade_autor_id()
    texto = gerar_correcao(merges, autor_id, args.script_merge.name, hashlib.sha256(conteudo).hexdigest())
    if args.saida:
        args.saida.write_text(texto, encoding="utf-8")
        print(f"{len(merges)} merges -> {args.saida}", file=sys.stderr)
    else:
        sys.stdout.write(texto)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
