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

The page itself (_PAGE below) is a self-contained HTML string -- no
build step, no external assets, so it renders instantly over a raw
socket and has nothing that can go stale between the Python version and
the frontend. It supports light/dark automatically (prefers-color-scheme)
plus an explicit override via `data-theme`. Risk is encoded both as a
color and as a left-edge stripe on each card, not color alone, and a
"reviewing as" field replaces what used to be a hardcoded "web-reviewer"
string in every decision.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from ..notifiers import Notifier, safe_notify

from .base import Backend

_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>approval-gate &middot; review inbox</title>
<style>
  :root {
    --bg: #f4f5f9;
    --surface: #ffffff;
    --surface-sunken: #eceef4;
    --border: #dcdfe9;
    --text: #1b1e2b;
    --text-muted: #5b5f75;
    --text-faint: #8a8ea3;
    --accent: #4338ca;
    --accent-text: #ffffff;
    --accent-soft: #eeecfd;
    --focus-ring: #4338ca;

    --risk-low-bg: #e4f1e6;
    --risk-low-fg: #2f6d3f;
    --risk-medium-bg: #fbeed9;
    --risk-medium-fg: #93591a;
    --risk-high-bg: #fbe2df;
    --risk-high-fg: #a13327;

    --danger: #a13327;
    --danger-soft: #fbe2df;
    --danger-text: #ffffff;

    --font-ui: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --font-mono: ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Menlo, Consolas, monospace;
  }

  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #101220;
      --surface: #171a2b;
      --surface-sunken: #1e2136;
      --border: #2c2f47;
      --text: #e7e8f3;
      --text-muted: #a5a8c0;
      --text-faint: #6d7091;
      --accent: #8b80f9;
      --accent-text: #14162a;
      --accent-soft: #262a48;
      --focus-ring: #8b80f9;

      --risk-low-bg: #1c2e21;
      --risk-low-fg: #7fd499;
      --risk-medium-bg: #332510;
      --risk-medium-fg: #eab35c;
      --risk-high-bg: #351f1c;
      --risk-high-fg: #f0897a;

      --danger: #f0897a;
      --danger-soft: #351f1c;
      --danger-text: #1c0f0d;
    }
  }

  :root[data-theme="dark"] {
    --bg: #101220;
    --surface: #171a2b;
    --surface-sunken: #1e2136;
    --border: #2c2f47;
    --text: #e7e8f3;
    --text-muted: #a5a8c0;
    --text-faint: #6d7091;
    --accent: #8b80f9;
    --accent-text: #14162a;
    --accent-soft: #262a48;
    --focus-ring: #8b80f9;

    --risk-low-bg: #1c2e21;
    --risk-low-fg: #7fd499;
    --risk-medium-bg: #332510;
    --risk-medium-fg: #eab35c;
    --risk-high-bg: #351f1c;
    --risk-high-fg: #f0897a;

    --danger: #f0897a;
    --danger-soft: #351f1c;
    --danger-text: #1c0f0d;
  }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-ui);
    font-size: 15px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }

  .shell {
    max-width: 760px;
    margin: 0 auto;
    padding: 2.5rem 1.5rem 5rem;
  }

  .masthead {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.35rem;
  }

  .masthead h1 {
    font-size: 1.05rem;
    font-weight: 650;
    letter-spacing: -0.01em;
    margin: 0;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .masthead h1 .mark {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--accent);
  }

  .count {
    font-family: var(--font-mono);
    font-size: 0.8rem;
    color: var(--text-faint);
    font-variant-numeric: tabular-nums;
  }

  .subhead {
    color: var(--text-muted);
    font-size: 0.85rem;
    margin: 0 0 2rem;
  }

  .reviewer-field {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 2rem;
    padding: 0.6rem 0.85rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
  }

  .reviewer-field label {
    font-size: 0.8rem;
    color: var(--text-muted);
    white-space: nowrap;
  }

  .reviewer-field input {
    flex: 1;
    border: none;
    background: transparent;
    color: var(--text);
    font-family: var(--font-ui);
    font-size: 0.85rem;
    padding: 0.15rem 0;
    min-width: 0;
  }

  .reviewer-field input:focus {
    outline: none;
  }

  .list {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .empty {
    text-align: center;
    padding: 4rem 1rem;
    color: var(--text-faint);
  }

  .empty .glyph {
    font-family: var(--font-mono);
    font-size: 1.6rem;
    color: var(--text-faint);
    margin-bottom: 0.75rem;
    opacity: 0.6;
  }

  .empty .title {
    color: var(--text-muted);
    font-size: 0.95rem;
    font-weight: 600;
    margin-bottom: 0.25rem;
  }

  .empty .hint {
    font-size: 0.8rem;
  }

  .card {
    display: flex;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    overflow: hidden;
  }

  .stripe {
    flex: 0 0 4px;
  }

  .stripe.risk-low { background: var(--risk-low-fg); }
  .stripe.risk-medium { background: var(--risk-medium-fg); }
  .stripe.risk-high { background: var(--risk-high-fg); }

  .card-body {
    flex: 1;
    min-width: 0;
    padding: 1.1rem 1.25rem 1.25rem;
  }

  .card-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.9rem;
  }

  .card-head .titles {
    min-width: 0;
  }

  .action-name {
    font-family: var(--font-mono);
    font-size: 0.98rem;
    font-weight: 600;
    letter-spacing: -0.01em;
    word-break: break-word;
  }

  .meta-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.3rem;
    flex-wrap: wrap;
  }

  .badge {
    display: inline-flex;
    align-items: center;
    font-size: 0.7rem;
    font-weight: 650;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 0.18rem 0.55rem;
    border-radius: 999px;
  }

  .badge.risk-low { background: var(--risk-low-bg); color: var(--risk-low-fg); }
  .badge.risk-medium { background: var(--risk-medium-bg); color: var(--risk-medium-fg); }
  .badge.risk-high { background: var(--risk-high-bg); color: var(--risk-high-fg); }

  .route-tag {
    font-family: var(--font-mono);
    font-size: 0.72rem;
    color: var(--text-muted);
    background: var(--surface-sunken);
    padding: 0.18rem 0.55rem;
    border-radius: 999px;
  }

  .waiting {
    font-size: 0.72rem;
    color: var(--text-faint);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    padding-top: 0.15rem;
  }

  .fields {
    display: grid;
    grid-template-columns: minmax(0, 30%) 1fr;
    gap: 0.5rem 0.75rem;
    margin-bottom: 0.9rem;
  }

  .field-key {
    font-family: var(--font-mono);
    font-size: 0.78rem;
    color: var(--text-muted);
    padding-top: 0.5rem;
    word-break: break-word;
  }

  .field-value textarea {
    width: 100%;
    box-sizing: border-box;
    resize: vertical;
    background: var(--surface-sunken);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-family: var(--font-ui);
    font-size: 0.85rem;
    line-height: 1.45;
    padding: 0.45rem 0.6rem;
  }

  .field-value textarea:focus {
    outline: 2px solid var(--focus-ring);
    outline-offset: -1px;
    background: var(--surface);
  }

  .findings {
    background: var(--risk-high-bg);
    color: var(--risk-high-fg);
    border-radius: 10px;
    padding: 0.65rem 0.8rem;
    font-size: 0.8rem;
    margin-bottom: 0.9rem;
  }

  .findings .findings-title {
    font-weight: 650;
    margin-bottom: 0.35rem;
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .findings ul {
    margin: 0;
    padding-left: 1.1rem;
  }

  .findings li {
    margin: 0.15rem 0;
  }

  .findings code {
    font-family: var(--font-mono);
    font-size: 0.78rem;
  }

  .actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  button {
    font-family: var(--font-ui);
    font-size: 0.85rem;
    font-weight: 600;
    padding: 0.5rem 0.95rem;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
    transition: filter 0.1s ease, transform 0.05s ease;
  }

  button:hover { filter: brightness(0.97); }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) button:hover { filter: brightness(1.15); }
  }
  :root[data-theme="dark"] button:hover { filter: brightness(1.15); }

  button:active { transform: translateY(1px); }

  button:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  button.primary {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--accent-text);
  }

  button.quiet {
    background: transparent;
  }

  button.danger-text {
    background: transparent;
    border-color: transparent;
    color: var(--danger);
    margin-left: auto;
  }

  .reject-panel {
    margin-top: 0.75rem;
    padding: 0.75rem;
    background: var(--danger-soft);
    border-radius: 10px;
    display: none;
  }

  .reject-panel.open { display: block; }

  .reject-panel label {
    display: block;
    font-size: 0.75rem;
    font-weight: 650;
    color: var(--danger);
    margin-bottom: 0.4rem;
  }

  .reject-panel textarea {
    width: 100%;
    box-sizing: border-box;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    color: var(--text);
    font-family: var(--font-ui);
    font-size: 0.85rem;
    padding: 0.5rem 0.6rem;
    margin-bottom: 0.6rem;
    resize: vertical;
  }

  .reject-panel .reject-actions {
    display: flex;
    gap: 0.5rem;
  }

  button.confirm-reject {
    background: var(--danger);
    border-color: var(--danger);
    color: var(--danger-text);
  }

  .toast {
    position: fixed;
    bottom: 1.5rem;
    left: 50%;
    transform: translateX(-50%) translateY(8px);
    background: var(--text);
    color: var(--bg);
    padding: 0.55rem 1rem;
    border-radius: 8px;
    font-size: 0.82rem;
    font-weight: 600;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.15s ease, transform 0.15s ease;
  }

  .toast.show {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
  }

  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; }
  }
