"""Multi-Agent ADK Coordinator, Specialist Subagents, and Strategic Model Router.

Implements:
1. `StrategicModelRouter`: Dynamically selects `gemini-2.5-flash` for low-latency
   discovery, deduplication, and formatting checks, vs. `gemini-2.5-pro` for multi-tab
   executive note synthesis and complex reasoning.
2. Native **Google ADK (`google.adk.agents`)** multi-agent topology (`root_agent`):
   - `SessionDiscoveryAgent` (`LlmAgent` on Flash tier)
   - `NotesSynthesizerAgent` (`LlmAgent` on Pro tier)
   - `ComplianceAndPublishAgent` (`LlmAgent` on Flash tier with HITL stop)
   orchestrated sequentially by `SessionPublisherPipeline` (`SequentialAgent`) under
   `MeetingPublisherCoordinator` (`LlmAgent`) with `PublisherPolicyGuardrailPlugin`
   and `EventsCompactionConfig`.
3. `SessionPublisherOrchestrator`: Deterministic/hybrid execution engine powering the
   Web UI, CLI, and automated golden evaluation suite with full OpenTelemetry span
   propagation, persistent memory, guardrails, and two-phase HITL approval.
"""

from __future__ import annotations

from typing import Any

from .config import AgentSettings, load_settings
from .guardrails import PublisherPolicyGuardrailPlugin, get_guardrail_plugin
from .memory.async_worker import get_async_memory_worker
from .memory.compaction import ContextCompactor, build_adk_compaction_config
from .memory.session_store import PersistentSessionStore, get_session_store
from .observability.telemetry import get_structured_logger, get_tracer
from .prompts import (
    AGENT_CONSTITUTION,
    DISCOVERY_SUBAGENT_PROMPT,
    PUBLISH_SUBAGENT_PROMPT,
    SYNTHESIZER_SUBAGENT_PROMPT,
)
from .tools.workspace_tools import (
    discover_hosted_calendar_sessions,
    extract_gemini_meeting_notes,
    publish_approved_session_recap,
    stage_recap_for_hitl_approval,
    verify_chat_space_deduplication,
)

try:
    from google.adk.agents import LlmAgent, SequentialAgent  # type: ignore

    HAS_GOOGLE_ADK = True
except ImportError:  # pragma: no cover - lightweight ADK spec shim when google-adk is not installed

    class LlmAgent:  # type: ignore[no-redef]
        def __init__(
            self,
            *,
            name: str,
            model: str,
            description: str = "",
            instruction: str = "",
            tools: list[Any] | None = None,
            sub_agents: list[Any] | None = None,
            output_key: str | None = None,
        ) -> None:
            self.name = name
            self.model = model
            self.description = description
            self.instruction = instruction
            self.tools = tools or []
            self.sub_agents = sub_agents or []
            self.output_key = output_key

    class SequentialAgent:  # type: ignore[no-redef]
        def __init__(
            self,
            *,
            name: str,
            description: str = "",
            sub_agents: list[Any] | None = None,
        ) -> None:
            self.name = name
            self.description = description
            self.sub_agents = sub_agents or []

    HAS_GOOGLE_ADK = False


class StrategicModelRouter:
    """Routes subtasks between fast (`gemini-2.5-flash`) and reasoning (`gemini-2.5-pro`) models."""

    def __init__(self, settings: AgentSettings | None = None) -> None:
        self.settings = settings or load_settings()

    def select_model(self, task_type: str, estimated_input_tokens: int = 0) -> dict[str, str]:
        """Selects the optimal Gemini model tier based on task complexity and input size."""
        task = task_type.strip().lower()
        if task in {"synthesis", "executive_summary", "multi_tab_reasoning", "complex_planning"} or (
            estimated_input_tokens > 4000
        ):
            return {
                "model": self.settings.reasoning_model_name,
                "tier": "reasoning_pro",
                "rationale": (
                    f"Selected {self.settings.reasoning_model_name} for high-accuracy "
                    f"executive synthesis / large context ({estimated_input_tokens} est. tokens)."
                ),
            }
        return {
            "model": self.settings.fast_model_name,
            "tier": "fast_flash",
            "rationale": (
                f"Selected {self.settings.fast_model_name} for low-latency structured "
                f"tool orchestration ({task})."
            ),
        }


