"""Governance proposals, approvals, apply, reconciliation, crash idempotency."""

from __future__ import annotations

import json
from typing import Any

import httpx

from controller.api.client import SimulatorClient
from controller.api.models import Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.change_builder import (
    auto_apply_permitted,
    build_winner_proposal,
    changed_dims,
    set_allocation,
)
from controller.services.experiments import ExperimentArmState, ExperimentState, persist_experiment
from controller.services.governance import (
    apply_approved,
    reconcile_approvals,
    run_governance_cycle,
    submit_proposal,
)


def _workflow(**overrides) -> Workflow:
    base = {
        "workflow_id": "nw_renewal",
        "tenant_id": "northwind",
        "languages": ["hi"],
        "dialling_window": [10, 18],
        "dials_per_hour": 100,
        "cadence": [0, 1],
        "min_gap_minutes": 60,
        "daily_batch": {},
        "mid_flight": "allowed",
        "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
        "options": {
            "welcome_message": ["W1", "W2"],
            "voice": ["V1", "V2"],
            "caller_id": ["D1"],
            "prompt_version": ["P1"],
        },
        "prompt_catalog": {"P1": "ok"},
        "governance": {
            "auto_apply": {"enabled": False, "allowed_dims": ["welcome_message", "voice"], "max_shift": 1.0},
            "approver_hours": [10, 18],
            "approver_latency_minutes": [30, 120],
            "proposal_ttl_hours": 24,
            "apply_window_hours": 2,
            "max_requests_per_24h": 5,
            "base_reject_rate": 0.0,
            "min_validation_pass_rate": 0.9,
        },
        "current_version": 1,
    }
    base.update(overrides)
    return Workflow.model_validate(base)


def _config(version: int = 1) -> WorkflowConfig:
    return WorkflowConfig.model_validate(
        {
            "workflow_id": "nw_renewal",
            "version": version,
            "actor": "system",
            "applied_at": None,
            "config": {
                "prompt_version": "P1",
                "cohorts": [],
                "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0}}},
                "schedule": {},
            },
        }
    )


def _winner_experiment() -> ExperimentState:
    return ExperimentState(
        experiment_id="nw_renewal|default|welcome_message",
        workflow_id="nw_renewal",
        tenant_id="northwind",
        cohort_id="default",
        dimension="welcome_message",
        primary_kpi="engagement",
        guardrail_kpi="pickup",
        guardrail_max_drop=0.02,
        status="winner",
        control_variant="W1",
        planned_allocation={"W1": 0.45, "W2": 0.55},
        min_sample_per_arm=100,
        alpha=0.05,
        min_effect=0.02,
        created_at="t0",
        updated_at="t1",
        decision="winner",
        decision_reason="W2 beats W1",
        winner_variant="W2",
        promotion_status="ready_to_propose_observe_only",
        evidence={"current_allocation": {"W1": 1.0}},
        arms=[
            ExperimentArmState(
                experiment_id="nw_renewal|default|welcome_message",
                variant="W1",
                is_control=True,
                planned_weight=0.45,
                dials=200,
                connected=200,
                engaged=60,
                primary_rate=0.3,
                guardrail_rate=1.0,
                sample_n=200,
                sample_ok=True,
                verdict="control",
            ),
            ExperimentArmState(
                experiment_id="nw_renewal|default|welcome_message",
                variant="W2",
                is_control=False,
                planned_weight=0.55,
                dials=200,
                connected=200,
                engaged=120,
                primary_rate=0.6,
                guardrail_rate=1.0,
                sample_n=200,
                sample_ok=True,
                verdict="winner",
                effect_size=0.3,
                p_value=1e-6,
                adjusted_alpha=0.05,
            ),
        ],
    )


def _mock_client(handler) -> SimulatorClient:
    return SimulatorClient("http://sim.test", transport=httpx.MockTransport(handler), sleeper=lambda _s: None, max_retries=0)


