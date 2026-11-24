#!/usr/bin/env python3
"""Cloud Run Web Dashboard & REST API for the Session Publisher Agent.

Provides an interactive Human-in-the-Loop (HITL) review queue, one-click session
discovery & staging, model-routing inspector, and live OpenTelemetry trace viewer.
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from typing import Any
from urllib.parse import urlparse

from session_publisher_agent.agent import SessionPublisherOrchestrator
from session_publisher_agent.memory.session_store import get_session_store
from session_publisher_agent.observability.telemetry import get_structured_logger, get_tracer
from session_publisher_agent.tools.workspace_tools import discover_hosted_calendar_sessions

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Session Publisher Agent — Enterprise ADK Console</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      --bg: #f8fafc; --surface: #ffffff; --border: #e2e8f0;
      --text: #0f172a; --muted: #475569; --primary: #1a73e8;
      --success: #15803d; --warn: #b45309;
    }
    body { font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 24px; }
    .container { max-width: 1080px; margin: 0 auto; }
    header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
    h1 { font-size: 1.4rem; margin: 0; }
    .badge { background: #e0f2fe; color: #0369a1; padding: 4px 10px; border-radius: 999px; font-size: 0.8rem; font-weight: 600; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
    .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
    button { background: var(--primary); color: #fff; border: none; border-radius: 8px; padding: 9px 14px; font-weight: 600; cursor: pointer; }
    button.approve { background: var(--success); margin-top: 10px; }
    pre { background: #0f172a; color: #f8fafc; padding: 12px; border-radius: 8px; overflow-x: auto; font-size: 0.82rem; white-space: pre-wrap; }
    .meta { color: var(--muted); font-size: 0.85rem; margin-bottom: 12px; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>Enterprise Meeting Notes &amp; Recording Publisher Agent</h1>
        <div class="meta">Google ADK Multi-Agent Coordinator • Default Meet Permissions Preserved • HITL Gate Active</div>
      </div>
      <span class="badge">Google Cloud Run Ready</span>
    </header>
    <div class="grid">
      <div class="card">
        <h3>1. Discover &amp; Stage Latest Hosted Session</h3>
        <p class="meta">Runs SessionDiscoveryAgent (Flash) &rarr; NotesSynthesizerAgent (Pro) &rarr; HITL Gate.</p>
        <button onclick="stageLatest()">Run Multi-Agent Discovery &amp; Stage Recap</button>
        <pre id="stageOutput">Click above to stage the latest unpublished hosted session...</pre>
      </div>
      <div class="card">
        <h3>2. Human-in-the-Loop (HITL) Approval Queue</h3>
        <p class="meta">High-stakes Google Chat posts halt here until approved by the session host.</p>
        <div id="hitlQueue">Loading HITL queue...</div>
      </div>
    </div>
    <div class="card" style="margin-top: 20px;">
      <h3>3. Live OpenTelemetry Spans &amp; PII-Scrubbed Structured Logs</h3>
      <button onclick="refreshTelemetry()">Refresh Telemetry</button>
      <pre id="telemetryOutput">Loading spans...</pre>
    </div>
  </div>
  <script>
    async function stageLatest() {
      const res = await fetch('/api/stage', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}' });
      const data = await res.json();
      document.getElementById('stageOutput').textContent = JSON.stringify(data, null, 2);
      await loadHitl();
      await refreshTelemetry();
    }
    async function approveReq(id) {
      const res = await fetch('/api/approve', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ approval_request_id: id }) });
      const data = await res.json();
      alert('Publish status: ' + data.status + ' (' + (data.message_name || '') + ')');
      await loadHitl();
      await refreshTelemetry();
    }
    async function loadHitl() {
      const res = await fetch('/api/hitl');
      const items = await res.json();
      const el = document.getElementById('hitlQueue');
      if (!items.length) { el.innerHTML = '<p class="meta">No staged requests yet.</p>'; return; }
      el.innerHTML = items.map(item => `
        <div style="border-top:1px solid #e2e8f0; padding-top:10px; margin-top:10px;">
          <strong>${item.session_title}</strong> (${item.session_date}) — <code>${item.approval_status}</code>
          <pre>${item.formatted_markdown}</pre>
          ${item.approval_status === 'PENDING' ? `<button class="approve" onclick="approveReq('${item.approval_request_id}')">Approve &amp; Publish to Google Chat</button>` : ''}
        </div>
      `).join('');
    }
    async function refreshTelemetry() {
      const res = await fetch('/api/telemetry');
      const data = await res.json();
      document.getElementById('telemetryOutput').textContent = JSON.stringify(data, null, 2);
    }
    loadHitl(); refreshTelemetry();
  </script>
</body>
</html>
"""


class AgentDashboardHandler(BaseHTTPRequestHandler):
    orchestrator = SessionPublisherOrchestrator()

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json({"status": "healthy", "service": "session-publisher-agent"})
            return
        if path == "/api/sessions":
            self._send_json(discover_hosted_calendar_sessions())
            return
        if path == "/api/hitl":
            self._send_json(get_session_store().list_hitl_requests())
            return
        if path == "/api/telemetry":
            self._send_json(
                {
                    "spans": [s.to_dict() for s in get_tracer().recorded_spans[-20:]],
                    "structured_logs": get_structured_logger().events[-20:],
                }
            )
            return

        html_bytes = DASHBOARD_HTML.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_bytes)))
        self.end_headers()
        self.wfile.write(html_bytes)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        data = json.loads(raw or "{}")

        if path == "/api/stage":
            res = self.orchestrator.run_workflow(
                user_prompt=data.get("prompt", "Publish my latest hosted session"),
                title_query=data.get("title_query", ""),
            )
            self._send_json(res)
            return
        if path == "/api/approve":
            res = self.orchestrator.approve_and_publish(
                approval_request_id=str(data.get("approval_request_id", "")),
                reviewer_note=str(data.get("reviewer_note", "Approved via Web HITL Console")),
            )
            self._send_json(res)
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("PORT", "8080")), help="HTTP port"
    )
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), AgentDashboardHandler)
    print(f"Session Publisher Agent Console listening on http://0.0.0.0:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
