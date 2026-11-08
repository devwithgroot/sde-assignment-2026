"""Experiment planning, allocations, and winner selection."""

from __future__ import annotations

import json

from controller.api.models import ChunkMeta, Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.experiments import (
    balanced_allocation,
    evaluate_experiment,
    plan_experiment,
    run_workflow_experiments,
    validate_variants,
)
from controller.services.joiner import project_events


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
            "auto_apply": {"enabled": False, "allowed_dims": [], "max_shift": 0},
            "approver_hours": [10, 18],
            "approver_latency_minutes": [30, 120],
            "proposal_ttl_hours": 24,
            "apply_window_hours": 2,
            "max_requests_per_24h": 5,
            "base_reject_rate": 0.1,
            "min_validation_pass_rate": 0.9,
        },
        "current_version": 1,
    }
    base.update(overrides)
    return Workflow.model_validate(base)


def _config(workflow_id: str = "nw_renewal") -> WorkflowConfig:
    return WorkflowConfig.model_validate(
        {
            "workflow_id": workflow_id,
            "version": 1,
            "actor": "system",
            "applied_at": None,
            "config": {
                "prompt_version": "P1",
                "cohorts": [],
                "allocations": {"default": {"welcome_message": {"W1": 0.7, "W2": 0.3}, "voice": {"V1": 1.0}}},
                "schedule": {},
            },
        }
    )


def _meta(n: int) -> ChunkMeta:
    return ChunkMeta(
        chunk=0,
        events=n,
        first_event_id=1,
        kind="history",
        last_event_id=n,
        path="x",
        until_minute=0,
        until_ts="t",
    )


def _call(event_id: int, lead_id: str, *, welcome: str, voice: str, connected: bool, engaged: bool) -> dict:
    return {
        "type": "call",
        "event_id": event_id,
        "workflow_id": "nw_renewal",
        "campaign_id": "c1",
        "lead_id": lead_id,
        "person_id": f"p-{lead_id}",
        "attempt_no": 1,
        "cohort_id": "default",
        "variant": {
            "caller_id": "D1",
            "voice": voice,
            "welcome_message": welcome,
            "prompt_version": "P1",
        },
        "connected": connected,
        "engaged": engaged,
        "disposition": "concluded_interested" if engaged else "no_answer",
        "ts": "2027-01-04T11:00:00",
        "qa": {"hallucination_flag": False, "flow_adherence": 0.9},
    }


def _seed_arms(store: Store, *, control_engage_rate: float, treat_engage_rate: float, n: int = 120) -> None:
    events: list[dict] = []
    eid = 1
    for i in range(n):
        lead = f"lc{i}"
        events.append(
            {
                "type": "lead",
                "event_id": eid,
                "workflow_id": "nw_renewal",
                "campaign_id": "c1",
                "lead_id": lead,
                "person_id": f"p-{lead}",
                "features": {"lang": "hi"},
                "ts": "2027-01-04T10:00:00",
            }
        )
        eid += 1
        events.append(_call(eid, lead, welcome="W1", voice="V1", connected=True, engaged=(i / n) < control_engage_rate))
        eid += 1
    for i in range(n):
        lead = f"lt{i}"
        events.append(
            {
                "type": "lead",
                "event_id": eid,
                "workflow_id": "nw_renewal",
                "campaign_id": "c1",
                "lead_id": lead,
                "person_id": f"p-{lead}",
                "features": {"lang": "hi"},
                "ts": "2027-01-04T10:00:00",
            }
        )
        eid += 1
        events.append(_call(eid, lead, welcome="W2", voice="V1", connected=True, engaged=(i / n) < treat_engage_rate))
        eid += 1
    store.commit_chunk(_meta(len(events)), "\n".join(json.dumps(e) for e in events))
    project_events(store)


def test_balanced_allocation_two_arms():
    alloc = balanced_allocation(["W1", "W2"], "W1")
    assert abs(sum(alloc.values()) - 1.0) < 1e-6
    assert alloc["W1"] == alloc["W2"]


def test_validate_variants_rejects_unknown():
    wf = _workflow()
    assert validate_variants("welcome_message", ["W1", "W9", "W2"], wf) == ["W1", "W2"]


