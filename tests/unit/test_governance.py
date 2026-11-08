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
    next_advance_minutes,
    promote_experiment_winners,
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
