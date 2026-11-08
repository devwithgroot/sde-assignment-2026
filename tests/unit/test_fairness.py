"""Fair scheduler, credit reserves, and starvation checks."""

from __future__ import annotations

from controller.api.models import Workflow
from controller.persistence.repositories import Store
from controller.services.credits import CreditBudget
from controller.services.fairness import (
    ACTION_EXPERIMENT,
    FairScheduler,
    simulate_horizon,
)
from controller.services.incidents import Incident
from controller.services.observability import build_supervisor_summary, emit_alert


def _wf(wid: str, tenant: str) -> Workflow:
    return Workflow.model_validate(
        {
            "workflow_id": wid,
            "tenant_id": tenant,
            "languages": ["en"],
            "dialling_window": [10, 18],
            "dials_per_hour": 100,
            "cadence": [0],
            "min_gap_minutes": 60,
            "daily_batch": {},
            "mid_flight": "allowed",
            "objective": {"primary": "engagement_rate", "guardrail": {}},
            "options": {
                "welcome_message": ["W1"],
                "voice": ["V1"],
                "caller_id": ["D1"],
                "prompt_version": ["P1"],
            },
            "prompt_catalog": {"P1": "x"},
            "governance": {
                "auto_apply": {"enabled": False, "allowed_dims": [], "max_shift": 0},
                "approver_hours": [10, 18],
                "approver_latency_minutes": [30, 60],
                "proposal_ttl_hours": 24,
                "apply_window_hours": 2,
                "max_requests_per_24h": 3,
                "base_reject_rate": 0.0,
                "min_validation_pass_rate": 0.9,
            },
            "current_version": 1,
        }
    )


def test_round_robin_no_starvation(tmp_path):
    store = Store(tmp_path / "f.sqlite")
    workflows = [_wf("a", "t1"), _wf("b", "t1"), _wf("c", "t2")]
    counts = simulate_horizon(store, workflows, ticks=30, action_kind=ACTION_EXPERIMENT)
    assert counts["a"] >= 8
    assert counts["b"] >= 8
    assert counts["c"] >= 8
    # Deterministic re-run on fresh store state already advanced — new store for equality.
    store2 = Store(tmp_path / "f2.sqlite")
    c1 = simulate_horizon(store2, workflows, ticks=9)
    store3 = Store(tmp_path / "f3.sqlite")
    c2 = simulate_horizon(store3, workflows, ticks=9)
    assert c1 == c2


def test_weighted_preference(tmp_path):
    store = Store(tmp_path / "fw.sqlite")
    workflows = [_wf("heavy", "t1"), _wf("light", "t1")]
    sched = FairScheduler(store)
    sched.ensure_workflows(workflows, weights={"heavy": 3.0, "light": 1.0})
    counts = {"heavy": 0, "light": 0}
    for _ in range(40):
        d = sched.select_next(sched.build_candidates(workflows, action_kind=ACTION_EXPERIMENT))
        assert d.selected is not None
        counts[d.selected.workflow_id] += 1
    assert counts["heavy"] > counts["light"]
    assert counts["light"] >= 5  # not starved


def test_incident_priority_preempts_but_does_not_starve(tmp_path):
    store = Store(tmp_path / "fp.sqlite")
    workflows = [_wf("urgent", "t1"), _wf("routine", "t1")]
    store.upsert_incident(
        Incident(
            incident_id="inc1",
            workflow_id="urgent",
            tenant_id="t1",
            signal_fingerprint="fp1",
            signal_type="hallucination_spike",
            severity="high",
            status="detected",
            title="h",
            message="m",
            evidence={},
            created_at="t",
            updated_at="t",
        )
    )
    sched = FairScheduler(store)
    sched.ensure_workflows(workflows)
    counts = {"urgent": 0, "routine": 0}
    for _ in range(20):
        d = sched.select_next(sched.build_candidates(workflows, action_kind=ACTION_EXPERIMENT))
        counts[d.selected.workflow_id] += 1
    assert counts["urgent"] > counts["routine"]
    assert counts["routine"] >= 1


def test_emergency_reserve_blocks_experiment_spend(tmp_path):
    store = Store(tmp_path / "fc.sqlite")
    budget = CreditBudget(store, emergency_reserve=30, experiment_budget_cap=20)
    # 35 credits: 5 above reserve.
    d = budget.can_spend(10, purpose="experiment", sim_credits=35)
    assert d.allowed is False
    assert "reserve_protected" in d.reason
    # Emergency prompt can spend.
    d2 = budget.can_spend(3, purpose="emergency_prompt", sim_credits=35)
    assert d2.allowed is True
    assert d2.pool == "emergency"
    budget.record_spend(3, pool="emergency", sim_credits_after=32)
    row = store.get_credit_budget()
    assert float(row["emergency_spent"]) == 3


def test_experiment_cap_exhaustion(tmp_path):
    store = Store(tmp_path / "fcap.sqlite")
    budget = CreditBudget(store, emergency_reserve=10, experiment_budget_cap=5)
    assert budget.can_spend(5, purpose="experiment", sim_credits=100).allowed is True
    budget.record_spend(5, pool="experiment", sim_credits_after=95)
    d = budget.can_spend(1, purpose="experiment", sim_credits=95)
    assert d.allowed is False
    assert "experiment_budget_exhausted" in d.reason


def test_deferral_and_supervisor_summary(tmp_path):
    store = Store(tmp_path / "fs.sqlite")
    workflows = [_wf("a", "t1"), _wf("b", "t1")]
    sched = FairScheduler(store, max_wait_before_starvation_alert=2)
    sched.ensure_workflows(workflows)
    for _ in range(5):
        sched.select_next(sched.build_candidates(workflows, action_kind=ACTION_EXPERIMENT))
    sched.record_deferral(
        workflow_id="b",
        tenant_id="t1",
        action_kind=ACTION_EXPERIMENT,
        priority=10,
        reason="budget",
    )
    emit_alert(store, kind="test", severity="low", message="hello", workflow_id="a")
    summary = build_supervisor_summary(store, starvation_wait=2)
    assert summary.deferred_open
    assert summary.actions_by_tenant.get("t1", 0) >= 1
    assert summary.counters
