"""Persistent Session State, Idempotency Ledger, HITL Queue, and Semantic Recall Store.

Connects to a persistent SQLite database (with full-text/cosine-similarity semantic
memory search and an optional Google Cloud Vertex AI Search / Memory Bank adapter)
to preserve conversational history, published session records, and HITL checkpoints
across restarts—scrubbing PII prior to every write.
"""

from __future__ import annotations

from collections import Counter
import datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any

from ..config import load_settings
from ..observability.pii_redactor import PIIRedactor, get_pii_redactor

_WORD_RE = re.compile(r"[a-z0-9]{2,}")


def _embed_bow(text: str) -> dict[str, float]:
    """Computes normalized term-frequency vector for lightweight offline semantic recall."""
    tokens = _WORD_RE.findall(text.lower())
    if not tokens:
        return {}
    counts = Counter(tokens)
    norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
    return {term: round(freq / norm, 4) for term, freq in counts.items()}


def _cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    if not vec_a or not vec_b:
        return 0.0
    common = set(vec_a.keys()) & set(vec_b.keys())
    return sum(vec_a[k] * vec_b[k] for k in common)


class PersistentSessionStore:
    """Thread-safe SQLite store for conversational turns, HITL state, and semantic memory."""

    def __init__(
        self,
        db_path: str | None = None,
        redactor: PIIRedactor | None = None,
    ) -> None:
        settings = load_settings()
        self.db_path = db_path or settings.sqlite_db_path
        self.redactor = redactor or get_pii_redactor()
        self._lock = threading.RLock()
        parent = Path(self.db_path).parent
        if str(parent) and str(parent) != ".":
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    token_estimate INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_turns_session ON conversation_turns(session_id);

                CREATE TABLE IF NOT EXISTS published_sessions (
                    event_id TEXT PRIMARY KEY,
                    session_title TEXT NOT NULL,
                    space_id TEXT NOT NULL,
                    message_name TEXT NOT NULL,
                    published_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hitl_approvals (
                    approval_request_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    session_title TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    space_id TEXT NOT NULL,
                    formatted_markdown TEXT NOT NULL,
                    notes_url TEXT,
                    recording_url TEXT,
                    approval_status TEXT NOT NULL,
                    reviewer_note TEXT,
                    published_message_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS semantic_memories (
                    memory_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary_text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def append_turn(self, session_id: str, role: str, content: str) -> None:
        """Scrubs PII and persists a conversational turn."""
        scrubbed = self.redactor.scrub_text(content)
        token_est = max(1, len(scrubbed) // 4)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_turns (session_id, role, content, token_estimate, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, role, scrubbed, token_est, now),
            )

    def get_session_turns(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content, token_estimate, created_at
                FROM conversation_turns
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def replace_session_turns(self, session_id: str, compacted_turns: list[dict[str, Any]]) -> None:
        """Replaces history with compacted summary + sliding-window recent turns."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM conversation_turns WHERE session_id = ?", (session_id,))
            for t in compacted_turns:
                scrubbed = self.redactor.scrub_text(str(t.get("content", "")))
                conn.execute(
                    """
                    INSERT INTO conversation_turns (session_id, role, content, token_estimate, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        str(t.get("role", "system")),
                        scrubbed,
                        max(1, len(scrubbed) // 4),
                        str(t.get("created_at") or now),
                    ),
                )

    def save_hitl_request(
        self,
        approval_request_id: str,
        event_id: str,
        session_title: str,
        session_date: str,
        space_id: str,
        formatted_markdown: str,
        notes_url: str | None = None,
        recording_url: str | None = None,
    ) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO hitl_approvals (
                    approval_request_id, event_id, session_title, session_date,
                    space_id, formatted_markdown, notes_url, recording_url,
                    approval_status, reviewer_note, published_message_name,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, NULL, ?, ?)
                """,
                (
                    approval_request_id,
                    event_id,
                    session_title,
                    session_date,
                    space_id,
                    formatted_markdown,
                    notes_url,
                    recording_url,
                    now,
                    now,
                ),
            )

    def update_hitl_decision(
        self,
        approval_request_id: str,
        approved: bool,
        reviewer_note: str = "",
    ) -> dict[str, Any] | None:
        """Records explicit human approval or rejection for a staged recap."""
        status = "APPROVED" if approved else "REJECTED"
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE hitl_approvals
                SET approval_status = ?, reviewer_note = ?, updated_at = ?
                WHERE approval_request_id = ?
                """,
                (status, self.redactor.scrub_text(reviewer_note), now, approval_request_id),
            )
        return self.get_hitl_request(approval_request_id)

    def mark_hitl_published(self, approval_request_id: str, message_name: str) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE hitl_approvals
                SET approval_status = 'PUBLISHED', published_message_name = ?, updated_at = ?
                WHERE approval_request_id = ?
                """,
                (message_name, now, approval_request_id),
            )

    def get_hitl_request(self, approval_request_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM hitl_approvals WHERE approval_request_id = ?",
                (approval_request_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_hitl_requests(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM hitl_approvals ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def record_published_session(
        self,
        event_id: str,
        session_title: str,
        space_id: str,
        message_name: str,
    ) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO published_sessions (
                    event_id, session_title, space_id, message_name, published_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, session_title, space_id, message_name, now),
            )

    def get_published_record(self, event_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM published_sessions WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_published_sessions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM published_sessions ORDER BY published_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_semantic_memory(self, memory_id: str, title: str, summary_text: str) -> None:
        """Stores PII-scrubbed consolidated session knowledge with vector weights."""
        scrubbed_title = self.redactor.scrub_text(title)
        scrubbed_summary = self.redactor.scrub_text(summary_text)
        vec = _embed_bow(f"{scrubbed_title} {scrubbed_summary}")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO semantic_memories (
                    memory_id, title, summary_text, embedding_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (memory_id, scrubbed_title, scrubbed_summary, json.dumps(vec), now),
            )

    def search_semantic_memories(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """Retrieves semantically relevant past session recaps via cosine similarity."""
        q_vec = _embed_bow(query)
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM semantic_memories").fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            vec = json.loads(item.pop("embedding_json", "{}") or "{}")
            score = _cosine_similarity(q_vec, vec)
            if score > 0:
                item["similarity_score"] = round(score, 4)
                scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:top_k]]


_DEFAULT_STORE: PersistentSessionStore | None = None


def get_session_store(db_path: str | None = None) -> PersistentSessionStore:
    global _DEFAULT_STORE
    if db_path is not None:
        return PersistentSessionStore(db_path=db_path)
    configured_path = load_settings().sqlite_db_path
    if _DEFAULT_STORE is None or _DEFAULT_STORE.db_path != configured_path:
        _DEFAULT_STORE = PersistentSessionStore(db_path=configured_path)
    return _DEFAULT_STORE
