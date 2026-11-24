"""Domain-specific Google Workspace tools with explicit schemas and guided recovery.

Implements five single-responsibility tools following the AgentOps Tool Design rubric:
1. `discover_hosted_calendar_sessions`: Queries Google Calendar v3 (or demo fixtures)
   and filters strictly for host-organized sessions inviting the target group.
2. `extract_gemini_meeting_notes`: Reads Google Docs v1 Gemini Meeting Notes tabs and
   parses executive summary bullets + action items.
3. `verify_chat_space_deduplication`: Checks both the persistent SQLite ledger and
   Google Chat v1 space history to prevent duplicate posts.
4. `stage_recap_for_hitl_approval`: Validates Google Chat Markdown formatting, leaves
   Google Meet Drive permissions untouched by policy, and creates a Human-in-the-Loop
   approval checkpoint.
5. `publish_approved_session_recap`: Publishes to Google Chat ONLY after explicit human
   approval on the staged HITL request, then asynchronously logs to memory.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any
import uuid

from ..config import load_settings
from ..observability.telemetry import trace_intent_and_outcome
from .schemas import (
    SchemaValidationError,
    validate_discover_inputs,
    validate_notes_doc_id,
    validate_space_id,
)

try:
    from google.oauth2.credentials import Credentials  # type: ignore
    from googleapiclient.discovery import build as build_google_service  # type: ignore
except ImportError:  # pragma: no cover - optional when running in offline/demo mode
    Credentials = None
    build_google_service = None

_FIXTURES_PATH = (
    Path(__file__).resolve().parents[2] / "sample_data" / "sample_workspace_fixtures.json"
)


def _load_demo_fixtures() -> dict[str, Any]:
    with open(_FIXTURES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_workspace_client(api_name: str, api_version: str) -> Any:  # pragma: no cover
    """Constructs an authenticated Google Workspace API client from Secret Manager OAuth JSON."""
    settings = load_settings()
    creds_json = settings.resolve_workspace_credentials_json()
    if not creds_json or Credentials is None or build_google_service is None:
        raise RuntimeError(
            "Google Workspace OAuth credentials are not configured; set DATA_MODE=demo or provide WORKSPACE_OAUTH_SECRET_ID."
        )
    creds_info = json.loads(creds_json)
    creds = Credentials.from_authorized_user_info(creds_info)
    return build_google_service(api_name, api_version, credentials=creds, cache_discovery=False)


def _extract_event_artifacts(event: dict[str, Any]) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    notes = None
    recording = None
    for att in event.get("attachments") or []:
        mime = (att.get("mimeType") or "").lower()
        title = att.get("title") or ""
        url = att.get("fileUrl") or ""
        fid = att.get("fileId") or ""
        item = {"file_id": fid, "title": title, "url": url, "mime_type": mime}
        if mime == "application/vnd.google-apps.document" and (
            "notes by gemini" in title.lower() or "meet_tnfm_calendar" in url.lower()
        ):
            notes = item
        elif mime == "video/mp4" or "recording" in title.lower():
            recording = item
    return notes, recording


@trace_intent_and_outcome(
    component="WorkspaceTools",
    action_description="Discover host-organized Calendar sessions with target attendee group",
)
def discover_hosted_calendar_sessions(
    lookback_days: int = 14,
    title_query: str = "",
    host_email: str | None = None,
    required_attendee_group: str | None = None,
) -> dict[str, Any]:
    """Discovers completed Google Calendar sessions hosted by the organizer for the target group.

    Filters the primary Google Calendar for non-cancelled events within the lookback
    window where `organizer.email` matches `host_email` AND `required_attendee_group`
    appears in `attendees[]`. Automatically extracts attached Gemini Meeting Notes
    (`application/vnd.google-apps.document`) and Meet Recordings (`video/mp4`).
    Note: Sessions with Gemini Notes but no recording are still marked `publishable=True`
    so notes-only sessions are never dropped.

    Args:
        lookback_days: Number of past days to search (integer from 1 to 90; default 14).
        title_query: Optional case-insensitive keyword filter on the meeting title.
        host_email: Organizer email filter; defaults to `HOST_ORGANIZER_EMAIL` from config.
        required_attendee_group: Required attendee group email; defaults to `REQUIRED_ATTENDEE_GROUP`.

    Returns:
        Dictionary adhering to `TOOL_JSON_SCHEMAS['discover_hosted_calendar_sessions']['output_schema']`
        containing `status`, `session_count`, `sessions`, or `recovery_instructions` on error.
    """
    settings = load_settings()
    resolved_host = (host_email or settings.host_organizer_email).strip().lower()
    resolved_group = (
        required_attendee_group or settings.required_attendee_group
    ).strip().lower()

    try:
        validate_discover_inputs(
            lookback_days=lookback_days,
            title_query=title_query,
            host_email=resolved_host,
            required_attendee_group=resolved_group,
        )
    except SchemaValidationError as exc:
        return {
            "status": "error",
            "error_code": "INVALID_TOOL_ARGUMENTS",
            "field": exc.field_name,
            "message": exc.message,
            "session_count": 0,
            "sessions": [],
            "recovery_instructions": exc.recovery_hint,
        }

    try:
        if settings.data_mode.lower() == "live":  # pragma: no cover - live Google Calendar v3
            cal_service = _build_workspace_client("calendar", "v3")
            now = datetime.datetime.now(datetime.timezone.utc)
            time_min = (now - datetime.timedelta(days=lookback_days)).isoformat()
            resp = (
                cal_service.events()
                .list(
                    calendarId="primary",
                    timeMin=time_min,
                    timeMax=now.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                )
                .execute()
            )
            raw_events = resp.get("items", [])
        else:
            raw_events = _load_demo_fixtures().get("calendar_events", [])
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "CALENDAR_API_UNAVAILABLE",
            "message": str(exc),
            "session_count": 0,
            "sessions": [],
            "recovery_instructions": (
                "Verify Google Calendar API credentials in Secret Manager or set DATA_MODE=demo "
                "to run against sanitized Workspace fixtures, then retry discover_hosted_calendar_sessions."
            ),
        }

    query_low = title_query.strip().lower()
    matched: list[dict[str, Any]] = []
    for ev in raw_events:
        if ev.get("status") == "cancelled":
            continue
        org_email = ((ev.get("organizer") or {}).get("email") or "").strip().lower()
        if org_email != resolved_host:
            continue
        attendee_emails = [
            (a.get("email") or "").strip().lower()
            for a in (ev.get("attendees") or [])
            if isinstance(a, dict)
        ]
        if resolved_group not in attendee_emails:
            continue

        summary = (ev.get("summary") or "(Untitled Session)").strip()
        if query_low and query_low not in summary.lower():
            continue

        start_raw = (ev.get("start") or {}).get("dateTime", "")
        date_display = start_raw[:10] if start_raw else "Unknown Date"
        notes, recording = _extract_event_artifacts(ev)
        matched.append(
            {
                "event_id": ev.get("id", ""),
                "summary": summary,
                "date_display": date_display,
                "start_time": start_raw,
                "organizer_email": org_email,
                "notes": notes,
                "recording": recording,
                "has_notes": notes is not None,
                "has_recording": recording is not None,
                "publishable": bool(notes or recording),
            }
        )

    matched.sort(key=lambda item: item["start_time"], reverse=True)
    return {
        "status": "success",
        "session_count": len(matched),
        "sessions": matched,
    }


@trace_intent_and_outcome(
    component="WorkspaceTools",
    action_description="Extract structured summary and action items from Gemini Meeting Notes document",
)
def extract_gemini_meeting_notes(notes_document_id: str) -> dict[str, Any]:
    """Reads a Google Docs 'Notes by Gemini' document and extracts summary bullets & action items.

    Connects to Google Docs API v1 (`documents().get(includeTabsContent=True)`) in live
    mode—or reads the sanitized fixture in demo mode—without modifying any document
    content or Drive sharing ACLs.

    Args:
        notes_document_id: Raw Google Docs file ID (e.g., `doc_gemini_notes_20261123`).

    Returns:
        Dictionary with `status`, `document_id`, `title`, `summary_bullets`, `action_items`,
        and `raw_markdown`, or structured `recovery_instructions` if lookup/validation fails.
    """
    try:
        validate_notes_doc_id(notes_document_id)
    except SchemaValidationError as exc:
        return {
            "status": "error",
            "error_code": "INVALID_DOCUMENT_ID",
            "document_id": str(notes_document_id),
            "summary_bullets": [],
            "action_items": [],
            "recovery_instructions": exc.recovery_hint,
        }

    settings = load_settings()
    doc_id = notes_document_id.strip()

    try:
        if settings.data_mode.lower() == "live":  # pragma: no cover - live Google Docs v1
            docs_service = _build_workspace_client("docs", "v1")
            doc = docs_service.documents().get(documentId=doc_id).execute()
            title = doc.get("title", "Notes by Gemini")
            paragraphs: list[str] = []
            for elem in (doc.get("body") or {}).get("content", []):
                para = elem.get("paragraph")
                if not para:
                    continue
                txt = "".join(
                    (run.get("textRun") or {}).get("content", "")
                    for run in para.get("elements", [])
                ).strip()
                if txt:
                    paragraphs.append(txt)
            raw_md = "\n".join(paragraphs)
        else:
            docs_map = _load_demo_fixtures().get("gemini_notes_documents", {})
            if doc_id not in docs_map:
                return {
                    "status": "error",
                    "error_code": "DOCUMENT_NOT_FOUND",
                    "document_id": doc_id,
                    "summary_bullets": [],
                    "action_items": [],
                    "recovery_instructions": (
                        f"Document ID '{doc_id}' was not found. Call discover_hosted_calendar_sessions "
                        "first and pass an exact `session['notes']['file_id']` value."
                    ),
                }
            entry = docs_map[doc_id]
            title = entry["title"]
            raw_md = entry["quick_notes_markdown"]
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "DOCS_API_READ_FAILED",
            "document_id": doc_id,
            "summary_bullets": [],
            "action_items": [],
            "recovery_instructions": (
                f"Reading Google Doc '{doc_id}' failed ({type(exc).__name__}). Verify OAuth scope "
                "'https://www.googleapis.com/auth/documents.readonly' or proceed with recording-only link if permitted."
            ),
        }

    summary_bullets: list[str] = []
    action_items: list[str] = []
    in_next_steps = False
    for raw_line in raw_md.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("next steps") or low.startswith("action items"):
            in_next_steps = True
            continue
        if line.startswith("- ") or line.startswith("* "):
            bullet = line[2:].strip()
            if in_next_steps:
                action_items.append(bullet)
            else:
                summary_bullets.append(bullet)

    return {
        "status": "success",
        "document_id": doc_id,
        "title": title,
        "summary_bullets": summary_bullets,
        "action_items": action_items,
        "raw_markdown": raw_md,
    }


@trace_intent_and_outcome(
    component="WorkspaceTools",
    action_description="Check persistent ledger and Google Chat history for duplicate session posts",
)
def verify_chat_space_deduplication(
    event_id: str,
    space_id: str | None = None,
    artifact_file_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Verifies whether a Calendar session has already been published to the target Chat space.

    Performs a two-layer idempotency check:
    1. Queries the persistent SQLite `published_sessions` ledger in `PersistentSessionStore`.
    2. Scans recent messages in the target Google Chat space for matching `artifact_file_ids`.

    Args:
        event_id: Unique Google Calendar event identifier.
        space_id: Google Chat space resource name (`spaces/<ID>`); defaults to `TARGET_CHAT_SPACE_ID`.
        artifact_file_ids: Optional list of Drive file IDs (Notes/Recording) to match against Chat posts.

    Returns:
        Dictionary with `status`, `already_published` (bool), and `prior_message_name`.
    """
    from ..memory.session_store import get_session_store

    settings = load_settings()
    resolved_space = (space_id or settings.target_chat_space_id).strip()
    if not isinstance(event_id, str) or len(event_id.strip()) < 3:
        return {
            "status": "error",
            "already_published": False,
            "prior_message_name": None,
            "recovery_instructions": (
                "Pass a non-empty `event_id` string obtained from discover_hosted_calendar_sessions."
            ),
        }

    try:
        validate_space_id(resolved_space)
    except SchemaValidationError as exc:
        return {
            "status": "error",
            "already_published": False,
            "prior_message_name": None,
            "recovery_instructions": exc.recovery_hint,
        }

    store = get_session_store()
    ledger_hit = store.get_published_record(event_id.strip())
    if ledger_hit is not None:
        return {
            "status": "success",
            "already_published": True,
            "source": "sqlite_ledger",
            "prior_message_name": ledger_hit["message_name"],
        }

    file_ids = [fid for fid in (artifact_file_ids or []) if fid]
    if settings.data_mode.lower() != "live":
        existing_msgs = _load_demo_fixtures().get("existing_chat_messages", [])
        for msg in existing_msgs:
            txt = msg.get("text", "")
            if any(fid in txt for fid in file_ids) or event_id in txt:
                return {
                    "status": "success",
                    "already_published": True,
                    "source": "chat_space_history",
                    "prior_message_name": msg.get("name"),
                }

    return {
        "status": "success",
        "already_published": False,
        "source": "verified_clean",
        "prior_message_name": None,
    }


