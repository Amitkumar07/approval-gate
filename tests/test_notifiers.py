import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate.backends import WebBackend
from approval_gate.notifiers import SlackNotifier, safe_notify


def test_web_backend_calls_notifier_on_new_pending():
    calls = []

    def notifier(pending, review_url):
        calls.append((pending["audit_id"], pending["action"], review_url))

    backend = WebBackend(port=0, notifier=notifier)
    try:
        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "xyz", "action": "send_email", "args": {}, "pii_findings": [], "risk": "low"},),
        )
        t.start()
        time.sleep(0.2)
        assert calls == [("xyz", "send_email", backend.url)]

        # unblock so the thread doesn't leak past the test
        backend.resolve("xyz", {"decision": "approve", "by": "t"})
        t.join(timeout=2)
    finally:
        backend.shutdown()


def test_broken_notifier_does_not_block_approval():
    def notifier(pending, review_url):
        raise RuntimeError("webhook is down")

    backend = WebBackend(port=0, notifier=notifier)
    try:
        result_holder = {}

        def call():
            result_holder["decision"] = backend.wait_for_decision(
                {"audit_id": "abc", "action": "delete_records", "args": {}, "pii_findings": [], "risk": "high"}
            )

        t = threading.Thread(target=call)
        t.start()
        time.sleep(0.2)

        backend.resolve("abc", {"decision": "approve", "by": "t"})
        t.join(timeout=2)

        assert result_holder["decision"]["decision"] == "approve"
    finally:
        backend.shutdown()


def test_safe_notify_swallows_exceptions(capsys):
    def bad_notifier(pending, review_url):
        raise ValueError("boom")

    safe_notify(bad_notifier, {"action": "x"}, "http://example.com")
    captured = capsys.readouterr()
    assert "notifier failed" in captured.out


def test_slack_notifier_posts_expected_payload():
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = json.loads(self.rfile.read(length))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        webhook_url = f"http://127.0.0.1:{server.server_address[1]}/webhook"
        notifier = SlackNotifier(webhook_url=webhook_url)
        pending = {
            "action": "send_email",
            "risk": "medium",
            "pii_findings": [{"type": "email", "field": "to", "value_masked": "a***b", "source": "regex"}],
        }
        notifier(pending, "http://localhost:8642/")

        assert "send_email" in received["body"]["text"]
        assert "medium" in received["body"]["text"]
        assert "1 sensitive field" in received["body"]["text"]
        assert "http://localhost:8642/" in received["body"]["text"]
    finally:
        server.shutdown()


def test_slack_notifier_no_findings_omits_warning():
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = json.loads(self.rfile.read(length))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        webhook_url = f"http://127.0.0.1:{server.server_address[1]}/webhook"
        notifier = SlackNotifier(webhook_url=webhook_url)
        notifier({"action": "delete_records", "risk": "high", "pii_findings": []}, "http://localhost:8642/")

        assert "warning" not in received["body"]["text"]
    finally:
        server.shutdown()
