"""Governance proposals, approvals, apply, reconciliation, crash idempotency."""

from __future__ import annotations

import json
from typing import Any

import httpx

from controller.api.client import SimulatorClient
from controller.api.models import SimState, Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.change_builder import (
    ProposalDraft,
    auto_apply_permitted,
    build_winner_proposal,
    changed_dims,
    set_allocation,
)
from controller.services.detector import Signal
from controller.services.experiments import ExperimentArmState, ExperimentState, persist_experiment
from controller.services.governance import (
    ControllerApproval,
    apply_approved,
    next_advance_minutes,
    persist_proposal,
    promote_experiment_winners,
    propose_decay_rollbacks,
    propose_experiment_exploration,
    reconcile_approvals,
    reconcile_uncertain_auto_applies,
    run_governance_cycle,
    submit_proposal,
    workflow_has_uncertain_auto_apply,
)
from controller.services.incidents import mark_incident_mitigating, open_or_refresh_incident
from controller.services.prompt_incident import LlmAttempt, run_prompt_incident_cycle, should_start_attempt


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


def _mock_client(handler, retries: int = 0) -> SimulatorClient:
    return SimulatorClient(
        "http://sim.test",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _s: None,
        max_retries=retries,
    )


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
    exp = _winner_experiment()
    persist_experiment(store, exp)
    draft = build_winner_proposal(store, _workflow(), _config(), exp)
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
    assert store.get_experiment(exp.experiment_id)["promotion_status"] == "promotion_rejected"


def test_human_rejection_survives_replan_and_blocks_repromote(tmp_path):
    """Full path: winner → submit → human reject → replan → still rejected → no new approval."""
    store = Store(tmp_path / "rej_sticky.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path == "/approvals":
            body = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "approval_id": "apr_human",
                    "proposal_id": body["proposal_id"],
                    "workflow_id": "nw_renewal",
                    "status": "pending",
                    "base_version": body["base_version"],
                    "changed_dims": ["welcome_message"],
                },
            )
        if request.method == "GET" and request.url.path == "/approvals/apr_human":
            return httpx.Response(
                200,
                json={
                    "approval_id": "apr_human",
                    "proposal_id": draft.proposal_id,
                    "workflow_id": "nw_renewal",
                    "status": "rejected",
                    "base_version": 1,
                    "changed_dims": ["welcome_message"],
                    "reasons": ["approver declined (judgement call)"],
                    "decided_at": "t",
                },
            )
        if request.method == "GET" and request.url.path.endswith("/config"):
            return httpx.Response(200, json=_config().model_dump())
        raise AssertionError(request.url.path)

    client = _mock_client(handler)
    draft = build_winner_proposal(store, _workflow(), _config(), exp)
    assert draft is not None
    submit_proposal(client, store, _workflow(), _config(), draft)
    reconcile_approvals(client, store)
    assert store.get_experiment(exp.experiment_id)["promotion_status"] == "promotion_rejected"

    # Replan + persist must keep promotion_rejected sticky (SQL + STICKY set).
    exp.promotion_status = "ready_to_propose_observe_only"  # what a naive replan would assign
    persist_experiment(store, exp)
    row = store.get_experiment(exp.experiment_id)
    assert row is not None
    assert row["promotion_status"] == "promotion_rejected"

    exp.promotion_status = row["promotion_status"]
    posts_before = [p for p in posts if p == "POST /approvals"]
    out = promote_experiment_winners(client, store, [_workflow()], [_config()], [exp])
    assert out == []
    assert [p for p in posts if p == "POST /approvals"] == posts_before
    store.close()


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
    exp = _winner_experiment()
    persist_experiment(store, exp)
    draft = build_winner_proposal(store, _workflow(), _config(), exp)
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
    assert store.get_controller_approval("apr_stale")["status"] == "stale"
    assert store.get_experiment(exp.experiment_id)["promotion_status"] == "promotion_stale"
    # Stale approval must not be applied again.
    again = apply_approved(client, store)
    assert again == []

def test_build_explore_proposal_when_underpowered(tmp_path):
    from controller.services.change_builder import build_explore_proposal

    store = Store(tmp_path / "e.sqlite")
    exp = ExperimentState(
        experiment_id="nw_renewal|default|welcome_message",
        workflow_id="nw_renewal",
        tenant_id="northwind",
        cohort_id="default",
        dimension="welcome_message",
        primary_kpi="engagement",
        guardrail_kpi="pickup",
        guardrail_max_drop=0.02,
        status="running",
        control_variant="W1",
        planned_allocation={"W1": 0.5, "W2": 0.5},
        min_sample_per_arm=100,
        alpha=0.05,
        min_effect=0.02,
        created_at="t0",
        updated_at="t1",
        decision="underpowered",
        decision_reason="need samples",
        winner_variant=None,
        promotion_status="observe_only_not_promoted",
        evidence={},
        arms=[],
    )
    draft = build_explore_proposal(store, _workflow(), _config(), exp)
    assert draft is not None
    assert draft.source == "experiment_explore"
    assert "welcome_message" in draft.changed_dims
    assert abs(draft.proposed_config["allocations"]["default"]["welcome_message"]["W2"] - 0.5) < 1e-6
    assert draft.dry_run.get("guardrail_ok") is None
    assert draft.dry_run.get("guardrail_assessment") == "not_assessed"
    store.close()


def test_advance_capped_when_approvals_pending_or_approved(tmp_path):
    store = Store(tmp_path / "adv.sqlite")
    assert next_advance_minutes(store, 240) == 240
    from controller.services.governance import ControllerApproval

    store.upsert_controller_approval(
        ControllerApproval(
            approval_id="apr_p",
            proposal_id="prop_p",
            workflow_id="nw_renewal",
            status="pending",
            updated_at="t",
        )
    )
    # Cap never increases configured: 5 stays 5; 15 stays 15; 240 becomes 15.
    assert next_advance_minutes(store, 5) == 5
    assert next_advance_minutes(store, 15) == 15
    assert next_advance_minutes(store, 240) == 15
    store.upsert_controller_approval(
        ControllerApproval(
            approval_id="apr_p",
            proposal_id="prop_p",
            workflow_id="nw_renewal",
            status="applied",
            updated_at="t",
        )
    )
    assert next_advance_minutes(store, 240) == 240
    store.upsert_controller_approval(
        ControllerApproval(
            approval_id="apr_a",
            proposal_id="prop_a",
            workflow_id="nw_renewal",
            status="approved",
            updated_at="t",
            apply_by="2027-01-04T12:00:00+05:30",
        )
    )
    assert next_advance_minutes(store, 5) == 5
    assert next_advance_minutes(store, 15) == 15
    assert next_advance_minutes(store, 240) == 15
    store.close()


