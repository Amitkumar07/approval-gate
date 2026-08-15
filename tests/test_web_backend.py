import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate.backends import WebBackend


def make_backend():
    return WebBackend(host="127.0.0.1", port=0)


def http_get(url):
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())


def http_post(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_serves_index_page():
    backend = make_backend()
    try:
        with urllib.request.urlopen(backend.url + "/") as resp:
            assert resp.status == 200
            body = resp.read()
            assert b"approval-gate" in body
            assert b"<html>" in body
    finally:
        backend.shutdown()


def test_pending_appears_and_decision_unblocks():
    backend = make_backend()
    try:
        result_holder = {}

        def call():
            result_holder["decision"] = backend.wait_for_decision(
                {
                    "audit_id": "abc123",
                    "action": "send_email",
                    "args": {"to": "a@b.com"},
                    "pii_findings": [],
                    "risk": "medium",
                }
            )

        t = threading.Thread(target=call)
        t.start()

        # wait for the pending item to show up in the inbox
        for _ in range(50):
            items = http_get(backend.url + "/api/pending")
            if items:
                break
            time.sleep(0.05)
        assert items and items[0]["audit_id"] == "abc123"

        response = http_post(
            backend.url + "/api/decide",
            {"audit_id": "abc123", "decision": "approve", "by": "tester", "args": {"to": "a@b.com"}},
        )
        assert response["ok"] is True

        t.join(timeout=2)
        assert result_holder["decision"]["decision"] == "approve"
        assert result_holder["decision"]["by"] == "tester"

        # once decided, it should drop out of the pending list
        assert http_get(backend.url + "/api/pending") == []
    finally:
        backend.shutdown()


def test_decide_on_unknown_audit_id_returns_404():
    backend = make_backend()
    try:
        try:
            http_post(backend.url + "/api/decide", {"audit_id": "nope", "decision": "approve"})
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        backend.shutdown()


def test_malformed_json_body_returns_400_not_a_crash():
    backend = make_backend()
    try:
        req = urllib.request.Request(
            backend.url + "/api/decide",
            data=b"{not valid json",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req)
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 400

        # server must still be alive and serving after a bad request
        with urllib.request.urlopen(backend.url + "/") as resp:
            assert resp.status == 200
    finally:
        backend.shutdown()


def test_non_object_json_body_returns_400():
    backend = make_backend()
    try:
        req = urllib.request.Request(
            backend.url + "/api/decide",
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


def test_unknown_get_path_returns_404():
    backend = make_backend()
    try:
        try:
            http_get(backend.url + "/not-a-real-path")
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        backend.shutdown()


def test_unknown_post_path_returns_404():
    backend = make_backend()
    try:
        try:
            http_post(backend.url + "/not-a-real-path", {})
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        backend.shutdown()