def build_adk_agent_hierarchy(settings: AgentSettings | None = None) -> LlmAgent:
    """Constructs the Google ADK Coordinator + Sequential Specialist Subagent graph."""
    cfg = settings or load_settings()
    router = StrategicModelRouter(cfg)

    discovery_agent = LlmAgent(
        name="SessionDiscoveryAgent",
        model=router.select_model("calendar_discovery")["model"],
        description=(
            "Discovers host-organized Google Calendar sessions inviting the target group "
            "and filters out previously published sessions."
        ),
        instruction=DISCOVERY_SUBAGENT_PROMPT,
        tools=[discover_hosted_calendar_sessions, verify_chat_space_deduplication],
        output_key="candidate_session",
    )

    synthesizer_agent = LlmAgent(
        name="NotesSynthesizerAgent",
        model=router.select_model("executive_summary")["model"],
        description=(
            "Extracts Gemini Meeting Notes and synthesizes an executive TL;DR, Action Items, "
            "and resource links."
        ),
        instruction=SYNTHESIZER_SUBAGENT_PROMPT,
        tools=[extract_gemini_meeting_notes],
        output_key="draft_recap_markdown",
    )

    compliance_publish_agent = LlmAgent(
        name="ComplianceAndPublishAgent",
        model=router.select_model("compliance_and_hitl")["model"],
        description=(
            "Validates formatting & self-evaluation guardrails, stages recaps at the HITL "
            "approval gate, and publishes approved recaps to Google Chat."
        ),
        instruction=PUBLISH_SUBAGENT_PROMPT,
        tools=[stage_recap_for_hitl_approval, publish_approved_session_recap],
        output_key="publish_result",
    )

    publishing_pipeline = SequentialAgent(
        name="SessionPublisherSequentialPipeline",
        description=(
            "Three-stage sequential pipeline: Discovery -> Executive Synthesis -> "
            "Guardrailed HITL Staging & Chat Publication."
        ),
        sub_agents=[discovery_agent, synthesizer_agent, compliance_publish_agent],
    )

    coordinator = LlmAgent(
        name="MeetingPublisherCoordinator",
        model=router.select_model("coordinator_routing")["model"],
        description=(
            "Root Enterprise Coordinator managing hosted session recap workflows, memory "
            "recall, and Human-in-the-Loop publication approvals."
        ),
        instruction=AGENT_CONSTITUTION,
        sub_agents=[publishing_pipeline],
    )
    return coordinator


# Exported for `adk web`, `adk run`, and Vertex AI Agent Engine deployment
root_agent = build_adk_agent_hierarchy()
adk_compaction_config = build_adk_compaction_config()


def _format_recap_markdown(
    session: dict[str, Any],
    notes_payload: dict[str, Any] | None,
) -> str:
    """Formats a deterministic, Chat-validated CommonMark recap from extracted notes."""
    title = session["summary"]
    date_str = session["date_display"]
    notes = session.get("notes")
    recording = session.get("recording")

    resource_parts: list[str] = []
    if notes and notes.get("url"):
        resource_parts.append(f"[Gemini Meeting Notes]({notes['url']})")
    if recording and recording.get("url"):
        resource_parts.append(f"[Watch Recording]({recording['url']})")
    else:
        resource_parts.append("_(no recording attached)_")

    lines: list[str] = [
        f"**📝 Session Recap: {title} ({date_str})**",
        "",
        f"- **Resources:** {' | '.join(resource_parts)}",
        "",
        "**TL;DR**",
        "",
    ]

    bullets = (notes_payload or {}).get("summary_bullets") or []
    if bullets:
        for b in bullets[:5]:
            lines.append(f"- {b}")
    else:
        lines.append("- Session recording and materials are available via the resource links above.")

    actions = (notes_payload or {}).get("action_items") or []
    if actions:
        lines.extend(["", "**Action Items**", ""])
        for act in actions:
            # Convert `[Owner] Task` to `**Owner:** Task` when bracketed
            if act.startswith("[") and "]" in act:
                close_idx = act.index("]")
                owner = act[1:close_idx].strip()
                rest = act[close_idx + 1 :].lstrip(" :-")
                lines.append(f"- **{owner}:** {rest}")
            else:
                lines.append(f"- {act}")

    lines.extend(
        [
            "",
            "---",
            "",
            "_Published by AI Assistant on behalf of the Session Host_",
        ]
    )
    return "\n".join(lines)


