import hashlib
import hmac
import io
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate.backends.slack import SlackBackend, _blocks_for, _verify_slack_signature

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


def test_verify_signature_rejects_non_numeric_timestamp():
    # a forged/garbage timestamp header, not just an old one
    assert not _verify_slack_signature(SIGNING_SECRET, "not-a-number", b"payload=%7B%7D", "v0=whatever")


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


def test_unknown_get_path_returns_404():
    backend = make_backend()
    try:
        try:
            urllib.request.urlopen(f"{backend.url}/not-a-real-path")
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        backend.shutdown()


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


def test_pending_endpoint_lists_current_items():
    backend = make_backend()
    backend._post_message = lambda pending: None
    try:
        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "sl-4", "action": "restart_service", "args": {}, "pii_findings": [], "risk": "medium"},),
        )
        t.start()
        time.sleep(0.3)

        with urllib.request.urlopen(f"{backend.url}/pending") as resp:
            items = json.loads(resp.read())
        assert len(items) == 1
        assert items[0]["audit_id"] == "sl-4"

        backend.resolve("sl-4", {"decision": "approve", "by": "t"})
        t.join(timeout=2)

        with urllib.request.urlopen(f"{backend.url}/pending") as resp:
            assert json.loads(resp.read()) == []
    finally:
        backend.shutdown()


def test_unrecognized_action_id_returns_400():
    backend = make_backend()
    backend._post_message = lambda pending: None
    try:
        t = threading.Thread(
            target=backend.wait_for_decision,
            args=({"audit_id": "sl-5", "action": "x", "args": {}, "pii_findings": [], "risk": "low"},),
        )
        t.start()
        time.sleep(0.3)

        try:
            post_interaction(f"{backend.url}/slack/interactions", interaction_payload("sl-5", "some_other_action_id", "amit"))
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 400

        backend.resolve("sl-5", {"decision": "approve", "by": "t"})
        t.join(timeout=2)
    finally:
        backend.shutdown()


def test_post_message_failure_does_not_block_wait_for_decision():
    """Same policy as every other backend -- a failed outbound post
    (network error, bad token, Slack down) must not prevent the flow
    from proceeding once a decision arrives some other way. Simulated
    by intercepting urlopen rather than hitting the real network, so
    this stays fast and doesn't depend on external connectivity."""
    backend = make_backend()
    real_urlopen = urllib.request.urlopen

    def failing_urlopen(req, *args, **kwargs):
        raise urllib.error.URLError("simulated network failure")

    try:
        urllib.request.urlopen = failing_urlopen
        result_holder = {}

        def call():
            result_holder["decision"] = backend.wait_for_decision(
                {"audit_id": "sl-6", "action": "x", "args": {}, "pii_findings": [], "risk": "low"}
            )

        t = threading.Thread(target=call)
        t.start()
        time.sleep(0.3)

        assert backend.resolve("sl-6", {"decision": "approve", "by": "manual"})
        t.join(timeout=2)
        assert result_holder["decision"]["decision"] == "approve"
    finally:
        urllib.request.urlopen = real_urlopen
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


def test_blocks_for_includes_action_risk_and_buttons():
    blocks = _blocks_for(
        {
            "audit_id": "sl-blocks",
            "action": "delete_records",
            "args": {"table": "support_tickets"},
            "pii_findings": [],
            "risk": "high",
        }
    )
    section_text = blocks[0]["text"]["text"]
    assert "delete_records" in section_text
    assert "high" in section_text
    assert "support_tickets" in section_text

    buttons = blocks[1]["elements"]
    assert {b["action_id"] for b in buttons} == {"approval_gate_approve", "approval_gate_reject"}
    assert all(b["value"] == "sl-blocks" for b in buttons)
    assert blocks[1]["block_id"] == "approval-gate:sl-blocks"


def test_blocks_for_flags_pii_findings():
    blocks = _blocks_for(
        {
            "audit_id": "sl-blocks-2",
            "action": "send_email",
            "args": {"to": "a@b.com"},
            "pii_findings": [{"type": "email", "field": "to", "value_masked": "a***b", "source": "regex"}],
            "risk": "medium",
        }
    )
    assert "1 sensitive field" in blocks[0]["text"]["text"]


def test_blocks_for_handles_no_args():
    blocks = _blocks_for({"audit_id": "x", "action": "noop", "args": {}, "pii_findings": [], "risk": "low"})
    assert "(no arguments)" in blocks[0]["text"]["text"]


def test_post_message_calls_slacks_chat_postmessage_with_bot_token():
    """Exercises _post_message's actual request-building (not stubbed
    out, unlike the interaction tests above) by intercepting
    urllib.request.urlopen -- confirms the real code path sends the
    right URL, auth header, and payload shape, without needing a real
    Slack workspace."""
    backend = make_backend()
    captured = {}
    real_urlopen = urllib.request.urlopen

    def fake_urlopen(req, *args, **kwargs):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data)
        return io.BytesIO(b'{"ok": true}')

    try:
        urllib.request.urlopen = fake_urlopen
        backend._post_message({"audit_id": "sl-post", "action": "send_email", "args": {}, "pii_findings": [], "risk": "low"})

        assert captured["url"] == "https://slack.com/api/chat.postMessage"
        assert captured["headers"]["authorization"] == "Bearer xoxb-fake-token-not-called-in-tests"
        assert captured["body"]["channel"] == "#approvals"
        assert len(captured["body"]["blocks"]) == 2
    finally:
        urllib.request.urlopen = real_urlopen
        backend.shutdown()
