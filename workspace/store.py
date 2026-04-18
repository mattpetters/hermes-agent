"""SQLite FTS5 workspace store.

Manages the workspace.sqlite database: schema creation, file/chunk CRUD,
and BM25 full-text search.
"""

from __future__ import annotations

import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from workspace.constants import get_index_db_path, get_index_dir
from workspace.types import ChunkRecord, FileRecord, SearchResult

_SCHEMA_VERSION = "2"

_SCHEMA_SQL = """\
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    abs_path         TEXT PRIMARY KEY,
    root_path        TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    config_signature TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL,
    modified_at      TEXT NOT NULL,
    indexed_at       TEXT NOT NULL,
    chunk_count      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id       TEXT PRIMARY KEY,
    abs_path       TEXT NOT NULL REFERENCES files(abs_path) ON DELETE CASCADE,
    chunk_index    INTEGER NOT NULL,
    content        TEXT NOT NULL,
    context        TEXT,
    token_count    INTEGER NOT NULL,
    start_line     INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    start_char     INTEGER NOT NULL,
    end_char       INTEGER NOT NULL,
    section        TEXT,
    kind           TEXT NOT NULL,
    chunk_metadata TEXT,
    UNIQUE(abs_path, chunk_index)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    abs_path UNINDEXED,
    retrieval_text,
    section,
    tokenize = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(chunk_id, abs_path, retrieval_text, section)
    VALUES (
        new.chunk_id,
        new.abs_path,
        new.content || ' ' || COALESCE(new.context, ''),
        new.section
    );
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    DELETE FROM chunks_fts WHERE chunk_id = old.chunk_id;
END;
"""


