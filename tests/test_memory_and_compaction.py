"""Unit tests for Pillar 2: Context & Memory (SQLite State, Compaction, Async Worker)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from session_publisher_agent.memory.async_worker import AsyncMemoryConsolidationWorker
from session_publisher_agent.memory.compaction import ContextCompactor
from session_publisher_agent.memory.session_store import PersistentSessionStore


class TestMemoryAndCompaction(unittest.TestCase):
    def test_sliding_window_compaction_reduces_context_bloat(self) -> None:
        compactor = ContextCompactor(max_tokens=220, preserve_recent_turns=2)
        turns = [
            {"role": "user", "content": f"Historical discussion point #{i}: " + ("detail " * 40)}
            for i in range(10)
        ]
        before_tokens = compactor.total_tokens(turns)
        compacted = compactor.compact_history(turns)
        after_tokens = compactor.total_tokens(compacted)

        self.assertLess(len(compacted), len(turns))
        self.assertLess(after_tokens, before_tokens)
        self.assertIn("COMPACTED_SESSION_SUMMARY", compacted[0]["content"])

    def test_persistent_store_and_async_consolidation_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "memory.sqlite3")
            store = PersistentSessionStore(db_path=db_path)
            worker = AsyncMemoryConsolidationWorker(store=store)

            store.append_turn("sess_1", "user", "Contact lead@example.com for Lakehouse notes")
            turns = store.get_session_turns("sess_1")
            self.assertEqual(len(turns), 1)
            self.assertNotIn("lead@example.com", turns[0]["content"])
            self.assertIn("[REDACTED_EMAIL]", turns[0]["content"])

            worker.enqueue_consolidation(
                session_id="evt_lakehouse_01",
                title="Borderless Lakehouse Architecture",
                recap_markdown="Zero-copy Apache Iceberg federation with AlloyDB microVMs.",
            )
            worker.flush()

            hits = store.search_semantic_memories("Apache Iceberg lakehouse")
            self.assertGreaterEqual(len(hits), 1)
            self.assertEqual(hits[0]["memory_id"], "evt_lakehouse_01")


if __name__ == "__main__":
    unittest.main()
