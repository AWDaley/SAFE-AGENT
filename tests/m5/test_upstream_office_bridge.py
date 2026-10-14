"""Tests requiring the pinned TAL office-domain package (separate CI job)."""
from __future__ import annotations

import pytest

from safe_agent.contracts.control import DecisionStatus
from safe_agent.tal.upstream_office import UpstreamOfficePolicyGate
from tests.m2.conftest import NOW, arrange

pytest.importorskip("tal.office")


def test_tal_bridge_admits_only_matching_synthetic_calendar(kit):
    bridge = UpstreamOfficePolicyGate(kit["broker"])
    kit["coordinator"]._gate = bridge
    coordinator, snapshot, projection, simulation, proposal = arrange(kit)
    decision = coordinator.propose(
        kit["principal"], "task_m2", proposal.model_dump_json(),
        expected_state_version=snapshot.state_version, now=NOW,
    )
    assert decision.decision is DecisionStatus.AUTHORIZED
    assert "OFFLINE" in decision.reason_code
    assert coordinator.inspect(kit["principal"], "task_m2", now=NOW).proposal_hash


def test_tal_bridge_denies_if_exact_proposal_not_in_scope(kit):
    kit["coordinator"]._gate = UpstreamOfficePolicyGate(kit["broker"])
    coordinator, snapshot, projection, simulation, proposal = arrange(kit)
    bad = proposal.model_copy(update={"customer_ref": "ref_another_customer"})
    decision = coordinator.propose(
        kit["principal"], "task_m2", bad.model_dump_json(),
        expected_state_version=snapshot.state_version, now=NOW,
    )
    assert decision.decision is DecisionStatus.DENIED


def test_tal_bridge_requires_approval_for_approval_policy(kit):
    kit["coordinator"]._gate = UpstreamOfficePolicyGate(kit["broker"])
    coordinator, snapshot, projection, simulation, proposal = arrange(kit, approval=True)
    decision = coordinator.propose(
        kit["principal"], "task_m2", proposal.model_dump_json(),
        expected_state_version=snapshot.state_version, now=NOW,
    )
    assert decision.decision is DecisionStatus.APPROVAL_REQUIRED


def test_bridge_refuses_tal_policy_denial_even_when_local_admits(kit, monkeypatch):
    from tal import office as tal_office

    bridge = UpstreamOfficePolicyGate(kit["broker"])
    kit["coordinator"]._gate = bridge
    coordinator, snapshot, projection, simulation, proposal = arrange(kit)

    original = tal_office.admit_office_calendar

    def fake_deny(*args: object, **kwargs: object):
        # Build a valid response, but force TAL domain denial for a source mismatch.
        _, upstream_proposal, evidence = args
        mismatched = upstream_proposal.model_copy(update={"source_manifest_hash": tal_office.sha256("forged")})
        return original(args[0], mismatched, evidence, now=kwargs["now"])

    monkeypatch.setattr(tal_office, "admit_office_calendar", fake_deny)
    decision = coordinator.propose(
        kit["principal"], "task_m2", proposal.model_dump_json(),
        expected_state_version=snapshot.state_version, now=NOW,
    )
    assert decision.decision is DecisionStatus.DENIED
    assert decision.reason_code == "TAL_OFFICE_POLICY_DENIED"
