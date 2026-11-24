"""Active PII Redaction pipeline for logs, traces, and persistent memory writes.

Supports deterministic multi-pattern scrubbing (emails, phone numbers, OAuth
bearer tokens, API keys, SSNs, credit cards, and Drive resource keys) AND
optional cloud-native inspection/de-identification via Google Cloud Sensitive
Data Protection (`google-cloud-dlp`).
"""

from __future__ import annotations

import os
import re
from typing import Any

try:
    from google.cloud import dlp_v2  # type: ignore
except ImportError:  # pragma: no cover - optional when running offline
    dlp_v2 = None


class PIIRedactor:
    """Scrubs PII and sensitive credentials prior to logging or persistence."""

    _PATTERNS: list[tuple[re.Pattern[str], str]] = [
        # OAuth / Bearer / Google API keys
        (re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE), "Bearer [REDACTED_TOKEN]"),
        (re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), "[REDACTED_GCP_API_KEY]"),
        (re.compile(r"resourcekey=[0-9A-Za-z\-_]+", re.IGNORECASE), "resourcekey=[REDACTED_RESOURCE_KEY]"),
        # SSN & payment card patterns
        (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
        (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[REDACTED_CARD]"),
        # International & US phone numbers
        (
            re.compile(
                r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"
            ),
            "[REDACTED_PHONE]",
        ),
        # Email addresses
        (
            re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
            "[REDACTED_EMAIL]",
        ),
    ]

    def __init__(
        self,
        project_id: str | None = None,
        enable_cloud_dlp: bool | None = None,
    ) -> None:
        self.project_id = project_id or os.getenv("GCP_PROJECT_ID", "")
        self.enable_cloud_dlp = (
            enable_cloud_dlp
            if enable_cloud_dlp is not None
            else os.getenv("ENABLE_CLOUD_DLP_REDACTION", "false").lower() == "true"
        )
        self.redaction_count: int = 0

    def scrub_text(self, text: str) -> str:
        """Redacts sensitive tokens and PII from an arbitrary string."""
        if not text:
            return text

        scrubbed = text
        for pattern, replacement in self._PATTERNS:
            scrubbed, replacements = pattern.subn(replacement, scrubbed)
            self.redaction_count += replacements

        if self.enable_cloud_dlp and self.project_id and dlp_v2 is not None:
            scrubbed = self._deidentify_with_cloud_dlp(scrubbed)

        return scrubbed

    def scrub_payload(self, payload: Any) -> Any:
        """Recursively scrubs dictionaries, lists, and primitives before storage."""
        if isinstance(payload, str):
            return self.scrub_text(payload)
        if isinstance(payload, dict):
            cleaned: dict[str, Any] = {}
            for k, v in payload.items():
                key_low = str(k).lower()
                if any(
                    secret_word in key_low
                    for secret_word in ("api_key", "secret", "password", "access_token", "credential")
                ):
                    self.redaction_count += 1
                    cleaned[k] = "[REDACTED_SECRET]"
                else:
                    cleaned[k] = self.scrub_payload(v)
            return cleaned
        if isinstance(payload, list):
            return [self.scrub_payload(item) for item in payload]
        if isinstance(payload, tuple):
            return tuple(self.scrub_payload(item) for item in payload)
        return payload

    def _deidentify_with_cloud_dlp(self, text: str) -> str:  # pragma: no cover - requires live GCP
        """Calls Google Cloud Sensitive Data Protection (DLP) DeidentifyContent API."""
        try:
            client = dlp_v2.DlpServiceClient()
            parent = f"projects/{self.project_id}/locations/global"
            inspect_config = {
                "info_types": [
                    {"name": "EMAIL_ADDRESS"},
                    {"name": "PHONE_NUMBER"},
                    {"name": "PERSON_NAME"},
                    {"name": "AUTH_TOKEN"},
                ]
            }
            deidentify_config = {
                "info_type_transformations": {
                    "transformations": [
                        {"primitive_transformation": {"replace_with_info_type_config": {}}}
                    ]
                }
            }
            response = client.deidentify_content(
                request={
                    "parent": parent,
                    "inspect_config": inspect_config,
                    "deidentify_config": deidentify_config,
                    "item": {"value": text},
                }
            )
            return response.item.value
        except Exception:
            return text


_DEFAULT_REDACTOR: PIIRedactor | None = None


def get_pii_redactor() -> PIIRedactor:
    """Returns singleton PIIRedactor instance."""
    global _DEFAULT_REDACTOR
    if _DEFAULT_REDACTOR is None:
        _DEFAULT_REDACTOR = PIIRedactor()
    return _DEFAULT_REDACTOR
