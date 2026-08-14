# approval-gate

**A drop-in approval gate and audit trail for LangGraph agents.**

Before your agent sends an email, deletes a record, or calls an API it
shouldn't be calling unsupervised, `approval-gate` pauses it, shows a
human exactly what it's about to do (with any sensitive data flagged),
and waits for approve / edit / reject — with a permanent record of
what happened.

```
agent proposes an action
        │
        ▼
  ┌─────────────────┐      flags emails, phone numbers, card numbers,
  │  scan for PII /  │ ──   API keys, etc. in the proposed action
  │     secrets      │
  └────────┬─────────┘
           ▼
  ┌─────────────────┐      built on LangGraph's native interrupt() --
  │   PAUSE & ASK    │ ──   no custom execution engine, no lock-in
  │   a human        │
  └────────┬─────────┘
           ▼
  approve / edit / reject ──→ action runs (or doesn't) ──→ logged forever
```

## Why this exists

LangGraph already gives you `interrupt()` for human-in-the-loop control.
That part's free and built in. What's missing is everything *around*
it: a place to log what was proposed, a way to flag sensitive data
before a human sees it (or before it sits in a log file), and a
consistent pattern so every tool in your codebase pauses the same way
instead of every team hand-rolling it slightly differently.

This is that missing layer. Nothing more.

**What this is not:** a full observability platform (see
[Langfuse](https://langfuse.com) / [LangSmith](https://smith.langchain.com)
for that), a vulnerability scanner for MCP servers, or an enterprise
compliance suite. If you need those, go get those — this is meant to
sit alongside them, not replace them.

## Install

```bash
pip install approval-gate
# optional, for richer name/location PII detection beyond the built-in
# regex checks:
pip install presidio-analyzer && python -m spacy download en_core_web_sm
```

## 60-second example

```python
from approval_gate import ApprovalGate

gate = ApprovalGate(db_path="audit.db")

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

Drive the graph from outside, resuming whenever it pauses:

```python
from langgraph.types import Command

result = graph.invoke(initial_state, config)
while "__interrupt__" in result:
    pending = result["__interrupt__"][0].value   # action, args, pii_findings, risk
    decision = ask_a_human_somehow(pending)        # {"decision": "approve"|"reject"|"edit", ...}
    result = graph.invoke(Command(resume=decision), config)
```

See `examples/email_agent_demo.py` for a fully working version you can
run right now with no API key — it includes a terminal-based approval
inbox (`approval_gate.cli.run_with_cli_approval`) so you can see the
whole loop in under a minute:

```bash
git clone <repo>
cd approval-gate
pip install -r requirements.txt
python examples/email_agent_demo.py
python examples/view_audit_log.py   # see what got logged
```

## How sensitive-data scanning works

Every proposed action's arguments are scanned before being shown to a
reviewer or written to the log:

- **Always on, zero setup:** regex checks for emails, phone numbers,
  credit card numbers (Luhn-validated), SSN-shaped numbers, and
  API-key-shaped strings (OpenAI, AWS, GitHub, Slack, Google patterns).
- **Optional, richer:** if `presidio-analyzer` is installed, it also
  runs [Microsoft Presidio](https://github.com/microsoft/presidio)
  for NLP-based name/location/organization detection.

Findings are masked before they're logged — you get `pr***...om`,
not the raw email address, sitting in your audit database.

## A note on how this is built on LangGraph internals

When a node calling `interrupt()` is resumed, LangGraph re-runs that
node function from the top — so any code before the `interrupt()` call
executes again. `ApprovalGate` handles this by giving every pending
action a deterministic ID (a hash of thread + action + args) and
upserting instead of inserting, so a resume-replay updates the same
row rather than creating a duplicate. If you're extending this code,
that's the one piece of LangGraph-specific subtlety to keep in mind.

## Roadmap

- [ ] Hosted dashboard (web inbox instead of terminal prompts, team
      accounts, longer-retention Postgres-backed log) — paid tier,
      self-hosted core stays free and open forever.
- [ ] Slack/email notification when something's waiting for review.
- [ ] Per-action policies (e.g. auto-approve low-risk, always pause on
      "delete", route by action type to a specific reviewer).

## License

MIT. Use it, modify it, ship it in commercial products. If it's useful
to you, a GitHub star helps other people find it — that's the entire
marketing budget for this project.
