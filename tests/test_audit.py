import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate.audit import AuditLog, make_id


def make_log():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    return AuditLog(tmp.name)


def test_upsert_pending_then_get():
    log = make_log()
    rid = log.upsert_pending("thread-1", "send_email", {"to": "a@b.com"}, [], risk="medium")
    record = log.get(rid)
    assert record is not None
    assert record.status == "pending"
    assert record.action_name == "send_email"


def test_upsert_is_idempotent_on_replay():
    """This is the behavior that matters: LangGraph re-runs node code
    before an interrupt() call when resuming, so the same pending-row
    write can happen twice. It must not create two rows."""
    log = make_log()
    rid1 = log.upsert_pending("thread-1", "send_email", {"to": "a@b.com"}, [], risk="medium")
    rid2 = log.upsert_pending("thread-1", "send_email", {"to": "a@b.com"}, [], risk="medium")
    assert rid1 == rid2
    assert len(log.list_all()) == 1


def test_different_args_produce_different_ids():
    log = make_log()
    rid1 = log.upsert_pending("thread-1", "send_email", {"to": "a@b.com"}, [], risk="medium")
    rid2 = log.upsert_pending("thread-1", "send_email", {"to": "c@d.com"}, [], risk="medium")
    assert rid1 != rid2


def test_record_decision_updates_status():
    log = make_log()
    rid = log.upsert_pending("thread-1", "delete_records", {"table": "x"}, [], risk="high")
    log.record_decision(rid, status="approved", decided_by="amit", reason="")
    record = log.get(rid)
    assert record.status == "approved"
    assert record.decided_by == "amit"


def test_rejected_records_are_never_deleted():
    log = make_log()
    rid = log.upsert_pending("thread-1", "delete_records", {"table": "x"}, [], risk="high")
    log.record_decision(rid, status="rejected", decided_by="amit", reason="too risky")
    assert log.get(rid) is not None
    assert log.get(rid).status == "rejected"


def test_make_id_is_deterministic():
    id1 = make_id("t1", "send_email", {"to": "a@b.com"})
    id2 = make_id("t1", "send_email", {"to": "a@b.com"})
    assert id1 == id2
