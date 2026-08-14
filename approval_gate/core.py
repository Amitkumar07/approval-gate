"""
core.py
-------
The actual product. Call `gate.request_approval(...)` from inside any
LangGraph tool/node before doing something risky. Execution pauses
(via LangGraph's native `interrupt()`) until a human resumes the graph
with a decision. Every step is written to the audit log.

Usage inside a tool:

    from approval_gate import ApprovalGate

    gate = ApprovalGate(db_path="audit.db")

    def send_email(to: str, subject: str, body: str) -> str:
        decision = gate.request_approval(
            action_name="send_email",
            args={"to": to, "subject": subject, "body": body},
            risk="high",
        )
        if not decision.approved:
            return f"BLOCKED: {decision.reason}"
        # decision.args may have been edited by the human reviewer
        result = really_send_email(**decision.args)
        gate.log_result(decision.audit_id, result)
        return result

Driving the graph from the outside (see examples/email_agent_demo.py
for the full working version):

    result = graph.invoke(initial_state, config)
    while "__interrupt__" in result:
        pending = result["__interrupt__"][0].value
        # show `pending` to a human, get back a decision dict, then:
        result = graph.invoke(Command(resume=decision_dict), config)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from langgraph.types import interrupt

from . import pii
from .audit import AuditLog


@dataclass
class Decision:
    approved: bool
    args: dict
    reason: str
    audit_id: str


class ApprovalGate:
    def __init__(self, db_path: str = "audit.db", default_thread_id: str = "default"):
        self.audit = AuditLog(db_path)
        self.default_thread_id = default_thread_id

    def request_approval(
        self,
        action_name: str,
        args: dict[str, Any],
        risk: str = "high",
        thread_id: Optional[str] = None,
    ) -> Decision:
        thread_id = thread_id or self.default_thread_id

        findings = pii.scan(args)

        audit_id = self.audit.upsert_pending(
            thread_id=thread_id,
            action_name=action_name,
            args=args,
            pii_findings=findings,
            risk=risk,
        )

        # This is the actual pause. On first call: raises a GraphInterrupt
        # that LangGraph catches and surfaces as result["__interrupt__"].
        # On resume: returns the value passed via Command(resume=...).
        resume_value = interrupt(
            {
                "audit_id": audit_id,
                "action": action_name,
                "args": args,
                "pii_findings": findings,
                "risk": risk,
            }
        )

        decision_type = resume_value.get("decision", "reject")
        decided_by = resume_value.get("by", "unknown")
        reason = resume_value.get("reason", "")
        final_args = resume_value.get("args", args) if decision_type in ("approve", "edit") else args

        status_map = {"approve": "approved", "reject": "rejected", "edit": "edited"}
        status = status_map.get(decision_type, "rejected")

        self.audit.record_decision(
            audit_id, status=status, decided_by=decided_by, reason=reason, final_args=final_args
        )

        return Decision(
            approved=decision_type in ("approve", "edit"),
            args=final_args,
            reason=reason if decision_type == "reject" else "",
            audit_id=audit_id,
        )

    def log_result(self, audit_id: str, result: Any, error: bool = False) -> None:
        self.audit.record_result(audit_id, result, error=error)

    def close(self) -> None:
        self.audit.close()
