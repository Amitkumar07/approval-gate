# Adding a new approval channel

approval-gate ships four channels today: browser (`WebBackend`),
bring-your-own-system (`WebhookBackend`), email (`EmailBackend`), and
Slack (`SlackBackend`). "Every possible channel" — Teams, Discord,
SMS/Twilio, PagerDuty, a CLI daemon, whatever your team actually uses —
isn't something one maintainer builds ahead of demand. It's something
this template makes mechanical enough that a PR adding one is a few
hours of work, not a redesign.

Read `approval_gate/backends/webhook.py` first. It's the shortest of
the four and shows the whole shape without a third-party API's
specifics (SMTP, Slack's signing scheme) in the way.

## The contract

A backend is a class implementing one method:

```python
from approval_gate.backends.base import Backend

class YourBackend(Backend):
    def wait_for_decision(self, pending: dict) -> dict:
        ...
```

`pending` always has `audit_id`, `action`, `args`, `pii_findings`,
`risk`. Block however makes sense for your channel, then return (or
raise/suspend, if that's how your framework pauses — see
`LangGraphBackend`) a dict shaped like:

```python
{"decision": "approve" | "reject" | "edit", "by": str, "reason": str, "args": dict}
```

Only `decision` is required; `ApprovalGate` fills in sane defaults for
the rest (see `core.py`).

## If your channel is asynchronous (almost certainly)

Everything except `BlockingBackend` and `LangGraphBackend` needs to:
send/post/notify, then wait for a reply that arrives on a different
thread (an HTTP request, typically) — a click, a button, a reply email.

Don't reinvent the "shared pending dict + per-audit_id queue, guarded
by a lock" plumbing for this. Inherit `_PendingQueueBackend` from
`approval_gate/backends/_queue_base.py`:

```python
from ._queue_base import _PendingQueueBackend
from .base import Backend

class YourBackend(_PendingQueueBackend, Backend):
    def __init__(self, ...):
        super().__init__()
        # ... start whatever listener your channel needs ...

    def wait_for_decision(self, pending: dict) -> dict:
        audit_id = pending["audit_id"]
        result_queue = self._register(audit_id, pending)
        self._notify_somehow(pending)  # send the message/email/webhook
        try:
            return result_queue.get()  # blocks until resolve() is called
        finally:
            self._unregister(audit_id)
```

Whatever receives the external reply (an HTTP handler, typically) calls
`backend.resolve(audit_id, decision_dict)` to deliver it. This exists
because a hand-rolled version of this pattern is exactly what produced
a real database-corrupting concurrency bug early in this project — see
the git history around `audit.py`'s `threading.RLock` fix. Don't
re-solve "shared mutable state across threads" per backend.

## Checklist

A PR adding a channel should have all of these — look at any of the
four existing backends for the pattern:

- [ ] **The backend itself**, `approval_gate/backends/<name>.py`, with
      a module docstring explaining what problem this channel solves
      and any non-obvious tradeoff (see `email.py`'s docstring on why
      a signed link isn't the same as authenticating the clicker).
- [ ] **Registered** in `approval_gate/backends/__init__.py`'s imports
      and `__all__`. If it needs a heavy/optional dependency the way
      `LangGraphBackend` needs `langgraph`, use the same lazy
      `__getattr__` pattern so importing `approval_gate` doesn't
      require it.
- [ ] **A failed notify must never block or fail the approval flow.**
      If your channel's "tell someone" step can fail (network error,
      API down), catch it, print/log it, and keep waiting for a
      decision to arrive some other way — same policy as every
      existing backend (`safe_notify`, `WebhookBackend._notify`,
      `EmailBackend._send_email`, `SlackBackend._post_message`).
- [ ] **If anything external can call back into your listener**
      (a webhook POST, a button click, a clicked link), verify it.
      `SlackBackend` verifies Slack's request signature;
      `EmailBackend` HMAC-signs its own links. An unauthenticated
      callback endpoint that can resolve arbitrary pending actions is
      a real vulnerability, not a nice-to-have.
- [ ] **Tests that hit real HTTP**, not mocks, wherever practical —
      every existing backend's test file runs an actual local server
      and drives it with real requests. `test_slack_backend.py` is a
      good model if your channel needs request-signing tests
      (tampered body, wrong secret, replay/stale timestamp all get
      their own test).
- [ ] **A runnable example** in `examples/`, runnable with *no* real
      credentials for the third-party service — stub out just the
      outbound call (see `slack_demo.py`'s `fake_post_message`,
      `email_demo.py`'s fake SMTP server) so `python
      examples/your_backend_demo.py` works out of the box. This is
      what a first-time evaluator runs before reading any code; a demo
      that needs a Slack workspace or SMTP credentials to try defeats
      the point.
- [ ] **A README section** under "Backends," matching the style of
      the existing ones (short intro, code snippet, link to the demo).
- [ ] `pytest tests/ -q` passes with no new warnings.

## Scope reminder

Keep the channel itself thin. Routing logic, retry policies, and
multi-reviewer quorum belong in `policy.py`, not duplicated inside
every backend. If you're adding a feature that isn't really about
"how does a decision get back to ApprovalGate," it's probably not a
backend PR — open an issue first and say what you're trying to do.
