import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate import ApprovalGate
from approval_gate.backends import Backend, BlockingBackend


def make_gate(backend):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    return ApprovalGate(db_path=tmp.name, backend=backend)


def test_blocking_backend_approve():
    reviewed = {}

    def reviewer(pending):
        reviewed.update(pending)
        return {"decision": "approve", "by": "amit"}

    gate = make_gate(BlockingBackend(reviewer))
    decision = gate.request_approval("send_email", {"to": "a@b.com"}, risk="medium")

    assert decision.approved is True
    assert decision.args == {"to": "a@b.com"}
    assert reviewed["action"] == "send_email"


def test_blocking_backend_reject():
    def reviewer(pending):
        return {"decision": "reject", "by": "amit", "reason": "too risky"}

    gate = make_gate(BlockingBackend(reviewer))
    decision = gate.request_approval("delete_records", {"table": "x"}, risk="high")

    assert decision.approved is False
    assert decision.reason == "too risky"


def test_blocking_backend_edit():
    def reviewer(pending):
        edited = dict(pending["args"])
        edited["to"] = "corrected@b.com"
        return {"decision": "edit", "by": "amit", "args": edited}

    gate = make_gate(BlockingBackend(reviewer))
    decision = gate.request_approval("send_email", {"to": "wrong@b.com"}, risk="medium")

    assert decision.approved is True
    assert decision.args["to"] == "corrected@b.com"


def test_default_backend_is_langgraph_backend():
    gate = make_gate(None)
    from approval_gate.backends import LangGraphBackend

    assert isinstance(gate.backend, LangGraphBackend)


def test_unknown_backends_attribute_raises_attribute_error():
    import approval_gate.backends as backends_module

    try:
        backends_module.NotARealBackend
        assert False, "expected AttributeError"
    except AttributeError:
        pass


def test_custom_backend_must_implement_wait_for_decision():
    class Incomplete(Backend):
        pass

    try:
        Incomplete()
        assert False, "expected TypeError for missing abstract method"
    except TypeError:
        pass


def test_gate_close_closes_the_underlying_audit_log():
    def reviewer(pending):
        return {"decision": "approve", "by": "amit"}

    gate = make_gate(BlockingBackend(reviewer))
    gate.request_approval("send_email", {"to": "a@b.com"}, risk="low")
    gate.close()

    import sqlite3

    try:
        gate.audit.get("anything")
        assert False, "expected the audit log's connection to be closed"
    except sqlite3.ProgrammingError:
        pass
