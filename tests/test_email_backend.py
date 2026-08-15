import email as email_module
import json
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate.backends.email import EmailBackend, _sign, _verify


class _CapturingSMTPHandler(socketserver.StreamRequestHandler):
    """Minimal SMTP stand-in that captures the raw message bytes smtplib
    sends, so a test can parse them back with the stdlib email parser and
    check what a real mail client would actually see."""

    def handle(self):
        self.wfile.write(b"220 localhost\r\n")
        in_data = False
        lines: list[bytes] = []
        while True:
            line = self.rfile.readline()
            if not line:
                break
            if in_data:
                if line.strip() == b".":
                    in_data = False
                    self.server.captured.append(b"".join(lines))
                    self.wfile.write(b"250 OK\r\n")
                    lines = []
                else:
                    lines.append(line)
                continue
            cmd = line.split(b" ")[0].strip().upper()
            if cmd in (b"EHLO", b"HELO", b"MAIL", b"RCPT"):
                self.wfile.write(b"250 OK\r\n")
            elif cmd == b"DATA":
                self.wfile.write(b"354 End with <CRLF>.<CRLF>\r\n")
                in_data = True
            elif cmd == b"QUIT":
                self.wfile.write(b"221 Bye\r\n")
                break
            else:
                self.wfile.write(b"250 OK\r\n")


def start_capturing_smtp():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CapturingSMTPHandler)
    server.captured = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1], server


def make_backend(**overrides):
    kwargs = dict(
        smtp_host="127.0.0.1",
        smtp_port=1,  # nothing listens here -- send will fail, which is fine, it must not block
        smtp_user="",
        smtp_password="",
        from_addr="approval-gate@example.com",
        to_addr="reviewer@example.com",
        secret="test-secret",
        public_base_url="http://placeholder",  # overwritten to backend.url once constructed
        port=0,
    )
    kwargs.update(overrides)
    backend = EmailBackend(**kwargs)
    backend.public_base_url = backend.url  # links point back at this same test server
    return backend


def http_get(url):
    with urllib.request.urlopen(url) as resp:
        return resp.status, resp.read()


def http_post(url):
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_sign_and_verify_roundtrip():
    sig = _sign("secret", "audit-1", "approve")
    assert _verify("secret", "audit-1", "approve", sig)


def test_verify_rejects_tampered_decision():
    sig = _sign("secret", "audit-1", "approve")
    # same signature, different decision -- must not verify
    assert not _verify("secret", "audit-1", "reject", sig)


def test_verify_rejects_wrong_secret():
    sig = _sign("secret-a", "audit-1", "approve")
    assert not _verify("secret-b", "audit-1", "approve", sig)


def test_send_failure_does_not_block_wait_for_decision():
    """SMTP connect to port 1 will fail immediately -- confirms the
    backend doesn't block or crash on a broken mail config, matching
    every other backend's notify-failure policy."""
    backend = make_backend()
    try:
        result_holder = {}

        def call():
            result_holder["decision"] = backend.wait_for_decision(
                {"audit_id": "em-1", "action": "send_email", "args": {}, "pii_findings": [], "risk": "low"}
            )

        t = threading.Thread(target=call)
        t.start()
        time.sleep(0.3)

        assert backend.resolve("em-1", {"decision": "approve", "by": "test"})
        t.join(timeout=2)
        assert result_holder["decision"]["decision"] == "approve"
    finally:
        backend.shutdown()


def test_confirm_page_served_for_valid_link():
    backend = make_backend()
    try:
        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "em-2", "action": "delete_records", "args": {}, "pii_findings": [], "risk": "high"},),
        )
        t.start()
        time.sleep(0.3)

        sig = _sign(backend.secret, "em-2", "approve")
        status, body = http_get(f"{backend.url}/confirm?audit_id=em-2&decision=approve&sig={sig}")
        assert status == 200
        assert b"delete_records" in body
        assert b"Confirm" in body

        backend.resolve("em-2", {"decision": "approve", "by": "t"})
        t.join(timeout=2)
    finally:
        backend.shutdown()


def test_confirm_page_rejects_bad_signature():
    backend = make_backend()
    try:
        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "em-3", "action": "x", "args": {}, "pii_findings": [], "risk": "low"},),
        )
        t.start()
        time.sleep(0.2)

        try:
            http_get(f"{backend.url}/confirm?audit_id=em-3&decision=approve&sig=deadbeef")
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 400

        backend.resolve("em-3", {"decision": "approve", "by": "t"})
        t.join(timeout=2)
    finally:
        backend.shutdown()


