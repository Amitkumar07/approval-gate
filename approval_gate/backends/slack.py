"""
slack.py
---------
Interactive Approve/Reject buttons right in a Slack message, resolved
without anyone leaving Slack. This is a different (and more capable)
integration than `notifiers.SlackNotifier`: that one posts to an
incoming webhook and can only send, one-way, a link to click elsewhere.
This one posts via `chat.postMessage` with a bot token and Block Kit
buttons, and *receives* the click back through Slack's Interactivity
Request URL -- so the whole decision happens inside Slack.

Setup (once, in your Slack app config at api.slack.com/apps):
  1. Enable a bot token with `chat:write` scope, invite it to the
     channel you want approvals posted to.
  2. Enable Interactivity, and set the Request URL to wherever this
     backend's HTTP listener is reachable from Slack's servers (this
     means it needs a public URL -- a tunnel like ngrok in
     development, a real ingress in production; `localhost` alone is
     not reachable by Slack).
  3. Copy the bot token (`xoxb-...`) and the app's Signing Secret.

    from approval_gate import ApprovalGate
    from approval_gate.backends import SlackBackend

    backend = SlackBackend(
        bot_token="xoxb-...",
        signing_secret="...",
        channel="#approvals",
        host="0.0.0.0", port=8645,   # must be reachable at the Request URL you configured
    )
    gate = ApprovalGate(db_path="audit.db", backend=backend)

Why signature verification matters here specifically: this listener's
one job is "resolve a pending action when told to," so anyone who can
reach it and forge a payload could approve or reject arbitrary pending
actions. Slack signs every interactivity request with an HMAC-SHA256
over `v0:{timestamp}:{raw_body}` using your app's signing secret (see
https://api.slack.com/authentication/verifying-requests-from-slack);
requests that don't verify, or whose timestamp is more than 5 minutes
old (replay protection), are rejected with 400 before anything in the
payload is even parsed.

`args` cannot be edited from a Slack button (no form) -- only approve/
reject are offered, same tradeoff as EmailBackend. Use WebBackend if
in-place editing matters more than "resolve it without leaving Slack."
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .base import Backend
from ._queue_base import _PendingQueueBackend

_MAX_REQUEST_AGE_SECONDS = 60 * 5  # Slack's own recommended replay window


def _verify_slack_signature(signing_secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    try:
        if abs(time.time() - int(timestamp)) > _MAX_REQUEST_AGE_SECONDS:
            return False
    except ValueError:
        return False
    base = b"v0:" + timestamp.encode("utf-8") + b":" + raw_body
    expected = "v0=" + hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _blocks_for(pending: dict[str, Any]) -> list[dict[str, Any]]:
    findings = pending.get("pii_findings") or []
    args_lines = "\n".join(f"*{k}:* {v}" for k, v in pending["args"].items()) or "_(no arguments)_"
    findings_line = f"\n:warning: *{len(findings)} sensitive field(s) flagged*" if findings else ""

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Approval needed: `{pending['action']}`*  (risk: {pending['risk']})\n"
                    f"{args_lines}{findings_line}"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": f"approval-gate:{pending['audit_id']}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "approval_gate_approve",
                    "value": pending["audit_id"],
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "approval_gate_reject",
                    "value": pending["audit_id"],
                },
            ],
        },
    ]


class SlackBackend(_PendingQueueBackend, Backend):
    def __init__(
        self,
        bot_token: str,
        signing_secret: str,
        channel: str,
        host: str = "127.0.0.1",
        port: int = 8645,
        timeout: float = 5.0,
    ):
        super().__init__()
        self.bot_token = bot_token
        self.signing_secret = signing_secret
        self.channel = channel
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

        try:
            self._post_message(pending)
        except (urllib.error.URLError, OSError) as e:
            # Same policy as every other backend: a failed post must not
            # block or fail the flow -- it stays pending until a decision
            # arrives, however it eventually does.
            print(f"[approval_gate] SlackBackend post failed (ignored): {e!r}")

        try:
            return result_queue.get()  # blocks until a button click resolves it
        finally:
            self._unregister(audit_id)

    def _post_message(self, pending: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"channel": self.channel, "blocks": _blocks_for(pending), "text": f"Approval needed: {pending['action']}"}).encode("utf-8")
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.bot_token}",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _make_handler(backend: SlackBackend):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
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
            if self.path != "/slack/interactions":
                self.send_response(404)
                self.end_headers()
                return

            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length) if length > 0 else b""

            timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
            signature = self.headers.get("X-Slack-Signature", "")
            if not _verify_slack_signature(backend.signing_secret, timestamp, raw_body, signature):
                self._send_json({"error": "invalid Slack signature"}, status=400)
                return

            try:
                form = urllib.parse.parse_qs(raw_body.decode("utf-8"))
                payload = json.loads(form["payload"][0])
                action = payload["actions"][0]
                audit_id = action["value"]
                action_id = action["action_id"]
                user = payload.get("user", {}).get("username") or payload.get("user", {}).get("id", "slack-user")
            except (KeyError, IndexError, ValueError, json.JSONDecodeError):
                self._send_json({"error": "malformed Slack interaction payload"}, status=400)
                return

            if action_id == "approval_gate_approve":
                decision = "approve"
            elif action_id == "approval_gate_reject":
                decision = "reject"
            else:
                self._send_json({"error": f"unknown action_id {action_id!r}"}, status=400)
                return

            resolved = backend.resolve(
                audit_id, {"decision": decision, "by": user, "reason": "" if decision == "approve" else "rejected via Slack"}
            )
            if not resolved:
                # Slack expects a 200 even for "too late" clicks (e.g. a
                # second person clicking after it's already decided) --
                # a non-200 here makes Slack show a delivery error to the
                # user, which is misleading; "already handled" isn't a
                # server error.
                self._send_json({"text": "This action was already decided."})
                return
            self._send_json({"text": f"Recorded: {decision} by {user}"})

    return Handler
