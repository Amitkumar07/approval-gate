"""
_queue_base.py
---------------
Shared machinery for any backend where a decision arrives asynchronously
from an external channel (a browser click, a webhook POST, a Slack
button, a clicked email link) rather than synchronously in the calling
thread (that's what BlockingBackend is for).

The pattern is always the same: `wait_for_decision` registers a
per-audit_id `queue.Queue`, does whatever's needed to notify the outside
world (serve a page, POST a webhook, send an email), then blocks on
`queue.get()` until something calls `resolve(audit_id, decision)` --
typically from an HTTP handler thread receiving the external system's
callback.

This exists because WebBackend's original implementation had a real bug
here: a single dict + a per-key Queue accessed from multiple threads
(the caller's thread and every incoming HTTP request thread) with no
lock corrupted state under concurrency in exactly the way a shared
sqlite3.Connection did in audit.py (see that module's docstring for the
sibling bug and fix). Rather than re-solve "shared mutable dict across
threads" three more times for Webhook/Email/Slack backends, each with
its own chance to get the locking subtly wrong, it's solved once here.

Leading underscore: this is shared implementation, not a public
extension point. Backend authors write a Backend (see base.py), not a
subclass of this.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Optional


class _PendingQueueBackend:
    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        self._results: dict[str, "queue.Queue[dict[str, Any]]"] = {}
        self._lock = threading.Lock()

    def _register(self, audit_id: str, pending: dict[str, Any]) -> "queue.Queue[dict[str, Any]]":
        result_queue: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[audit_id] = pending
            self._results[audit_id] = result_queue
        return result_queue

    def _unregister(self, audit_id: str) -> None:
        with self._lock:
            self._pending.pop(audit_id, None)
            self._results.pop(audit_id, None)

    def _get_pending(self, audit_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._pending.get(audit_id)

    def _list_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._pending.values())

    def resolve(self, audit_id: str, decision: dict[str, Any]) -> bool:
        """Deliver a decision for a still-pending audit_id. Returns False
        (and does nothing) if audit_id is unknown or already decided --
        callers (HTTP handlers) should treat that as a 404, not an error."""
        with self._lock:
            result_queue = self._results.get(audit_id)
        if result_queue is None:
            return False
        result_queue.put(decision)
        return True
