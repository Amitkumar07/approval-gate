import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate import ApprovalGate
from approval_gate.backends import BlockingBackend
from approval_gate.policy import Rule, RulePolicy


def make_gate(policy, backend=None):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    if backend is None:
        backend = BlockingBackend(lambda pending: pytest_fail_if_called(pending))
    return ApprovalGate(db_path=tmp.name, backend=backend, policy=policy)


def pytest_fail_if_called(pending):
    raise AssertionError(f"backend should not have been called for {pending['action']!r} -- policy should have auto-decided")


def test_auto_approve_skips_backend():
    policy = RulePolicy([Rule(risk="low", auto_approve=True)])
    gate = make_gate(policy)

    decision = gate.request_approval("read_report", {"id": "1"}, risk="low")

    assert decision.approved is True
    assert decision.args == {"id": "1"}


def test_auto_reject_skips_backend():
    policy = RulePolicy([Rule(action_prefix="delete_", risk="high", auto_reject=True)])
    gate = make_gate(policy)

    decision = gate.request_approval("delete_records", {"table": "x"}, risk="high")

    assert decision.approved is False
    assert "auto-rejected" in decision.reason


def test_no_matching_rule_falls_through_to_backend():
    reviewed = {}

    def reviewer(pending):
        reviewed["called"] = True
        return {"decision": "approve", "by": "amit"}

    policy = RulePolicy([Rule(risk="low", auto_approve=True)])
    gate = make_gate(policy, backend=BlockingBackend(reviewer))

    decision = gate.request_approval("send_email", {"to": "a@b.com"}, risk="high")

    assert reviewed.get("called") is True
    assert decision.approved is True


def test_first_matching_rule_wins():
    policy = RulePolicy(
        [
            Rule(action_name="send_email", auto_reject=True, name="block-email"),
            Rule(risk="medium", auto_approve=True, name="approve-medium"),
        ]
    )
    gate = make_gate(policy)

    decision = gate.request_approval("send_email", {"to": "a@b.com"}, risk="medium")

    assert decision.approved is False


def test_route_to_falls_through_but_tags_pending():
    seen_pending = {}

    def reviewer(pending):
        seen_pending.update(pending)
        return {"decision": "approve", "by": "oncall-amit"}

    policy = RulePolicy([Rule(action_prefix="delete_", route_to="oncall")])
    gate = make_gate(policy, backend=BlockingBackend(reviewer))

    decision = gate.request_approval("delete_records", {"table": "x"}, risk="high")

    assert decision.approved is True
    assert seen_pending["route_to"] == "oncall"


def test_audit_log_records_policy_as_decider():
    policy = RulePolicy([Rule(risk="low", auto_approve=True, name="auto-low")])
    gate = make_gate(policy)

    decision = gate.request_approval("read_report", {"id": "1"}, risk="low")
    record = gate.audit.get(decision.audit_id)

    assert record.status == "approved"
    assert record.decided_by == "policy:auto-low"


def test_has_pii_rule_matches_on_findings():
    policy = RulePolicy([Rule(has_pii=False, auto_approve=True)])
    gate = make_gate(policy)

    clean_decision = gate.request_approval("send_email", {"note": "hello"}, risk="high")
    assert clean_decision.approved is True


def test_plain_callable_works_as_policy():
    def my_policy(pending):
        if pending["risk"] == "low":
            return {"decision": "approve", "by": "policy:callable"}
        return None

    gate = make_gate(my_policy)
    decision = gate.request_approval("read_report", {"id": "1"}, risk="low")
    assert decision.approved is True


def test_no_policy_always_defers_to_backend():
    def reviewer(pending):
        return {"decision": "approve", "by": "amit"}

    gate = make_gate(None, backend=BlockingBackend(reviewer))
    decision = gate.request_approval("read_report", {"id": "1"}, risk="low")
    assert decision.approved is True


def test_rule_matches_rejects_on_each_field_independently():
    pending = {"action": "delete_records", "risk": "high", "pii_findings": []}

    assert Rule(action_name="send_email").matches(pending) is False  # action_name mismatch
    assert Rule(action_prefix="send_").matches(pending) is False  # prefix mismatch
    assert Rule(risk="low").matches(pending) is False  # risk mismatch
    assert Rule(has_pii=True).matches(pending) is False  # has_pii mismatch (empty findings -> False)
    assert Rule(action_prefix="delete_", risk="high", has_pii=False).matches(pending) is True
    assert Rule(action_name="delete_records").matches(pending) is True
