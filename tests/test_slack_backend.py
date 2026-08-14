import hashlib
import hmac
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate.backends.slack import SlackBackend, _verify_slack_signature

SIGNING_SECRET = "test-signing-secret"


def make_backend(**overrides):
    kwargs = dict(
        bot_token="xoxb-fake-token-not-called-in-tests",
        signing_secret=SIGNING_SECRET,
        channel="#approvals",
        port=0,
    )
    kwargs.update(overrides)
    return SlackBackend(**kwargs)


def sign(secret, timestamp, body: bytes) -> str:
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


def post_interaction(url, payload_dict, timestamp=None, secret=SIGNING_SECRET, bad_signature=False):
    timestamp = timestamp or str(int(time.time()))
    body = urllib.parse.urlencode({"payload": json.dumps(payload_dict)}).encode("utf-8")
    signature = "v0=deadbeef" if bad_signature else sign(secret, timestamp, body)

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read())


def interaction_payload(audit_id, action_id="approval_gate_approve", username="amit"):
    return {
        "actions": [{"action_id": action_id, "value": audit_id}],
        "user": {"username": username, "id": "U123"},
    }


def test_verify_signature_roundtrip():
    ts = str(int(time.time()))
    body = b"payload=%7B%7D"
    sig = sign(SIGNING_SECRET, ts, body)
    assert _verify_slack_signature(SIGNING_SECRET, ts, body, sig)


def test_verify_signature_rejects_tampered_body():
    ts = str(int(time.time()))
    sig = sign(SIGNING_SECRET, ts, b"payload=original")
    assert not _verify_slack_signature(SIGNING_SECRET, ts, b"payload=tampered", sig)


def test_verify_signature_rejects_wrong_secret():
    ts = str(int(time.time()))
    body = b"payload=%7B%7D"
    sig = sign("wrong-secret", ts, body)
    assert not _verify_slack_signature(SIGNING_SECRET, ts, body, sig)


def test_verify_signature_rejects_stale_timestamp():
    old_ts = str(int(time.time()) - 60 * 10)  # 10 minutes old, beyond the 5 minute window
    body = b"payload=%7B%7D"
    sig = sign(SIGNING_SECRET, old_ts, body)
    assert not _verify_slack_signature(SIGNING_SECRET, old_ts, body, sig)


def test_interaction_with_bad_signature_returns_400():
    backend = make_backend()
    try:
        try:
            post_interaction(f"{backend.url}/slack/interactions", interaction_payload("nope"), bad_signature=True)
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        backend.shutdown()


def test_approve_button_click_resolves_pending_action():
    backend = make_backend()
    # skip the real Slack post -- tests exercise the interaction/verification
    # path, not the outbound API call (which needs a real bot token)
    backend._post_message = lambda pending: None
    try:
        result_holder = {}

        def call():
            result_holder["decision"] = backend.wait_for_decision(
                {"audit_id": "sl-1", "action": "send_email", "args": {}, "pii_findings": [], "risk": "medium"}
            )

        t = threading.Thread(target=call)
        t.start()
        time.sleep(0.3)

        status, body = post_interaction(f"{backend.url}/slack/interactions", interaction_payload("sl-1", "approval_gate_approve", "amit"))
        assert status == 200
        assert "Recorded" in body["text"]

        t.join(timeout=2)
        assert result_holder["decision"]["decision"] == "approve"
        assert result_holder["decision"]["by"] == "amit"
    finally:
        backend.shutdown()


def test_reject_button_click_resolves_as_reject():
    backend = make_backend()
    backend._post_message = lambda pending: None
    try:
        result_holder = {}

        def call():
            result_holder["decision"] = backend.wait_for_decision(
                {"audit_id": "sl-2", "action": "delete_records", "args": {}, "pii_findings": [], "risk": "high"}
            )

        t = threading.Thread(target=call)
        t.start()
        time.sleep(0.3)

        post_interaction(f"{backend.url}/slack/interactions", interaction_payload("sl-2", "approval_gate_reject", "amit"))
        t.join(timeout=2)
        assert result_holder["decision"]["decision"] == "reject"
    finally:
        backend.shutdown()


def test_click_on_already_decided_action_returns_200_not_error():
    """Slack expects 200 even for a stale/duplicate click -- a non-200
    surfaces a delivery-failed error to the user in Slack, which is
    misleading for 'someone else already decided this.'"""
    backend = make_backend()
    backend._post_message = lambda pending: None
    try:
        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "sl-3", "action": "x", "args": {}, "pii_findings": [], "risk": "low"},),
        )
        t.start()
        time.sleep(0.3)

        post_interaction(f"{backend.url}/slack/interactions", interaction_payload("sl-3", "approval_gate_approve", "amit"))
        t.join(timeout=2)

        status, body = post_interaction(f"{backend.url}/slack/interactions", interaction_payload("sl-3", "approval_gate_approve", "amit"))
        assert status == 200
        assert "already decided" in body["text"].lower()
    finally:
        backend.shutdown()


def test_malformed_payload_returns_400():
    backend = make_backend()
    try:
        ts = str(int(time.time()))
        body = b"not=a+valid+slack+payload"
        sig = sign(SIGNING_SECRET, ts, body)
        req = urllib.request.Request(
            f"{backend.url}/slack/interactions",
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": sig,
            },
        )
        try:
            urllib.request.urlopen(req)
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        backend.shutdown()
