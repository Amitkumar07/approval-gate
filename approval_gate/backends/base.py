"""
base.py
-------
The contract every pause/resume mechanism has to satisfy. ApprovalGate
itself doesn't know or care *how* a human's decision gets back to it --
that's entirely the backend's job.

Why this exists: the original implementation called LangGraph's
`interrupt()` directly from inside ApprovalGate. That works, but it means
"approval-gate" can only ever be "approval-gate for LangGraph." The parts
that actually matter -- scanning args for sensitive data, writing an
audit trail, producing a Decision -- have nothing to do with LangGraph at
all (see audit.py / pii.py, neither of which import it). Pulling the
pause/resume mechanic behind this interface means those parts can be
reused by anything: a raw Python tool-calling loop, a different graph
framework, a webhook-driven async reviewer, etc.

A backend has exactly one job: given a payload describing the proposed
action, block (however it wants to) until a decision is available, and
return that decision as a dict. What "block" means is entirely up to the
implementation -- LangGraphBackend raises a GraphInterrupt for the host
graph to catch; BlockingBackend just calls a Python callable right there
in the thread.

The returned dict is expected to look like:
    {"decision": "approve" | "reject" | "edit", "by": str, "reason": str, "args": dict}
Only "decision" is required; ApprovalGate treats missing keys as sane
defaults (see core.py).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Backend(ABC):
    @abstractmethod
    def wait_for_decision(self, pending: dict[str, Any]) -> dict[str, Any]:
        """Block until a human decision is available for this pending action.

        `pending` contains audit_id, action, args, pii_findings, and risk --
        everything a reviewer needs to see. Must return a decision dict
        (see module docstring). May raise/suspend instead of returning, if
        that's how the underlying framework implements a pause (LangGraph's
        interrupt() does exactly this on first call).
        """
        raise NotImplementedError
