import multiprocessing
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate.audit import AuditLog, make_id


def make_log():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    return AuditLog(tmp.name)


def _write_from_subprocess(db_path: str, worker_id: int) -> None:
    """Module-level (picklable) so multiprocessing can target it -- a
    closure won't work with the default 'spawn'/'fork' start methods."""
    log = AuditLog(db_path)
    rid = log.upsert_pending(f"proc-{worker_id}", "send_email", {"to": f"user{worker_id}@example.com"}, [], risk="low")
    log.record_decision(rid, status="approved", decided_by=f"reviewer-{worker_id}", reason="")
    log.close()


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


def test_list_pending_returns_only_undecided_rows():
    log = make_log()
    pending_id = log.upsert_pending("thread-1", "send_email", {"to": "a@b.com"}, [], risk="low")
    decided_id = log.upsert_pending("thread-1", "delete_records", {"table": "x"}, [], risk="high")
    log.record_decision(decided_id, status="approved", decided_by="amit", reason="")

    pending = log.list_pending()

    assert [r.id for r in pending] == [pending_id]
    assert pending[0].status == "pending"


def test_list_all_respects_limit():
    log = make_log()
    for i in range(5):
        log.upsert_pending("thread-1", "send_email", {"to": f"user{i}@example.com"}, [], risk="low")

    assert len(log.list_all(limit=2)) == 2
    assert len(log.list_all()) == 5


def test_close_makes_the_connection_unusable():
    log = make_log()
    log.upsert_pending("thread-1", "send_email", {"to": "a@b.com"}, [], risk="low")
    log.close()

    import sqlite3

    try:
        log.get("anything")
        assert False, "expected an error after close()"
    except sqlite3.ProgrammingError:
        pass


def test_concurrent_writes_from_many_threads_do_not_corrupt_or_error():
    """An agent doing parallel tool calls hits AuditLog from multiple
    threads sharing one connection concurrently. Without a lock around
    every access, this doesn't just raise -- it can corrupt the sqlite
    file on disk. Regression test for that."""
    log = make_log()
    errors = []

    def write_one(i):
        try:
            rid = log.upsert_pending(f"thread-{i}", "send_email", {"to": f"user{i}@example.com"}, [], risk="low")
            log.record_decision(rid, status="approved", decided_by=f"reviewer-{i}", reason="")
            log.record_result(rid, f"sent to user{i}")
            log.get(rid)
            log.list_all()
        except Exception as e:  # noqa: BLE001 -- any exception here is the failure this test guards against
            errors.append(e)

    threads = [threading.Thread(target=write_one, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent access raised: {errors}"
    records = log.list_all()
    assert len(records) == 20
    assert all(r.status == "approved" for r in records)


def test_wal_mode_and_busy_timeout_are_active():
    log = make_log()
    mode = log._conn.execute("PRAGMA journal_mode").fetchone()[0]
    timeout = log._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert mode.lower() == "wal"
    assert timeout > 0


def test_concurrent_writes_from_separate_processes_do_not_corrupt_or_error():
    """Different from the many-threads test above: this is what WAL mode
    + busy_timeout specifically address -- multiple separate OS processes
    (not just threads in one process, which the lock in AuditLog.__init__
    already covered) writing to the same db_path concurrently, the way
    running more than one worker process of an app would."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name

    AuditLog(db_path).close()  # create the schema before workers race to open it

    ctx = multiprocessing.get_context("spawn")
    processes = [ctx.Process(target=_write_from_subprocess, args=(db_path, i)) for i in range(8)]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=15)

    assert all(p.exitcode == 0 for p in processes), [p.exitcode for p in processes]

    log = AuditLog(db_path)
    records = log.list_all()
    assert len(records) == 8
    assert all(r.status == "approved" for r in records)