class SQLiteFTS5Store:
    """Concrete FTS5-based workspace index store."""

    def __init__(self, workspace_root: Path) -> None:
        self._db_path = get_index_db_path(workspace_root)
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        index_dir = get_index_dir(self._db_path.parent.parent)
        index_dir.mkdir(parents=True, exist_ok=True)
        # timeout=5.0 sets PRAGMA busy_timeout so regular writes wait on locks
        # instead of raising "database is locked" when another process is
        # mid-write. This covers every statement except `PRAGMA journal_mode
        # = WAL`, which SQLite doesn't subject to busy_timeout — that
        # specific pragma is retried in _init_schema.
        self._conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> SQLiteFTS5Store:
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Store not open — call open() or use as context manager")
        return self._conn

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        try:
            existing = cur.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            existing = None

        if existing is not None and existing[0] != _SCHEMA_VERSION:
            cur.executescript(
                "DROP TABLE IF EXISTS chunks_fts;"
                "DROP TABLE IF EXISTS chunks;"
                "DROP TABLE IF EXISTS files;"
                "DROP TABLE IF EXISTS meta;"
                "DROP TRIGGER IF EXISTS chunks_ai;"
                "DROP TRIGGER IF EXISTS chunks_ad;"
            )

        _execute_with_lock_retry(cur, _SCHEMA_SQL)

        cur.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (_SCHEMA_VERSION,),
        )
        self.conn.commit()

    def get_file_record(self, abs_path: str) -> FileRecord | None:
        row = self.conn.execute(
            "SELECT * FROM files WHERE abs_path = ?", (abs_path,)
        ).fetchone()
        if row is None:
            return None
        return FileRecord(
            abs_path=row["abs_path"],
            root_path=row["root_path"],
            content_hash=row["content_hash"],
            config_signature=row["config_signature"],
            size_bytes=row["size_bytes"],
            modified_at=row["modified_at"],
            indexed_at=row["indexed_at"],
            chunk_count=row["chunk_count"],
        )

    def upsert_file(self, record: FileRecord) -> None:
        self.conn.execute(
            """INSERT INTO files (abs_path, root_path, content_hash, config_signature,
                    size_bytes, modified_at, indexed_at, chunk_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(abs_path) DO UPDATE SET
                    root_path = excluded.root_path,
                    content_hash = excluded.content_hash,
                    config_signature = excluded.config_signature,
                    size_bytes = excluded.size_bytes,
                    modified_at = excluded.modified_at,
                    indexed_at = excluded.indexed_at,
                    chunk_count = excluded.chunk_count""",
            (
                record.abs_path,
                record.root_path,
                record.content_hash,
                record.config_signature,
                record.size_bytes,
                record.modified_at,
                record.indexed_at,
                record.chunk_count,
            ),
        )

    def delete_file(self, abs_path: str) -> None:
        self.conn.execute("DELETE FROM files WHERE abs_path = ?", (abs_path,))

    def insert_chunks(self, chunks: list[ChunkRecord]) -> None:
        self.conn.executemany(
            """INSERT INTO chunks (chunk_id, abs_path, chunk_index, content,
                    context, token_count, start_line, end_line, start_char,
                    end_char, section, kind, chunk_metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    c.chunk_id,
                    c.abs_path,
                    c.chunk_index,
                    c.content,
                    c.context,
                    c.token_count,
                    c.start_line,
                    c.end_line,
                    c.start_char,
                    c.end_char,
                    c.section,
                    c.kind,
                    c.chunk_metadata,
                )
                for c in chunks
            ],
        )

    def delete_chunks_for_file(self, abs_path: str) -> None:
        self.conn.execute("DELETE FROM chunks WHERE abs_path = ?", (abs_path,))

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        path_prefix: str | None = None,
        file_glob: str | None = None,
    ) -> list[SearchResult]:
        limit = max(1, limit)

        if not query.strip():
            return []

        fts_query = _build_fts_query(query)
        if not fts_query:
            return []

        sql = """
            SELECT
                c.abs_path,
                c.start_line,
                c.end_line,
                c.section,
                c.chunk_index,
                rank AS score,
                c.token_count,
                f.modified_at,
                c.content
            FROM chunks_fts
            JOIN chunks c ON chunks_fts.chunk_id = c.chunk_id
            JOIN files f ON c.abs_path = f.abs_path
            WHERE chunks_fts MATCH ?
        """
        params: list[Any] = [fts_query]

        if path_prefix:
            sql += " AND substr(c.abs_path, 1, ?) = ?"
            params.extend([len(path_prefix), path_prefix])

        if file_glob:
            sql += " AND c.abs_path GLOB ?"
            params.append(
                "*" + file_glob if not file_glob.startswith("*") else file_glob
            )

        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        return [
            SearchResult(
                path=row[0],
                line_start=row[1],
                line_end=row[2],
                section=row[3],
                chunk_index=row[4],
                score=row[5],
                tokens=row[6],
                modified=row[7],
                content=row[8],
            )
            for row in rows
        ]

    def all_indexed_paths(self) -> set[str]:
        rows = self.conn.execute("SELECT abs_path FROM files").fetchall()
        return {row[0] for row in rows}

    def status(self) -> dict[str, Any]:
        file_count = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        chunk_count = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        db_size = self._db_path.stat().st_size if self._db_path.exists() else 0
        return {
            "file_count": file_count,
            "chunk_count": chunk_count,
            "db_size_bytes": db_size,
            "db_path": str(self._db_path),
        }

    def commit(self) -> None:
        self.conn.commit()


def _execute_with_lock_retry(
    cur: sqlite3.Cursor,
    sql: str,
    *,
    attempts: int = 25,
    base_delay: float = 0.05,
) -> None:
    """Run a schema-bootstrap executescript() with retry on transient locks.

    `PRAGMA journal_mode = WAL` (inside our schema bootstrap) requires an
    exclusive file lock and is NOT subject to busy_timeout — so two processes
    racing to initialize an empty DB can see one of them fail immediately with
    "database is locked". Everything else in the schema honors busy_timeout,
    but we retry the whole script uniformly to keep the code simple. The first
    winner's bootstrap runs in microseconds; the loser retries a few times
    until WAL mode is already set and its pragma becomes a no-op.
    """
    for attempt in range(attempts):
        try:
            cur.executescript(sql)
            return
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc) or attempt == attempts - 1:
                raise
            # Jittered backoff: up to base_delay * (1 + random) per attempt.
            time.sleep(base_delay * (1 + random.random()))


_FTS5_COMPOUND_SEPARATORS = re.compile(r"[-_]")
_FTS5_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _build_fts_query(raw_query: str) -> str:
    """Build a safe FTS5 query from raw user input.

    All tokens are double-quoted to prevent FTS5 operator injection.
    Compound terms (hyphenated/underscored) get phrase + AND boost.
    """
    tokens = _FTS5_TOKEN_RE.findall(raw_query)
    tokens = [t.lower() for t in tokens if len(t) >= 2]
    if not tokens:
        return ""

    words = raw_query.split()
    parts: list[str] = []
    for word in words:
        sub_tokens = _FTS5_TOKEN_RE.findall(word)
        sub_tokens = [t.lower() for t in sub_tokens if len(t) >= 2]
        if not sub_tokens:
            continue
        if len(sub_tokens) > 1 and _FTS5_COMPOUND_SEPARATORS.search(word):
            phrase = " ".join(sub_tokens)
            and_clause = " AND ".join(f'"{t}"' for t in sub_tokens)
            parts.append(f'("{phrase}" OR ({and_clause}))')
        else:
            for t in sub_tokens:
                parts.append(f'"{t}"')

    return " OR ".join(parts)
