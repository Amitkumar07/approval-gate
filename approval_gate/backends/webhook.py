"""
webhook.py
-----------
The "bring your own system" channel: instead of showing a page or
sending a message through a specific provider, this POSTs the pending
action to a URL you control, and waits for your system to POST the
decision back. Good for teams with their own ticketing/admin tooling
who don't want to adopt Slack or email specifically for this.

Two directions of traffic, both plain JSON over HTTP:

  1. Outbound: on every wait_for_decision() call, POSTs the pending
     payload (audit_id, action, args, pii_findings, risk) to the
     `notify_url` you configured. Your system is responsible for
     surfacing that to a human however it wants.

  2. Inbound: runs its own small HTTP listener (the same
     _PendingQueueBackend + HTTP-handler pattern WebBackend uses) with
     one endpoint, POST /decide, that your system calls back with the
     decision:

         {"audit_id": "...", "decision": "approve"|"reject"|"edit",
          "by": "...", "reason": "...", "args": {...}}

Like SlackNotifier, a failed outbound POST doesn't raise -- it's logged
via safe_notify and the backend keeps waiting; a webhook being briefly
down is not a reason to silently stop asking a human. What it does NOT
retry: if the outbound POST never reaches you, nothing will call
POST /decide, and wait_for_decision blocks until something does (there
is no timeout by design -- see Backend's docstring on why "block" is
deliberately open-ended).

    from approval_gate import ApprovalGate
    from approval_gate.backends import WebhookBackend

    backend = WebhookBackend(
        notify_url="https://internal-tools.example.com/approval-gate/incoming",
        host="127.0.0.1", port=8643,
    )
    gate = ApprovalGate(db_path="audit.db", backend=backend)
    print(f"Listening for decisions at {backend.url}/decide")
    # your system POSTs pending payloads it receives, and later
    # POSTs a decision back to f"{backend.url}/decide"
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .base import Backend
from ._queue_base import _PendingQueueBackend


class WebhookBackend(_PendingQueueBackend, Backend):
    def __init__(
        self,
        notify_url: str,
        host: str = "127.0.0.1",
        port: int = 8643,
        timeout: float = 5.0,
    ):
        super().__init__()
        self.notify_url = notify_url
        self.timeout = timeout

        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        self.host, self.port = self._server.server_address[0], self._server.server_address[1]
        self.url = f"http://{self.host}:{self.port}"

    def wait_for_decision(self, pending: dict[str, Any]) -> dict[str, Any]:
        audit_id = pending["audit_id"]
        result_queue = self._register(audit_id, pending)

        self._notify(pending)

        try:
            return result_queue.get()  # blocks until POST /decide delivers a decision
        finally:
            self._unregister(audit_id)

    def _notify(self, pending: dict[str, Any]) -> None:
        try:
            body = json.dumps({**pending, "callback_url": f"{self.url}/decide"}).encode("utf-8")
            req = urllib.request.Request(
                self.notify_url, data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except (urllib.error.URLError, OSError) as e:
            # A down webhook must not block or fail the approval flow --
            # same policy as notifiers.safe_notify -- it stays pending
            # until something calls POST /decide, however that happens.
            print(f"[approval_gate] WebhookBackend notify failed (ignored): {e!r}")

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _make_handler(backend: WebhookBackend):
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
            if self.path == "/pending":
                self._send_json(backend._list_pending())
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/decide":
                self.send_response(404)
                self.end_headers()
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length > 0 else b"{}"
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("expected a JSON object")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"error": "malformed request body"}, status=400)
                return

            audit_id = body.get("audit_id")
            decision = {
                "decision": body.get("decision", "reject"),
                "by": body.get("by", "webhook"),
                "reason": body.get("reason", ""),
                "args": body.get("args"),
            }
            if not backend.resolve(audit_id, decision):
                self._send_json({"error": "unknown or already-decided audit_id"}, status=404)
                return
            self._send_json({"ok": True})

    return Handler
