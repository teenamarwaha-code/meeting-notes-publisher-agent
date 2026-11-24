"""Unit tests for Pillar 4: Observability, Distributed Tracing, and PII Redaction."""

from __future__ import annotations

import unittest

from session_publisher_agent.observability.pii_redactor import PIIRedactor
from session_publisher_agent.observability.telemetry import get_structured_logger, get_tracer
from session_publisher_agent.tools.workspace_tools import discover_hosted_calendar_sessions


class TestObservabilityAndPII(unittest.TestCase):
    def test_pii_redactor_scrubs_emails_phones_tokens_and_secrets(self) -> None:
        redactor = PIIRedactor()
        dirty = {
            "message": "Email alice.smith@example.org or call +1 (650) 555-0142",
            "auth": "Bearer mock_oauth_bearer_token_xyz",
            "gemini_api_key": "should_never_be_logged",
            "url": "https://docs.google.com/document/d/abc/edit?resourcekey=0-secretKey99",
        }
        cleaned = redactor.scrub_payload(dirty)
        self.assertIn("[REDACTED_EMAIL]", cleaned["message"])
        self.assertIn("[REDACTED_PHONE]", cleaned["message"])
        self.assertIn("[REDACTED_TOKEN]", cleaned["auth"])
        self.assertEqual(cleaned["gemini_api_key"], "[REDACTED_SECRET]")
        self.assertIn("resourcekey=[REDACTED_RESOURCE_KEY]", cleaned["url"])

    def test_distributed_tracing_and_intent_vs_outcome_logs(self) -> None:
        tracer = get_tracer()
        slogger = get_structured_logger()
        initial_spans = len(tracer.recorded_spans)

        with tracer.start_span("TestRootRequest") as root_span:
            discover_hosted_calendar_sessions(lookback_days=7)

        new_spans = tracer.recorded_spans[initial_spans:]
        self.assertGreaterEqual(len(new_spans), 2)
        child_span = new_spans[0]
        self.assertEqual(child_span.trace_id, root_span.trace_id)
        self.assertEqual(child_span.parent_span_id, root_span.span_id)

        phases = [e.get("event_phase") for e in slogger.events[-4:]]
        self.assertIn("INTENT", phases)
        self.assertIn("OUTCOME", phases)


if __name__ == "__main__":
    unittest.main()