</style>
</head>
<body>
<div class="shell">
  <div class="masthead">
    <h1><span class="mark"></span>approval-gate</h1>
    <span class="count" id="count"></span>
  </div>
  <p class="subhead">Actions waiting on a human decision before they run.</p>

  <div class="reviewer-field">
    <label for="reviewer">Reviewing as</label>
    <input id="reviewer" type="text" value="reviewer" autocomplete="off" spellcheck="false">
  </div>

  <div id="root" class="list"></div>
</div>

<div class="toast" id="toast"></div>

<script>
const REVIEWER_KEY = "approval-gate:reviewer-name";
const reviewerInput = document.getElementById("reviewer");
reviewerInput.value = localStorage.getItem(REVIEWER_KEY) || "reviewer";
reviewerInput.addEventListener("change", () => {
  localStorage.setItem(REVIEWER_KEY, reviewerInput.value.trim() || "reviewer");
});

function reviewerName() {
  return reviewerInput.value.trim() || "reviewer";
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.remove("show"), 1800);
}

function timeAgo(seconds) {
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60);
  return h + "h ago";
}

async function refresh() {
  let items;
  try {
    const res = await fetch("/api/pending");
    items = await res.json();
  } catch (e) {
    return; // transient fetch failure -- next poll will retry
  }

  const root = document.getElementById("root");
  const countEl = document.getElementById("count");
  countEl.textContent = items.length > 0 ? items.length + " pending" : "";

  if (items.length === 0) {
    root.className = "list";
    root.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.innerHTML = '<div class="glyph">&#10003;</div>' +
      '<div class="title">Inbox clear</div>' +
      '<div class="hint">New actions will appear here the moment they need review.</div>';
    root.appendChild(empty);
    return;
  }

  const openIds = new Set(
    Array.from(root.querySelectorAll(".reject-panel.open")).map((el) => el.dataset.auditId)
  );

  root.className = "list";
  root.innerHTML = "";
  for (const item of items) {
    root.appendChild(renderCard(item, openIds.has(item.audit_id)));
  }
}

