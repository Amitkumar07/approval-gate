"""
email.py
---------
Sends a review request email with one-click approve/reject links
instead of requiring a browser tab left open or a Slack integration.
Good for slower-moving, less time-sensitive approvals -- the reviewer
doesn't have to be watching anything, the email just sits in their
inbox until they act on it.

Two things had to be solved to make a *link* (not a form) carry a
decision safely:

  - No accounts, no login. A GET request from an email client can't
    carry a POST body or a session, so the decision has to be encoded
    in the URL itself, and there's no free "you must be the recipient"
    guarantee. That's what the signature is for.

  - The signature (HMAC-SHA256 over "audit_id:decision", stdlib
    `hmac`/`hashlib` only) proves the link wasn't forged or edited --
    someone can't take an "approve" link they were sent and hand-edit
    it into a "delete_records" approval for a different audit_id, or
    turn a "reject" link into "approve". It does NOT prove the clicker
    is the intended recipient; anyone who has the link (forwarded,
    leaked from mail server logs) can use it. That's an acceptable
    tradeoff for "no login needed," but if that's not acceptable for
    your use case, use WebBackend or SlackBackend instead, where the
    decision happens inside an authenticated surface.

  - Clicking approve/reject is a GET (mail clients pre-fetch/scan links,
    but they don't submit forms), so the click itself just shows a
    confirmation page with a button that does the actual POST -- this
    also protects against a mail scanner "clicking" the link on the
    recipient's behalf and silently deciding for them.

`args` cannot be edited from an email link (there's no form to edit
them in) -- only approve/reject are offered. If you need edit-in-place,
use WebBackend.

    from approval_gate import ApprovalGate
    from approval_gate.backends import EmailBackend

    backend = EmailBackend(
        smtp_host="smtp.example.com", smtp_port=587,
        smtp_user="bot@example.com", smtp_password="...",
        from_addr="approval-gate@example.com", to_addr="reviewer@example.com",
        secret="a-random-string-you-generate-once",
        public_base_url="https://approvals.example.com",  # must be reachable by the reviewer's browser
    )
    gate = ApprovalGate(db_path="audit.db", backend=backend)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import smtplib
import threading
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .base import Backend
from ._queue_base import _PendingQueueBackend


def _sign(secret: str, audit_id: str, decision: str) -> str:
    msg = f"{audit_id}:{decision}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]


def _verify(secret: str, audit_id: str, decision: str, sig: str) -> bool:
    expected = _sign(secret, audit_id, decision)
    return hmac.compare_digest(expected, sig)


_CONFIRM_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Confirm decision</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 480px; margin: 4rem auto; padding: 0 1.5rem;
         background: #f4f5f9; color: #1b1e2b; }}
  .card {{ background: #fff; border: 1px solid #dcdfe9; border-radius: 12px; padding: 1.5rem; }}
  h1 {{ font-size: 1.1rem; margin: 0 0 0.5rem; }}
  p {{ color: #5b5f75; font-size: 0.9rem; }}
  button {{ font-size: 0.95rem; font-weight: 600; padding: 0.6rem 1.2rem; border-radius: 8px;
           border: none; cursor: pointer; margin-top: 1rem; }}
  button.approve {{ background: #1e7e34; color: white; }}
  button.reject {{ background: #a13327; color: white; }}
</style></head>
<body><div class="card">
  <h1>Confirm: {decision_label} &ldquo;{action}&rdquo;?</h1>
  <p>This action was proposed for approval-gate review. Clicking confirm records your decision.</p>
  <button class="{decision}" onclick="fetch('{callback_path}', {{method:'POST'}}).then(() => document.getElementById('done').style.display='block')">
    Confirm {decision_label}
  </button>
  <p id="done" style="display:none; color:#1e7e34; font-weight:600;">Done -- you can close this tab.</p>
</div></body></html>
"""


