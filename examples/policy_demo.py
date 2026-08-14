"""
policy_demo.py
----------------
Shows a RulePolicy in action: three actions proposed, only one of them
actually reaches a human. Watch the terminal output -- the low-risk
read auto-approves instantly (no browser needed), and the delete gets
routed to a specific reviewer tag before landing in the inbox.

Run:
    python examples/policy_demo.py
Then open the printed URL to approve/reject the one action that
actually needs a human.
"""

import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate import ApprovalGate
from approval_gate.backends import WebBackend
from approval_gate.policy import Rule, RulePolicy

DB_PATH = str(Path(__file__).resolve().parent / "demo_audit.db")

policy = RulePolicy(
    [
        # Low-risk, no sensitive data -> never bother a human.
        Rule(risk="low", has_pii=False, auto_approve=True, name="auto-low-risk"),
        # Deletes always escalate, regardless of risk label, and get
        # tagged for a specific reviewer instead of the general queue.
        Rule(action_prefix="delete_", route_to="oncall-reviewer"),
    ]
)

backend = WebBackend(port=8642)
gate = ApprovalGate(db_path=DB_PATH, backend=backend, policy=policy)


def run_action(action_name: str, args: dict, risk: str) -> None:
    decision = gate.request_approval(action_name=action_name, args=args, risk=risk)
    record = gate.audit.get(decision.audit_id)
    print(f"  {action_name:20s} risk={risk:6s} -> {'approved' if decision.approved else 'blocked'}"
          f"  (decided_by={record.decided_by})")


if __name__ == "__main__":
    print(f"Review inbox running at: {backend.url}")
    print("(Only the delete_records action below should actually need you.)\n")
    try:
        webbrowser.open(backend.url)
    except Exception:
        pass

    print("Proposing three actions:")
    run_action("read_report", {"report_id": "Q3-summary"}, risk="low")
    run_action("delete_records", {"table": "support_tickets", "filter": "age_days > 365"}, risk="high")
    run_action("send_email", {"to": "customer@example.com", "subject": "hi"}, risk="medium")

    print(f"\nFull audit trail written to: {DB_PATH}")
    print("Inspect it with: python examples/view_audit_log.py")
    backend.shutdown()
