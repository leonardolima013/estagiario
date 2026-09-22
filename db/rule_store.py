"""Rule store: interface RuleStore + implementação SQLite (SPEC.md §6.3).

O resto da aplicação depende só de `RuleStore.consultar`/`registrar`/`listar_todas` —
trocar o backend para Postgres no futuro é uma troca de implementação desta classe,
não uma reescrita da lógica de sessão.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS estagiario_regras (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    categoria         TEXT NOT NULL,
    campo             TEXT,
    condicao          TEXT NOT NULL,
    resolucao         TEXT NOT NULL,
    grupo_exemplo_ref TEXT,
    criado_por        TEXT NOT NULL,
    criado_em         TEXT NOT NULL DEFAULT (datetime('now')),
    ativo             INTEGER NOT NULL DEFAULT 1,
    titulo            TEXT,
    caso_episodico    TEXT,
    sinais_busca      TEXT
);
"""

_COLUNAS_INTERVENCAO = {
    "titulo": "TEXT",
    "caso_episodico": "TEXT",
    "sinais_busca": "TEXT",
}


@dataclass(frozen=True)
class Regra:
    id: int
    categoria: str
    campo: str | None
    condicao: str
    resolucao: str
    grupo_exemplo_ref: str | None
    criado_por: str
    criado_em: str
    ativo: bool
    # Metadados opcionais da memória de intervenção. Defaults preservam a
    # construção/uso das regras antigas e tornam a extensão aditiva.
    titulo: str | None = None
    caso_episodico: str | None = None
    sinais_busca: str | None = None


def _row_to_regra(row: sqlite3.Row) -> Regra:
    return Regra(
        id=row["id"],
        categoria=row["categoria"],
        campo=row["campo"],
        condicao=row["condicao"],
        resolucao=row["resolucao"],
        grupo_exemplo_ref=row["grupo_exemplo_ref"],
        criado_por=row["criado_por"],
        criado_em=row["criado_em"],
        ativo=bool(row["ativo"]),
        titulo=row["titulo"],
        caso_episodico=row["caso_episodico"],
        sinais_busca=row["sinais_busca"],
    )


class RuleStore:
    """Rule store compartilhado, hoje sobre um arquivo SQLite local."""

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            self._garantir_colunas_intervencao(conn)

    @staticmethod
    def _garantir_colunas_intervencao(conn: sqlite3.Connection) -> None:
        """Aplica a extensão do schema sem quebrar arquivos criados pela versão antiga.

        SQLite não oferece `ADD COLUMN IF NOT EXISTS`; consultar PRAGMA antes torna a
        migração idempotente e segura para reabrir o mesmo RuleStore várias vezes.
        """
        existentes = {row[1] for row in conn.execute("PRAGMA table_info(estagiario_regras)")}
        for coluna, tipo in _COLUNAS_INTERVENCAO.items():
            if coluna not in existentes:
                conn.execute(f"ALTER TABLE estagiario_regras ADD COLUMN {coluna} {tipo}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def consultar(self, categoria: str, campo: str | None = None) -> list[Regra]:
        """Retorna regras ativas de `categoria`, opcionalmente filtradas por `campo`."""
        query = "SELECT * FROM estagiario_regras WHERE categoria = ? AND ativo = 1"
        params: list[str] = [categoria]
        if campo is not None:
            query += " AND campo = ?"
            params.append(campo)
        query += " ORDER BY criado_em DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_regra(row) for row in rows]

    def registrar(
        self,
        categoria: str,
        condicao: str,
        resolucao: str,
        criado_por: str,
        campo: str | None = None,
        grupo_exemplo_ref: str | None = None,
    ) -> Regra:
        """Grava uma nova regra e retorna a linha persistida."""
        criado_em = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO estagiario_regras
                    (categoria, campo, condicao, resolucao, grupo_exemplo_ref, criado_por, criado_em, ativo)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (categoria, campo, condicao, resolucao, grupo_exemplo_ref, criado_por, criado_em),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM estagiario_regras WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return _row_to_regra(row)

    def registrar_intervencao(
        self,
        *,
        titulo: str,
        caso_episodico: str,
        condicao: str,
        resolucao: str,
        criado_por: str,
        sinais_busca: str | None = None,
        campo: str | None = None,
        grupo_exemplo_ref: str | None = None,
    ) -> Regra:
        """Registra uma intervenção aprendida na categoria dedicada.

        `condicao`/`resolucao` são a camada semântica destilada; `titulo`,
        `sinais_busca` e `caso_episodico` preservam o índice legível e a
        evidência concreta que originou a regra.
        """
        criado_em = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO estagiario_regras
                    (categoria, campo, condicao, resolucao, grupo_exemplo_ref,
                     criado_por, criado_em, ativo, titulo, caso_episodico, sinais_busca)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    "intervencao_humana", campo, condicao, resolucao, grupo_exemplo_ref,
                    criado_por, criado_em, titulo, caso_episodico, sinais_busca,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM estagiario_regras WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return _row_to_regra(row)

    def listar_intervencoes(self, campo: str | None = None) -> list[Regra]:
        """Lista intervenções ativas, opcionalmente restritas ao ponto de decisão."""
        query = (
            "SELECT * FROM estagiario_regras "
            "WHERE categoria = 'intervencao_humana' AND ativo = 1"
        )
        params: list[str] = []
        if campo is not None:
            query += " AND campo = ?"
            params.append(campo)
        query += " ORDER BY criado_em DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_regra(row) for row in rows]

    def listar_todas(self) -> list[Regra]:
        """Retorna todas as regras (ativas ou não), para telas de auditoria/TUI."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM estagiario_regras ORDER BY criado_em DESC"
            ).fetchall()
        return [_row_to_regra(row) for row in rows]