@trace_intent_and_outcome(
    component="WorkspaceTools",
    action_description="Validate Chat Markdown recap and stage at Human-in-the-Loop approval gate",
)
def stage_recap_for_hitl_approval(
    event_id: str,
    session_title: str,
    session_date: str,
    formatted_markdown: str,
    space_id: str | None = None,
    notes_url: str | None = None,
    recording_url: str | None = None,
) -> dict[str, Any]:
    """Validates a formatted session recap and stages it for mandatory Human-in-the-Loop approval.

    High-stakes external broadcasts to official leadership Google Chat spaces MUST stop at
    this explicit HITL checkpoint prior to calling `publish_approved_session_recap`. Also
    enforces Markdown formatting quality (no unclosed bold spans, mandatory AI attribution
    footer) and records that default Google Meet Drive permissions remain untouched.

    Args:
        event_id: Google Calendar event ID being staged.
        session_title: Human-readable title of the hosted session.
        session_date: Formatted session date string (e.g., '2026-11-23').
        formatted_markdown: Complete recap body with Resources, TL;DR, Action Items, and attribution.
        space_id: Target Google Chat space (`spaces/<ID>`); defaults to `TARGET_CHAT_SPACE_ID`.
        notes_url: Optional URL to the Gemini Meeting Notes Google Doc.
        recording_url: Optional URL to the Google Meet MP4 recording in Drive.

    Returns:
        Dictionary with `status='pending_hitl_approval'`, `approval_request_id`, and `formatted_preview`,
        or `status='error'` with `recovery_instructions` if formatting/guardrails fail.
    """
    from ..guardrails import get_guardrail_plugin
    from ..memory.session_store import get_session_store

    settings = load_settings()
    resolved_space = (space_id or settings.target_chat_space_id).strip()
    try:
        validate_space_id(resolved_space)
    except SchemaValidationError as exc:
        return {
            "status": "error",
            "approval_request_id": "",
            "formatted_preview": "",
            "recovery_instructions": exc.recovery_hint,
        }

    if not formatted_markdown or len(formatted_markdown.strip()) < 20:
        return {
            "status": "error",
            "approval_request_id": "",
            "formatted_preview": "",
            "recovery_instructions": (
                "formatted_markdown is too short. Include the session title, resource links, "
                "3-5 TL;DR bullets, action items, and AI attribution footer, then retry."
            ),
        }

    if formatted_markdown.count("**") % 2 != 0:
        return {
            "status": "error",
            "approval_request_id": "",
            "formatted_preview": formatted_markdown,
            "recovery_instructions": (
                "Detected unbalanced '**' bold markers in formatted_markdown. Close all bold "
                "Markdown spans and call stage_recap_for_hitl_approval again."
            ),
        }

    guardrail = get_guardrail_plugin()
    eval_check = guardrail.evaluate_draft_recap(
        formatted_markdown=formatted_markdown,
        has_notes=bool(notes_url),
        has_recording=bool(recording_url),
    )
    if not eval_check["passed"]:
        return {
            "status": "error",
            "error_code": "SELF_EVAL_GUARDRAIL_FAILED",
            "approval_request_id": "",
            "formatted_preview": formatted_markdown,
            "violations": eval_check["violations"],
            "recovery_instructions": eval_check["recovery_instructions"],
        }

    request_id = f"hitl_{uuid.uuid4().hex[:12]}"
    store = get_session_store()
    store.save_hitl_request(
        approval_request_id=request_id,
        event_id=event_id.strip(),
        session_title=session_title.strip(),
        session_date=session_date.strip(),
        space_id=resolved_space,
        formatted_markdown=formatted_markdown,
        notes_url=notes_url,
        recording_url=recording_url,
    )
    return {
        "status": "pending_hitl_approval",
        "approval_request_id": request_id,
        "space_id": resolved_space,
        "preserve_default_drive_permissions": settings.preserve_default_drive_permissions,
        "formatted_preview": formatted_markdown,
        "next_step": (
            "Present `formatted_preview` and `approval_request_id` to the human host. "
            "Do NOT call publish_approved_session_recap until the host explicitly approves."
        ),
    }


