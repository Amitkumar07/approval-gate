"""
plain_python_demo.py
----------------------
Proof that approval-gate doesn't require LangGraph (or any agent
framework) at all: same PII scanning, same audit trail, same
approve/edit/reject decision model -- just a plain Python function
calling `gate.request_approval(...)` directly, with BlockingBackend
handling the pause synchronously instead of via graph interrupt/resume.

Run:
    python examples/plain_python_demo.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate import ApprovalGate
from approval_gate.backends import BlockingBackend

DB_PATH = str(Path(__file__).resolve().parent / "demo_audit.db")


def ask_in_terminal(pending: dict) -> dict:
    print("\n" + "=" * 60)
    print(f"  APPROVAL NEEDED   action: {pending['action']}   risk: {pending['risk']}")
    print("=" * 60)
    print("  Proposed arguments:")
    for k, v in pending["args"].items():
        print(f"    {k}: {v}")
    if pending["pii_findings"]:
        print("\n  ⚠ Sensitive data detected in this action:")
        for f in pending["pii_findings"]:
            print(f"    - {f['type']} in field '{f['field']}': {f['value_masked']}  (via {f['source']})")
    else:
        print("\n  No sensitive data detected.")
    print("-" * 60)

    choice = input("  Approve / Reject? [a/r]: ").strip().lower()
    if choice == "a":
        return {"decision": "approve", "by": "you"}
    reason = input("  Reason for rejecting: ").strip()
    return {"decision": "reject", "by": "you", "reason": reason or "rejected by reviewer"}


gate = ApprovalGate(db_path=DB_PATH, backend=BlockingBackend(ask_in_terminal))


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


if __name__ == "__main__":
    print("Starting a plain Python tool call. No LangGraph, no graph, no interrupt() --")
    print("just a function calling gate.request_approval() directly.\n")

    result = send_followup_email(customer_email="priya.k@example.com", customer_phone="+91 98765 43210")

    print("\n" + "=" * 60)
    print("  RESULT:", result)
    print(f"  Full audit trail written to: {DB_PATH}")
    print("=" * 60)
