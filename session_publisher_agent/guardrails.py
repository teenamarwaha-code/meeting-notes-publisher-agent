"""Security Guardrails, Policy Plugin, Self-Evaluation, and HITL Enforcement Hooks.

Implements a Google ADK `BasePlugin` (`PublisherPolicyGuardrailPlugin`) providing:
1. Prompt-Injection & Jailbreak Blocking (`before_model_callback` / `validate_user_input`).
2. Default Drive Permission Preservation Policy (`before_tool_callback` blocking any
   unauthorized Drive ACL sharing or permission-widening tool calls).
3. Automated Self-Evaluation Rubric (`evaluate_draft_recap`) checking structural
   completeness, unrecorded-session disclosure, link integrity, and AI attribution
   before any draft can enter the HITL queue.
4. Human-in-the-Loop Stop Enforcement (`before_tool_callback` on `publish_approved_session_recap`).
"""

from __future__ import annotations

import re
from typing import Any

from .config import load_settings
from .observability.telemetry import get_structured_logger

try:
    from google.adk.plugins.base_plugin import BasePlugin as _ADKBasePlugin  # type: ignore
except ImportError:  # pragma: no cover - lightweight base class when running without ADK installed

    class _ADKBasePlugin:  # type: ignore[no-redef]
        def __init__(self, name: str = "policy_guardrail_plugin") -> None:
            self.name = name


_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|system)\s+instructions", re.IGNORECASE),
    re.compile(r"bypass\s+(hitl|human\s+approval|guardrail|security)", re.IGNORECASE),
    re.compile(r"(share|make)\s+.*(public|everyone|anyone\s+with\s+the\s+link)", re.IGNORECASE),
    re.compile(r"reveal\s+(system\s+prompt|api[_\s]?key|secret)", re.IGNORECASE),
]

_FORBIDDEN_ACL_TOOLS = {
    "share_drive_file",
    "update_drive_permissions",
    "make_file_public",
    "gdrive_mutate_share",
}


class PublisherPolicyGuardrailPlugin(_ADKBasePlugin):
    """ADK Policy & Self-Evaluation Plugin enforcing security, ACL, and HITL invariants."""

    def __init__(self) -> None:
        super().__init__(name="publisher_policy_guardrail_plugin")
        self.slogger = get_structured_logger()

    def validate_user_input(self, user_prompt: str) -> dict[str, Any]:
        """Screens incoming user prompts against prompt-injection and policy-bypass attacks."""
        text = (user_prompt or "").strip()
        for pat in _INJECTION_PATTERNS:
            if pat.search(text):
                self.slogger.emit(
                    "WARNING",
                    "Blocked unsafe prompt matching guardrail policy pattern",
                    component="PublisherPolicyGuardrailPlugin",
                    pattern=pat.pattern,
                )
                return {
                    "allowed": False,
                    "reason": (
                        "Request blocked by security & ACL guardrail policy: prompt attempted "
                        "to override system instructions, bypass HITL approval, or widen Drive file permissions."
                    ),
                }
        return {"allowed": True, "reason": "OK"}

    def validate_tool_invocation(
        self, tool_name: str, tool_args: dict[str, Any]
    ) -> dict[str, Any]:
        """Enforces pre-tool policy checks (Drive ACL immutability & HITL approval state)."""
        settings = load_settings()
        if settings.preserve_default_drive_permissions and tool_name in _FORBIDDEN_ACL_TOOLS:
            self.slogger.emit(
                "WARNING",
                f"Blocked forbidden Drive ACL mutation tool: {tool_name}",
                component="PublisherPolicyGuardrailPlugin",
                tool_name=tool_name,
            )
            return {
                "allowed": False,
                "reason": (
                    "Policy violation: PRESERVE_DEFAULT_DRIVE_PERMISSIONS=true prohibits modifying "
                    "Google Drive sharing permissions."
                ),
            }

        if tool_name == "publish_approved_session_recap" and settings.require_hitl_approval:
            from .memory.session_store import get_session_store

            req_id = str(tool_args.get("approval_request_id", "")).strip()
            staged = get_session_store().get_hitl_request(req_id)
            if staged is None or staged.get("approval_status") != "APPROVED":
                return {
                    "allowed": False,
                    "reason": (
                        f"HITL guardrail blocked publish_approved_session_recap: request '{req_id}' "
                        "has not been explicitly APPROVED by a human reviewer."
                    ),
                }

        return {"allowed": True, "reason": "OK"}

    def evaluate_draft_recap(
        self,
        formatted_markdown: str,
        has_notes: bool,
        has_recording: bool,
    ) -> dict[str, Any]:
        """Self-evaluation quality gate scoring drafted Chat recaps prior to HITL staging."""
        violations: list[str] = []
        text = formatted_markdown or ""

        if "Session Recap:" not in text:
            violations.append("Missing '**📝 Session Recap: <Title> (<Date>)**' header.")
        if "**TL;DR**" not in text:
            violations.append("Missing '**TL;DR**' executive summary section.")
        if has_notes and "https://docs.google.com/document/" not in text:
            violations.append("Missing Gemini Meeting Notes link despite `has_notes=True`.")
        if has_recording and "https://drive.google.com/" not in text:
            violations.append("Missing Meet Recording link despite `has_recording=True`.")
        if not has_recording and "no recording attached" not in text.lower():
            violations.append(
                "Unrecorded session must explicitly state '_(no recording attached)_' in Resources."
            )
        if "on behalf of" not in text.lower():
            violations.append("Missing mandatory AI attribution footer ('_Published by ... on behalf of ..._').")

        passed = len(violations) == 0
        recovery = (
            ""
            if passed
            else "Fix the following self-evaluation findings and re-stage: " + " ".join(violations)
        )
        return {
            "passed": passed,
            "score": round(max(0.0, 1.0 - 0.2 * len(violations)), 2),
            "violations": violations,
            "recovery_instructions": recovery,
        }

    # --- Native Google ADK Plugin Callback Hooks ---
    async def before_model_callback(
        self, *, callback_context: Any, llm_request: Any
    ) -> Any:  # pragma: no cover - invoked by ADK Runner during live LLM turns
        prompt_text = str(getattr(llm_request, "contents", ""))
        verdict = self.validate_user_input(prompt_text)
        if not verdict["allowed"]:
            raise PermissionError(verdict["reason"])
        return None

    async def before_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any
    ) -> dict[str, Any] | None:  # pragma: no cover - invoked by ADK Runner
        tname = getattr(tool, "name", str(tool))
        verdict = self.validate_tool_invocation(tname, tool_args)
        if not verdict["allowed"]:
            return {
                "status": "error",
                "error_code": "POLICY_GUARDRAIL_BLOCKED",
                "recovery_instructions": verdict["reason"],
            }
        return None


_DEFAULT_GUARDRAIL: PublisherPolicyGuardrailPlugin | None = None


def get_guardrail_plugin() -> PublisherPolicyGuardrailPlugin:
    global _DEFAULT_GUARDRAIL
    if _DEFAULT_GUARDRAIL is None:
        _DEFAULT_GUARDRAIL = PublisherPolicyGuardrailPlugin()
    return _DEFAULT_GUARDRAIL
