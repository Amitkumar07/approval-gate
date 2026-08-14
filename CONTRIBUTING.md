# Contributing to approval-gate

Thanks for considering it. This project is small on purpose -- see the
"Why this exists" section in the README before proposing something
that expands scope (observability platform, MCP scanner, compliance
suite). If in doubt, open an issue first and describe the approach;
that's much cheaper than a PR that has to be re-scoped.

## Setup

```bash
git clone <repo>
cd approval-gate
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e . pytest
pytest tests/ -q
```

The core package has no required dependencies -- `requirements.txt`
pulls in `langgraph`/`langchain-core` for `LangGraphBackend` and the
examples that use it. If your change doesn't touch that backend, you
don't need them installed to test it.

## Where things live

- `approval_gate/core.py` -- `ApprovalGate`, the orchestration: scan
  for PII, check policy, wait on a backend, write the audit record.
- `approval_gate/audit.py` -- the SQLite audit log. Read the module
  docstring before touching this file; the deterministic-id upsert
  exists specifically to survive LangGraph's resume-replay behavior,
  and it's easy to accidentally break that property.
- `approval_gate/pii.py` -- regex + optional Presidio scanning.
- `approval_gate/backends/` -- pluggable pause/resume mechanisms
  (`Backend` protocol, `LangGraphBackend`, `BlockingBackend`,
  `WebBackend`).
- `approval_gate/notifiers.py` -- pluggable "something needs review"
  pings (`Notifier`, `SlackNotifier`).
- `approval_gate/policy.py` -- pluggable auto-approve/reject/routing
  rules (`Policy`, `RulePolicy`, `Rule`).
- `examples/` -- runnable, no-API-key demos. Every new capability
  should ship with one of these, not just tests -- they're what a
  first-time evaluator runs before reading any code.

## Good first issues

- **A new `Backend`.** Implement `wait_for_decision(pending) -> dict`
  (see `approval_gate/backends/base.py`) for a different graph
  framework, or an async/webhook-driven reviewer. `BlockingBackend` is
  the simplest existing example to model against.
- **A new `Notifier`.** Any callable matching
  `(pending: dict, review_url: str) -> None` works -- email, PagerDuty,
  MS Teams. `SlackNotifier` in `notifiers.py` is ~25 lines and a
  reasonable template.
- **Richer `Rule` matching.** Time-of-day windows, argument-value
  conditions (e.g. amount thresholds on a `wire_transfer` action),
  multi-reviewer quorum. Keep `Rule` declarative and pure -- see the
  module docstring on why policies can't have side effects.

## Pull requests

- Add or extend a test in `tests/` for any behavior change. Tests here
  favor real execution over mocking where practical (e.g.
  `test_web_backend.py` drives an actual local HTTP server) -- follow
  that pattern rather than mocking internals.
- If you're adding a capability (not just fixing a bug), add a runnable
  example under `examples/` and a short README section, matching the
  style of the existing ones.
- Keep PRs scoped to one capability. A new backend and a new notifier
  in the same PR makes review harder for no benefit.
- `pytest tests/ -q` should pass with no new warnings before you open
  the PR.