def test_changed_dims_and_auto_apply_gate():
    before = _config().config
    after = set_allocation(before, cohort_id="default", dimension="welcome_message", weights={"W1": 0.4, "W2": 0.6})
    assert "welcome_message" in changed_dims(before, after)
    wf = _workflow()
    ok, problems = auto_apply_permitted(wf, before, after, impact_score=0.5)
    assert ok is False  # auto_apply disabled
    assert problems
    wf2 = _workflow(
        governance={
            "auto_apply": {"enabled": True, "allowed_dims": ["welcome_message"], "max_shift": 1.0},
            "approver_hours": [10, 18],
            "approver_latency_minutes": [30, 120],
            "proposal_ttl_hours": 24,
            "apply_window_hours": 2,
            "max_requests_per_24h": 5,
            "base_reject_rate": 0.0,
            "min_validation_pass_rate": 0.9,
        }
    )
    ok2, problems2 = auto_apply_permitted(wf2, before, after, impact_score=0.5)
    assert ok2 is True
    assert problems2 == []


def test_build_winner_proposal(tmp_path):
    store = Store(tmp_path / "g.sqlite")
    draft = build_winner_proposal(store, _workflow(), _config(), _winner_experiment())
    assert draft is not None
    assert draft.source == "experiment_winner"
    assert "welcome_message" in draft.changed_dims
    assert 0 <= draft.impact_score <= 1
    assert draft.diff
    assert draft.dry_run.get("limitation")


def test_submit_approval_and_apply(tmp_path):
    store = Store(tmp_path / "g2.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    draft = build_winner_proposal(store, _workflow(), _config(), exp)
    assert draft is not None
    approvals: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/workflows/nw_renewal/config":
            return httpx.Response(200, json=_config().model_dump())
        if request.method == "POST" and request.url.path == "/approvals":
            body = json.loads(request.content.decode())
            assert body["proposal_id"] == draft.proposal_id
            assert "proposed_config" in body
            assert body["diff"]
            assert body["dry_run"]
            aid = "apr_00001"
            approvals[aid] = {
                "approval_id": aid,
                "proposal_id": body["proposal_id"],
                "workflow_id": "nw_renewal",
                "status": "pending",
                "base_version": body["base_version"],
                "changed_dims": ["welcome_message"],
                "created_at": "t0",
                "expires_at": "t_exp",
                "apply_by": None,
                "decided_at": None,
                "reasons": [],
            }
            return httpx.Response(200, json=approvals[aid])
        if request.method == "GET" and request.url.path.startswith("/approvals/"):
            aid = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=approvals[aid])
        if request.method == "GET" and request.url.path == "/approvals":
            return httpx.Response(200, json=list(approvals.values()))
        if request.method == "POST" and request.url.path == "/workflows/nw_renewal/apply":
            body = json.loads(request.content.decode())
            assert set(body.keys()) == {"approval_id"}
            approvals[body["approval_id"]]["status"] = "applied"
            approvals[body["approval_id"]]["applied_version"] = 2
            return httpx.Response(200, json={"workflow_id": "nw_renewal", "version": 2, "changed_dims": ["welcome_message"]})
        raise AssertionError(f"{request.method} {request.url.path}")

    client = _mock_client(handler)
    submitted = submit_proposal(client, store, _workflow(), _config(), draft)
    assert submitted.status == "submitted"
    assert submitted.approval_id == "apr_00001"
    assert store.get_proposal(draft.proposal_id)["status"] == "submitted"

    # Reconcile to approved
    approvals["apr_00001"]["status"] = "approved"
    approvals["apr_00001"]["decided_at"] = "t_dec"
    approvals["apr_00001"]["apply_by"] = "t_by"
    reconcile_approvals(client, store)
    assert store.get_controller_approval("apr_00001")["status"] == "approved"
    assert store.get_proposal(draft.proposal_id)["status"] == "approved"

    applied = apply_approved(client, store)
    assert applied and applied[0]["version"] == 2
    assert store.get_proposal(draft.proposal_id)["status"] == "applied"
    assert store.get_controller_approval("apr_00001")["status"] == "applied"
    # Idempotent re-submit does not create a second network proposal when already applied.
    again = submit_proposal(client, store, _workflow(), _config(), draft)
    assert again.status == "applied"


