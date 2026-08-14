"""
policy.py
---------
Without policies, every single call to `request_approval` pauses for a
human -- which is safe, but doesn't scale past a handful of action
types before reviewers start rubber-stamping everything, which defeats
the point. A Policy lets you say, up front: which actions can
auto-approve, which must always escalate no matter what, and who
should be routed to review the rest.

A Policy is evaluated once per `request_approval` call, before the
backend is touched at all:

  - If it returns an auto-decision ("approve" or "reject"), the human
    step is skipped entirely -- nothing shows up in a review inbox,
    nothing pings a notifier. This is the whole point: auto-approved
    low-risk actions shouldn't add noise to what a human has to look
    at. The decision is still written to the audit log, with
    `decided_by` set to the rule's name, so "this was auto-approved by
    policy X" is fully traceable later.
  - If it returns None, the action proceeds to the backend exactly as
    it does today (unless a `route_to` was set, which is attached to
    the pending payload as metadata for the backend/notifier to use --
    approval-gate doesn't enforce reviewer identity itself, that's the
    backend's job if it wants one).

Rules are evaluated in order; the first one that matches wins. A
RulePolicy with no matching rule (and a policy of `None` on
ApprovalGate) always falls through to a human -- the safe default.

    from approval_gate.policy import RulePolicy, Rule

    policy = RulePolicy([
        Rule(risk="low", auto_approve=True),
        Rule(action_prefix="delete_", route_to="oncall"),
        Rule(action_name="wire_transfer", route_to="finance-lead"),
    ])
    gate = ApprovalGate(db_path="audit.db", policy=policy)

For anything a declarative rule can't express, pass a plain callable
instead -- same escape hatch as Backend/Notifier:

    def my_policy(pending: dict) -> Optional[dict]:
        if pending["risk"] == "low" and not pending["pii_findings"]:
            return {"decision": "approve", "by": "policy:auto-low-risk"}
        return None

    gate = ApprovalGate(db_path="audit.db", policy=my_policy)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

Policy = Callable[[dict[str, Any]], Optional[dict[str, Any]]]


@dataclass
class Rule:
    """One line of policy. Matching is AND across whichever of
    action_name / action_prefix / risk / has_pii are set; unset fields
    are wildcards. Exactly one of auto_approve / auto_reject / route_to
    should be set -- auto_approve/auto_reject short-circuit the human
    step, route_to tags the pending payload for the backend to use."""

    action_name: Optional[str] = None
    action_prefix: Optional[str] = None
    risk: Optional[str] = None
    has_pii: Optional[bool] = None
    auto_approve: bool = False
    auto_reject: bool = False
    route_to: Optional[str] = None
    name: Optional[str] = None

    def matches(self, pending: dict[str, Any]) -> bool:
        if self.action_name is not None and pending["action"] != self.action_name:
            return False
        if self.action_prefix is not None and not pending["action"].startswith(self.action_prefix):
            return False
        if self.risk is not None and pending["risk"] != self.risk:
            return False
        if self.has_pii is not None and bool(pending["pii_findings"]) != self.has_pii:
            return False
        return True

    def label(self) -> str:
        if self.name:
            return self.name
        bits = [
            f"action={self.action_name}" if self.action_name else None,
            f"prefix={self.action_prefix}" if self.action_prefix else None,
            f"risk={self.risk}" if self.risk else None,
            f"has_pii={self.has_pii}" if self.has_pii is not None else None,
        ]
        return "rule(" + ",".join(b for b in bits if b) + ")"


class RulePolicy:
    """Evaluates Rules in order; the first match wins. No match => defer
    to a human (returns None), same as an empty policy would."""

    def __init__(self, rules: list[Rule]):
        self.rules = rules

    def __call__(self, pending: dict[str, Any]) -> Optional[dict[str, Any]]:
        for rule in self.rules:
            if not rule.matches(pending):
                continue
            label = rule.label()
            if rule.auto_approve:
                return {"decision": "approve", "by": f"policy:{label}"}
            if rule.auto_reject:
                return {"decision": "reject", "by": f"policy:{label}", "reason": f"auto-rejected by {label}"}
            if rule.route_to:
                pending["route_to"] = rule.route_to
            return None
        return None