def test_decide_endpoint_unblocks_with_valid_signature():
    backend = make_backend()
    try:
        result_holder = {}

        def call():
            result_holder["decision"] = backend.wait_for_decision(
                {"audit_id": "em-4", "action": "send_email", "args": {}, "pii_findings": [], "risk": "medium"}
            )

        t = threading.Thread(target=call)
        t.start()
        time.sleep(0.3)

        sig = _sign(backend.secret, "em-4", "reject")
        response = http_post(f"{backend.url}/decide?audit_id=em-4&decision=reject&sig={sig}")
        assert response["ok"] is True

        t.join(timeout=2)
        assert result_holder["decision"]["decision"] == "reject"
    finally:
        backend.shutdown()


def test_decide_endpoint_rejects_tampered_link():
    backend = make_backend()
    try:
        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "em-5", "action": "delete_records", "args": {}, "pii_findings": [], "risk": "high"},),
        )
        t.start()
        time.sleep(0.2)

        # sign for "reject" but submit "approve" -- an attacker editing the URL
        sig = _sign(backend.secret, "em-5", "reject")
        try:
            http_post(f"{backend.url}/decide?audit_id=em-5&decision=approve&sig={sig}")
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 400

        backend.resolve("em-5", {"decision": "approve", "by": "t"})
        t.join(timeout=2)
    finally:
        backend.shutdown()


def test_double_click_second_request_gets_404():
    backend = make_backend()
    try:
        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "em-6", "action": "x", "args": {}, "pii_findings": [], "risk": "low"},),
        )
        t.start()
        time.sleep(0.3)

        sig = _sign(backend.secret, "em-6", "approve")
        response = http_post(f"{backend.url}/decide?audit_id=em-6&decision=approve&sig={sig}")
        assert response["ok"] is True
        t.join(timeout=2)

        try:
            http_post(f"{backend.url}/decide?audit_id=em-6&decision=approve&sig={sig}")
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        backend.shutdown()


def test_sent_email_urls_survive_the_wire_encoding_intact():
    """Regression test: EmailMessage.set_content()'s default
    quoted-printable encoding soft-wraps long lines, which silently
    splits (and breaks) a long signed URL across two lines -- a real
    mail client would show a dead link. EmailBackend must send with
    cte="base64" instead, which has no such wrapping of the decoded
    content. Verified by actually parsing the captured SMTP bytes back
    with the stdlib email parser, the way a real client would."""
    smtp_port, smtp_server = start_capturing_smtp()
    backend = None
    try:
        backend = EmailBackend(
            smtp_host="127.0.0.1",
            smtp_port=smtp_port,
            smtp_user="",
            smtp_password="",
            from_addr="approval-gate@example.com",
            to_addr="reviewer@example.com",
            secret="test-secret",
            public_base_url="http://127.0.0.1:9999",  # long enough to force wrapping if unfixed
            port=0,
            use_tls=False,
        )

        t = threading.Thread(
            target=backend.wait_for_decision,
            args=(
                {
                    "audit_id": "em-long-url-check",
                    "action": "delete_records",
                    "args": {"table": "support_tickets", "filter": "status = 'closed' AND age_days > 365"},
                    "pii_findings": [],
                    "risk": "high",
                },
            ),
        )
        t.start()

        for _ in range(50):
            if smtp_server.captured:
                break
            time.sleep(0.05)
        assert smtp_server.captured, "no message was captured by the fake SMTP server"

        raw = smtp_server.captured[0]
        parsed = email_module.message_from_bytes(raw)
        assert parsed["Content-Transfer-Encoding"] == "base64"
        body = parsed.get_payload(decode=True).decode("utf-8")

        expected_sig = _sign(backend.secret, "em-long-url-check", "approve")
        expected_url = f"http://127.0.0.1:9999/confirm?audit_id=em-long-url-check&decision=approve&sig={expected_sig}"
        assert expected_url in body, f"approve URL not found intact in decoded body:\n{body}"

        backend.resolve("em-long-url-check", {"decision": "approve", "by": "test"})
        t.join(timeout=2)
    finally:
        if backend is not None:
            backend.shutdown()
        smtp_server.shutdown()


def test_unknown_post_path_returns_404():
    backend = make_backend()
    try:
        req = urllib.request.Request(f"{backend.url}/not-a-real-path", data=b"", method="POST")
        try:
            urllib.request.urlopen(req)
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        backend.shutdown()


