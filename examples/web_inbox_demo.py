"""
web_inbox_demo.py
-------------------
The same two risky actions as email_agent_demo.py, but reviewed from a
browser tab instead of a terminal prompt. No LangGraph, no new
dependency -- WebBackend is stdlib-only (http.server).

Run:
    python examples/web_inbox_demo.py
Then open the printed URL and approve/edit/reject both actions.
"""

import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate import ApprovalGate
from approval_gate.backends import WebBackend

DB_PATH = str(Path(__file__).resolve().parent / "demo_audit.db")

backend = WebBackend(port=8642)
gate = ApprovalGate(db_path=DB_PATH, backend=backend)


def send_followup_email(customer_email: str, customer_phone: str) -> str:
    args = {
        "to": customer_email,
        "subject": "Following up on your support ticket",
        "body": f"Hi! Noted your callback number {customer_phone} in case we need to reach you.",
    }
    decision = gate.request_approval(action_name="send_email", args=args, risk="medium")
    if not decision.approved:
        return f"BLOCKED: {decision.reason}"
    result = f"Email sent to {decision.args['to']} -- subject: '{decision.args['subject']}'"
    gate.log_result(decision.audit_id, result)
    return result


def cleanup_old_records() -> str:
    args = {"table": "support_tickets", "filter": "status = 'closed' AND age_days > 365"}
    decision = gate.request_approval(action_name="delete_records", args=args, risk="high")
    if not decision.approved:
        return f"BLOCKED: {decision.reason}"
    result = f"Deleted rows from {decision.args['table']} where {decision.args['filter']}"
    gate.log_result(decision.audit_id, result)
    return result


if __name__ == "__main__":
    print(f"Review inbox running at: {backend.url}")
    print("Waiting for you to approve/edit/reject two actions in the browser...\n")
    try:
        webbrowser.open(backend.url)
    except Exception:
        pass

    email_result = send_followup_email(customer_email="priya.k@example.com", customer_phone="+91 98765 43210")
    print("Email step:  ", email_result)

    cleanup_result = cleanup_old_records()
    print("Cleanup step:", cleanup_result)

    print(f"\nFull audit trail written to: {DB_PATH}")
    print("Inspect it with: python examples/view_audit_log.py")
    backend.shutdown()
