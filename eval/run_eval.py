#!/usr/bin/env python3
"""Automated Golden Dataset Regression Evaluation Harness.

Statically and behaviorally evaluates the Enterprise Session Publisher Agent against
`eval/golden_dataset.json` across Tool Design, Context & Memory, Orchestration & HITL,
Guardrails, and Observability/PII Redaction—exiting non-zero on any regression.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from session_publisher_agent.agent import SessionPublisherOrchestrator
from session_publisher_agent.memory.session_store import PersistentSessionStore
from session_publisher_agent.observability.pii_redactor import PIIRedactor
from session_publisher_agent.observability.telemetry import get_structured_logger, get_tracer
from session_publisher_agent.tools.workspace_tools import (
    discover_hosted_calendar_sessions,
    publish_approved_session_recap,
    verify_chat_space_deduplication,
)


def run_golden_evaluation() -> dict:
    dataset_path = Path(__file__).resolve().parent / "golden_dataset.json"
    cases = json.loads(dataset_path.read_text(encoding="utf-8"))

    results: list[dict] = []
    prev_db = os.environ.get("SQLITE_DB_PATH")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = str(Path(tmpdir) / "eval_state.sqlite3")
        os.environ["SQLITE_DB_PATH"] = db_file
        try:
            store = PersistentSessionStore(db_path=db_file)
            orchestrator = SessionPublisherOrchestrator(store=store)

            for case in cases:
                cid = case["case_id"]
                passed = False
                detail = ""

                if cid == "eval_01_host_and_group_filtering":
                    resp = discover_hosted_calendar_sessions(
                        lookback_days=case["input"]["lookback_days"]
                    )
                    returned_ids = {s["event_id"] for s in resp["sessions"]}
                    inc_ok = set(case["expected"]["must_include_event_ids"]).issubset(returned_ids)
                    exc_ok = set(case["expected"]["must_exclude_event_ids"]).isdisjoint(returned_ids)
                    passed = resp["status"] == "success" and inc_ok and exc_ok
                    detail = f"returned_ids={sorted(returned_ids)}"

                elif cid == "eval_02_deduplication_skips_prior_chat_posts":
                    resp = verify_chat_space_deduplication(
                        event_id=case["input"]["event_id"],
                        artifact_file_ids=case["input"]["artifact_file_ids"],
                    )
                    passed = resp["already_published"] is case["expected"]["already_published"]
                    detail = f"already_published={resp['already_published']} source={resp.get('source')}"

                elif cid == "eval_03_unrecorded_notes_only_session_still_publishes":
                    resp = orchestrator.run_workflow(
                        user_prompt=case["input"]["user_prompt"],
                        title_query=case["input"]["title_query"],
                    )
                    preview = resp.get("formatted_preview", "")
                    subs_ok = all(s in preview for s in case["expected"]["must_contain_substrings"])
                    passed = resp.get("status") == case["expected"]["status"] and subs_ok
                    detail = f"status={resp.get('status')} subs_ok={subs_ok}"

                elif cid == "eval_04_hitl_gate_blocks_unapproved_publish":
                    staged = orchestrator.run_workflow(
                        user_prompt=case["input"]["user_prompt"],
                        title_query=case["input"]["title_query"],
                    )
                    req_id = staged["approval_request_id"]
                    premature = publish_approved_session_recap(req_id)
                    approved = orchestrator.approve_and_publish(req_id)
                    passed = (
                        premature.get("status") == case["expected"]["unapproved_publish_status"]
                        and approved.get("status") == case["expected"]["approved_publish_status"]
                        and approved.get("permissions_modified") is False
                    )
                    detail = (
                        f"premature={premature.get('status')} approved={approved.get('status')} "
                        f"permissions_modified={approved.get('permissions_modified')}"
                    )

                elif cid == "eval_05_prompt_injection_and_acl_widening_blocked":
                    resp = orchestrator.run_workflow(user_prompt=case["input"]["user_prompt"])
                    passed = resp.get("status") == case["expected"]["status"]
                    detail = f"status={resp.get('status')}"

                elif cid == "eval_06_guided_error_recovery_on_bad_schema_args":
                    resp = discover_hosted_calendar_sessions(
                        lookback_days=case["input"]["lookback_days"]
                    )
                    has_guidance = bool(resp.get("recovery_instructions"))
                    passed = resp.get("status") == "error" and has_guidance
                    detail = f"status={resp.get('status')} recovery_instructions_present={has_guidance}"

                elif cid == "eval_07_pii_redaction_and_intent_outcome_tracing":
                    redactor = PIIRedactor()
                    scrubbed = redactor.scrub_text(case["input"]["raw_log"])
                    no_leaks = all(tok not in scrubbed for tok in case["expected"]["must_not_contain"])
                    phases = {ev.get("event_phase") for ev in get_structured_logger().events}
                    has_spans = len(get_tracer().recorded_spans) > 0
                    passed = no_leaks and {"INTENT", "OUTCOME"}.issubset(phases) and has_spans
                    detail = f"no_leaks={no_leaks} phases={sorted(p for p in phases if p)} spans={len(get_tracer().recorded_spans)}"

                results.append(
                    {
                        "case_id": cid,
                        "category": case["category"],
                        "passed": passed,
                        "detail": detail,
                    }
                )
        finally:
            if prev_db is None:
                os.environ.pop("SQLITE_DB_PATH", None)
            else:
                os.environ["SQLITE_DB_PATH"] = prev_db

    passed_count = sum(1 for r in results if r["passed"])
    summary = {
        "total_cases": len(results),
        "passed_cases": passed_count,
        "pass_rate": round(passed_count / max(1, len(results)), 4),
        "all_passed": passed_count == len(results),
        "results": results,
    }
    return summary


def main() -> int:
    report = run_golden_evaluation()
    print(json.dumps(report, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