def test_real_smtp_send_path():
    """Exercises the actual smtplib.SMTP(...) send (starttls, login,
    send_message), not just the send-failure path -- against a local
    protocol-accurate fake server, since that's what the send
    machinery actually talks to."""
    import socketserver

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b"220 localhost\r\n")
            in_data = False
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                if in_data:
                    if line.strip() == b".":
                        in_data = False
                        self.wfile.write(b"250 OK\r\n")
                    continue
                cmd = line.split(b" ")[0].strip().upper()
                if cmd in (b"EHLO", b"HELO", b"MAIL", b"RCPT", b"AUTH"):
                    self.wfile.write(b"250 OK\r\n")
                elif cmd == b"STARTTLS":
                    self.wfile.write(b"220 Go ahead\r\n")
                elif cmd == b"DATA":
                    self.wfile.write(b"354 End with <CRLF>.<CRLF>\r\n")
                    in_data = True
                elif cmd == b"QUIT":
                    self.wfile.write(b"221 Bye\r\n")
                    break
                else:
                    self.wfile.write(b"250 OK\r\n")

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    smtp_port = server.server_address[1]

    backend = None
    try:
        # use_tls=False -- the fake server above doesn't do a real TLS
        # handshake, only enough of STARTTLS's text reply to unblock a
        # client that checks for one
        backend = EmailBackend(
            smtp_host="127.0.0.1",
            smtp_port=smtp_port,
            smtp_user="",
            smtp_password="",
            from_addr="a@example.com",
            to_addr="b@example.com",
            secret="s",
            public_base_url="http://placeholder",
            port=0,
            use_tls=False,
        )
        backend.public_base_url = backend.url

        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "em-smtp", "action": "x", "args": {}, "pii_findings": [], "risk": "low"},),
        )
        t.start()
        time.sleep(0.5)  # let the real SMTP send complete

        backend.resolve("em-smtp", {"decision": "approve", "by": "t"})
        t.join(timeout=2)
    finally:
        if backend is not None:
            backend.shutdown()
        server.shutdown()


def test_send_email_uses_starttls_and_login_when_configured():
    """Verifies _send_email's use_tls/smtp_user branches are actually
    exercised by intercepting smtplib.SMTP -- a real STARTTLS handshake
    needs a real TLS cert, so this checks the call sequence instead of
    running one against a fake server."""
    calls = []

    class FakeSMTP:
        def __init__(self, host, port):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, msg):
            calls.append(("send_message",))

    backend = EmailBackend(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="bot@example.com",
        smtp_password="hunter2",
        from_addr="a@example.com",
        to_addr="b@example.com",
        secret="s",
        public_base_url="http://placeholder",
        port=0,
        use_tls=True,
    )
    backend.public_base_url = backend.url

    import smtplib

    real_smtp = smtplib.SMTP
    try:
        smtplib.SMTP = FakeSMTP
        backend._send_email({"audit_id": "x", "action": "y", "args": {}, "risk": "low", "pii_findings": []})
    finally:
        smtplib.SMTP = real_smtp
        backend.shutdown()

    assert ("starttls",) in calls
    assert ("login", "bot@example.com", "hunter2") in calls
    assert ("send_message",) in calls


def test_unknown_get_path_returns_404():
    backend = make_backend()
    try:
        try:
            http_get(f"{backend.url}/not-a-real-path")
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        backend.shutdown()


def test_pending_endpoint_lists_current_items():
    backend = make_backend()
    try:
        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "em-7", "action": "send_email", "args": {}, "pii_findings": [], "risk": "low"},),
        )
        t.start()
        time.sleep(0.3)

        status, body = http_get(f"{backend.url}/pending")
        assert status == 200
        items = json.loads(body)
        assert len(items) == 1
        assert items[0]["audit_id"] == "em-7"

        backend.resolve("em-7", {"decision": "approve", "by": "t"})
        t.join(timeout=2)

        status, body = http_get(f"{backend.url}/pending")
        assert json.loads(body) == []
    finally:
        backend.shutdown()


def test_confirm_page_for_already_decided_action_returns_404():
    """A confirm link clicked after the action was already resolved some
    other way (e.g. a second reviewer, or a duplicate click racing a
    first one that already went through) should say so clearly, not
    show a stale confirm page for something no longer pending."""
    backend = make_backend()
    try:
        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "em-8", "action": "delete_records", "args": {}, "pii_findings": [], "risk": "high"},),
        )
        t.start()
        time.sleep(0.3)

        backend.resolve("em-8", {"decision": "approve", "by": "someone-else"})
        t.join(timeout=2)

        sig = _sign(backend.secret, "em-8", "approve")
        try:
            http_get(f"{backend.url}/confirm?audit_id=em-8&decision=approve&sig={sig}")
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        backend.shutdown()
