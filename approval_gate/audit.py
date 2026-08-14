"""
audit.py
--------
Permanent, append-friendly record of every action an agent proposed,
what (if anything) looked sensitive about it, and what a human decided.

Design notes (read this before changing anything):

- Rows are keyed by a DETERMINISTIC id: hash(thread_id, action_name, args).
  This matters because of how LangGraph's `interrupt()` works: when a node
  that calls `interrupt()` is resumed, LangGraph re-runs that node function
  from the top. Any code you wrote *before* the interrupt() call (like
  "create a pending audit row") will therefore run again. Using a
  deterministic id + UPSERT means that re-run is harmless — it just
  overwrites the same pending row instead of creating a duplicate.
  This is the single trickiest part of building on top of LangGraph
  interrupts correctly, so it gets a comment instead of being silently
  "clever."

- We never delete rows. "Audit trail" means the record persists even if
  the decision was "reject" or the action later errored.

- All access to the underlying sqlite3.Connection goes through a
  threading.RLock (see AuditLog.__init__). One ApprovalGate instance is
  commonly shared across threads -- an agent doing parallel tool calls
  will call request_approval from more than one thread concurrently --
  and an unsynchronized shared sqlite3.Connection under concurrent
  writes doesn't just error, it can corrupt the database file on disk.
  Every public method takes the lock for its full duration.
  record_result reads the current status via the private _get_locked,
  not the public get(), so it doesn't try to re-acquire a lock it's
  already holding.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT,
    action_name     TEXT NOT NULL,
    args_json       TEXT NOT NULL,
    pii_findings    TEXT,
    risk            TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|edited|error
    decided_by      TEXT,
    decision_reason TEXT,
    final_args_json TEXT,
    result          TEXT,
    created_at      REAL NOT NULL,
    decided_at      REAL,
    completed_at    REAL
);
"""


def make_id(thread_id: str, action_name: str, args: dict) -> str:
    """Deterministic id so resume-replays upsert instead of duplicate."""
    payload = json.dumps({"t": thread_id, "a": action_name, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass
class AuditRecord:
    id: str
    thread_id: str
    action_name: str
    args: dict
    pii_findings: list
    risk: str
    status: str
    decided_by: Optional[str]
    decision_reason: Optional[str]
    final_args: Optional[dict]
    result: Optional[str]
    created_at: float
    decided_at: Optional[float]
    completed_at: Optional[float]

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> "AuditRecord":
        return cls(
            id=row["id"],
            thread_id=row["thread_id"],
            action_name=row["action_name"],
            args=json.loads(row["args_json"]),
            pii_findings=json.loads(row["pii_findings"]) if row["pii_findings"] else [],
            risk=row["risk"],
            status=row["status"],
            decided_by=row["decided_by"],
            decision_reason=row["decision_reason"],
            final_args=json.loads(row["final_args_json"]) if row["final_args_json"] else None,
            result=row["result"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
            completed_at=row["completed_at"],
        )


class AuditLog:
    def __init__(self, db_path: str = "audit.db"):
        self.db_path = str(Path(db_path))
        # A single sqlite3.Connection is not safe for concurrent use from
        # multiple threads even with check_same_thread=False -- that flag
        # only disables Python's same-thread assertion, it does not add
        # synchronization. Concurrent agent tool calls (e.g. parallel tool
        # use) call request_approval from separate threads all sharing one
        # ApprovalGate, so every access to _conn goes through this lock.
        # Reentrant because record_result's status lookup reuses _get_locked
        # while already holding the lock.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(SCHEMA)
        self._conn.commit()

    # ---- writes -------------------------------------------------------

    def upsert_pending(
        self,
        thread_id: str,
        action_name: str,
        args: dict,
        pii_findings: list,
        risk: str = "high",
    ) -> str:
        record_id = make_id(thread_id, action_name, args)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_log (id, thread_id, action_name, args_json, pii_findings, risk, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(id) DO UPDATE SET
                    pii_findings = excluded.pii_findings,
                    risk = excluded.risk
                """,
                (
                    record_id,
                    thread_id,
                    action_name,
                    json.dumps(args, default=str),
                    json.dumps(pii_findings, default=str),
                    risk,
                    time.time(),
                ),
            )
            self._conn.commit()
        return record_id

    def record_decision(
        self,
        record_id: str,
        status: str,
        decided_by: str,
        reason: str = "",
        final_args: Optional[dict] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE audit_log
                SET status = ?, decided_by = ?, decision_reason = ?, final_args_json = ?, decided_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    decided_by,
                    reason,
                    json.dumps(final_args, default=str) if final_args is not None else None,
                    time.time(),
                    record_id,
                ),
            )
            self._conn.commit()

    def record_result(self, record_id: str, result: Any, error: bool = False) -> None:
        with self._lock:
            status = "error" if error else self._get_locked(record_id).status
            self._conn.execute(
                "UPDATE audit_log SET result = ?, status = ?, completed_at = ? WHERE id = ?",
                (str(result), status, time.time(), record_id),
            )
            self._conn.commit()

    # ---- reads ----------------------------------------------------------

    def _get_locked(self, record_id: str) -> Optional[AuditRecord]:
        row = self._conn.execute("SELECT * FROM audit_log WHERE id = ?", (record_id,)).fetchone()
        return AuditRecord._from_row(row) if row else None

    def get(self, record_id: str) -> Optional[AuditRecord]:
        with self._lock:
            return self._get_locked(record_id)

    def list_pending(self) -> list[AuditRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log WHERE status = 'pending' ORDER BY created_at ASC"
            ).fetchall()
            return [AuditRecord._from_row(r) for r in rows]

    def list_all(self, limit: int = 200) -> list[AuditRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [AuditRecord._from_row(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
