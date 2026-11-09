"""Experiment planning, allocations, and winner selection."""

from __future__ import annotations

import json

from controller.api.models import ChunkMeta, Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.change_builder import build_explore_proposal
from controller.services.experiments import (
    ExperimentArmState,
    ExperimentState,
    NO_SAFE_CANDIDATES,
    balanced_allocation,
    evaluate_experiment,
    persist_experiment,
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


def _persist_arm_verdict(
    store: Store,
    *,
    experiment_id: str = "nw_renewal|default|welcome_message",
    workflow_id: str = "nw_renewal",
    cohort_id: str = "default",
    dimension: str = "welcome_message",
    control: str = "W1",
    variants: dict[str, str],
    planned: dict[str, float] | None = None,
    promotion_status: str = "observe_only_not_promoted",
) -> ExperimentState:
    planned = planned or {v: round(1.0 / len(variants), 6) for v in variants}
    state = ExperimentState(
        experiment_id=experiment_id,
        workflow_id=workflow_id,
        tenant_id="northwind",
        cohort_id=cohort_id,
        dimension=dimension,
        primary_kpi="engagement",
        guardrail_kpi="pickup",
        guardrail_max_drop=0.02,
        status="guardrail_violation" if "guardrail_violation" in variants.values() else "inconclusive",
        control_variant=control,
        planned_allocation=planned,
        min_sample_per_arm=100,
        alpha=0.05,
        min_effect=0.02,
        created_at="t0",
        updated_at="t1",
        decision="guardrail_violation" if "guardrail_violation" in variants.values() else "no_winner",
        decision_reason="seeded",
        winner_variant=None,
        promotion_status=promotion_status,
        evidence={
            "guardrail_violations": [v for v, verdict in variants.items() if verdict == "guardrail_violation"],
            "losers": [v for v, verdict in variants.items() if verdict == "loser"],
        },
        arms=[
            ExperimentArmState(
                experiment_id=experiment_id,
                variant=variant,
                is_control=(variant == control),
                planned_weight=planned.get(variant, 0.0),
                dials=120,
                connected=100,
                engaged=40,
                primary_rate=0.4,
                guardrail_rate=0.5,
                sample_n=100,
                sample_ok=True,
                verdict=verdict,
            )
            for variant, verdict in variants.items()
        ],
    )
    persist_experiment(store, state)
    return state


def test_plan_excludes_prior_guardrail_violation(tmp_path):
    store = Store(tmp_path / "excl.sqlite")
    _seed_arms(store, control_engage_rate=0.3, treat_engage_rate=0.35, n=40)
    wf = _workflow(
        options={
            "welcome_message": ["W1", "W2", "W3"],
            "voice": ["V1", "V2"],
            "caller_id": ["D1"],
            "prompt_version": ["P1"],
        }
    )
    _persist_arm_verdict(
        store,
        variants={"W1": "control", "W2": "guardrail_violation", "W3": "underpowered"},
        planned={"W1": 0.34, "W2": 0.33, "W3": 0.33},
    )
    planned = plan_experiment(store, wf, _config(), dimension="welcome_message", min_sample=100)
    assert planned is not None
    assert planned.status == "planned"
    assert "W2" not in planned.planned_allocation
    assert set(planned.planned_allocation) == {"W1", "W3"}
    assert abs(sum(planned.planned_allocation.values()) - 1.0) < 1e-6
    assert planned.control_variant != "W2"
    assert planned.evidence.get("excluded_guardrail_violations") == ["W2"]
    store.close()


def test_guardrail_exclusion_survives_persistence_and_replan(tmp_path):
    db = tmp_path / "persist_excl.sqlite"
    store = Store(db)
    _seed_arms(store, control_engage_rate=0.3, treat_engage_rate=0.35, n=40)
    wf = _workflow(
        options={
            "welcome_message": ["W1", "W2", "W3"],
            "voice": ["V1", "V2"],
            "caller_id": ["D1"],
            "prompt_version": ["P1"],
        }
    )
    _persist_arm_verdict(
        store,
        variants={"W1": "control", "W2": "guardrail_violation", "W3": "inconclusive"},
        planned={"W1": 0.34, "W2": 0.33, "W3": 0.33},
    )
    store.close()

    store2 = Store(db)
    planned = plan_experiment(store2, wf, _config(), dimension="welcome_message", min_sample=100)
    assert planned is not None
    assert "W2" not in planned.planned_allocation
    assert set(planned.planned_allocation) <= {"W1", "W3"}
    store2.close()


def test_guardrail_violating_control_never_selected(tmp_path):
    store = Store(tmp_path / "ctrl.sqlite")
    _seed_arms(store, control_engage_rate=0.3, treat_engage_rate=0.35, n=40)
    wf = _workflow(
        options={
            "welcome_message": ["W1", "W2", "W3"],
            "voice": ["V1", "V2"],
            "caller_id": ["D1"],
            "prompt_version": ["P1"],
        }
    )
    # W1 dominates current allocation but was previously marked harmful.
    _persist_arm_verdict(
        store,
        control="W1",
        variants={"W1": "guardrail_violation", "W2": "control", "W3": "inconclusive"},
        planned={"W1": 0.7, "W2": 0.2, "W3": 0.1},
    )
    cfg = _config()
    cfg.config["allocations"]["default"]["welcome_message"] = {"W1": 0.9, "W2": 0.05, "W3": 0.05}
    planned = plan_experiment(store, wf, cfg, dimension="welcome_message", min_sample=100)
    assert planned is not None
    assert planned.control_variant != "W1"
    assert "W1" not in planned.planned_allocation
    assert planned.control_variant in {"W2", "W3"}
    store.close()


def test_no_safe_candidates_blocks_explore_and_records_decision(tmp_path):
    store = Store(tmp_path / "nosafe.sqlite")
    _seed_arms(store, control_engage_rate=0.3, treat_engage_rate=0.35, n=40)
    wf = _workflow()
    _persist_arm_verdict(
        store,
        variants={"W1": "control", "W2": "guardrail_violation"},
        planned={"W1": 0.5, "W2": 0.5},
    )
    planned = plan_experiment(store, wf, _config(), dimension="welcome_message", min_sample=100)
    assert planned is not None
    assert planned.status == NO_SAFE_CANDIDATES
    assert planned.decision == NO_SAFE_CANDIDATES
    assert planned.planned_allocation == {}
    assert any(row["kind"] == NO_SAFE_CANDIDATES for row in store.list_audit("nw_renewal"))

    evaluated = evaluate_experiment(planned)
    persist_experiment(store, evaluated)
    draft = build_explore_proposal(store, wf, _config(), evaluated)
    assert draft is None
    store.close()


def test_no_safe_candidates_full_pipeline_terminal_and_sticky(tmp_path):
    """plan → evaluate → persist → reload → replan → explore must keep no_safe_candidates."""
    from controller.services.governance import propose_experiment_exploration

    db = tmp_path / "nosafe_pipeline.sqlite"
    store = Store(db)
    _seed_arms(store, control_engage_rate=0.3, treat_engage_rate=0.35, n=120)
    wf = _workflow()
    cfg = _config()
    _persist_arm_verdict(
        store,
        variants={"W1": "control", "W2": "guardrail_violation"},
        planned={"W1": 0.5, "W2": 0.5},
        promotion_status="promotion_rejected",
    )

    results = run_workflow_experiments(store, wf, cfg, min_sample=100)
    assert results
    exp = results[0]
    assert exp.status == NO_SAFE_CANDIDATES
    assert exp.decision == NO_SAFE_CANDIDATES
    assert exp.decision != "no_winner"
    assert exp.status != "inconclusive"
    assert exp.planned_allocation == {}
    assert exp.promotion_status == "promotion_rejected"
    assert store.active_experiment_dimension("nw_renewal", "default") is None

    row = store.get_experiment(exp.experiment_id)
    assert row is not None
    assert row["status"] == NO_SAFE_CANDIDATES
    assert row["decision"] == NO_SAFE_CANDIDATES
    assert row["promotion_status"] == "promotion_rejected"
    store.close()

    store2 = Store(db)
    again = run_workflow_experiments(store2, wf, cfg, min_sample=100)
    assert again
    assert again[0].status == NO_SAFE_CANDIDATES
    assert again[0].decision == NO_SAFE_CANDIDATES
    assert again[0].promotion_status == "promotion_rejected"
    assert store2.active_experiment_dimension("nw_renewal", "default") is None

    class _Client:
        def workflow_config(self, workflow_id: str) -> WorkflowConfig:
            return cfg

    assert build_explore_proposal(store2, wf, cfg, again[0]) is None
    assert propose_experiment_exploration(_Client(), store2, [wf], [cfg], again) == []
    assert any(r["kind"] == NO_SAFE_CANDIDATES for r in store2.list_audit("nw_renewal"))
    store2.close()


def test_loser_is_not_treated_as_harmful(tmp_path):
    store = Store(tmp_path / "loser.sqlite")
    _seed_arms(store, control_engage_rate=0.3, treat_engage_rate=0.35, n=40)
    wf = _workflow(
        options={
            "welcome_message": ["W1", "W2", "W3"],
            "voice": ["V1", "V2"],
            "caller_id": ["D1"],
            "prompt_version": ["P1"],
        }
    )
    _persist_arm_verdict(
        store,
        variants={"W1": "control", "W2": "loser", "W3": "inconclusive"},
        planned={"W1": 0.34, "W2": 0.33, "W3": 0.33},
    )
    planned = plan_experiment(store, wf, _config(), dimension="welcome_message", min_sample=100)
    assert planned is not None
    assert "W2" in planned.planned_allocation
    assert planned.evidence.get("excluded_guardrail_violations") == []
    store.close()


def test_build_explore_cannot_reintroduce_persisted_harmful_variant(tmp_path):
    store = Store(tmp_path / "reintro.sqlite")
    wf = _workflow(
        options={
            "welcome_message": ["W1", "W2", "W3"],
            "voice": ["V1", "V2"],
            "caller_id": ["D1"],
            "prompt_version": ["P1"],
        }
    )
    _persist_arm_verdict(
        store,
        variants={"W1": "control", "W2": "guardrail_violation", "W3": "underpowered"},
        planned={"W1": 0.34, "W2": 0.33, "W3": 0.33},
    )
    # Stale in-memory allocation still lists the harmful variant.
    stale = ExperimentState(
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
        planned_allocation={"W1": 0.34, "W2": 0.33, "W3": 0.33},
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
    draft = build_explore_proposal(store, wf, _config(), stale)
    assert draft is not None
    welcome = draft.proposed_config["allocations"]["default"]["welcome_message"]
    assert "W2" not in welcome
    assert set(welcome) == {"W1", "W3"}
    assert abs(sum(welcome.values()) - 1.0) < 1e-6
    assert draft.dry_run.get("guardrail_ok") is None
    assert draft.dry_run.get("guardrail_assessment") == "not_assessed"
    assert "not_assessed" in draft.dry_run["limitation"]
    # Voice and other cohorts untouched.
    assert draft.proposed_config["allocations"]["default"]["voice"] == {"V1": 1.0}
    assert draft.changed_dims == ["welcome_message"]
    store.close()


def test_build_explore_no_safe_candidates_when_only_harmful_remain(tmp_path):
    store = Store(tmp_path / "explore_nosafe.sqlite")
    wf = _workflow()
    _persist_arm_verdict(
        store,
        variants={"W1": "control", "W2": "guardrail_violation"},
        planned={"W1": 0.5, "W2": 0.5},
    )
    stale = ExperimentState(
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
    assert build_explore_proposal(store, wf, _config(), stale) is None
    assert any(row["kind"] == NO_SAFE_CANDIDATES for row in store.list_audit("nw_renewal"))
    store.close()


def test_build_explore_dry_run_does_not_claim_guardrail_ok(tmp_path):
    store = Store(tmp_path / "dry.sqlite")
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
    assert draft.dry_run.get("guardrail_ok") is None
    assert draft.dry_run.get("guardrail_assessment") == "not_assessed"
    assert "not been assessed" in draft.dry_run["limitation"].lower() or "not_assessed" in draft.dry_run[
        "limitation"
    ]
    store.close()


def test_explore_proposal_does_not_modify_other_cohort_or_dimension(tmp_path):
    store = Store(tmp_path / "scope.sqlite")
    cfg = _config()
    cfg.config["allocations"] = {
        "default": {
            "welcome_message": {"W1": 1.0},
            "voice": {"V1": 0.6, "V2": 0.4},
            "caller_id": {"D1": 1.0},
        },
        "cohort_north": {
            "welcome_message": {"W1": 0.8, "W2": 0.2},
            "voice": {"V1": 1.0},
            "caller_id": {"D1": 1.0},
        },
    }
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
    draft = build_explore_proposal(store, _workflow(), cfg, exp)
    assert draft is not None
    assert draft.changed_dims == ["welcome_message"]
    assert draft.proposed_config["allocations"]["default"]["voice"] == {"V1": 0.6, "V2": 0.4}
    assert draft.proposed_config["allocations"]["default"]["caller_id"] == {"D1": 1.0}
    assert draft.proposed_config["allocations"]["cohort_north"] == cfg.config["allocations"]["cohort_north"]
    store.close()