def test_apply_approved_orders_by_apply_by(tmp_path):
    store = Store(tmp_path / "order.sqlite")
    from controller.services.governance import ControllerApproval, persist_proposal
    from controller.services.change_builder import ProposalDraft

    applied_order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/apply"):
            body = json.loads(request.content.decode())
            applied_order.append(body["approval_id"])
            return httpx.Response(
                200,
                json={"workflow_id": "nw_renewal", "version": 2 + len(applied_order), "changed_dims": ["welcome_message"]},
            )
        raise AssertionError(request.url.path)

    for aid, apply_by, pid in (
        ("apr_late", "2027-01-04T18:00:00+05:30", "prop_late"),
        ("apr_early", "2027-01-04T12:00:00+05:30", "prop_early"),
    ):
        persist_proposal(
            store,
            ProposalDraft(
                proposal_id=pid,
                workflow_id="nw_renewal",
                base_version=1,
                status="approved",
                source="experiment_winner",
                source_ref="exp",
                changed_dims=["welcome_message"],
                proposed_config={"allocations": {"default": {"welcome_message": {"W2": 1.0}}}},
                change_summary="x",
                diff={"changed_dims": ["welcome_message"]},
                dry_run={},
                impact_score=0.5,
                affected_leads=1,
                rationale="r",
                approval_id=aid,
                created_at="t0",
                updated_at="t0",
            ),
        )
        store.upsert_controller_approval(
            ControllerApproval(
                approval_id=aid,
                proposal_id=pid,
                workflow_id="nw_renewal",
                status="approved",
                base_version=1,
                changed_dims=["welcome_message"],
                updated_at="t",
                apply_by=apply_by,
            )
        )
    apply_approved(_mock_client(handler), store)
    assert applied_order == ["apr_early", "apr_late"]
    store.close()


