"""Armazenamento local de HTML por conteúdo (content-addressable), em SQLite.

Cada página é salva comprimida (gzip) e indexada pelo sha256 do HTML decodificado,
o que deduplica automaticamente páginas idênticas buscadas para peças diferentes
(ex.: mesmo distribuidor referenciado por mais de um código).

Uso:
    store = PageStore("estagiario.db")
    store.setup()
    sha = store.save_html(
        html_text, url="https://...", url_normalized="https://...",
        domain="drift.com.br", part_search_ref="DK860431", part_brand="BOSCH",
        match_field="sku", confidence="high",
    )
    html = store.get_html(sha)
"""
from __future__ import annotations

import gzip
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS page_content (
    sha256      TEXT PRIMARY KEY,
    html_gzip   BLOB NOT NULL,
    size_bytes  INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_fetch (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    url                 TEXT NOT NULL,
    url_normalized      TEXT NOT NULL,
    domain              TEXT NOT NULL,
    sha256              TEXT NOT NULL REFERENCES page_content(sha256),
    part_search_ref     TEXT NOT NULL,
    part_brand          TEXT NOT NULL,
    match_field         TEXT NOT NULL,
    confidence          TEXT NOT NULL,
    fetched_at          TEXT NOT NULL,
    status_code         INTEGER,
    extracted           INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_page_fetch_url ON page_fetch(url_normalized);
CREATE INDEX IF NOT EXISTS idx_page_fetch_part ON page_fetch(part_search_ref, part_brand);
"""


@dataclass
class FetchRecord:
    id: int
    url: str
    sha256: str
    match_field: str
    confidence: str


class PageStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def setup(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def already_fetched(self, url_normalized: str, max_age_days: int | None = None) -> FetchRecord | None:
        """Verifica se a URL já foi salva, opcionalmente dentro de uma janela de validade."""
        query = (
            "SELECT id, url, sha256, match_field, confidence, fetched_at "
            "FROM page_fetch WHERE url_normalized = ? ORDER BY fetched_at DESC LIMIT 1"
        )
        with self._connect() as conn:
            row = conn.execute(query, (url_normalized,)).fetchone()
        if row is None:
            return None
        if max_age_days is not None:
            fetched_at = datetime.fromisoformat(row[5])
            age_days = (datetime.now(timezone.utc) - fetched_at).days
            if age_days > max_age_days:
                return None
        return FetchRecord(id=row[0], url=row[1], sha256=row[2], match_field=row[3], confidence=row[4])

    def save_html(
        self,
        html: str,
        *,
        url: str,
        url_normalized: str,
        domain: str,
        part_search_ref: str,
        part_brand: str,
        match_field: str,
        confidence: str,
        status_code: int | None = None,
    ) -> str:
        raw = html.encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        compressed = gzip.compress(raw, compresslevel=6)
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO page_content (sha256, html_gzip, size_bytes, created_at) "
                "VALUES (?, ?, ?, ?)",
                (sha, compressed, len(raw), now),
            )
            conn.execute(
                """INSERT INTO page_fetch
                   (url, url_normalized, domain, sha256, part_search_ref, part_brand,
                    match_field, confidence, fetched_at, status_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (url, url_normalized, domain, sha, part_search_ref, part_brand,
                 match_field, confidence, now, status_code),
            )
        return sha

    def get_html(self, sha256: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT html_gzip FROM page_content WHERE sha256 = ?", (sha256,)
            ).fetchone()
        if row is None:
            raise KeyError(sha256)
        return gzip.decompress(row[0]).decode("utf-8")

    def pending_extraction(self, part_search_ref: str, part_brand: str) -> list[FetchRecord]:
        """Páginas já armazenadas para a peça, ainda não processadas pela extração."""
        query = """SELECT id, url, sha256, match_field, confidence FROM page_fetch
                   WHERE part_search_ref = ? AND part_brand = ? AND extracted = 0
                   ORDER BY confidence DESC, fetched_at ASC"""
        with self._connect() as conn:
            rows = conn.execute(query, (part_search_ref, part_brand)).fetchall()
        return [
            FetchRecord(id=r[0], url=r[1], sha256=r[2], match_field=r[3], confidence=r[4])
            for r in rows
        ]
