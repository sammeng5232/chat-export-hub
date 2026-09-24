#!/usr/bin/env python3
"""SQLite FTS5 full-text index over exported session transcripts.

Every exported session is a single flat ``.txt`` file on disk (see
``chat_export_common.filesystem_safe_output_path``); this module indexes
those files so the hub UI can locate a session by keywords appearing in the
conversation body, not just its title/path/model metadata.

Uses FTS5's ``trigram`` tokenizer (not the default ``unicode61``) because
``unicode61`` has no word-boundary concept inside a run of CJK characters —
a Chinese sentence tokenizes as one giant token, so a query for a 2-character
substring like "报错" would never match. Trigram indexing makes substring
search work uniformly for English and CJK text. Its one limitation is that a
query shorter than 3 characters has no trigrams to match on, so short tokens
fall back to a plain ``LIKE`` scan over the same table instead of ``MATCH``.

Every public method opens and closes its own short-lived connection so the
index can be driven from a background QThread without sharing a
``sqlite3.Connection`` across threads.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DB_PATH = Path.home() / ".grok" / "tools" / "chat_export_search_index.sqlite3"

_SCHEMA_FILES = (
    "CREATE TABLE IF NOT EXISTS files ("
    " path TEXT PRIMARY KEY,"
    " mtime_ns INTEGER NOT NULL,"
    " size INTEGER NOT NULL"
    ")"
)
_SCHEMA_FTS = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5("
    " path UNINDEXED, session_id UNINDEXED, agent_id UNINDEXED, body,"
    " tokenize='{tokenizer}'"
    ")"
)


class SearchIndex:
    """Incremental full-text index of exported session transcripts."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(_SCHEMA_FILES)
        try:
            conn.execute(_SCHEMA_FTS.format(tokenizer="trigram"))
        except sqlite3.OperationalError:
            # Older SQLite builds without the trigram tokenizer: fall back to
            # the default tokenizer. Short-token LIKE search still works;
            # only MATCH-based CJK substring search degrades.
            conn.execute(_SCHEMA_FTS.format(tokenizer="unicode61"))
        return conn

    def sync(self, records: Iterable[tuple[str, str, str]]) -> int:
        """Reindex changed files and drop rows for files no longer present.

        ``records`` is an iterable of ``(path, session_id, agent_id)``.
        Returns the number of files (re)indexed this call.
        """
        wanted: dict[str, tuple[str, str]] = {}
        for path, session_id, agent_id in records:
            if path:
                wanted[path] = (session_id, agent_id)

        conn = self._connect()
        try:
            existing = {
                row[0]: (row[1], row[2])
                for row in conn.execute("SELECT path, mtime_ns, size FROM files")
            }
            reindexed = 0
            with conn:
                for path, (session_id, agent_id) in wanted.items():
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    if existing.get(path) == (st.st_mtime_ns, st.st_size):
                        continue
                    try:
                        text = Path(path).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    conn.execute("DELETE FROM content_fts WHERE path = ?", (path,))
                    conn.execute(
                        "INSERT INTO content_fts (path, session_id, agent_id, body)"
                        " VALUES (?, ?, ?, ?)",
                        (path, session_id, agent_id, text),
                    )
                    conn.execute(
                        "INSERT INTO files (path, mtime_ns, size) VALUES (?, ?, ?)"
                        " ON CONFLICT(path) DO UPDATE SET"
                        " mtime_ns = excluded.mtime_ns, size = excluded.size",
                        (path, st.st_mtime_ns, st.st_size),
                    )
                    reindexed += 1

                for path in existing:
                    if path in wanted:
                        continue
                    # The caller's record set can be transiently incomplete
                    # (e.g. one agent's state file was briefly unreadable this
                    # poll cycle) without any export file actually vanishing.
                    # Only treat a path as deleted once the file itself is
                    # gone, so a caller-side hiccup can never silently wipe
                    # good entries out of the index.
                    if os.path.exists(path):
                        continue
                    conn.execute("DELETE FROM content_fts WHERE path = ?", (path,))
                    conn.execute("DELETE FROM files WHERE path = ?", (path,))
            return reindexed
        finally:
            conn.close()

    def search(self, needle: str, limit: int = 5000) -> tuple[set[str], bool]:
        """Return (matching file paths, whether the result was truncated).

        ``needle`` is whitespace-split into tokens that must ALL be present
        (ANDed), case-insensitive substrings. Tokens of 3+ characters are
        matched via FTS5 MATCH (fast, indexed); shorter tokens (including
        common 2-character CJK words) fall back to a LIKE scan, since the
        trigram tokenizer has no grams to match on below 3 characters.
        """
        # Control characters can never meaningfully appear in a real search
        # term and (e.g. an embedded NUL) can break out of a quoted MATCH
        # phrase and raise sqlite3.OperationalError; strip them so a stray
        # control byte degrades gracefully instead of masquerading as "no
        # results found".
        cleaned = "".join(ch for ch in (needle or "") if ch >= " " or ch.isspace())
        tokens = [t for t in cleaned.split() if t]
        if not tokens:
            return set(), False

        match_terms: list[str] = []
        like_params: list[str] = []
        for tok in tokens:
            if len(tok) >= 3:
                match_terms.append('"' + tok.replace('"', '""') + '"')
            else:
                escaped = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                like_params.append(f"%{escaped}%")

        clauses: list[str] = []
        params: list[Any] = []
        if match_terms:
            clauses.append("content_fts MATCH ?")
            params.append(" AND ".join(match_terms))
        for pat in like_params:
            clauses.append("body LIKE ? ESCAPE '\\'")
            params.append(pat)
        if not clauses:
            return set(), False

        conn = self._connect()
        try:
            sql = (
                "SELECT DISTINCT path FROM content_fts WHERE "
                + " AND ".join(clauses)
                + " ORDER BY path LIMIT ?"
            )
            params.append(limit + 1)
            rows = conn.execute(sql, params).fetchall()
            paths = [row[0] for row in rows]
            truncated = len(paths) > limit
            return set(paths[:limit]), truncated
        except sqlite3.OperationalError:
            return set(), False
        finally:
            conn.close()
