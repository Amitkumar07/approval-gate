"""
notifiers.py
------------
A review sitting in a browser tab nobody's looking at doesn't help --
someone needs to be told a decision is waiting. A Notifier is called
once per pending action, right after it's queued in WebBackend, with
the pending payload and a URL a human can click through to review it.

This is intentionally just a `Callable[[dict, str], None]` -- any
function matching that shape works as a notifier, no base class
required:

    def my_notifier(pending: dict, review_url: str) -> None:
        my_paging_system.send(f"{pending['action']} needs review: {review_url}")

    backend = WebBackend(port=8642, notifier=my_notifier)

SlackNotifier below is the one built-in implementation, using an
incoming webhook URL (https://api.slack.com/messaging/webhooks) --
no slack-sdk dependency, just a POST via urllib.

Notifier failures never block or fail the approval flow: if a webhook
is down, that's a paging problem to fix, not a reason to silently
prevent a human from being asked to review something risky. Exceptions
are caught and printed, not raised.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable, Protocol

Notifier = Callable[[dict[str, Any], str], None]


class SupportsNotify(Protocol):
    def __call__(self, pending: dict[str, Any], review_url: str) -> None: ...


class SlackNotifier:
    """Posts to a Slack incoming webhook when an action needs review.

    Create a webhook at https://api.slack.com/messaging/webhooks and
    pass its URL here -- nothing else to configure.
    """

    def __init__(self, webhook_url: str, timeout: float = 5.0):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def __call__(self, pending: dict[str, Any], review_url: str) -> None:
        findings = pending.get("pii_findings") or []
        flag = f" :warning: {len(findings)} sensitive field(s) flagged" if findings else ""
        text = (
            f"*Approval needed:* `{pending['action']}` (risk: {pending['risk']}){flag}\n"
            f"<{review_url}|Review in approval-gate inbox>"
        )
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()


def safe_notify(notifier: Notifier, pending: dict[str, Any], review_url: str) -> None:
    """Run a notifier, swallowing (and printing) any error -- see module
    docstring for why a broken notifier must never block the approval flow."""
    try:
        notifier(pending, review_url)
    except Exception as e:  # noqa: BLE001 -- deliberately broad, see docstring
        print(f"[approval_gate] notifier failed (ignored): {e!r}")
