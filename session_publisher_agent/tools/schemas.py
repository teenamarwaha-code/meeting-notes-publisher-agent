"""Explicit JSON Schemas and strict runtime input/output validation for LLM tools.

Uses Pydantic v2 (`BaseModel`, `Field`, `model_json_schema`) when installed, with
an automatic strict schema validator fallback so schema validation and JSON Schema
export work identically in minimal/offline test runners.
"""

from __future__ import annotations

import re
from typing import Any

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
SPACE_RE = re.compile(r"^spaces/[A-Za-z0-9_-]{4,64}$")


class SchemaValidationError(ValueError):
    """Raised when tool input or output violates its declared JSON Schema."""

    def __init__(self, field_name: str, message: str, recovery_hint: str) -> None:
        super().__init__(f"{field_name}: {message}")
        self.field_name = field_name
        self.message = message
        self.recovery_hint = recovery_hint


TOOL_JSON_SCHEMAS: dict[str, dict[str, Any]] = {
    "discover_hosted_calendar_sessions": {
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["lookback_days"],
            "properties": {
                "lookback_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 90,
                    "description": "Number of past days to scan on the host's primary Google Calendar (1..90).",
                },
                "title_query": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "Optional case-insensitive substring filter on calendar event summary.",
                },
                "host_email": {
                    "type": "string",
                    "format": "email",
                    "description": "Organizer email required for a session to qualify as host-led.",
                },
                "required_attendee_group": {
                    "type": "string",
                    "format": "email",
                    "description": "Target distribution list email that must be invited to the session.",
                },
            },
        },
        "output_schema": {
            "type": "object",
            "required": ["status", "session_count", "sessions"],
            "properties": {
                "status": {"type": "string", "enum": ["success", "error"]},
                "session_count": {"type": "integer", "minimum": 0},
                "sessions": {"type": "array", "items": {"type": "object"}},
                "recovery_instructions": {"type": "string"},
            },
        },
    },
    "extract_gemini_meeting_notes": {
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["notes_document_id"],
            "properties": {
                "notes_document_id": {
                    "type": "string",
                    "minLength": 4,
                    "maxLength": 128,
                    "description": "Google Docs file ID of the 'Notes by Gemini' attachment.",
                }
            },
        },
        "output_schema": {
            "type": "object",
            "required": ["status", "document_id", "summary_bullets", "action_items"],
            "properties": {
                "status": {"type": "string", "enum": ["success", "error"]},
                "document_id": {"type": "string"},
                "title": {"type": "string"},
                "summary_bullets": {"type": "array", "items": {"type": "string"}},
                "action_items": {"type": "array", "items": {"type": "string"}},
                "recovery_instructions": {"type": "string"},
            },
        },
    },
    "verify_chat_space_deduplication": {
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["event_id", "space_id"],
            "properties": {
                "event_id": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 128,
                    "description": "Unique Google Calendar event ID to check for prior publication.",
                },
                "space_id": {
                    "type": "string",
                    "pattern": "^spaces/[A-Za-z0-9_-]{4,64}$",
                    "description": "Google Chat space resource name (e.g., 'spaces/AAAA_OFFICIAL_SPACE').",
                },
                "artifact_file_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Drive file IDs for Gemini Notes and Meet Recording used to detect prior posts.",
                },
            },
        },
        "output_schema": {
            "type": "object",
            "required": ["status", "already_published"],
            "properties": {
                "status": {"type": "string", "enum": ["success", "error"]},
                "already_published": {"type": "boolean"},
                "prior_message_name": {"type": ["string", "null"]},
                "recovery_instructions": {"type": "string"},
            },
        },
    },
    "stage_recap_for_hitl_approval": {
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["event_id", "session_title", "session_date", "space_id", "formatted_markdown"],
            "properties": {
                "event_id": {"type": "string", "minLength": 3},
                "session_title": {"type": "string", "minLength": 2},
                "session_date": {"type": "string", "minLength": 4},
                "space_id": {"type": "string", "pattern": "^spaces/[A-Za-z0-9_-]{4,64}$"},
                "formatted_markdown": {"type": "string", "minLength": 20, "maxLength": 8000},
                "notes_url": {"type": ["string", "null"]},
                "recording_url": {"type": ["string", "null"]},
            },
        },
        "output_schema": {
            "type": "object",
            "required": ["status", "approval_request_id", "formatted_preview"],
            "properties": {
                "status": {"type": "string", "enum": ["pending_hitl_approval", "error"]},
                "approval_request_id": {"type": "string"},
                "formatted_preview": {"type": "string"},
                "recovery_instructions": {"type": "string"},
            },
        },
    },
    "publish_approved_session_recap": {
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["approval_request_id"],
            "properties": {
                "approval_request_id": {
                    "type": "string",
                    "minLength": 6,
                    "description": "HITL approval token returned by stage_recap_for_hitl_approval after human confirmation.",
                }
            },
        },
        "output_schema": {
            "type": "object",
            "required": ["status"],
            "properties": {
                "status": {"type": "string", "enum": ["success", "pending_hitl_approval", "error"]},
                "message_name": {"type": ["string", "null"]},
                "space_id": {"type": ["string", "null"]},
                "permissions_modified": {"type": "boolean"},
                "recovery_instructions": {"type": "string"},
            },
        },
    },
}


def validate_discover_inputs(
    lookback_days: int,
    title_query: str,
    host_email: str,
    required_attendee_group: str,
) -> None:
    if not isinstance(lookback_days, int) or isinstance(lookback_days, bool) or not (1 <= lookback_days <= 90):
        raise SchemaValidationError(
            "lookback_days",
            f"Expected integer between 1 and 90, got {lookback_days!r}.",
            "Retry discover_hosted_calendar_sessions with lookback_days set to an integer in [1, 90] (e.g., 14).",
        )
    if len(title_query) > 120:
        raise SchemaValidationError(
            "title_query",
            "Query exceeds 120 characters.",
            "Shorten title_query to a concise keyword phrase under 120 characters.",
        )
    if not EMAIL_RE.match(host_email):
        raise SchemaValidationError(
            "host_email",
            f"Invalid email format: {host_email!r}.",
            "Provide a valid organizer email address (e.g., 'organizer@example.com') or omit to use configured default.",
        )
    if not EMAIL_RE.match(required_attendee_group):
        raise SchemaValidationError(
            "required_attendee_group",
            f"Invalid group email format: {required_attendee_group!r}.",
            "Provide a valid Google Group email address (e.g., 'account-leads@example.com').",
        )


def validate_space_id(space_id: str) -> None:
    if not isinstance(space_id, str) or not SPACE_RE.match(space_id):
        raise SchemaValidationError(
            "space_id",
            f"Invalid Google Chat space resource name {space_id!r}.",
            "Pass space_id formatted as 'spaces/<SPACE_ID>' (for example, 'spaces/AAAA_OFFICIAL_SPACE').",
        )


def validate_notes_doc_id(notes_document_id: str) -> None:
    if (
        not isinstance(notes_document_id, str)
        or not (4 <= len(notes_document_id.strip()) <= 128)
        or "/" in notes_document_id
    ):
        raise SchemaValidationError(
            "notes_document_id",
            f"Invalid Google Docs document ID {notes_document_id!r}.",
            "Extract only the raw document ID string (not the full https://docs.google.com/... URL) from session.notes.file_id and retry.",
        )