function renderCard(item, rejectOpen) {
  const card = document.createElement("div");
  card.className = "card";

  const stripe = document.createElement("div");
  stripe.className = "stripe risk-" + item.risk;
  card.appendChild(stripe);

  const body = document.createElement("div");
  body.className = "card-body";

  const head = document.createElement("div");
  head.className = "card-head";

  const titles = document.createElement("div");
  titles.className = "titles";
  const name = document.createElement("div");
  name.className = "action-name";
  name.textContent = item.action;
  titles.appendChild(name);

  const metaRow = document.createElement("div");
  metaRow.className = "meta-row";
  const badge = document.createElement("span");
  badge.className = "badge risk-" + item.risk;
  badge.textContent = item.risk + " risk";
  metaRow.appendChild(badge);
  if (item.route_to) {
    const route = document.createElement("span");
    route.className = "route-tag";
    route.textContent = "→ " + item.route_to;
    metaRow.appendChild(route);
  }
  titles.appendChild(metaRow);
  head.appendChild(titles);

  if (typeof item._first_seen === "number") {
    const waiting = document.createElement("div");
    waiting.className = "waiting";
    waiting.textContent = timeAgo((Date.now() / 1000) - item._first_seen);
    head.appendChild(waiting);
  }

  body.appendChild(head);

  if (item.pii_findings && item.pii_findings.length > 0) {
    const findings = document.createElement("div");
    findings.className = "findings";
    const title = document.createElement("div");
    title.className = "findings-title";
    title.textContent = "⚠ Sensitive data detected";
    findings.appendChild(title);
    const ul = document.createElement("ul");
    for (const f of item.pii_findings) {
      const li = document.createElement("li");
      li.innerHTML = `<code>${f.type}</code> in <code>${f.field}</code> &mdash; ${f.value_masked}`;
      ul.appendChild(li);
    }
    findings.appendChild(ul);
    body.appendChild(findings);
  }

  const fields = document.createElement("div");
  fields.className = "fields";
  const inputs = {};
  for (const [k, v] of Object.entries(item.args)) {
    const key = document.createElement("div");
    key.className = "field-key";
    key.textContent = k;
    const val = document.createElement("div");
    val.className = "field-value";
    const textarea = document.createElement("textarea");
    textarea.rows = String(v).length > 70 ? 3 : 1;
    textarea.value = v;
    inputs[k] = textarea;
    val.appendChild(textarea);
    fields.appendChild(key);
    fields.appendChild(val);
  }
  body.appendChild(fields);

  const actions = document.createElement("div");
  actions.className = "actions";

  const approveBtn = document.createElement("button");
  approveBtn.className = "primary";
  approveBtn.textContent = "Approve";
  approveBtn.onclick = () => decide(item.audit_id, "approve", inputs);
  actions.appendChild(approveBtn);

  const editBtn = document.createElement("button");
  editBtn.className = "quiet";
  editBtn.textContent = "Approve with edits";
  editBtn.onclick = () => decide(item.audit_id, "edit", inputs);
  actions.appendChild(editBtn);

  const rejectBtn = document.createElement("button");
  rejectBtn.className = "danger-text";
  rejectBtn.textContent = "Reject";
  rejectBtn.onclick = () => rejectPanel.classList.toggle("open");
  actions.appendChild(rejectBtn);

  body.appendChild(actions);

  const rejectPanel = document.createElement("div");
  rejectPanel.className = "reject-panel" + (rejectOpen ? " open" : "");
  rejectPanel.dataset.auditId = item.audit_id;
  rejectPanel.innerHTML = '<label>Reason for rejecting (optional)</label>';
  const reasonBox = document.createElement("textarea");
  reasonBox.rows = 2;
  reasonBox.placeholder = "e.g. wrong recipient, needs a smaller scope, not needed yet...";
  rejectPanel.appendChild(reasonBox);
  const rejectActions = document.createElement("div");
  rejectActions.className = "reject-actions";
  const confirmBtn = document.createElement("button");
  confirmBtn.className = "confirm-reject";
  confirmBtn.textContent = "Confirm reject";
  confirmBtn.onclick = () => decide(item.audit_id, "reject", inputs, reasonBox.value);
  const cancelBtn = document.createElement("button");
  cancelBtn.className = "quiet";
  cancelBtn.textContent = "Cancel";
  cancelBtn.onclick = () => rejectPanel.classList.remove("open");
  rejectActions.appendChild(confirmBtn);
  rejectActions.appendChild(cancelBtn);
  rejectPanel.appendChild(rejectActions);
  body.appendChild(rejectPanel);

  card.appendChild(body);
  return card;
}

async function decide(auditId, decision, inputs, reason) {
  const args = {};
  for (const [k, el] of Object.entries(inputs)) args[k] = el.value;
  await fetch("/api/decide", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      audit_id: auditId,
      decision,
      args,
      reason: reason || "",
      by: reviewerName(),
    }),
  });
  showToast(decision === "approve" ? "Approved" : decision === "edit" ? "Approved with edits" : "Rejected");
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
        # _first_seen is UI-only (drives the "waiting Xm" indicator), so it's
        # added to a copy rather than the payload handed to the notifier.
        display_pending = {**pending, "_first_seen": time.time()}
        with self._lock:
            self._pending[audit_id] = display_pending
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
