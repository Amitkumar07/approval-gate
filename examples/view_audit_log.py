"""
view_audit_log.py
------------------
Inspect the audit trail produced by email_agent_demo.py.
This is the kind of record you'd show an auditor: every proposed
action, what was flagged, who decided, and what happened.

Run:
    python examples/view_audit_log.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate import AuditLog

DB_PATH = str(Path(__file__).resolve().parent / "demo_audit.db")

if __name__ == "__main__":
    log = AuditLog(DB_PATH)
    records = log.list_all()

    if not records:
        print("No audit records yet. Run examples/email_agent_demo.py first.")
        sys.exit(0)

    for r in records:
        print("=" * 70)
        print(f"id:           {r.id}")
        print(f"action:       {r.action_name}  (risk: {r.risk})")
        print(f"status:       {r.status}")
        print(f"proposed args:{r.args}")
        if r.pii_findings:
            print(f"flagged:      {[(f['type'], f['field']) for f in r.pii_findings]}")
        if r.decided_by:
            print(f"decided by:   {r.decided_by}  reason: {r.decision_reason!r}")
        if r.final_args and r.final_args != r.args:
            print(f"edited args:  {r.final_args}")
        if r.result:
            print(f"result:       {r.result}")
    print("=" * 70)
    print(f"\n{len(records)} total record(s) in {DB_PATH}")