class SessionPublisherOrchestrator:
    """End-to-end Coordinator orchestrating Discovery -> Synthesis -> Guardrail/HITL -> Publish."""

    def __init__(
        self,
        settings: AgentSettings | None = None,
        store: PersistentSessionStore | None = None,
        guardrail: PublisherPolicyGuardrailPlugin | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.store = store or get_session_store()
        self.guardrail = guardrail or get_guardrail_plugin()
        self.router = StrategicModelRouter(self.settings)
        self.compactor = ContextCompactor(max_tokens=self.settings.max_context_tokens)
        self.tracer = get_tracer()
        self.slogger = get_structured_logger()

    def run_workflow(
        self,
        user_prompt: str = "Publish my latest hosted account-leads session",
        title_query: str = "",
        lookback_days: int = 14,
        conversation_id: str = "default_session",
    ) -> dict[str, Any]:
        """Executes the multi-agent workflow from user request through HITL staging."""
        with self.tracer.start_span(
            "MeetingPublisherCoordinator.run_workflow",
            attributes={"conversation_id": conversation_id, "title_query": title_query},
        ) as root_span:
            # 1. Input guardrail check
            input_check = self.guardrail.validate_user_input(user_prompt)
            if not input_check["allowed"]:
                root_span.status = "BLOCKED_GUARDRAIL"
                return {
                    "status": "blocked_by_guardrail",
                    "reason": input_check["reason"],
                    "trace_id": root_span.trace_id,
                }

            # Persist & compact conversational history
            self.store.append_turn(conversation_id, "user", user_prompt)
            turns = self.store.get_session_turns(conversation_id)
            compacted = self.compactor.compact_history(turns)
            if len(compacted) != len(turns):
                self.store.replace_session_turns(conversation_id, compacted)

            # 2. Subagent 1: SessionDiscoveryAgent (Fast Flash Model)
            discovery_route = self.router.select_model("calendar_discovery")
            with self.tracer.start_span(
                "SessionDiscoveryAgent.execute",
                attributes={"model": discovery_route["model"], "tier": discovery_route["tier"]},
            ):
                discovered = discover_hosted_calendar_sessions(
                    lookback_days=lookback_days,
                    title_query=title_query,
                    host_email=self.settings.host_organizer_email,
                    required_attendee_group=self.settings.required_attendee_group,
                )
                if discovered["status"] != "success":
                    return {**discovered, "trace_id": root_span.trace_id}

                selected_session: dict[str, Any] | None = None
                skipped_published: list[str] = []
                for sess in discovered["sessions"]:
                    if not sess["publishable"]:
                        continue
                    fids = [
                        x["file_id"]
                        for x in (sess.get("notes"), sess.get("recording"))
                        if x and x.get("file_id")
                    ]
                    dedup = verify_chat_space_deduplication(
                        event_id=sess["event_id"],
                        space_id=self.settings.target_chat_space_id,
                        artifact_file_ids=fids,
                    )
                    if dedup.get("already_published"):
                        skipped_published.append(sess["event_id"])
                        continue
                    selected_session = sess
                    break

            if selected_session is None:
                msg = "No unpublished, publishable hosted sessions found in lookback window."
                self.store.append_turn(conversation_id, "assistant", msg)
                return {
                    "status": "no_unpublished_sessions",
                    "message": msg,
                    "skipped_already_published": skipped_published,
                    "trace_id": root_span.trace_id,
                }

            # 3. Subagent 2: NotesSynthesizerAgent (Reasoning Pro Model)
            notes_payload: dict[str, Any] | None = None
            notes_info = selected_session.get("notes")
            synth_route = self.router.select_model("executive_summary", estimated_input_tokens=4500)
            with self.tracer.start_span(
                "NotesSynthesizerAgent.execute",
                attributes={"model": synth_route["model"], "tier": synth_route["tier"]},
            ):
                if notes_info and notes_info.get("file_id"):
                    notes_payload = extract_gemini_meeting_notes(notes_info["file_id"])
                    if notes_payload.get("status") != "success":
                        return {**notes_payload, "trace_id": root_span.trace_id}
                draft_markdown = _format_recap_markdown(selected_session, notes_payload)

            # 4. Subagent 3: ComplianceAndPublishAgent (Guardrailed + HITL Checkpoint)
            publish_route = self.router.select_model("compliance_and_hitl")
            with self.tracer.start_span(
                "ComplianceAndPublishAgent.stage_hitl",
                attributes={"model": publish_route["model"], "tier": publish_route["tier"]},
            ):
                staged = stage_recap_for_hitl_approval(
                    event_id=selected_session["event_id"],
                    session_title=selected_session["summary"],
                    session_date=selected_session["date_display"],
                    formatted_markdown=draft_markdown,
                    space_id=self.settings.target_chat_space_id,
                    notes_url=(notes_info or {}).get("url"),
                    recording_url=(selected_session.get("recording") or {}).get("url"),
                )

            self.store.append_turn(
                conversation_id,
                "assistant",
                f"Staged session '{selected_session['summary']}' as {staged.get('approval_request_id')}.",
            )
            return {
                **staged,
                "session": selected_session,
                "model_routing": {
                    "discovery": discovery_route,
                    "synthesis": synth_route,
                    "compliance": publish_route,
                },
                "skipped_already_published": skipped_published,
                "trace_id": root_span.trace_id,
            }

    def approve_and_publish(
        self,
        approval_request_id: str,
        reviewer_note: str = "Approved by session host",
    ) -> dict[str, Any]:
        """Records human sign-off on a staged HITL request and publishes to Google Chat."""
        with self.tracer.start_span(
            "MeetingPublisherCoordinator.approve_and_publish",
            attributes={"approval_request_id": approval_request_id},
        ) as span:
            updated = self.store.update_hitl_decision(
                approval_request_id=approval_request_id,
                approved=True,
                reviewer_note=reviewer_note,
            )
            if updated is None:
                return {
                    "status": "error",
                    "error_code": "UNKNOWN_APPROVAL_REQUEST",
                    "recovery_instructions": "Stage a session recap first via run_workflow.",
                    "trace_id": span.trace_id,
                }
            verdict = self.guardrail.validate_tool_invocation(
                "publish_approved_session_recap",
                {"approval_request_id": approval_request_id},
            )
            if not verdict["allowed"]:
                return {
                    "status": "error",
                    "error_code": "POLICY_GUARDRAIL_BLOCKED",
                    "recovery_instructions": verdict["reason"],
                    "trace_id": span.trace_id,
                }
            result = publish_approved_session_recap(approval_request_id=approval_request_id)
            get_async_memory_worker().flush(timeout=2.0)
            return {**result, "trace_id": span.trace_id}
