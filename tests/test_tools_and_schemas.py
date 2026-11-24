"""Unit tests for Pillar 1: Tool & Interface Design (Docstrings, Schemas, Guided Errors)."""

from __future__ import annotations

import inspect
import unittest

from session_publisher_agent.tools import (
    ALL_WORKSPACE_TOOLS,
    TOOL_JSON_SCHEMAS,
    discover_hosted_calendar_sessions,
    extract_gemini_meeting_notes,
    verify_chat_space_deduplication,
)


class TestToolsAndSchemas(unittest.TestCase):
    def test_all_tools_have_comprehensive_docstrings_and_schemas(self) -> None:
        for tool_fn in ALL_WORKSPACE_TOOLS:
            name = tool_fn.__name__
            doc = inspect.getdoc(tool_fn) or ""
            self.assertGreater(len(doc), 120, f"Tool {name} needs a comprehensive docstring")
            self.assertIn("Args:", doc, f"Tool {name} docstring must document Args")
            self.assertIn("Returns:", doc, f"Tool {name} docstring must document Returns")
            self.assertIn(name, TOOL_JSON_SCHEMAS, f"Missing explicit JSON schema for {name}")
            self.assertIn("input_schema", TOOL_JSON_SCHEMAS[name])
            self.assertIn("output_schema", TOOL_JSON_SCHEMAS[name])

    def test_discover_sessions_filters_host_and_attendee_group(self) -> None:
        res = discover_hosted_calendar_sessions(lookback_days=30)
        self.assertEqual(res["status"], "success")
        ids = [s["event_id"] for s in res["sessions"]]
        self.assertIn("evt_agentic_data_cloud_20261123", ids)
        self.assertIn("evt_quarterly_cal_sync_unrecorded_20261120", ids)
        self.assertNotIn("evt_global_community_non_host_20261118", ids)

    def test_guided_error_handling_returns_recovery_instructions(self) -> None:
        bad_days = discover_hosted_calendar_sessions(lookback_days=0)
        self.assertEqual(bad_days["status"], "error")
        self.assertIn("recovery_instructions", bad_days)

        bad_doc = extract_gemini_meeting_notes("https://docs.google.com/full/url/not/allowed")
        self.assertEqual(bad_doc["status"], "error")
        self.assertIn("recovery_instructions", bad_doc)

        bad_space = verify_chat_space_deduplication(
            event_id="evt_agentic_data_cloud_20261123",
            space_id="invalid_space_name",
        )
        self.assertEqual(bad_space["status"], "error")
        self.assertIn("recovery_instructions", bad_space)


if __name__ == "__main__":
    unittest.main()
