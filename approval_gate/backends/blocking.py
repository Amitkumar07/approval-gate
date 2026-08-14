"""
blocking.py
------------
The simplest possible backend, and proof that ApprovalGate doesn't
actually need a graph framework at all: it just blocks the current
thread and hands the pending action to a callback you provide. Use this
for a raw tool-calling loop, a script, a Jupyter notebook -- anything
that isn't LangGraph.

    from approval_gate import ApprovalGate
    from approval_gate.backends import BlockingBackend

    def ask_in_terminal(pending: dict) -> dict:
        print(pending["action"], pending["args"])
        return {"decision": "approve", "by": "amit"}

    gate = ApprovalGate(db_path="audit.db", backend=BlockingBackend(ask_in_terminal))

No pause/resume plumbing, no interrupt/replay semantics to worry about --
`reviewer` runs synchronously and its return value is the decision.
"""

from __future__ import annotations

from typing import Any, Callable

from .base import Backend


class BlockingBackend(Backend):
    def __init__(self, reviewer: Callable[[dict[str, Any]], dict[str, Any]]):
        self.reviewer = reviewer

    def wait_for_decision(self, pending: dict[str, Any]) -> dict[str, Any]:
        return self.reviewer(pending)