def test_reconcile_then_apply_same_cycle(tmp_path):
    """Pending → approved on poll is applied in the same governance cycle (no extra advance)."""
    store = Store(tmp_path / "same.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    draft = build_winner_proposal(store, _workflow(), _config(), exp)
    assert draft is not None
    approvals: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/approvals":
            body = json.loads(request.content.decode())
            approvals["apr_1"] = {
                "approval_id": "apr_1",
                "proposal_id": body["proposal_id"],
                "workflow_id": "nw_renewal",
                "status": "pending",
                "base_version": 1,
                "changed_dims": ["welcome_message"],
                "created_at": "t0",
                "expires_at": "t_exp",
                "apply_by": None,
            }
            return httpx.Response(200, json=approvals["apr_1"])
        if request.method == "GET" and request.url.path == "/approvals/apr_1":
            return httpx.Response(200, json=approvals["apr_1"])
        if request.method == "POST" and request.url.path.endswith("/apply"):
            assert approvals["apr_1"]["status"] == "approved"
            approvals["apr_1"]["status"] = "applied"
            return httpx.Response(
                200, json={"workflow_id": "nw_renewal", "version": 2, "changed_dims": ["welcome_message"]}
            )
        if request.method == "GET" and request.url.path.endswith("/config"):
            return httpx.Response(200, json=_config().model_dump())
        raise AssertionError(request.url.path)

    client = _mock_client(handler)
    submit_proposal(client, store, _workflow(), _config(), draft)
    assert store.get_controller_approval("apr_1")["status"] == "pending"
    approvals["apr_1"]["status"] = "approved"
    approvals["apr_1"]["apply_by"] = "2027-01-04T12:00:00+05:30"
    approvals["apr_1"]["decided_at"] = "t_dec"
    # Same cycle: reconcile then apply (mirrors run_governance_cycle order) — no advance.
    reconciled = reconcile_approvals(client, store)
    assert any(a.approval_id == "apr_1" and a.status == "approved" for a in reconciled)
    assert store.get_controller_approval("apr_1")["status"] == "approved"
    assert next_advance_minutes(store, 240) == 15
    applied = apply_approved(client, store)
    assert applied and applied[0]["version"] == 2
    assert store.get_proposal(draft.proposal_id)["status"] == "applied"
    assert store.get_controller_approval("apr_1")["status"] == "applied"
    assert next_advance_minutes(store, 240) == 240
    store.close()


def test_run_governance_cycle_applies_approval_same_cycle(tmp_path):
    """run_governance_cycle itself reconciles then applies — no extra sim advance required."""
    store = Store(tmp_path / "gov_cycle.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    draft = build_winner_proposal(store, _workflow(), _config(), exp)
    assert draft is not None
    approvals: dict[str, dict[str, Any]] = {}
    advance_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/approvals":
            body = json.loads(request.content.decode())
            approvals["apr_1"] = {
                "approval_id": "apr_1",
                "proposal_id": body["proposal_id"],
                "workflow_id": "nw_renewal",
                "status": "pending",
                "base_version": 1,
                "changed_dims": ["welcome_message"],
                "created_at": "t0",
                "expires_at": "t_exp",
                "apply_by": None,
            }
            return httpx.Response(200, json=approvals["apr_1"])
        if request.method == "GET" and request.url.path == "/approvals/apr_1":
            return httpx.Response(200, json=approvals["apr_1"])
        if request.method == "POST" and request.url.path.endswith("/apply"):
            assert approvals["apr_1"]["status"] == "approved"
            approvals["apr_1"]["status"] = "applied"
            return httpx.Response(
                200, json={"workflow_id": "nw_renewal", "version": 2, "changed_dims": ["welcome_message"]}
            )
        if request.method == "GET" and request.url.path.endswith("/config"):
            return httpx.Response(200, json=_config().model_dump())
        if request.method == "POST" and request.url.path == "/sim/advance":
            advance_calls["n"] += 1
            raise AssertionError("governance cycle must not advance the simulator")
        raise AssertionError(request.url.path)

    client = _mock_client(handler)
    submit_proposal(client, store, _workflow(), _config(), draft)
    assert store.get_controller_approval("apr_1")["status"] == "pending"
    # Approver decides between cycles; next governance cycle must apply without advancing.
    approvals["apr_1"]["status"] = "approved"
    approvals["apr_1"]["apply_by"] = "2027-01-04T12:00:00+05:30"
    approvals["apr_1"]["decided_at"] = "t_dec"

    # Reload experiment so promote sees sticky proposal_submitted from the DB.
    row = store.get_experiment(exp.experiment_id)
    assert row is not None
    exp.promotion_status = row["promotion_status"]

    report = run_governance_cycle(
        client, store, [_workflow()], [_config()], [exp], mutate=True
    )
    assert report.approvals_reconciled >= 1
    assert report.applied == 1
    assert store.get_proposal(draft.proposal_id)["status"] == "applied"
    assert store.get_controller_approval("apr_1")["status"] == "applied"
    assert advance_calls["n"] == 0
    store.close()


def test_stale_then_fresh_reproposal_new_id(tmp_path):
    store = Store(tmp_path / "retry.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    live_version = {"v": 1}
    drafts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/config"):
            cfg = _config(version=live_version["v"]).model_dump()
            return httpx.Response(200, json=cfg)
        if request.method == "POST" and request.url.path == "/approvals":
            body = json.loads(request.content.decode())
            drafts.append(body["proposal_id"])
            return httpx.Response(
                200,
                json={
                    "approval_id": f"apr_{len(drafts)}",
                    "proposal_id": body["proposal_id"],
                    "workflow_id": "nw_renewal",
                    "status": "pending",
                    "base_version": body["base_version"],
                    "changed_dims": ["welcome_message"],
                },
            )
        if request.method == "POST" and request.url.path.endswith("/apply"):
            return httpx.Response(409, json={"error": "stale_base_version", "detail": {"current_version": 2}})
        raise AssertionError(request.url.path)

    client = _mock_client(handler)
    first = build_winner_proposal(store, _workflow(), _config(version=1), exp)
    assert first is not None
    submit_proposal(client, store, _workflow(), _config(version=1), first)
    from controller.services.governance import ControllerApproval, _sync_proposal_status_from_approval

    store.upsert_controller_approval(
        ControllerApproval(
            approval_id="apr_1",
            proposal_id=first.proposal_id,
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
            approval_id="apr_1",
            proposal_id=first.proposal_id,
            workflow_id="nw_renewal",
            status="approved",
            updated_at="t",
        ),
    )
    apply_approved(client, store)
    assert store.get_proposal(first.proposal_id)["status"] == "stale"
    assert store.get_controller_approval("apr_1")["status"] == "stale"
    assert store.get_experiment(exp.experiment_id)["promotion_status"] == "promotion_stale"

    # Supervisor bumped live config with an unrelated schedule edit; rebuild against that.
    live_version["v"] = 2
    supervisor_schedule = {"default": {"start_hour": 11, "end_hour": 19}}
    live_cfg_v2 = _config(version=2)
    live_cfg_v2 = WorkflowConfig.model_validate(
        {
            **live_cfg_v2.model_dump(),
            "config": {**live_cfg_v2.config, "schedule": supervisor_schedule},
        }
    )

    def handler_v2(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/config"):
            return httpx.Response(200, json=live_cfg_v2.model_dump())
        if request.method == "POST" and request.url.path == "/approvals":
            body = json.loads(request.content.decode())
            drafts.append(body["proposal_id"])
            # Fresh proposal must carry supervisor schedule unchanged.
            assert body["proposed_config"]["schedule"] == supervisor_schedule
            assert body["base_version"] == 2
            return httpx.Response(
                200,
                json={
                    "approval_id": f"apr_{len(drafts)}",
                    "proposal_id": body["proposal_id"],
                    "workflow_id": "nw_renewal",
                    "status": "pending",
                    "base_version": body["base_version"],
                    "changed_dims": body.get("diff", {}).get("changed_dims", ["welcome_message"]),
                },
            )
        raise AssertionError(request.url.path)

    client = _mock_client(handler_v2)
    exp.promotion_status = "promotion_stale"
    second_batch = promote_experiment_winners(
        client, store, [_workflow(current_version=2)], [live_cfg_v2], [exp]
    )
    assert len(second_batch) == 1
    second = second_batch[0]
    assert second.proposal_id != first.proposal_id
    assert second.base_version == 2
    assert second.status == "submitted"
    assert second.proposed_config["schedule"] == supervisor_schedule
    assert first.proposed_config.get("schedule") != supervisor_schedule
    assert second.proposed_config["allocations"]["default"]["welcome_message"]["W2"] == 0.55
    assert "welcome_message" in second.changed_dims
    assert "schedule" not in second.changed_dims
    # Only one active winner proposal.
    active = [
        p
        for p in store.list_proposals()
        if p["source"] == "experiment_winner" and p["status"] in {"draft", "submitted", "approved"}
    ]
    assert len(active) == 1
    # Repeat cycle must not create another active duplicate.
    again = promote_experiment_winners(
        client, store, [_workflow(current_version=2)], [_config(version=2)], [exp]
    )
    assert again == []
    store.close()


def test_lapsed_makes_retry_with_new_id(tmp_path):
    store = Store(tmp_path / "lapse.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    first = build_winner_proposal(store, _workflow(), _config(), exp)
    assert first is not None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/approvals":
            body = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "approval_id": "apr_x",
                    "proposal_id": body["proposal_id"],
                    "workflow_id": "nw_renewal",
                    "status": "pending",
                    "base_version": body["base_version"],
                    "changed_dims": ["welcome_message"],
                },
            )
        if request.method == "GET" and request.url.path.endswith("/config"):
            return httpx.Response(200, json=_config().model_dump())
        if request.method == "GET" and request.url.path.startswith("/approvals/"):
            return httpx.Response(
                200,
                json={
                    "approval_id": "apr_x",
                    "proposal_id": first.proposal_id,
                    "workflow_id": "nw_renewal",
                    "status": "lapsed",
                    "base_version": 1,
                    "changed_dims": ["welcome_message"],
                    "decided_at": "t",
                    "reasons": ["apply window missed"],
                },
            )
        raise AssertionError(request.url.path)

    client = _mock_client(handler)
    submit_proposal(client, store, _workflow(), _config(), first)
    from controller.services.governance import ControllerApproval

    store.upsert_controller_approval(
        ControllerApproval(
            approval_id="apr_x",
            proposal_id=first.proposal_id,
            workflow_id="nw_renewal",
            status="approved",
            base_version=1,
            changed_dims=["welcome_message"],
            updated_at="t",
        )
    )
    reconcile_approvals(client, store)
    assert store.get_proposal(first.proposal_id)["status"] == "lapsed"
    assert store.get_controller_approval("apr_x")["status"] == "lapsed"
    assert store.get_experiment(exp.experiment_id)["promotion_status"] == "promotion_lapsed"

    exp.promotion_status = "promotion_lapsed"
    # Same config version still yields a new proposal id via attempt sequence.
    second = build_winner_proposal(store, _workflow(), _config(), exp)
    assert second is not None
    assert second.proposal_id != first.proposal_id
    assert second.base_version == 1
    store.close()


def test_approval_lapsed_without_followup_get_marks_terminal(tmp_path):
    store = Store(tmp_path / "lapse2.sqlite")
    from controller.services.governance import ControllerApproval, persist_proposal
    from controller.services.change_builder import ProposalDraft

    persist_proposal(
        store,
        ProposalDraft(
            proposal_id="prop_l",
            workflow_id="nw_renewal",
            base_version=1,
            status="approved",
            source="experiment_winner",
            source_ref="exp_l",
            changed_dims=["welcome_message"],
            proposed_config={"allocations": {"default": {"welcome_message": {"W2": 1.0}}}},
            change_summary="x",
            diff={},
            dry_run={},
            impact_score=0.4,
            affected_leads=1,
            rationale="r",
            approval_id="apr_l",
            created_at="t0",
            updated_at="t0",
        ),
    )
    store.upsert_controller_approval(
        ControllerApproval(
            approval_id="apr_l",
            proposal_id="prop_l",
            workflow_id="nw_renewal",
            status="approved",
            base_version=1,
            changed_dims=["welcome_message"],
            updated_at="t",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/apply"):
            return httpx.Response(410, json={"error": "approval_lapsed", "detail": "window missed"})
        if request.method == "GET" and request.url.path.startswith("/approvals/"):
            return httpx.Response(500, json={"error": "unavailable", "detail": "down"})
        raise AssertionError(request.url.path)

    apply_approved(_mock_client(handler, retries=0), store)
    assert store.get_controller_approval("apr_l")["status"] == "lapsed"
    assert store.get_proposal("prop_l")["status"] == "lapsed"
    store.close()


def test_outdated_draft_is_not_submitted_and_rebuilds(tmp_path):
    """Crash-resumed draft with stale base_version must not be POSTed; rebuild instead."""
    store = Store(tmp_path / "draft_stale.sqlite")
    from controller.services.governance import persist_proposal
    from controller.services.change_builder import ProposalDraft

    exp = _winner_experiment()
    persist_experiment(store, exp)
    old_draft = build_winner_proposal(store, _workflow(), _config(version=1), exp)
    assert old_draft is not None
    assert old_draft.base_version == 1
    persist_proposal(store, old_draft)
    assert store.get_proposal(old_draft.proposal_id)["status"] == "draft"

    live_v2 = _config(version=2)
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/config"):
            return httpx.Response(200, json=live_v2.model_dump())
        if request.method == "POST" and request.url.path == "/approvals":
            body = json.loads(request.content.decode())
            posted.append(body)
            assert body["proposal_id"] != old_draft.proposal_id
            assert body["base_version"] == 2
            return httpx.Response(
                200,
                json={
                    "approval_id": f"apr_{len(posted)}",
                    "proposal_id": body["proposal_id"],
                    "workflow_id": "nw_renewal",
                    "status": "pending",
                    "base_version": body["base_version"],
                    "changed_dims": ["welcome_message"],
                },
            )
        raise AssertionError(request.url.path)

    out = promote_experiment_winners(
        _mock_client(handler), store, [_workflow(current_version=2)], [live_v2], [exp]
    )
    assert len(out) == 1
    assert out[0].proposal_id != old_draft.proposal_id
    assert out[0].base_version == 2
    assert out[0].status == "submitted"
    assert store.get_proposal(old_draft.proposal_id)["status"] == "stale"
    assert len(posted) == 1
    store.close()


def test_current_draft_resumes_without_new_id(tmp_path):
    """Crash-safe: draft still matching live version is submitted as-is."""
    store = Store(tmp_path / "draft_ok.sqlite")
    from controller.services.governance import persist_proposal

    exp = _winner_experiment()
    persist_experiment(store, exp)
    draft = build_winner_proposal(store, _workflow(), _config(version=1), exp)
    assert draft is not None
    persist_proposal(store, draft)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/config"):
            return httpx.Response(200, json=_config(version=1).model_dump())
        if request.method == "POST" and request.url.path == "/approvals":
            body = json.loads(request.content.decode())
            assert body["proposal_id"] == draft.proposal_id
            assert body["base_version"] == 1
            return httpx.Response(
                200,
                json={
                    "approval_id": "apr_resume",
                    "proposal_id": body["proposal_id"],
                    "workflow_id": "nw_renewal",
                    "status": "pending",
                    "base_version": body["base_version"],
                    "changed_dims": ["welcome_message"],
                },
            )
        raise AssertionError(request.url.path)

    out = promote_experiment_winners(
        _mock_client(handler), store, [_workflow()], [_config()], [exp]
    )
    assert len(out) == 1
    assert out[0].proposal_id == draft.proposal_id
    assert out[0].status == "submitted"
    assert store.get_proposal(draft.proposal_id)["status"] == "submitted"
    store.close()


def _proposed_config() -> dict[str, Any]:
    return {
        "prompt_version": "P1",
        "cohorts": [],
        "allocations": {"default": {"welcome_message": {"W2": 1.0}, "voice": {"V1": 1.0}}},
        "schedule": {},
    }


def _uncertain_proposal(source: str, source_ref: str, proposal_id: str) -> ProposalDraft:
    return ProposalDraft(
        proposal_id=proposal_id,
        workflow_id="nw_renewal",
        base_version=1,
        status="auto_apply_uncertain",
        source=source,
        source_ref=source_ref,
        changed_dims=["welcome_message"],
        proposed_config=_proposed_config(),
        change_summary="lost auto-apply response",
        diff={"changed_dims": ["welcome_message"]},
        dry_run={},
        impact_score=0.5,
        affected_leads=10,
        rationale="response lost after the simulator may have applied it",
        route="auto_apply",
    )


def _experiment(experiment_id: str, *, status: str, decision: str) -> ExperimentState:
    exp = _winner_experiment()
    exp.experiment_id = experiment_id
    exp.status = status
    exp.decision = decision
    if status != "winner":
        exp.winner_variant = None
    exp.promotion_status = "ready_to_propose_observe_only"
    for arm in exp.arms:
        arm.experiment_id = experiment_id
    return exp


def _counting_client(live_config: dict[str, Any] | None, *, version: int = 2, status: int = 200):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "PUT":
            return httpx.Response(500, json={"error": "should_not_retry"})
        if status != 200:
            return httpx.Response(status, json={"error": "config_unavailable"})
        body = _config(version=version).model_dump()
        if live_config is not None:
            body["config"] = live_config
        return httpx.Response(200, json=body)

    return _mock_client(handler), calls


def _recover(tmp_path, source: str, experiment_id: str, *, status: str, decision: str):
    store = Store(tmp_path / "recover.sqlite")
    exp = _experiment(experiment_id, status=status, decision=decision)
    persist_experiment(store, exp)
    draft = _uncertain_proposal(source, experiment_id, f"prop_{source}")
    persist_proposal(store, draft)
    linked = open_or_refresh_incident(
        store,
        Signal(
            fingerprint=f"linked|{source}",
            workflow_id="nw_renewal",
            signal_type="kpi_drop",
            kpi="engagement",
            dimension=None,
            cohort_id=None,
            caller_id=None,
            severity="medium",
            baseline_value=0.5,
            recent_value=0.3,
            baseline_n=100,
            recent_n=100,
            threshold=0.08,
            message="unrelated to this auto-apply",
            evidence={},
            sim_ts="t",
        ),
    )
    mark_incident_mitigating(store, linked.incident_id, draft.proposal_id)
    client, calls = _counting_client(_proposed_config())
    recovered = reconcile_uncertain_auto_applies(client, store, [_workflow()])
    return store, draft, linked, calls, recovered


def test_winner_auto_apply_lost_response_reuses_the_same_proposal(tmp_path):
    store, draft, linked, calls, recovered = _recover(
        tmp_path,
        "experiment_winner",
        "nw_renewal|default|welcome_message",
        status="winner",
        decision="winner",
    )
    assert [item.proposal_id for item in recovered] == [draft.proposal_id]
    saved = store.get_proposal(draft.proposal_id)
    assert saved["status"] == "auto_applied"
    assert saved["route"] == "auto_apply"
    assert store.get_experiment(draft.source_ref)["promotion_status"] == "promoted_auto_apply"
    snap = store.snapshot_for_proposal(draft.proposal_id, "post_apply")
    assert snap is not None and int(snap["version"]) == 2
    assert store.get_incident(linked.incident_id)["status"] == "mitigating"
    assert not any(method == "PUT" for method, _path in calls)
    assert any(
        row["kind"] == "auto_apply_recovered" and row["related_proposal_id"] == draft.proposal_id
        for row in store.list_audit("nw_renewal")
    )
    again, calls_again = _counting_client(_proposed_config())
    assert reconcile_uncertain_auto_applies(again, store, [_workflow()]) == []
    assert not any(method == "PUT" for method, _path in calls_again)
    store.close()


def test_explore_auto_apply_lost_response_does_not_put_again(tmp_path):
    store, draft, _linked, calls, recovered = _recover(
        tmp_path, "experiment_explore", "exp_explore", status="running", decision="underpowered"
    )
    assert [item.proposal_id for item in recovered] == [draft.proposal_id]
    assert store.get_proposal(draft.proposal_id)["status"] == "auto_applied"
    assert store.get_experiment("exp_explore")["promotion_status"] == "exploration_submitted"
    assert not any(method == "PUT" for method, _path in calls)
    assert len(store.list_proposals("nw_renewal")) == 1
    store.close()


def test_decay_rollback_auto_apply_lost_response_is_recovered(tmp_path):
    store, draft, _linked, calls, recovered = _recover(
        tmp_path, "decay_rollback", "exp_decay", status="winner", decision="winner"
    )
    assert [item.proposal_id for item in recovered] == [draft.proposal_id]
    assert store.get_proposal(draft.proposal_id)["status"] == "auto_applied"
    assert store.get_experiment("exp_decay")["promotion_status"] == "rolled_back"
    assert not any(method == "PUT" for method, _path in calls)
    store.close()


def test_unproven_auto_apply_blocks_other_proposals_for_the_workflow(tmp_path):
    store = Store(tmp_path / "block.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    draft = _uncertain_proposal("experiment_winner", exp.experiment_id, "prop_uncertain")
    draft.proposed_config = _config().config
    persist_proposal(store, draft)
    open_or_refresh_incident(
        store,
        Signal(
            fingerprint="nw|hallucination_spike|block",
            workflow_id="nw_renewal",
            signal_type="hallucination_spike",
            kpi="halluc_rate",
            dimension="workflow",
            cohort_id=None,
            caller_id=None,
            severity="high",
            baseline_value=0.01,
            recent_value=0.08,
            baseline_n=200,
            recent_n=200,
            threshold=0.02,
            message="hallucination flag rate rose",
            evidence={},
            sim_ts="t",
        ),
    )
    client, calls = _counting_client(_proposed_config(), version=2)
    recovered = reconcile_uncertain_auto_applies(client, store, [_workflow()])
    assert recovered == []
    assert store.get_proposal(draft.proposal_id)["status"] == "auto_apply_uncertain"
    assert promote_experiment_winners(client, store, [_workflow()], [_config()], [exp]) == []
    explore = _experiment("exp_explore", status="running", decision="underpowered")
    persist_experiment(store, explore)
    assert propose_experiment_exploration(client, store, [_workflow()], [_config()], [explore]) == []
    assert (
        propose_decay_rollbacks(
            client,
            store,
            [_workflow()],
            [_config()],
            [{"workflow_id": "nw_renewal", "experiment_id": exp.experiment_id, "reason": "decayed"}],
        )
        == []
    )
    state = SimState.model_validate(
        {
            "scenario": "unit",
            "sim_minute": 0,
            "ts": "2027-01-04T00:00:00+05:30",
            "end_minute": 100,
            "finished": False,
            "tick_minutes": 15,
            "credits_remaining": 50,
            "chunks": 1,
            "last_event_id": 1,
        }
    )
    report = run_prompt_incident_cycle(client, store, [_workflow()], [_config()], state)
    assert any("auto_apply_uncertain" in note for note in report.notes)
    assert not any(method in {"PUT", "POST"} for method, _path in calls)
    assert [row["proposal_id"] for row in store.list_proposals("nw_renewal")] == [draft.proposal_id]
    store.close()


def test_live_config_lookup_failure_keeps_the_workflow_blocked(tmp_path):
    store = Store(tmp_path / "lookup.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    draft = _uncertain_proposal("experiment_winner", exp.experiment_id, "prop_lookup")
    persist_proposal(store, draft)
    client, calls = _counting_client(None, status=500)
    recovered = reconcile_uncertain_auto_applies(client, store, [_workflow()])
    assert recovered == []
    assert store.get_proposal(draft.proposal_id)["status"] == "auto_apply_uncertain"
    assert any(row["kind"] == "auto_apply_uncertain_unreconciled" for row in store.list_audit("nw_renewal"))
    assert promote_experiment_winners(client, store, [_workflow()], [_config()], [exp]) == []
    assert not any(method in {"PUT", "POST"} for method, _path in calls)
    assert len(store.list_proposals("nw_renewal")) == 1
    store.close()


def test_http_408_auto_apply_stays_uncertain(tmp_path):
    store = Store(tmp_path / "timeout.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    built = build_winner_proposal(store, _workflow(), _config(), exp)
    assert built is not None
    puts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(request.url.path)
            return httpx.Response(408, json={"error": "request_timeout", "detail": "timed out"})
        raise AssertionError(request.method + " " + request.url.path)

    wf = _workflow(
        governance={
            "auto_apply": {"enabled": True, "allowed_dims": ["welcome_message", "voice"], "max_shift": 1.0},
            "approver_hours": [10, 18],
            "approver_latency_minutes": [30, 120],
            "proposal_ttl_hours": 24,
            "apply_window_hours": 2,
            "max_requests_per_24h": 5,
            "base_reject_rate": 0.0,
            "min_validation_pass_rate": 0.9,
        }
    )
    submitted = submit_proposal(_mock_client(handler, retries=2), store, wf, _config(), built)
    assert submitted.status == "auto_apply_uncertain"
    assert submitted.route == "auto_apply"
    assert puts == ["/workflows/nw_renewal/config"]
    assert store.get_proposal(built.proposal_id)["status"] == "auto_apply_uncertain"
    store.close()


def test_apply_approved_skips_while_auto_apply_is_uncertain(tmp_path):
    store = Store(tmp_path / "apply_block.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    uncertain = _uncertain_proposal("experiment_explore", "exp_explore", "prop_uncertain_block")
    persist_proposal(store, uncertain)
    approved = ProposalDraft(
        proposal_id="prop_approved_blocked",
        workflow_id="nw_renewal",
        base_version=1,
        status="approved",
        source="experiment_winner",
        source_ref=exp.experiment_id,
        changed_dims=["welcome_message"],
        proposed_config=_proposed_config(),
        change_summary="approved winner",
        diff={"changed_dims": ["welcome_message"]},
        dry_run={},
        impact_score=0.5,
        affected_leads=10,
        rationale="approved while another write is uncertain",
        approval_id="apr_blocked",
        route="approval",
    )
    persist_proposal(store, approved)
    store.upsert_controller_approval(
        ControllerApproval(
            approval_id="apr_blocked",
            proposal_id=approved.proposal_id,
            workflow_id="nw_renewal",
            status="approved",
            base_version=1,
            changed_dims=["welcome_message"],
            updated_at="t0",
            apply_by="t_apply",
        )
    )
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    applied = apply_approved(_mock_client(handler), store)
    assert applied == []
    assert calls == []
    assert store.get_proposal(uncertain.proposal_id)["status"] == "auto_apply_uncertain"
    assert store.get_proposal(approved.proposal_id)["status"] == "approved"
    assert store.get_controller_approval("apr_blocked")["status"] == "approved"
    assert any(
        row["kind"] == "apply_deferred_uncertain_auto_apply" for row in store.list_audit("nw_renewal")
    )
    store.close()


def test_run_governance_cycle_blocks_on_live_config_lookup_failure(tmp_path):
    store = Store(tmp_path / "cycle_lookup.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    draft = _uncertain_proposal("experiment_winner", exp.experiment_id, "prop_cycle_lookup")
    persist_proposal(store, draft)
    approved = ProposalDraft(
        proposal_id="prop_cycle_approved",
        workflow_id="nw_renewal",
        base_version=1,
        status="approved",
        source="experiment_winner",
        source_ref=exp.experiment_id,
        changed_dims=["welcome_message"],
        proposed_config=_proposed_config(),
        change_summary="approved winner",
        diff={"changed_dims": ["welcome_message"]},
        dry_run={},
        impact_score=0.5,
        affected_leads=10,
        rationale="must stay blocked while uncertain",
        approval_id="apr_cycle",
        route="approval",
    )
    persist_proposal(store, approved)
    store.upsert_controller_approval(
        ControllerApproval(
            approval_id="apr_cycle",
            proposal_id=approved.proposal_id,
            workflow_id="nw_renewal",
            status="approved",
            base_version=1,
            changed_dims=["welcome_message"],
            updated_at="t0",
        )
    )
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.startswith("/approvals"):
            return httpx.Response(
                200,
                json={
                    "approval_id": "apr_cycle",
                    "proposal_id": approved.proposal_id,
                    "workflow_id": "nw_renewal",
                    "status": "approved",
                    "base_version": 1,
                    "changed_dims": ["welcome_message"],
                    "created_at": "t0",
                    "expires_at": None,
                    "apply_by": None,
                    "decided_at": None,
                    "reasons": [],
                },
            )
        if request.method == "GET" and request.url.path.endswith("/config"):
            return httpx.Response(500, json={"error": "config_unavailable"})
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    report = run_governance_cycle(
        _mock_client(handler), store, [_workflow()], [_config()], [exp]
    )
    assert report.applied == 0
    assert report.auto_applied == 0
    assert store.get_proposal(draft.proposal_id)["status"] == "auto_apply_uncertain"
    assert workflow_has_uncertain_auto_apply(store, "nw_renewal")
    assert not any(method == "POST" and path.endswith("/apply") for method, path in calls)
    assert not any(method == "PUT" for method, _path in calls)
    assert promote_experiment_winners(
        _mock_client(handler), store, [_workflow()], [_config()], [exp]
    ) == []
    store.close()


def test_proven_recovery_unblocks_the_workflow(tmp_path):
    store = Store(tmp_path / "unblock.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    draft = _uncertain_proposal("experiment_winner", exp.experiment_id, "prop_unblock")
    persist_proposal(store, draft)
    client, calls = _counting_client(_proposed_config())
    recovered = reconcile_uncertain_auto_applies(client, store, [_workflow()])
    assert [item.proposal_id for item in recovered] == [draft.proposal_id]
    assert not workflow_has_uncertain_auto_apply(store, "nw_renewal")
    assert store.get_proposal(draft.proposal_id)["status"] == "auto_applied"
    assert not any(method == "PUT" for method, _path in calls)
    store.close()


def test_recovered_auto_apply_count_is_retained_in_governance_report(tmp_path):
    store = Store(tmp_path / "count.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    draft = _uncertain_proposal("experiment_winner", exp.experiment_id, "prop_count")
    persist_proposal(store, draft)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/config"):
            body = _config(version=2).model_dump()
            body["config"] = _proposed_config()
            return httpx.Response(200, json=body)
        if request.method == "GET" and request.url.path.startswith("/approvals"):
            return httpx.Response(404, json={"error": "not_found"})
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    # Pass no live experiment objects so this report only counts recovery, not a
    # same-cycle rebuild against the still-stale in-memory promotion status.
    report = run_governance_cycle(
        _mock_client(handler), store, [_workflow()], [_config()], []
    )
    assert report.auto_applied == 1
    assert any("auto_apply_recovered=1" in note for note in report.notes)
    assert store.get_proposal(draft.proposal_id)["status"] == "auto_applied"
    assert store.get_experiment(exp.experiment_id)["promotion_status"] == "promoted_auto_apply"
    store.close()


def test_recovered_winner_does_not_duplicate_in_same_governance_cycle(tmp_path):
    store = Store(tmp_path / "same_cycle_winner.sqlite")
    exp = _winner_experiment()
    persist_experiment(store, exp)
    draft = _uncertain_proposal("experiment_winner", exp.experiment_id, "prop_same_cycle_winner")
    persist_proposal(store, draft)
    # Simulate a cycle that still holds the pre-recovery in-memory status.
    exp.promotion_status = "ready_to_propose_observe_only"
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/config"):
            body = _config(version=2).model_dump()
            body["config"] = _proposed_config()
            return httpx.Response(200, json=body)
        if request.method == "GET" and request.url.path.startswith("/approvals"):
            return httpx.Response(404, json={"error": "not_found"})
        if request.method == "POST":
            posts.append(request.url.path)
            raise AssertionError(f"unexpected proposal submit {request.url.path}")
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    report = run_governance_cycle(
        _mock_client(handler), store, [_workflow()], [_config()], [exp]
    )
    assert report.auto_applied == 1
    assert posts == []
    assert store.get_proposal(draft.proposal_id)["status"] == "auto_applied"
    assert store.get_experiment(exp.experiment_id)["promotion_status"] == "promoted_auto_apply"
    assert exp.promotion_status == "promoted_auto_apply"
    assert [
        row["proposal_id"]
        for row in store.list_proposals("nw_renewal")
        if row["source"] == "experiment_winner"
    ] == [draft.proposal_id]
    store.close()


def test_recovered_explore_does_not_duplicate_in_same_governance_cycle(tmp_path):
    store = Store(tmp_path / "same_cycle_explore.sqlite")
    exp = _experiment("exp_explore_same", status="running", decision="underpowered")
    persist_experiment(store, exp)
    draft = _uncertain_proposal("experiment_explore", exp.experiment_id, "prop_same_cycle_explore")
    persist_proposal(store, draft)
    exp.promotion_status = "ready_to_propose_observe_only"
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/config"):
            body = _config(version=2).model_dump()
            body["config"] = _proposed_config()
            return httpx.Response(200, json=body)
        if request.method == "GET" and request.url.path.startswith("/approvals"):
            return httpx.Response(404, json={"error": "not_found"})
        if request.method == "POST":
            posts.append(request.url.path)
            raise AssertionError(f"unexpected proposal submit {request.url.path}")
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    report = run_governance_cycle(
        _mock_client(handler), store, [_workflow()], [_config()], [exp]
    )
    assert report.auto_applied == 1
    assert posts == []
    assert store.get_proposal(draft.proposal_id)["status"] == "auto_applied"
    assert store.get_experiment(exp.experiment_id)["promotion_status"] == "exploration_submitted"
    assert exp.promotion_status == "exploration_submitted"
    assert [
        row["proposal_id"]
        for row in store.list_proposals("nw_renewal")
        if row["source"] == "experiment_explore"
    ] == [draft.proposal_id]
    store.close()


def test_stale_in_memory_experiment_does_not_create_duplicate_proposals(tmp_path):
    store = Store(tmp_path / "stale_memory.sqlite")
    winner = _winner_experiment()
    winner.promotion_status = "promoted_auto_apply"
    persist_experiment(store, winner)
    explore = _experiment("exp_explore_memory", status="running", decision="underpowered")
    explore.promotion_status = "exploration_submitted"
    persist_experiment(store, explore)

    stale_winner = _winner_experiment()
    stale_winner.promotion_status = "ready_to_propose_observe_only"
    stale_explore = _experiment("exp_explore_memory", status="running", decision="underpowered")
    stale_explore.promotion_status = "ready_to_propose_observe_only"

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    client = _mock_client(handler)
    assert (
        promote_experiment_winners(client, store, [_workflow()], [_config()], [stale_winner])
        == []
    )
    assert (
        propose_experiment_exploration(client, store, [_workflow()], [_config()], [stale_explore])
        == []
    )
    assert stale_winner.promotion_status == "promoted_auto_apply"
    assert stale_explore.promotion_status == "exploration_submitted"
    store.close()


def test_persisted_retryable_and_rejected_promotion_statuses_are_respected(tmp_path):
    store = Store(tmp_path / "status_sync.sqlite")
    stale = _winner_experiment()
    stale.experiment_id = "exp_stale_sync"
    for arm in stale.arms:
        arm.experiment_id = stale.experiment_id
    stale.promotion_status = "promotion_stale"
    persist_experiment(store, stale)

    rejected = _winner_experiment()
    rejected.experiment_id = "exp_rejected_sync"
    for arm in rejected.arms:
        arm.experiment_id = rejected.experiment_id
    rejected.promotion_status = "promotion_rejected"
    persist_experiment(store, rejected)

    stale_memory = _winner_experiment()
    stale_memory.experiment_id = "exp_stale_sync"
    for arm in stale_memory.arms:
        arm.experiment_id = stale_memory.experiment_id
    stale_memory.promotion_status = "ready_to_propose_observe_only"

    rejected_memory = _winner_experiment()
    rejected_memory.experiment_id = "exp_rejected_sync"
    for arm in rejected_memory.arms:
        arm.experiment_id = rejected_memory.experiment_id
    rejected_memory.promotion_status = "ready_to_propose_observe_only"

    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/config"):
            return httpx.Response(200, json=_config().model_dump())
        if request.method == "POST" and request.url.path == "/approvals":
            body = json.loads(request.content.decode())
            posts.append(body["proposal_id"])
            return httpx.Response(
                200,
                json={
                    "approval_id": f"apr_{len(posts)}",
                    "proposal_id": body["proposal_id"],
                    "workflow_id": "nw_renewal",
                    "status": "pending",
                    "base_version": body["base_version"],
                    "changed_dims": ["welcome_message"],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    client = _mock_client(handler)
    stale_out = promote_experiment_winners(
        client, store, [_workflow()], [_config()], [stale_memory]
    )
    assert len(stale_out) == 1
    assert stale_memory.promotion_status == "promotion_stale"
    rejected_out = promote_experiment_winners(
        client, store, [_workflow()], [_config()], [rejected_memory]
    )
    assert rejected_out == []
    assert rejected_memory.promotion_status == "promotion_rejected"
    assert len(posts) == 1
    store.close()


def test_recovered_prompt_proposal_syncs_llm_attempt_without_re_suggest(tmp_path):
    store = Store(tmp_path / "prompt_recover.sqlite")
    incident = open_or_refresh_incident(
        store,
        Signal(
            fingerprint="nw|hallucination_spike|recover",
            workflow_id="nw_renewal",
            signal_type="hallucination_spike",
            kpi="halluc_rate",
            dimension="workflow",
            cohort_id=None,
            caller_id=None,
            severity="high",
            baseline_value=0.01,
            recent_value=0.08,
            baseline_n=200,
            recent_n=200,
            threshold=0.02,
            message="hallucination flag rate rose",
            evidence={},
            sim_ts="t",
        ),
    )
    proposed = {
        "prompt_version": "P2",
        "cohorts": [],
        "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0}}},
        "schedule": {},
    }
    draft = ProposalDraft(
        proposal_id="prop_prompt_uncertain",
        workflow_id="nw_renewal",
        base_version=1,
        status="auto_apply_uncertain",
        source="prompt_incident",
        source_ref=incident.incident_id,
        changed_dims=["prompt_version"],
        proposed_config=proposed,
        change_summary="prompt fix",
        diff={"changed_dims": ["prompt_version"]},
        dry_run={},
        impact_score=0.4,
        affected_leads=10,
        rationale="lost prompt auto-apply response",
        route="auto_apply",
    )
    persist_proposal(store, draft)
    mark_incident_mitigating(store, incident.incident_id, draft.proposal_id)
    store.upsert_llm_attempt(
        LlmAttempt(
            attempt_id="llm_prompt_uncertain",
            incident_id=incident.incident_id,
            workflow_id="nw_renewal",
            status="proposed",
            problem="hallucination",
            created_at="t0",
            updated_at="t0",
            selected_prompt_version="P2",
            selected_validation_run_id="val_1",
            related_proposal_id=draft.proposal_id,
        )
    )
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/config"):
            body = _config(version=2).model_dump()
            body["config"] = proposed
            return httpx.Response(200, json=body)
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    recovered = reconcile_uncertain_auto_applies(_mock_client(handler), store, [_workflow()])
    assert [item.proposal_id for item in recovered] == [draft.proposal_id]
    attempt = store.list_llm_attempts(incident.incident_id)[0]
    assert attempt["status"] == "submitted"
    assert attempt["related_proposal_id"] == draft.proposal_id
    ok, reason = should_start_attempt(store, incident)
    assert ok is False
    assert "proposal_already_in_flight_or_applied" in reason or "prior_attempt" in reason
    state = SimState.model_validate(
        {
            "scenario": "unit",
            "sim_minute": 0,
            "ts": "2027-01-04T00:00:00+05:30",
            "end_minute": 100,
            "finished": False,
            "tick_minutes": 15,
            "credits_remaining": 50,
            "chunks": 1,
            "last_event_id": 1,
        }
    )
    report = run_prompt_incident_cycle(
        _mock_client(handler), store, [_workflow()], [_config()], state
    )
    assert report.attempted == 0
    assert not any(path.startswith("/llm/") for _method, path in calls)
    assert not any(path.startswith("/validation/") for _method, path in calls)
    assert not any(method == "PUT" for method, _path in calls)
    store.close()