def test_duplicate_proposal_reconciliation(tmp_path):
    store = Store(tmp_path / "g3.sqlite")
    draft = build_winner_proposal(store, _workflow(), _config(), _winner_experiment())
    assert draft is not None
    calls = {"post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/approvals":
            calls["post"] += 1
            if calls["post"] == 1:
                return httpx.Response(409, json={"error": "duplicate_proposal_id", "detail": draft.proposal_id})
            raise AssertionError("should not POST twice after recovery path uses list")
        if request.method == "GET" and request.url.path == "/approvals":
            return httpx.Response(
                200,
                json=[
                    {
                        "approval_id": "apr_dup",
                        "proposal_id": draft.proposal_id,
                        "workflow_id": "nw_renewal",
                        "status": "pending",
                        "base_version": 1,
                        "changed_dims": ["welcome_message"],
                    }
                ],
            )
        if request.method == "GET" and request.url.path == "/workflows/nw_renewal/config":
            return httpx.Response(200, json=_config().model_dump())
        raise AssertionError(request.url.path)

    client = _mock_client(handler)
    submitted = submit_proposal(client, store, _workflow(), _config(), draft)
    assert submitted.status == "submitted"
    assert submitted.approval_id == "apr_dup"


def test_rejected_and_expired_statuses(tmp_path):
    store = Store(tmp_path / "g4.sqlite")
    draft = build_winner_proposal(store, _workflow(), _config(), _winner_experiment())
    assert draft is not None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/approvals":
            return httpx.Response(
                200,
                json={
                    "approval_id": "apr_rej",
                    "proposal_id": draft.proposal_id,
                    "workflow_id": "nw_renewal",
                    "status": "pending",
                    "base_version": 1,
                    "changed_dims": ["welcome_message"],
                },
            )
        if request.method == "GET" and request.url.path == "/approvals/apr_rej":
            return httpx.Response(
                200,
                json={
                    "approval_id": "apr_rej",
                    "proposal_id": draft.proposal_id,
                    "workflow_id": "nw_renewal",
                    "status": "rejected",
                    "base_version": 1,
                    "changed_dims": ["welcome_message"],
                    "reasons": ["approver declined (judgement call)"],
                    "decided_at": "t",
                },
            )
        if request.method == "GET" and request.url.path == "/workflows/nw_renewal/config":
            return httpx.Response(200, json=_config().model_dump())
        raise AssertionError(request.url.path)

    client = _mock_client(handler)
    submit_proposal(client, store, _workflow(), _config(), draft)
    reconcile_approvals(client, store)
    assert store.get_proposal(draft.proposal_id)["status"] == "rejected"
    assert store.get_controller_approval("apr_rej")["status"] == "rejected"


def test_governance_cycle_mutate_false(tmp_path):
    store = Store(tmp_path / "g5.sqlite")
    report = run_governance_cycle(
        _mock_client(lambda r: httpx.Response(500)),
        store,
        [_workflow()],
        [_config()],
        [_winner_experiment()],
        mutate=False,
    )
    assert report.proposals_submitted == 0
    assert any("disabled" in n for n in report.notes)


def test_stale_apply_marks_proposal(tmp_path):
    store = Store(tmp_path / "g6.sqlite")
    draft = build_winner_proposal(store, _workflow(), _config(), _winner_experiment())
    assert draft is not None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/approvals":
            return httpx.Response(
                200,
                json={
                    "approval_id": "apr_stale",
                    "proposal_id": draft.proposal_id,
                    "workflow_id": "nw_renewal",
                    "status": "approved",
                    "base_version": 1,
                    "changed_dims": ["welcome_message"],
                    "decided_at": "t",
                    "apply_by": "t2",
                },
            )
        if request.method == "POST" and request.url.path.endswith("/apply"):
            return httpx.Response(409, json={"error": "stale_base_version", "detail": {"current_version": 9}})
        if request.method == "GET" and request.url.path == "/workflows/nw_renewal/config":
            return httpx.Response(200, json=_config().model_dump())
        raise AssertionError(request.url.path)

    client = _mock_client(handler)
    submit_proposal(client, store, _workflow(), _config(), draft)
    # Force approved locally
    from controller.services.governance import ControllerApproval, _sync_proposal_status_from_approval

    store.upsert_controller_approval(
        ControllerApproval(
            approval_id="apr_stale",
            proposal_id=draft.proposal_id,
            workflow_id="nw_renewal",
            status="approved",
            base_version=1,
            changed_dims=["welcome_message"],
            updated_at="t",
        )
    )
    _sync_proposal_status_from_approval(
        store,
        ControllerApproval(
            approval_id="apr_stale",
            proposal_id=draft.proposal_id,
            workflow_id="nw_renewal",
            status="approved",
            updated_at="t",
        ),
    )
    apply_approved(client, store)
    assert store.get_proposal(draft.proposal_id)["status"] == "stale"
