"""Asynchronous Background Memory Consolidation Worker.

Offloads expensive post-publish memory summarization, PII scrubbing, vector/BM25
embedding generation, and SQLite history compaction onto a non-blocking background
worker thread (with `asyncio` awaitable support) so user turns and UI actions
return immediately without blocking on storage I/O.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any

from ..config import load_settings
from ..observability.telemetry import get_structured_logger
from .compaction import ContextCompactor
from .session_store import PersistentSessionStore, get_session_store


class AsyncMemoryConsolidationWorker:
    """Non-blocking background worker for session compaction and semantic indexing."""

    def __init__(
        self,
        store: PersistentSessionStore | None = None,
        compactor: ContextCompactor | None = None,
    ) -> None:
        settings = load_settings()
        self.store = store or get_session_store()
        self.compactor = compactor or ContextCompactor(
            max_tokens=settings.max_context_tokens
        )
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.completed_jobs: int = 0
        self._worker_thread = threading.Thread(
            target=self._run_loop,
            name="async-memory-consolidation-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def enqueue_consolidation(
        self,
        session_id: str,
        title: str,
        recap_markdown: str,
    ) -> None:
        """Enqueues a memory consolidation & history compaction task without blocking."""
        self._queue.put(
            {
                "type": "consolidate_publish",
                "session_id": session_id,
                "title": title,
                "recap_markdown": recap_markdown,
            }
        )

    async def enqueue_consolidation_async(
        self,
        session_id: str,
        title: str,
        recap_markdown: str,
    ) -> None:
        """Asyncio-friendly non-blocking wrapper around `enqueue_consolidation`."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self.enqueue_consolidation, session_id, title, recap_markdown
        )

    def flush(self, timeout: float = 5.0) -> bool:
        """Waits for queued background memory operations to finish (useful in tests/CLI exit)."""
        self._queue.join()
        return self._queue.empty()

    def _run_loop(self) -> None:
        slogger = get_structured_logger()
        while True:
            job = self._queue.get()
            if job is None:  # pragma: no cover - shutdown sentinel
                self._queue.task_done()
                break
            try:
                session_id = str(job["session_id"])
                title = str(job["title"])
                recap_md = str(job["recap_markdown"])

                # 1. Consolidate published recap into semantic long-term memory
                self.store.upsert_semantic_memory(
                    memory_id=session_id,
                    title=title,
                    summary_text=recap_md,
                )

                # 2. Compact conversational turns for this session if exceeding budget
                turns = self.store.get_session_turns(session_id)
                if turns:
                    compacted = self.compactor.compact_history(turns)
                    if len(compacted) != len(turns):
                        self.store.replace_session_turns(session_id, compacted)

                self.completed_jobs += 1
                slogger.emit(
                    "INFO",
                    f"Async memory consolidation completed for session_id={session_id}",
                    component="AsyncMemoryConsolidationWorker",
                    session_id=session_id,
                    completed_jobs=self.completed_jobs,
                )
            except Exception as exc:  # pragma: no cover
                slogger.emit(
                    "ERROR",
                    f"Async memory worker error: {type(exc).__name__}: {exc}",
                    component="AsyncMemoryConsolidationWorker",
                )
            finally:
                self._queue.task_done()


_DEFAULT_WORKER: AsyncMemoryConsolidationWorker | None = None


def get_async_memory_worker() -> AsyncMemoryConsolidationWorker:
    global _DEFAULT_WORKER
    if _DEFAULT_WORKER is None:
        _DEFAULT_WORKER = AsyncMemoryConsolidationWorker()
    return _DEFAULT_WORKER
