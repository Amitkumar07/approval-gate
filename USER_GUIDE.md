# approval-gate user guide

This is a task-oriented walkthrough. If you want the pitch and a
60-second example, read [README.md](README.md) first. If you want to
add a new approval channel, read
[BACKEND_TEMPLATE.md](BACKEND_TEMPLATE.md). This guide is for
everything in between: getting it running, wiring up the channel you
actually want, and the day-to-day of using it.

## Contents

- [1. Install](#1-install)
- [2. Core concepts](#2-core-concepts)
- [3. Quickstart](#3-quickstart)
- [4. Choosing a channel](#4-choosing-a-channel)
  - [4.1 Browser (`WebBackend`)](#41-browser-webbackend)
  - [4.2 Bring your own system (`WebhookBackend`)](#42-bring-your-own-system-webhookbackend)
  - [4.3 Email (`EmailBackend`)](#43-email-emailbackend)
  - [4.4 Slack (`SlackBackend`)](#44-slack-slackbackend)
  - [4.5 LangGraph (`LangGraphBackend`)](#45-langgraph-langgraphbackend)
  - [4.6 A plain Python callback (`BlockingBackend`)](#46-a-plain-python-callback-blockingbackend)
- [5. Cutting down on approval fatigue with policies](#5-cutting-down-on-approval-fatigue-with-policies)
- [6. Getting pinged when something needs review](#6-getting-pinged-when-something-needs-review)
- [7. Reading the audit trail](#7-reading-the-audit-trail)
- [8. How PII/secret scanning works](#8-how-piisecret-scanning-works)
- [9. Troubleshooting](#9-troubleshooting)
- [10. Reference: `request_approval` and `Decision`](#10-reference-request_approval-and-decision)

## 1. Install

```bash
pip install approval-gate
```

The core package (`ApprovalGate`, the audit log, PII scanning,
`BlockingBackend`) has **no required dependencies**. Add an extra only
for what you're using:

```bash
pip install "approval-gate[langgraph]"   # if you're using LangGraphBackend
pip install presidio-analyzer            # optional, richer PII detection
python -m spacy download en_core_web_sm  # required by presidio-analyzer
```

`EmailBackend`, `SlackBackend`, and `WebhookBackend` use only the
Python standard library (`smtplib`, `urllib`, `http.server`) — nothing
extra to install for those.

## 2. Core concepts

Three things work together:

- **`ApprovalGate`** — the object you call. `gate.request_approval(...)`
  scans the arguments for sensitive data, writes a row to the audit
  log, then pauses until a decision arrives. It returns a `Decision`
  telling you whether to proceed, and with what (possibly edited) args.
- **A `Backend`** — *how* the pause happens and *how* a decision comes
  back. This is the piece you pick based on where your team actually
  wants to review things (browser, Slack, email, your own system, or
  driven straight from LangGraph). Swapping backends never changes how
  you call `request_approval`.
- **A `Policy`** (optional) — runs before the backend is even touched.
  Lets you skip the human step entirely for low-risk actions, or route
  high-risk ones to a specific reviewer.

Everything gets written to a SQLite **audit log** regardless of which
backend or policy you use — that part is not configurable, by design.

## 3. Quickstart

The fastest way to see the whole loop, no setup required:

```bash
git clone <repo>
cd approval-gate
pip install -r requirements.txt
python examples/plain_python_demo.py
```

This runs a terminal-based approval prompt with no LangGraph, no
credentials, nothing to configure. Every other example in `examples/`
follows the same "runs immediately, no real credentials needed" rule —
see [Section 4](#4-choosing-a-channel) for which one matches your
actual setup, and run `python examples/view_audit_log.py` afterward to
see what got recorded.

For your own code, the shape is always this:

```python
from approval_gate import ApprovalGate

gate = ApprovalGate(db_path="audit.db")  # defaults to LangGraphBackend

def send_email(to: str, subject: str, body: str) -> str:
    decision = gate.request_approval(
        action_name="send_email",
        args={"to": to, "subject": subject, "body": body},
        risk="medium",
    )
    if not decision.approved:
        return f"BLOCKED: {decision.reason}"

    result = really_send_email(**decision.args)  # decision.args reflects any edits
    gate.log_result(decision.audit_id, result)
    return result
```

If you're not using LangGraph, pass a different `backend=` — see
below.

## 4. Choosing a channel

Pick based on where your team wants to actually see and act on these
requests. You can mix channels across different `ApprovalGate`
instances in the same codebase if different actions warrant different
review paths.

### 4.1 Browser (`WebBackend`)

Best for: active development, a team that's watching a dashboard, or
when you want reviewers to be able to **edit** the proposed arguments
before approving (the only channel that supports this).

```python
from approval_gate import ApprovalGate
from approval_gate.backends import WebBackend

backend = WebBackend(port=8642)
gate = ApprovalGate(db_path="audit.db", backend=backend)
print(f"Review inbox running at {backend.url}")
# ... call gate.request_approval(...) as usual, from any thread ...
backend.shutdown()  # when your program is done
```

Open `backend.url` in a browser. Pending actions show up as rows;
click one to expand it, edit any field inline, and Approve / Approve
with edits / Reject. Multiple pending actions queue up and are all
shown at once — this backend is the only one that comfortably handles
several concurrent reviews, since the inbox lists every pending item.

Try it: `python examples/web_inbox_demo.py`

### 4.2 Bring your own system (`WebhookBackend`)

Best for: teams with an existing ticket queue, internal admin panel,
or on-call tool who don't want to adopt Slack or email specifically
for this.

```python
from approval_gate import ApprovalGate
from approval_gate.backends import WebhookBackend

backend = WebhookBackend(
    notify_url="https://internal-tools.example.com/approval-gate/incoming",
    host="127.0.0.1", port=8643,
)
gate = ApprovalGate(db_path="audit.db", backend=backend)
```

Every pending action gets POSTed as JSON to `notify_url`, with a
`callback_url` field included. Your system decides however it wants,
then POSTs the decision back:

```bash
curl -X POST "$callback_url" -H "Content-Type: application/json" -d '{
  "audit_id": "...", "decision": "approve", "by": "your-system", "reason": ""
}'
```

`GET {backend.url}/pending` lists everything currently waiting, if your
system wants to poll instead of relying on the initial POST.

Try it: `python examples/webhook_demo.py`

### 4.3 Email (`EmailBackend`)

Best for: slower-moving approvals where nobody needs to be watching
anything in real time — the email just sits in an inbox.

```python
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
```

The reviewer gets an email with Approve/Reject links. Clicking one
opens a confirmation page (so a mail scanner pre-fetching the link
can't silently decide on the recipient's behalf); confirming resolves
it. Links are HMAC-signed so they can't be edited into approving a
different action.

**Two things to get right:**
- `secret` — generate one random string once (e.g.
  `python -c "import secrets; print(secrets.token_hex(32))"`) and keep
  it stable. Changing it invalidates every link already sent.
- `public_base_url` — must be a URL the reviewer's *browser* can reach
  when they click the link, not `localhost` unless the reviewer is on
  the same machine. In production this typically means putting this
  listener behind a real domain/ingress.

`args` cannot be edited from an email link — only approve/reject. Use
`WebBackend` if in-place editing matters.

Try it: `python examples/email_demo.py` (uses a local fake SMTP server,
no real mail account needed)

### 4.4 Slack (`SlackBackend`)

Best for: teams that already live in Slack and want to resolve
approvals without switching tools.

One-time setup in your Slack app config at
[api.slack.com/apps](https://api.slack.com/apps):

1. Add a bot token with the `chat:write` scope; invite the bot to the
   channel you want approvals posted to.
2. Enable **Interactivity**, and set the **Request URL** to wherever
   this backend's listener will be reachable from Slack's servers —
   this needs a public URL (a tunnel like `ngrok` in development, real
   ingress in production). `localhost` alone is not reachable by Slack.
3. Copy the bot token (`xoxb-...`) and the app's **Signing Secret**.

```python
from approval_gate import ApprovalGate
from approval_gate.backends import SlackBackend

backend = SlackBackend(
    bot_token="xoxb-...",
    signing_secret="...",
    channel="#approvals",
    host="0.0.0.0", port=8645,
)
gate = ApprovalGate(db_path="audit.db", backend=backend)
```

Every pending action posts as a message with Approve/Reject buttons.
Clicking resolves it immediately — every click is cryptographically
verified against Slack's signing scheme before anything is trusted, so
this listener can't be tricked into approving things by a forged
request. `args` aren't editable from Slack, same tradeoff as email.

Try it: `python examples/slack_demo.py` (fakes the outbound Slack post,
but the inbound signed interaction is real)

### 4.5 LangGraph (`LangGraphBackend`)

This is the **default** — if you don't pass `backend=`, this is what
you get. Use it if your agent is a LangGraph graph and you're driving
it with `interrupt()`/`Command(resume=...)` already.

```python
from approval_gate import ApprovalGate

gate = ApprovalGate(db_path="audit.db")  # LangGraphBackend by default
```

Drive the graph from outside, resuming whenever it pauses:

```python
from langgraph.types import Command

result = graph.invoke(initial_state, config)
while "__interrupt__" in result:
    pending = result["__interrupt__"][0].value   # action, args, pii_findings, risk
    decision = ask_a_human_somehow(pending)        # {"decision": "approve"|"reject"|"edit", ...}
    result = graph.invoke(Command(resume=decision), config)
```

`approval_gate.cli.run_with_cli_approval(graph, initial_state, config)`
wraps this loop with a terminal prompt, if you just want something
working immediately.

Try it: `python examples/email_agent_demo.py`

**One LangGraph-specific thing to know:** when a node calling
`interrupt()` is resumed, LangGraph re-runs that node function from the
top, so any code you wrote *before* the `interrupt()` call runs again.
`ApprovalGate` handles this automatically (deterministic audit-row IDs
+ upsert), but if you're doing anything unusual before calling
`request_approval`, keep this replay behavior in mind.

### 4.6 A plain Python callback (`BlockingBackend`)

Best for: a script, a notebook, a raw tool-calling loop — anywhere
with no framework and no need for anything async.

```python
from approval_gate import ApprovalGate
from approval_gate.backends import BlockingBackend

def ask_a_human(pending: dict) -> dict:
    print(pending["action"], pending["args"])
    choice = input("approve/reject? ")
    return {"decision": "approve" if choice == "approve" else "reject", "by": "amit"}

gate = ApprovalGate(db_path="audit.db", backend=BlockingBackend(ask_a_human))
```

Your `reviewer` function runs synchronously right there in the calling
thread — no pause/resume plumbing, no server. This is also the
simplest reference implementation to read if you're building a new
`Backend` yourself.

## 5. Cutting down on approval fatigue with policies

Pausing for a human on *every* call doesn't scale — past a handful of
action types, reviewers start rubber-stamping everything, which
defeats the point. `policy=` runs before the backend is touched at all
and can skip the human step, or route the request to a specific
reviewer without skipping it:

```python
from approval_gate import ApprovalGate
from approval_gate.policy import Rule, RulePolicy

policy = RulePolicy([
    Rule(risk="low", has_pii=False, auto_approve=True, name="auto-low-risk"),
    Rule(action_prefix="delete_", route_to="oncall-reviewer"),
    Rule(action_name="wire_transfer", auto_reject=True, name="block-wire-transfers"),
])
gate = ApprovalGate(db_path="audit.db", policy=policy)
```

Rules are evaluated **in order**, first match wins:

| Field | Matches on |
|---|---|
| `action_name` | exact action name |
| `action_prefix` | action name starting with this string |
| `risk` | the `risk=` passed to `request_approval` |
| `has_pii` | whether any PII/secret findings were detected |

And what a matching rule does:

- `auto_approve=True` — decided instantly, `decided_by` in the audit
  log is `"policy:<rule-name>"`. Nothing shows up in any review
  channel, no notifier fires. Still fully logged.
- `auto_reject=True` — same, but rejected. Use this for actions that
  should never be allowed to run unattended, e.g. "never auto-approve,
  but also never even ask — just block it."
- `route_to="..."` — does **not** auto-decide. The action still goes
  to a human, but the pending payload gets a `route_to` field your
  backend/notifier can use to direct it (e.g. `WebBackend`'s inbox
  shows it as a tag; a `WebhookBackend` receiver could route on it).

If nothing matches, or you didn't pass `policy=` at all, every action
goes to a human — the safe default.

For anything a declarative rule can't express, pass a plain callable
instead — `(pending: dict) -> Optional[dict]`, same shape as a `Rule`
list's effective behavior. It must be a pure function: no side
effects, because (like everything before a pause) it can re-run on a
LangGraph resume-replay.

Try it: `python examples/policy_demo.py`

## 6. Getting pinged when something needs review

A pending action sitting in a browser tab nobody has open doesn't
help. `notifier=` on `WebBackend` fires the moment something's queued:

```python
from approval_gate.backends import WebBackend
from approval_gate.notifiers import SlackNotifier

backend = WebBackend(
    port=8642,
    notifier=SlackNotifier(webhook_url="https://hooks.slack.com/services/..."),
)
```

`SlackNotifier` posts to a Slack **incoming webhook** — this is
one-way (it can only send a link), distinct from `SlackBackend`
(Section 4.4), which is two-way and resolves the decision itself. Use
a `Notifier` when you want a *ping* into some other review surface;
use `SlackBackend` when you want the decision to happen in Slack
directly.

Write your own notifier for anything else — it's just a function:

```python
def my_notifier(pending: dict, review_url: str) -> None:
    my_paging_system.send(f"{pending['action']} needs review: {review_url}")

backend = WebBackend(port=8642, notifier=my_notifier)
```

A broken notifier (webhook down, bad credentials) is caught and
logged, never allowed to block the approval flow — the pending action
just sits there waiting, same as if there were no notifier.

Try it: `python examples/notifier_demo.py`

## 7. Reading the audit trail

Every action proposed — approved, rejected, edited, still pending — is
a row in the SQLite database at whatever `db_path=` you gave
`ApprovalGate`. Rows are never deleted.

```python
from approval_gate import AuditLog

log = AuditLog("audit.db")
for record in log.list_all():
    print(record.action_name, record.status, record.decided_by)
```

Or from the command line — `examples/view_audit_log.py` is a small,
readable script you can copy and point at your own `db_path` (it
hardcodes a demo path; there's no `--db` flag, just edit the `DB_PATH`
constant at the top):

```bash
python examples/view_audit_log.py
```

Each `AuditRecord` has: `id`, `thread_id`, `action_name`, `args`,
`pii_findings`, `risk`, `status` (`pending`/`approved`/`rejected`/
`edited`/`error`), `decided_by`, `decision_reason`, `final_args`
(only set if edited), `result`, and timestamps (`created_at`,
`decided_at`, `completed_at`).

`log.list_pending()` returns only rows still awaiting a decision —
useful for a dashboard or a "what's stuck" check.

## 8. How PII/secret scanning works

Every string-valued field in `args` is scanned before it's shown to a
reviewer or written to the log:

- **Always on:** regex checks for emails, phone numbers, credit card
  numbers (Luhn-validated, so `1234 5678 9012 3456` is *not* flagged
  but a real test card number is), SSN-shaped numbers, and API-key
  shaped strings (OpenAI, AWS, GitHub, Slack, Google patterns).
- **Optional, richer:** if `presidio-analyzer` is installed
  (`pip install presidio-analyzer` + a spaCy model), it also runs for
  NLP-based name/location/organization detection.

Findings are **masked** before they're shown or logged — you get
`pr***...om`, never the raw value, even in your own audit database.
Check `approval_gate.pii.using_presidio()` if you want to confirm
which mode is active.

## 9. Troubleshooting

**`ModuleNotFoundError: No module named 'langgraph'`** — you're using
the default backend without installing the extra. Either
`pip install "approval-gate[langgraph]"`, or pass a different
`backend=` if you're not actually using LangGraph.

**A Slack/email/webhook backend never resolves, `request_approval`
just hangs.** There is no timeout by design — see `Backend`'s
docstring on why "block" is deliberately open-ended. Check:
- Is the listener actually reachable from wherever the click/callback
  is coming from? (`localhost` isn't reachable from Slack's servers or
  a real mail client — you need a public URL there.)
- For `SlackBackend`: is the Request URL in your Slack app config
  actually pointing at this process?
- `GET {backend.url}/pending` (Web/Webhook/Email/Slack backends all
  expose this) shows what's currently stuck waiting.

**"Invalid or tampered link" / "invalid Slack signature".** The
`secret`/`signing_secret` used to verify doesn't match what signed the
request. For email, this happens if `secret` changed since the email
was sent — links don't survive a secret rotation. For Slack, double
check you copied the *Signing Secret*, not the bot token, into
`signing_secret=`.

**Two reviewers click at the same time / a link gets clicked twice.**
The second one gets a 404 (webhook/email) or a friendly "already
decided" response (Slack) — whichever decision arrives first at the
backend wins; the audit log records that one.

**Duplicate rows in the audit log after a LangGraph resume.** This
shouldn't happen — audit rows are keyed by a deterministic hash of
`(thread_id, action_name, args)` specifically so a resume-replay
upserts instead of duplicating (see `audit.py`'s module docstring). If
you're seeing duplicates, check that `thread_id` is actually stable
across the resume (it defaults to `ApprovalGate`'s `default_thread_id`
if you don't pass one explicitly to `request_approval`).

**Concurrent `request_approval()` calls from multiple threads.** This
is supported and tested — `AuditLog` locks around every access
specifically to make this safe (see `audit.py`). If you're doing
parallel tool calls from an agent, this is fine.

## 10. Reference: `request_approval` and `Decision`

```python
gate.request_approval(
    action_name: str,       # e.g. "send_email" -- shown to reviewers, matched by Rule
    args: dict[str, Any],   # the proposed arguments -- scanned for PII, editable by some backends
    risk: str = "high",     # "low" | "medium" | "high" -- your label, matched by Rule
    thread_id: str | None = None,  # groups related actions; defaults to ApprovalGate's default_thread_id
) -> Decision
```

Returns a `Decision`:

```python
Decision(
    approved: bool,   # True for "approve" or "edit"
    args: dict,        # final args -- reflects edits if the reviewer changed anything
    reason: str,        # only set when rejected
    audit_id: str,       # pass this to gate.log_result(...) after you act on the decision
)
```

After acting on an approved decision, call
`gate.log_result(decision.audit_id, result, error=False)` to record
the outcome in the audit trail — this is a separate step because the
result often isn't known until after you've actually performed the
action.
