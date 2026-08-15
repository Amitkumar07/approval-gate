import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate.backends import WebhookBackend


def start_receiver():
    """Stands in for the user's own system: records every payload it
    receives at /incoming and returns 200."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/incoming"
    return url, received, server


def http_get(url):
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())


def http_post(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_notifies_configured_url_with_callback():
    receiver_url, received, receiver_server = start_receiver()
    backend = WebhookBackend(notify_url=receiver_url, port=0)
    try:
        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "wh-1", "action": "send_email", "args": {"to": "a@b.com"}, "pii_findings": [], "risk": "medium"},),
        )
        t.start()
        time.sleep(0.3)

        assert len(received) == 1
        assert received[0]["audit_id"] == "wh-1"
        assert received[0]["action"] == "send_email"
        assert received[0]["callback_url"] == f"{backend.url}/decide"

        backend.resolve("wh-1", {"decision": "approve", "by": "webhook-caller"})
        t.join(timeout=2)
    finally:
        backend.shutdown()
        receiver_server.shutdown()


def test_decide_endpoint_unblocks_waiting_call():
    receiver_url, received, receiver_server = start_receiver()
    backend = WebhookBackend(notify_url=receiver_url, port=0)
    try:
        result_holder = {}

        def call():
            result_holder["decision"] = backend.wait_for_decision(
                {"audit_id": "wh-2", "action": "delete_records", "args": {}, "pii_findings": [], "risk": "high"}
            )

        t = threading.Thread(target=call)
        t.start()

        for _ in range(50):
            if received:
                break
            time.sleep(0.05)
        assert received and received[0]["audit_id"] == "wh-2"

        response = http_post(f"{backend.url}/decide", {"audit_id": "wh-2", "decision": "reject", "by": "ops-bot", "reason": "not now"})
        assert response["ok"] is True

        t.join(timeout=2)
        assert result_holder["decision"]["decision"] == "reject"
        assert result_holder["decision"]["by"] == "ops-bot"
    finally:
        backend.shutdown()
        receiver_server.shutdown()


def test_pending_endpoint_lists_current_items():
    receiver_url, received, receiver_server = start_receiver()
    backend = WebhookBackend(notify_url=receiver_url, port=0)
    try:
        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "wh-3", "action": "rotate_key", "args": {}, "pii_findings": [], "risk": "high"},),
        )
        t.start()
        time.sleep(0.2)

        items = http_get(f"{backend.url}/pending")
        assert len(items) == 1
        assert items[0]["audit_id"] == "wh-3"

        backend.resolve("wh-3", {"decision": "approve", "by": "x"})
        t.join(timeout=2)
        assert http_get(f"{backend.url}/pending") == []
    finally:
        backend.shutdown()
        receiver_server.shutdown()


def test_decide_on_unknown_audit_id_returns_404():
    receiver_url, received, receiver_server = start_receiver()
    backend = WebhookBackend(notify_url=receiver_url, port=0)
    try:
        try:
            http_post(f"{backend.url}/decide", {"audit_id": "nope", "decision": "approve"})
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        backend.shutdown()
        receiver_server.shutdown()


def test_unknown_get_path_returns_404():
    receiver_url, received, receiver_server = start_receiver()
    backend = WebhookBackend(notify_url=receiver_url, port=0)
    try:
        try:
            http_get(f"{backend.url}/not-a-real-path")
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        backend.shutdown()
        receiver_server.shutdown()


def test_unknown_post_path_returns_404():
    receiver_url, received, receiver_server = start_receiver()
    backend = WebhookBackend(notify_url=receiver_url, port=0)
    try:
        try:
            http_post(f"{backend.url}/not-a-real-path", {})
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        backend.shutdown()
        receiver_server.shutdown()


def test_malformed_decide_body_returns_400_not_a_crash():
    receiver_url, received, receiver_server = start_receiver()
    backend = WebhookBackend(notify_url=receiver_url, port=0)
    try:
        req = urllib.request.Request(
            f"{backend.url}/decide",
            data=b"{not valid json",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req)
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 400

        # server must still be alive afterward
        assert http_get(f"{backend.url}/pending") == []
    finally:
        backend.shutdown()
        receiver_server.shutdown()


def test_non_object_decide_body_returns_400():
    receiver_url, received, receiver_server = start_receiver()
    backend = WebhookBackend(notify_url=receiver_url, port=0)
    try:
        req = urllib.request.Request(
            f"{backend.url}/decide",
            data=b'"just a string"',
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req)
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        backend.shutdown()
        receiver_server.shutdown()


def test_dead_notify_url_does_not_block_or_raise():
    """A webhook that's down must not prevent the approval flow from
    proceeding once a decision eventually arrives some other way --
    same guarantee notifiers.safe_notify gives WebBackend."""
    backend = WebhookBackend(notify_url="http://127.0.0.1:1/nowhere", port=0, timeout=1.0)
    try:
        result_holder = {}

        def call():
            result_holder["decision"] = backend.wait_for_decision(
                {"audit_id": "wh-4", "action": "x", "args": {}, "pii_findings": [], "risk": "low"}
            )

        t = threading.Thread(target=call)
        t.start()
        time.sleep(1.5)  # let the failed notify attempt happen and be swallowed

        assert backend.resolve("wh-4", {"decision": "approve", "by": "manual"})
        t.join(timeout=2)
        assert result_holder["decision"]["decision"] == "approve"
    finally:
        backend.shutdown()
