"""History Compaction & Context Bloat Management (Sliding Window + ADK Compaction + Context Cache).

Prevents long meeting transcripts and multi-session histories from overflowing LLM
context budgets via:
1. Token-budget sliding window with rolling extractive/LLM summarization (`ContextCompactor`).
2. Native Google ADK `EventsCompactionConfig` builder for `App` / `Runner` sessions.
3. Optional Google Cloud Gemini Context Caching (`google.genai.Client().caches.create`)
   when summarizing large full-meeting transcripts.
"""

from __future__ import annotations

from typing import Any

try:
    from google.adk.apps.app import EventsCompactionConfig  # type: ignore
except ImportError:  # pragma: no cover - clean shim when ADK is not installed locally
    EventsCompactionConfig = None

try:
    from google import genai  # type: ignore
    from google.genai import types as genai_types  # type: ignore
except ImportError:  # pragma: no cover
    genai = None
    genai_types = None


def estimate_tokens(text: str) -> int:
    """Approximates Gemini BPE token count (~4 chars/token heuristic)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


class ContextCompactor:
    """Manages conversational context bloat via token truncation & rolling summarization."""

    def __init__(
        self,
        max_tokens: int = 6000,
        preserve_recent_turns: int = 4,
    ) -> None:
        self.max_tokens = max(200, max_tokens)
        self.preserve_recent_turns = max(1, preserve_recent_turns)

    def total_tokens(self, turns: list[dict[str, Any]]) -> int:
        return sum(estimate_tokens(str(t.get("content", ""))) for t in turns)

    def compact_history(self, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compacts older turns into a concise rolling summary when budget is exceeded."""
        if not turns or self.total_tokens(turns) <= self.max_tokens:
            return list(turns)

        if len(turns) <= self.preserve_recent_turns:
            # Truncate individual oversized payloads to fit token ceiling
            per_turn_budget_chars = max(160, (self.max_tokens * 4) // max(1, len(turns)))
            compacted_short: list[dict[str, Any]] = []
            for turn in turns:
                content = str(turn.get("content", ""))
                if len(content) > per_turn_budget_chars:
                    content = content[: per_turn_budget_chars - 24] + " ...[COMPACTED]"
                compacted_short.append({**turn, "content": content})
            return compacted_short

        older = turns[: -self.preserve_recent_turns]
        recent = turns[-self.preserve_recent_turns :]

        key_points: list[str] = []
        for item in older:
            role = item.get("role", "user")
            snippet = " ".join(str(item.get("content", "")).split())
            if len(snippet) > 140:
                snippet = snippet[:137] + "..."
            if snippet:
                key_points.append(f"[{role}] {snippet}")

        summary_body = " | ".join(key_points[-8:])
        max_summary_chars = max(200, (self.max_tokens // 3) * 4)
        if len(summary_body) > max_summary_chars:
            summary_body = summary_body[: max_summary_chars - 18] + " ...[TRUNCATED]"

        summary_turn = {
            "role": "system",
            "content": (
                f"[COMPACTED_SESSION_SUMMARY ({len(older)} earlier turns consolidated)]: "
                f"{summary_body}"
            ),
        }
        return [summary_turn, *recent]

    def create_gemini_transcript_cache(
        self,
        api_key: str,
        model_name: str,
        transcript_text: str,
        ttl_seconds: int = 900,
    ) -> str | None:  # pragma: no cover - requires live Gemini API key & >32k tokens
        """Creates a Google Cloud / Gemini Context Cache handle for oversized transcripts."""
        if not api_key or genai is None or genai_types is None or estimate_tokens(transcript_text) < 32768:
            return None
        try:
            client = genai.Client(api_key=api_key)
            cache = client.caches.create(
                model=model_name,
                config=genai_types.CreateCachedContentConfig(
                    contents=[transcript_text],
                    ttl=f"{ttl_seconds}s",
                ),
            )
            return getattr(cache, "name", None)
        except Exception:
            return None


def build_adk_compaction_config(
    compaction_interval: int = 5, overlap_size: int = 2
) -> Any:
    """Returns native Google ADK `EventsCompactionConfig` when running under `google-adk`."""
    if EventsCompactionConfig is not None:
        return EventsCompactionConfig(
            compaction_interval=compaction_interval,
            overlap_size=overlap_size,
        )
    return {
        "compaction_interval": compaction_interval,
        "overlap_size": overlap_size,
    }
