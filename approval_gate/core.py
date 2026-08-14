"""
core.py
-------
The actual product. Call `gate.request_approval(...)` from inside any
agent tool/node before doing something risky. Execution pauses until a
human resumes with a decision. Every step is written to the audit log.

*How* execution pauses is delegated to a `Backend` (see backends/) --
ApprovalGate itself has no framework-specific code. The default backend
is LangGraphBackend, so existing LangGraph usage needs no changes:

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

For anything that isn't LangGraph -- a plain Python tool-calling loop,
a script, a notebook -- pass a different backend instead:

    from approval_gate.backends import BlockingBackend

    gate = ApprovalGate(db_path="audit.db", backend=BlockingBackend(ask_a_human))

See examples/plain_python_demo.py for a fully working version of that.

Pass `policy=` to skip the human step entirely for actions that don't
need it (auto-approve low risk, always escalate deletes, route to a
specific reviewer). See policy.py.

    from approval_gate.policy import RulePolicy, Rule

    gate = ApprovalGate(
        db_path="audit.db",
        policy=RulePolicy([Rule(risk="low", auto_approve=True)]),
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import pii
from .audit import AuditLog
from .backends import Backend
from .policy import Policy


@dataclass
class Decision:
    approved: bool
    args: dict
    reason: str
    audit_id: str


class ApprovalGate:
    def __init__(
        self,
        db_path: str = "audit.db",
        default_thread_id: str = "default",
        backend: Optional[Backend] = None,
        policy: Optional[Policy] = None,
    ):
        self.audit = AuditLog(db_path)
        self.default_thread_id = default_thread_id
        self.backend = backend or _default_backend()
        self.policy = policy

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

        pending = {
            "audit_id": audit_id,
            "action": action_name,
            "args": args,
            "pii_findings": findings,
            "risk": risk,
        }

        # A policy gets first look, before a human is ever bothered. It
        # must be a pure function of `pending` -- like everything before
        # a pause, it can re-run on a LangGraph resume-replay (see
        # audit.py), so it can't have side effects of its own. Returning
        # a decision short-circuits the backend entirely (nothing shows
        # up in a review inbox, no notifier fires); returning None falls
        # through to a human exactly as if there were no policy at all.
        resume_value = self.policy(pending) if self.policy is not None else None

        if resume_value is None:
            # This is the actual pause. What "pause" means depends on the
            # backend -- LangGraphBackend raises a GraphInterrupt that LangGraph
            # catches and surfaces as result["__interrupt__"], resuming here
            # with the value passed via Command(resume=...). Other backends
            # (e.g. BlockingBackend) just return a decision synchronously.
            resume_value = self.backend.wait_for_decision(pending)

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


def _default_backend() -> Backend:
    """LangGraphBackend stays the default so existing code (`ApprovalGate(db_path=...)`
    with no backend argument) keeps working unchanged. Imported lazily so
    that using a different backend doesn't require langgraph to be installed."""
    from .backends import LangGraphBackend

    return LangGraphBackend()
