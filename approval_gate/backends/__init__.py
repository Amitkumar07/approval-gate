"""
`LangGraphBackend` is imported lazily (inside `__getattr__` below) so
that importing `approval_gate.backends` -- and therefore `approval_gate`
itself -- doesn't require langgraph to be installed. Only users who
actually construct a LangGraphBackend need the dependency.
"""

from __future__ import annotations

from .base import Backend
from .blocking import BlockingBackend
from .web import WebBackend
from .webhook import WebhookBackend
from .email import EmailBackend

__all__ = ["Backend", "LangGraphBackend", "BlockingBackend", "WebBackend", "WebhookBackend", "EmailBackend"]


def __getattr__(name: str):
    if name == "LangGraphBackend":
        from .langgraph_backend import LangGraphBackend

        return LangGraphBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
