"""
web.py
------
A real review inbox in a browser tab instead of a blocking terminal
`input()` prompt -- the single biggest gap between "works" and "something
a stranger would actually use in production."

Design: `wait_for_decision` has to block the calling thread synchronously
(same contract as every other backend), while a human reviews and decides
from a browser, possibly minutes later. So this runs a tiny HTTP server
(stdlib only -- no new dependency) on a background thread, and bridges
"browser submits a decision" back to "the blocked Python thread" with a
per-pending `queue.Queue`. The page itself polls `/api/pending` every
couple seconds; there's no websocket/SSE machinery because a polling
loop is simpler, has nothing to reconnect, and the latency doesn't
matter for a human clicking a button.

Usage:

    from approval_gate import ApprovalGate
    from approval_gate.backends import WebBackend

    backend = WebBackend(port=8642)
    gate = ApprovalGate(db_path="audit.db", backend=backend)
    print(f"Review inbox running at {backend.url}")
    ...
    backend.shutdown()  # when your program is done

Multiple pending actions queue up and are all shown in the inbox at
once; each is decided independently by its own audit_id.

Pass `notifier=` to get pinged (Slack, email, anything) the moment an
action needs review, instead of relying on someone to have the inbox
open. See notifiers.py.

    from approval_gate.notifiers import SlackNotifier

    backend = WebBackend(port=8642, notifier=SlackNotifier(webhook_url="https://hooks.slack.com/..."))
"""

from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from ..notifiers import Notifier, safe_notify

from .base import Backend