class EmailBackend(_PendingQueueBackend, Backend):
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        from_addr: str,
        to_addr: str,
        secret: str,
        public_base_url: str,
        host: str = "127.0.0.1",
        port: int = 8644,
        use_tls: bool = True,
    ):
        super().__init__()
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.from_addr = from_addr
        self.to_addr = to_addr
        self.secret = secret
        self.public_base_url = public_base_url.rstrip("/")
        self.use_tls = use_tls

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
            self._send_email(pending)
        except (smtplib.SMTPException, OSError) as e:
            # Same policy as every other backend's notify step: a failed
            # send must not block or fail the approval flow -- it just
            # means nobody's been told yet. It stays pending until a
            # decision arrives some other way.
            print(f"[approval_gate] EmailBackend send failed (ignored): {e!r}")

        try:
            return result_queue.get()  # blocks until a confirm link is clicked
        finally:
            self._unregister(audit_id)

    def _link(self, audit_id: str, decision: str) -> str:
        sig = _sign(self.secret, audit_id, decision)
        return f"{self.public_base_url}/confirm?audit_id={audit_id}&decision={decision}&sig={sig}"

    def _send_email(self, pending: dict[str, Any]) -> None:
        approve_link = self._link(pending["audit_id"], "approve")
        reject_link = self._link(pending["audit_id"], "reject")

        findings = pending.get("pii_findings") or []
        flag = f"\n\n{len(findings)} sensitive field(s) flagged in this action." if findings else ""

        msg = EmailMessage()
        msg["Subject"] = f"Approval needed: {pending['action']} (risk: {pending['risk']})"
        msg["From"] = self.from_addr
        msg["To"] = self.to_addr
        # cte="base64": the default quoted-printable encoding soft-wraps
        # long lines, which silently splits (and breaks) a long signed URL
        # across two lines. Base64 has no line-length-driven wrapping of
        # the *content* -- clients decode it back byte-for-byte regardless
        # of how the encoded form is wrapped on the wire.
        msg.set_content(
            f"An agent proposed: {pending['action']} (risk: {pending['risk']})\n"
            f"Arguments: {pending['args']}"
            f"{flag}\n\n"
            f"Approve: {approve_link}\n"
            f"Reject:  {reject_link}\n",
            cte="base64",
        )

        with smtplib.SMTP(self.smtp_host, self.smtp_port) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.smtp_user:
                smtp.login(self.smtp_user, self.smtp_password)
            smtp.send_message(msg)

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _make_handler(backend: EmailBackend):
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

        def _send_html(self, body: str, status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/confirm":
                self._handle_confirm(parsed)
            elif parsed.path == "/pending":
                self._send_json(backend._list_pending())
            else:
                self.send_response(404)
                self.end_headers()

        def _handle_confirm(self, parsed) -> None:
            qs = parse_qs(parsed.query)
            audit_id = (qs.get("audit_id") or [""])[0]
            decision = (qs.get("decision") or [""])[0]
            sig = (qs.get("sig") or [""])[0]

            if decision not in ("approve", "reject") or not _verify(backend.secret, audit_id, decision, sig):
                self._send_html("<h1>Invalid or expired link</h1>", status=400)
                return

            pending = backend._get_pending(audit_id)
            if pending is None:
                self._send_html("<h1>This action has already been decided (or doesn't exist).</h1>", status=404)
                return

            callback_path = f"/decide?audit_id={audit_id}&decision={decision}&sig={sig}"
            self._send_html(
                _CONFIRM_PAGE.format(
                    decision=decision,
                    decision_label="Approve" if decision == "approve" else "Reject",
                    action=pending["action"],
                    callback_path=callback_path,
                )
            )

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/decide":
                self.send_response(404)
                self.end_headers()
                return

            qs = parse_qs(parsed.query)
            audit_id = (qs.get("audit_id") or [""])[0]
            decision = (qs.get("decision") or [""])[0]
            sig = (qs.get("sig") or [""])[0]

            if decision not in ("approve", "reject") or not _verify(backend.secret, audit_id, decision, sig):
                self._send_json({"error": "invalid or tampered link"}, status=400)
                return

            resolved = backend.resolve(
                audit_id,
                {"decision": decision, "by": backend.to_addr, "reason": "" if decision == "approve" else "rejected via email link"},
            )
            if not resolved:
                self._send_json({"error": "already decided or unknown audit_id"}, status=404)
                return
            self._send_json({"ok": True})

    return Handler
