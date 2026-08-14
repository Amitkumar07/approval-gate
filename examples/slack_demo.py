"""
slack_demo.py
---------------
Shows SlackBackend end to end without a real Slack workspace: the
outbound `chat.postMessage` call is replaced with a print of the
Block Kit message (same trick as notifier_demo.py's fake webhook), and
the inbound half -- Slack's signed interaction POST when a button is
clicked -- runs for real against this backend's actual HTTP listener,
using the same request-signing scheme Slack itself uses.

To use this for real: create a Slack app, enable a bot token with
chat:write, invite it to a channel, enable Interactivity with the
Request URL pointing at wherever this backend's listener is reachable
from Slack's servers, and pass the real bot_token/signing_secret. See
the module docstring in approval_gate/backends/slack.py for the full
setup.

Run:
    python examples/slack_demo.py
"""

import hashlib
import hmac
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate import ApprovalGate
from approval_gate.backends import SlackBackend

DB_PATH = str(Path(__file__).resolve().parent / "demo_audit.db")
SIGNING_SECRET = "demo-signing-secret-change-me"


def fake_post_message(pending):
    """Stands in for the real Slack chat.postMessage call -- prints
    what would be posted instead of requiring a real bot token."""
    print("[fake Slack chat.postMessage] would post:")
    print(f"  Approval needed: {pending['action']} (risk: {pending['risk']})")
    print(f"  args: {pending['args']}")
    print("  [Approve]  [Reject]  <- these would be real Slack buttons")


def click_button(backend_url: str, audit_id: str, action_id: str, username: str) -> dict:
    """Simulates Slack POSTing a signed interaction payload -- exactly
    what happens when a real user clicks a button in the message above."""
    payload = {"actions": [{"action_id": action_id, "value": audit_id}], "user": {"username": username, "id": "U1"}}
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode("utf-8")
    timestamp = str(int(time.time()))
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    signature = "v0=" + hmac.new(SIGNING_SECRET.encode("utf-8"), base, hashlib.sha256).hexdigest()

    req = urllib.request.Request(
        f"{backend_url}/slack/interactions",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


if __name__ == "__main__":
    backend = SlackBackend(
        bot_token="xoxb-not-used-in-this-demo",
        signing_secret=SIGNING_SECRET,
        channel="#approvals",
        port=8645,
    )
    backend._post_message = fake_post_message  # swap out only the outbound call, see module docstring
    gate = ApprovalGate(db_path=DB_PATH, backend=backend)

    print(f"SlackBackend listening at: {backend.url}\n")

    import threading

    result_holder = {}

    def propose():
        result_holder["decision"] = gate.request_approval(
            action_name="rotate_api_key",
            args={"service": "payments-gateway", "current_key": "sk-liveAAAAAAAAAAAAAAAAAAAAAAAA"},
            risk="high",
        )

    t = threading.Thread(target=propose)
    t.start()
    time.sleep(0.5)  # let the "message" post before the button click arrives

    print("\nSimulating a teammate clicking 'Approve' in Slack...")
    response = click_button(backend.url, list(backend._list_pending())[0]["audit_id"], "approval_gate_approve", "amit")
    print(f"[Slack backend responds]: {response['text']}\n")

    t.join(timeout=5)
    decision = result_holder["decision"]
    print(f"Final decision: {'approved' if decision.approved else 'blocked'}")

    backend.shutdown()
