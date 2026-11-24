#!/usr/bin/env python3
"""Command-line interface for the Enterprise Meeting Notes & Recording Publisher Agent."""

from __future__ import annotations

import argparse
import json
import sys

from session_publisher_agent.agent import SessionPublisherOrchestrator
from session_publisher_agent.memory.session_store import get_session_store
from session_publisher_agent.observability.telemetry import get_tracer


def run_demo() -> int:
    """Runs an end-to-end demonstration showing Discovery -> Synthesis -> HITL -> Publish."""
    orchestrator = SessionPublisherOrchestrator()
    print("=" * 78)
    print(" Enterprise Meeting Notes & Recording Publisher Agent (Google ADK Demo)")
    print("=" * 78)

    print("\n[Phase 1] Running Coordinator -> Discovery -> Notes Synthesis -> HITL Gate...")
    staged = orchestrator.run_workflow(
        user_prompt="Publish my latest hosted session for account leads",
        conversation_id="cli_demo_session",
    )
    print(f"  Status              : {staged.get('status')}")
    print(f"  Trace ID            : {staged.get('trace_id')}")
    print(f"  Skipped Published   : {staged.get('skipped_already_published')}")
    print(f"  Approval Request ID : {staged.get('approval_request_id')}")
    print("\n--- Formatted Google Chat Preview (Awaiting Human Approval) ---")
    print(staged.get("formatted_preview", ""))
    print("---------------------------------------------------------------")

    req_id = staged.get("approval_request_id", "")
    if not req_id:
        return 0

    print("\n[Phase 2] Approving HITL Checkpoint & Publishing to Official Chat Space...")
    published = orchestrator.approve_and_publish(
        approval_request_id=req_id,
        reviewer_note="Verified TL;DR, action items, and default Meet ACL policy.",
    )
    print(json.dumps(published, indent=2))

    spans = [s.to_dict() for s in get_tracer().recorded_spans[-6:]]
    print(f"\n[Observability] Captured {len(spans)} OpenTelemetry trace spans:")
    for sp in spans:
        print(
            f"  - {sp['name']} (span={sp['span_id']}, parent={sp['parent_span_id']}, "
            f"{sp['duration_ms']} ms, status={sp['status']})"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Run full two-phase HITL demo")
    parser.add_argument("--stage", type=str, default="", help="Stage recap matching optional title query")
    parser.add_argument("--approve", type=str, default="", help="Approve and publish HITL request ID")
    parser.add_argument("--search-memory", type=str, default="", help="Query consolidated semantic memory")
    args = parser.parse_args()

    if args.demo:
        return run_demo()

    orchestrator = SessionPublisherOrchestrator()
    if args.approve:
        res = orchestrator.approve_and_publish(args.approve)
        print(json.dumps(res, indent=2))
        return 0 if res.get("status") == "success" else 1

    if args.search_memory:
        hits = get_session_store().search_semantic_memories(args.search_memory)
        print(json.dumps(hits, indent=2))
        return 0

    res = orchestrator.run_workflow(title_query=args.stage)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