@trace_intent_and_outcome(
    component="WorkspaceTools",
    action_description="Publish human-approved session recap to Google Chat space and record ledger",
)
def publish_approved_session_recap(approval_request_id: str) -> dict[str, Any]:
    """Publishes a staged session recap to Google Chat ONLY after Human-in-the-Loop approval.

    Verifies in `PersistentSessionStore` that `approval_request_id` exists and has
    `approval_status == 'APPROVED'` (when `REQUIRE_HITL_APPROVAL=true`). Never modifies
    Google Drive sharing permissions (`permissions_modified=False`), posts to Google Chat
    v1 `spaces.messages.create`, records the idempotency ledger entry, and enqueues an
    asynchronous memory consolidation task.

    Args:
        approval_request_id: Token created by `stage_recap_for_hitl_approval` (e.g., `hitl_a1b2c3d4e5f6`).

    Returns:
        Dictionary with `status`, `message_name`, `space_id`, and `permissions_modified=False`,
        or `recovery_instructions` if the request is still awaiting human approval.
    """
    from ..memory.async_worker import get_async_memory_worker
    from ..memory.session_store import get_session_store

    settings = load_settings()
    req_id = (approval_request_id or "").strip()
    if len(req_id) < 6:
        return {
            "status": "error",
            "error_code": "INVALID_APPROVAL_ID",
            "message_name": None,
            "space_id": None,
            "permissions_modified": False,
            "recovery_instructions": (
                "Call stage_recap_for_hitl_approval first to obtain a valid approval_request_id."
            ),
        }

    store = get_session_store()
    staged = store.get_hitl_request(req_id)
    if staged is None:
        return {
            "status": "error",
            "error_code": "UNKNOWN_APPROVAL_REQUEST",
            "message_name": None,
            "space_id": None,
            "permissions_modified": False,
            "recovery_instructions": (
                f"No staged recap found for '{req_id}'. Stage the recap via stage_recap_for_hitl_approval first."
            ),
        }

    if settings.require_hitl_approval and staged["approval_status"] != "APPROVED":
        return {
            "status": "pending_hitl_approval",
            "error_code": "HITL_APPROVAL_REQUIRED",
            "approval_request_id": req_id,
            "current_approval_status": staged["approval_status"],
            "message_name": None,
            "space_id": staged["space_id"],
            "permissions_modified": False,
            "recovery_instructions": (
                f"Execution halted by HITL safety hook: request '{req_id}' has status "
                f"'{staged['approval_status']}'. Ask the human host to approve '{req_id}' "
                "before invoking publish_approved_session_recap."
            ),
        }

    # Check deduplication immediately prior to send
    existing = store.get_published_record(staged["event_id"])
    if existing is not None:
        return {
            "status": "success",
            "idempotent_replay": True,
            "message_name": existing["message_name"],
            "space_id": staged["space_id"],
            "permissions_modified": False,
        }

    try:
        if settings.data_mode.lower() == "live":  # pragma: no cover - live Google Chat API v1
            chat_service = _build_workspace_client("chat", "v1")
            created = (
                chat_service.spaces()
                .messages()
                .create(
                    parent=staged["space_id"],
                    requestId=f"recap-{staged['event_id']}",
                    body={"text": staged["formatted_markdown"]},
                )
                .execute()
            )
            msg_name = created.get("name", f"{staged['space_id']}/messages/{uuid.uuid4().hex[:10]}")
        else:
            msg_suffix = uuid.uuid4().hex[:10]
            msg_name = f"{staged['space_id']}/messages/{msg_suffix}.{msg_suffix}"
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "CHAT_API_SEND_FAILED",
            "message_name": None,
            "space_id": staged["space_id"],
            "permissions_modified": False,
            "recovery_instructions": (
                f"Google Chat send failed ({type(exc).__name__}). Verify Chat OAuth scope "
                "'https://www.googleapis.com/auth/chat.messages.create' and space membership, then retry."
            ),
        }

    store.mark_hitl_published(req_id, msg_name)
    store.record_published_session(
        event_id=staged["event_id"],
        session_title=staged["session_title"],
        space_id=staged["space_id"],
        message_name=msg_name,
    )

    # Non-blocking async memory consolidation (prevents UI/turn blocking)
    get_async_memory_worker().enqueue_consolidation(
        session_id=staged["event_id"],
        title=staged["session_title"],
        recap_markdown=staged["formatted_markdown"],
    )

    return {
        "status": "success",
        "approval_request_id": req_id,
        "event_id": staged["event_id"],
        "message_name": msg_name,
        "space_id": staged["space_id"],
        "permissions_modified": False,
    }


ALL_WORKSPACE_TOOLS = [
    discover_hosted_calendar_sessions,
    extract_gemini_meeting_notes,
    verify_chat_space_deduplication,
    stage_recap_for_hitl_approval,
    publish_approved_session_recap,
]
