"""
email_agent_demo.py
--------------------
A minimal, runnable proof that the whole loop works:
  agent proposes a risky action -> graph pauses -> PII gets flagged ->
  human approves/edits/rejects in the terminal -> action runs (or doesn't)
  -> everything lands in audit.db.

No LLM API key required -- the "agent" here is a tiny scripted node so
you can run this in 10 seconds and see the mechanic, end to end. Swap
the scripted node for a real LLM-driven LangGraph agent and nothing
about ApprovalGate changes.

Run:
    python examples/email_agent_demo.py
"""

import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

from approval_gate import ApprovalGate
from approval_gate.cli import run_with_cli_approval

DB_PATH = str(Path(__file__).resolve().parent / "demo_audit.db")
gate = ApprovalGate(db_path=DB_PATH)


class AgentState(TypedDict, total=False):
    customer_email: str
    customer_phone: str
    notes: str
    email_result: str
    cleanup_result: str


def send_followup_email(state: AgentState) -> AgentState:
    """Risky action #1: sending an email on the customer's behalf."""
    args = {
        "to": state["customer_email"],
        "subject": "Following up on your support ticket",
        "body": (
            f"Hi! Following up on ticket -- noted your callback number "
            f"{state['customer_phone']} in case we need to reach you. {state.get('notes', '')}"
        ),
    }

    decision = gate.request_approval(action_name="send_email", args=args, risk="medium")

    if not decision.approved:
        return {"email_result": f"BLOCKED: {decision.reason}"}

    final = decision.args
    result = f"Email sent to {final['to']} -- subject: '{final['subject']}'"
    gate.log_result(decision.audit_id, result)
    return {"email_result": result}


def cleanup_old_records(state: AgentState) -> AgentState:
    """Risky action #2: an irreversible delete."""
    args = {"table": "support_tickets", "filter": "status = 'closed' AND age_days > 365"}

    decision = gate.request_approval(action_name="delete_records", args=args, risk="high")

    if not decision.approved:
        return {"cleanup_result": f"BLOCKED: {decision.reason}"}

    result = f"Deleted rows from {decision.args['table']} where {decision.args['filter']}"
    gate.log_result(decision.audit_id, result)
    return {"cleanup_result": result}


def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("send_followup_email", send_followup_email)
    builder.add_node("cleanup_old_records", cleanup_old_records)
    builder.add_edge(START, "send_followup_email")
    builder.add_edge("send_followup_email", "cleanup_old_records")
    builder.add_edge("cleanup_old_records", END)
    return builder.compile(checkpointer=InMemorySaver())


if __name__ == "__main__":
    graph = build_graph()
    config = {"configurable": {"thread_id": "demo-thread-1"}}

    initial_state: AgentState = {
        "customer_email": "priya.k@example.com",
        "customer_phone": "+91 98765 43210",
        "notes": "Card on file ending in 4242 4242 4242 4242 was retried successfully.",
    }

    print("Starting agent run. You'll be asked to approve two risky actions.\n")
    final_state = run_with_cli_approval(graph, initial_state, config, reviewer="amit")

    print("\n" + "=" * 60)
    print("  RUN COMPLETE")
    print("=" * 60)
    print("  Email step:  ", final_state.get("email_result"))
    print("  Cleanup step:", final_state.get("cleanup_result"))
    print(f"\n  Full audit trail written to: {DB_PATH}")
    print("  Inspect it with: python examples/view_audit_log.py")
