"""
langgraph_backend.py
---------------------
The original mechanic, unchanged, just moved behind the Backend
interface: pausing via LangGraph's native `interrupt()`.

On first call, `interrupt()` raises a GraphInterrupt that LangGraph
catches and surfaces as `result["__interrupt__"]`. When the graph is
resumed with `Command(resume=decision)`, the *same* `interrupt()` call
returns `decision` instead of raising. This is what makes LangGraph's
node-replay-on-resume behavior safe to build on -- see audit.py for the
other half of that story (the deterministic-id upsert).
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from .base import Backend


class LangGraphBackend(Backend):
    def wait_for_decision(self, pending: dict[str, Any]) -> dict[str, Any]:
        return interrupt(pending)