_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>approval-gate review inbox</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  h1 { font-size: 1.25rem; }
  .empty { color: #666; font-style: italic; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; }
  .card h2 { margin: 0 0 0.25rem; font-size: 1.05rem; }
  .risk { display: inline-block; font-size: 0.75rem; padding: 0.1rem 0.5rem; border-radius: 4px; margin-left: 0.5rem; vertical-align: middle; }
  .risk-low { background: #e6f4ea; color: #1e7e34; }
  .risk-medium { background: #fff4e5; color: #b06000; }
  .risk-high { background: #fdecea; color: #c0392b; }
  table { width: 100%; border-collapse: collapse; margin: 0.5rem 0; }
  td { padding: 0.25rem 0; vertical-align: top; }
  td.k { color: #666; width: 30%; padding-right: 0.5rem; }
  td.v textarea { width: 100%; box-sizing: border-box; font-family: inherit; font-size: 0.9rem; }
  .findings { background: #fff8f0; border-radius: 4px; padding: 0.5rem 0.75rem; font-size: 0.85rem; margin: 0.5rem 0; }
  .findings div { margin: 0.15rem 0; }
  .buttons { margin-top: 0.75rem; }
  button { padding: 0.4rem 1rem; margin-right: 0.5rem; border-radius: 6px; border: 1px solid #ccc; cursor: pointer; background: #fff; }
  button.approve { background: #1e7e34; color: white; border-color: #1e7e34; }
  button.reject { background: #c0392b; color: white; border-color: #c0392b; }
</style>
</head>
<body>
<h1>approval-gate review inbox</h1>
<div id="root" class="empty">Loading...</div>
<script>
async function refresh() {
  const res = await fetch("/api/pending");
  const items = await res.json();
  const root = document.getElementById("root");
  if (items.length === 0) {
    root.className = "empty";
    root.textContent = "Nothing waiting for review.";
    return;
  }
  root.className = "";
  root.innerHTML = "";
  for (const item of items) {
    root.appendChild(renderCard(item));
  }
}

function renderCard(item) {
  const card = document.createElement("div");
  card.className = "card";

  const title = document.createElement("h2");
  title.textContent = item.action;
  const risk = document.createElement("span");
  risk.className = "risk risk-" + item.risk;
  risk.textContent = item.risk;
  title.appendChild(risk);
  card.appendChild(title);

  const table = document.createElement("table");
  const inputs = {};
  for (const [k, v] of Object.entries(item.args)) {
    const tr = document.createElement("tr");
    const tdK = document.createElement("td");
    tdK.className = "k";
    tdK.textContent = k;
    const tdV = document.createElement("td");
    tdV.className = "v";
    const textarea = document.createElement("textarea");
    textarea.rows = String(v).length > 80 ? 3 : 1;
    textarea.value = v;
    inputs[k] = textarea;
    tdV.appendChild(textarea);
    tr.appendChild(tdK);
    tr.appendChild(tdV);
    table.appendChild(tr);
  }
  card.appendChild(table);

  if (item.pii_findings && item.pii_findings.length > 0) {
    const findings = document.createElement("div");
    findings.className = "findings";
    findings.innerHTML = "<strong>Sensitive data detected:</strong>";
    for (const f of item.pii_findings) {
      const d = document.createElement("div");
      d.textContent = `${f.type} in "${f.field}": ${f.value_masked} (via ${f.source})`;
      findings.appendChild(d);
    }
    card.appendChild(findings);
  }

  const buttons = document.createElement("div");
  buttons.className = "buttons";

  const approveBtn = document.createElement("button");
  approveBtn.className = "approve";
  approveBtn.textContent = "Approve";
  approveBtn.onclick = () => decide(item.audit_id, "approve", inputs);
  buttons.appendChild(approveBtn);

  const editBtn = document.createElement("button");
  editBtn.textContent = "Approve with edits";
  editBtn.onclick = () => decide(item.audit_id, "edit", inputs);
  buttons.appendChild(editBtn);

  const rejectBtn = document.createElement("button");
  rejectBtn.className = "reject";
  rejectBtn.textContent = "Reject";
  rejectBtn.onclick = () => decide(item.audit_id, "reject", inputs);
  buttons.appendChild(rejectBtn);

  card.appendChild(buttons);
  return card;
}

async function decide(auditId, decision, inputs) {
  const args = {};
  for (const [k, el] of Object.entries(inputs)) args[k] = el.value;
  let reason = "";
  if (decision === "reject") {
    reason = prompt("Reason for rejecting (optional):") || "";
  }
  await fetch("/api/decide", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({audit_id: auditId, decision, args, reason, by: "web-reviewer"}),
  });
  refresh();
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class WebBackend(Backend):
    def __init__(self, host: str = "127.0.0.1", port: int = 8642, notifier: Optional[Notifier] = None):
        self._pending: dict[str, dict[str, Any]] = {}
        self._results: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        self.notifier = notifier

        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        self.host, self.port = self._server.server_address[0], self._server.server_address[1]
        self.url = f"http://{self.host}:{self.port}"

    def wait_for_decision(self, pending: dict[str, Any]) -> dict[str, Any]:
        audit_id = pending["audit_id"]
        result_queue: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[audit_id] = pending
            self._results[audit_id] = result_queue

        if self.notifier is not None:
            safe_notify(self.notifier, pending, self.url)

        try:
            return result_queue.get()  # blocks until the browser POSTs a decision
        finally:
            with self._lock:
                self._pending.pop(audit_id, None)
                self._results.pop(audit_id, None)

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _make_handler(backend: WebBackend):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silence default request logging
            pass

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                body = _PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/pending":
                with backend._lock:
                    items = list(backend._pending.values())
                self._send_json(items)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/api/decide":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            audit_id = body.get("audit_id")
            with backend._lock:
                result_queue = backend._results.get(audit_id)
            if result_queue is None:
                self._send_json({"error": "unknown or already-decided audit_id"}, status=404)
                return
            result_queue.put(
                {
                    "decision": body.get("decision", "reject"),
                    "by": body.get("by", "web-reviewer"),
                    "reason": body.get("reason", ""),
                    "args": body.get("args"),
                }
            )
            self._send_json({"ok": True})

    return Handler
