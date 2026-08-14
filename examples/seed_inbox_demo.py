"""
seed_inbox_demo.py
--------------------
Queues up every visual scenario the review inbox renders differently,
all at once, so the queue shows a fully populated, realistic state
instead of one card at a time. Useful for looking at the UI itself
(design/QA), not a "how do I use approval-gate" walkthrough -- see
policy_demo.py / web_inbox_demo.py for that.

Each request_approval() call blocks the thread that made it, so to get
several items pending simultaneously they're fired from separate
threads -- this mirrors an agent with several tool calls in flight at
once (e.g. parallel tool use), not a contrived UI trick.

Scenarios covered:
  - all three risk levels (low / medium / high)
  - PII findings present vs. absent
  - route_to present (routed to a specific reviewer) vs. absent
  - a long field value (forces the textarea to grow past one line)
  - multiple sensitive-data findings on a single action
  - an action with no arguments at all

Run:
    python examples/seed_inbox_demo.py
Then open the printed URL -- six items will be sitting in the queue.
Decide any of them from the browser; the script prints the outcome as
each one resolves and exits once all six are done.
"""

import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate import ApprovalGate
from approval_gate.backends import WebBackend
from approval_gate.policy import Rule, RulePolicy

DB_PATH = str(Path(__file__).resolve().parent / "demo_audit.db")

policy = RulePolicy([Rule(action_prefix="delete_", route_to="oncall-reviewer")])

backend = WebBackend(port=8642)
gate = ApprovalGate(db_path=DB_PATH, backend=backend, policy=policy)

SCENARIOS = [
    dict(
        action_name="send_email",
        risk="low",
        args={"to": "team@example.com", "subject": "Weekly digest is ready"},
    ),
    dict(
        action_name="send_email",
        risk="medium",
        args={
            "to": "priya.k@example.com",
            "subject": "Following up on your support ticket",
            "body": (
                "Hi Priya, following up on ticket #4821 -- noted your callback number "
                "+91 98765 43210 in case we need to reach you, and confirmed the card "
                "on file ending in 4242 4242 4242 4242 was retried successfully this morning."
            ),
        },
    ),
    dict(
        action_name="delete_records",
        risk="high",
        args={"table": "support_tickets", "filter": "status = 'closed' AND age_days > 365"},
    ),
    dict(
        action_name="rotate_api_key",
        risk="high",
        args={"service": "payments-gateway", "current_key": "sk-liveAAAAAAAAAAAAAAAAAAAAAAAA"},
    ),
    dict(
        action_name="post_slack_message",
        risk="low",
        args={"channel": "#eng-announcements", "text": "Deploy finished, all green."},
    ),
    dict(
        action_name="restart_service",
        risk="medium",
        args={},
    ),
]


def run_scenario(scenario: dict) -> None:
    decision = gate.request_approval(**scenario)
    record = gate.audit.get(decision.audit_id)
    label = "approved" if decision.approved else f"blocked ({decision.reason})"
    print(f"  {scenario['action_name']:20s} risk={scenario['risk']:6s} -> {label}  (decided_by={record.decided_by})")


if __name__ == "__main__":
    print(f"Review inbox running at: {backend.url}")
    print(f"Queuing {len(SCENARIOS)} actions at once -- open the inbox to see them all.\n")
    try:
        webbrowser.open(backend.url)
    except Exception:
        pass

    threads = [threading.Thread(target=run_scenario, args=(s,)) for s in SCENARIOS]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"\nAll {len(SCENARIOS)} scenarios decided. Full audit trail written to: {DB_PATH}")
    print("Inspect it with: python examples/view_audit_log.py")
    backend.shutdown()