def test_underpowered_experiment(tmp_path):
    store = Store(tmp_path / "e.sqlite")
    # Tiny samples — cannot decide.
    events = [
        {
            "type": "lead",
            "event_id": 1,
            "workflow_id": "nw_renewal",
            "campaign_id": "c1",
            "lead_id": "l1",
            "person_id": "p1",
            "features": {"lang": "hi"},
            "ts": "2027-01-04T10:00:00",
        },
        _call(2, "l1", welcome="W1", voice="V1", connected=True, engaged=True),
        {
            "type": "lead",
            "event_id": 3,
            "workflow_id": "nw_renewal",
            "campaign_id": "c1",
            "lead_id": "l2",
            "person_id": "p2",
            "features": {"lang": "hi"},
            "ts": "2027-01-04T10:00:00",
        },
        _call(4, "l2", welcome="W2", voice="V1", connected=True, engaged=False),
    ]
    store.commit_chunk(_meta(4), "\n".join(json.dumps(e) for e in events))
    project_events(store)
    # Force plan even with tiny cohort by calling plan_experiment directly.
    state = plan_experiment(store, _workflow(), _config(), min_sample=100)
    assert state is not None
    evaluated = evaluate_experiment(state)
    assert evaluated.status == "running"
    assert evaluated.decision == "underpowered"
    assert evaluated.winner_variant is None
    assert evaluated.evidence.get("insufficient_data") is True


def test_known_winner_welcome(tmp_path):
    store = Store(tmp_path / "win.sqlite")
    _seed_arms(store, control_engage_rate=0.30, treat_engage_rate=0.60, n=150)
    results = run_workflow_experiments(store, _workflow(), _config(), min_sample=100)
    assert results
    exp = results[0]
    assert exp.dimension == "welcome_message"
    assert exp.status == "winner"
    assert exp.winner_variant == "W2"
    assert exp.promotion_status == "ready_to_propose_observe_only"
    assert exp.decision_reason
    # Deterministic re-run
    again = run_workflow_experiments(store, _workflow(), _config(), min_sample=100)
    assert again[0].winner_variant == exp.winner_variant
    assert again[0].decision == exp.decision


def test_no_difference_inconclusive(tmp_path):
    store = Store(tmp_path / "tie.sqlite")
    _seed_arms(store, control_engage_rate=0.40, treat_engage_rate=0.40, n=150)
    results = run_workflow_experiments(store, _workflow(), _config(), min_sample=100)
    assert results
    exp = results[0]
    assert exp.status == "inconclusive"
    assert exp.decision == "no_winner"
    assert exp.winner_variant is None


def test_one_dimension_at_a_time_conflict(tmp_path):
    store = Store(tmp_path / "one.sqlite")
    _seed_arms(store, control_engage_rate=0.3, treat_engage_rate=0.35, n=120)
    # Start with underpowered-style running experiment by evaluating with high min_sample.
    state = plan_experiment(store, _workflow(), _config(), dimension="welcome_message", min_sample=10_000)
    assert state is not None
    evaluated = evaluate_experiment(state)
    from controller.services.experiments import persist_experiment

    persist_experiment(store, evaluated)
    assert evaluated.status == "running"
    # Planning voice while welcome is active must be blocked by conflict check.
    blocked = plan_experiment(store, _workflow(), _config(), dimension="voice", min_sample=100)
    assert blocked is None


def test_guardrail_violation(tmp_path):
    """Challenger lifts engagement but destroys pickup beyond max drop."""
    store = Store(tmp_path / "g.sqlite")
    events: list[dict] = []
    eid = 1
    n = 200
    # Control: pickup 50% (100), engagement 40% of connects.
    for i in range(n):
        lead = f"c{i}"
        events.append(
            {
                "type": "lead",
                "event_id": eid,
                "workflow_id": "nw_renewal",
                "campaign_id": "c1",
                "lead_id": lead,
                "person_id": f"p-{lead}",
                "features": {"lang": "hi"},
                "ts": "2027-01-04T10:00:00",
            }
        )
        eid += 1
        connected = i < 100
        engaged = i < 40
        events.append(_call(eid, lead, welcome="W1", voice="V1", connected=connected, engaged=engaged))
        eid += 1
    # Treatment: pickup 35% (70) → drop 0.15 > 0.02; engagement 90% of connects.
    for i in range(n):
        lead = f"t{i}"
        events.append(
            {
                "type": "lead",
                "event_id": eid,
                "workflow_id": "nw_renewal",
                "campaign_id": "c1",
                "lead_id": lead,
                "person_id": f"p-{lead}",
                "features": {"lang": "hi"},
                "ts": "2027-01-04T10:00:00",
            }
        )
        eid += 1
        connected = i < 70
        engaged = i < 63
        events.append(_call(eid, lead, welcome="W2", voice="V1", connected=connected, engaged=engaged))
        eid += 1
    store.commit_chunk(_meta(len(events)), "\n".join(json.dumps(e) for e in events))
    project_events(store)
    wf = _workflow(objective={"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}})
    results = run_workflow_experiments(store, wf, _config(), min_sample=50)
    assert results
    exp = results[0]
    arm_w2 = next(a for a in exp.arms if a.variant == "W2")
    assert arm_w2.verdict == "guardrail_violation" or exp.status == "guardrail_violation"
