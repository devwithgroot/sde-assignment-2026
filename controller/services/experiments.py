"""Observational workflow experiments (welcome_message / voice).

Phase 5 is observe-only: analyze call variants already present in history,
record planned balanced traffic splits, run deterministic stats, and store
winner/inconclusive decisions. It does not POST approvals or apply config.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from controller.api.models import Workflow, WorkflowConfig
from controller.domain.cohorts import DEFAULT_COHORT_ID, MIN_COHORT_SAMPLE, cohort_is_actionable
from controller.persistence.repositories import Store
from controller.services.statistics import (
    Proportion,
    decide_challenger,
    holm_adjust,
    two_proportion_ztest,
)

EXPERIMENT_DIMS = ("welcome_message", "voice")
DEFAULT_ALPHA = 0.05
DEFAULT_MIN_EFFECT = 0.02
CONTROL_FLOOR = 0.30
EXPLORATION_FLOOR = 0.10
ACTIVE_STATUSES = frozenset({"planned", "running"})
TERMINAL_STATUSES = frozenset({"winner", "inconclusive", "guardrail_violation", "stopped"})


@dataclass
class ExperimentArmState:
    experiment_id: str
    variant: str
    is_control: bool
    planned_weight: float
    dials: int = 0
    connected: int = 0
    engaged: int = 0
    primary_rate: float | None = None
    guardrail_rate: float | None = None
    sample_n: int = 0
    sample_ok: bool = False
    verdict: str | None = None
    effect_size: float | None = None
    p_value: float | None = None
    adjusted_alpha: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    reason: str | None = None


@dataclass
class ExperimentState:
    experiment_id: str
    workflow_id: str
    tenant_id: str | None
    cohort_id: str
    dimension: str
    primary_kpi: str
    guardrail_kpi: str
    guardrail_max_drop: float
    status: str
    control_variant: str
    planned_allocation: dict[str, float]
    min_sample_per_arm: int
    alpha: float
    min_effect: float
    created_at: str
    updated_at: str
    decision: str | None = None
    decision_reason: str | None = None
    winner_variant: str | None = None
    promotion_status: str | None = "observe_only_not_promoted"
    evidence: dict[str, Any] = field(default_factory=dict)
    arms: list[ExperimentArmState] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_primary_kpi(primary: str) -> str:
    raw = (primary or "").strip().lower()
    if raw in {"engagement", "engagement_rate"}:
        return "engagement"
    if raw in {"pickup", "pickup_rate"}:
        return "pickup"
    if raw in {"conclusion", "conclusion_rate"}:
        return "conclusion"
    return "engagement"


def parse_guardrail(guardrail: Mapping[str, Any] | None) -> tuple[str, float]:
    """Map objective.guardrail keys like pickup_rate_max_drop → (pickup, 0.02)."""
    if not guardrail:
        return "pickup", 0.02
    for key, value in guardrail.items():
        try:
            drop = float(value)
        except (TypeError, ValueError):
            continue
        k = str(key).lower()
        if "pickup" in k:
            return "pickup", drop
        if "engagement" in k:
            return "engagement", drop
        if "conclusion" in k:
            return "conclusion", drop
    first = next(iter(guardrail.items()))
    try:
        return "pickup", float(first[1])
    except (TypeError, ValueError):
        return "pickup", 0.02


def allowed_options(workflow: Workflow, dimension: str) -> list[str]:
    opts = getattr(workflow.options, dimension, None)
    if not isinstance(opts, list):
        return []
    return [str(v) for v in opts if v is not None and str(v)]


def validate_variants(dimension: str, variants: list[str], workflow: Workflow) -> list[str]:
    allowed = set(allowed_options(workflow, dimension))
    if not allowed:
        return []
    out = []
    for v in variants:
        if v in allowed and v not in out:
            out.append(v)
    return out


def balanced_allocation(
    variants: list[str],
    control: str,
    *,
    control_floor: float = CONTROL_FLOOR,
    exploration_floor: float = EXPLORATION_FLOOR,
) -> dict[str, float]:
    """Balanced weights with a control floor and exploration floor per challenger."""
    uniq = []
    for v in variants:
        if v not in uniq:
            uniq.append(v)
    if control not in uniq:
        uniq.insert(0, control)
    n = len(uniq)
    if n == 0:
        return {}
    if n == 1:
        return {uniq[0]: 1.0}

    equal = 1.0 / n
    weights = {v: equal for v in uniq}
    # Enforce control floor without starving challengers below exploration_floor when possible.
    if weights[control] < control_floor:
        deficit = control_floor - weights[control]
        challengers = [v for v in uniq if v != control]
        take = deficit / len(challengers)
        for v in challengers:
            weights[v] = max(exploration_floor, weights[v] - take)
        weights[control] = 1.0 - sum(weights[v] for v in challengers)
        # Renormalize if floors made the sum drift.
        total = sum(weights.values())
        weights = {k: v / total for k, v in weights.items()}
    # Final normalize for float safety.
    total = sum(weights.values())
    return {k: round(v / total, 6) for k, v in sorted(weights.items())}


def current_allocation(config: Mapping[str, Any] | None, cohort_id: str, dimension: str) -> dict[str, float]:
    if not config:
        return {}
    allocations = config.get("allocations") or {}
    if not isinstance(allocations, dict):
        return {}
    cohort_block = allocations.get(cohort_id) or allocations.get(DEFAULT_COHORT_ID) or {}
    if not isinstance(cohort_block, dict):
        return {}
    raw = cohort_block.get(dimension) or {}
    if not isinstance(raw, dict) or not raw:
        return {}
    cleaned: dict[str, float] = {}
    for k, v in raw.items():
        try:
            w = float(v)
        except (TypeError, ValueError):
            continue
        if w > 0:
            cleaned[str(k)] = w
    total = sum(cleaned.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in cleaned.items()}


def allocation_diff(current: dict[str, float], proposed: dict[str, float]) -> dict[str, Any]:
    keys = sorted(set(current) | set(proposed))
    changes = []
    for k in keys:
        before = current.get(k, 0.0)
        after = proposed.get(k, 0.0)
        if abs(before - after) > 1e-9:
            changes.append({"variant": k, "from": before, "to": after, "delta": after - before})
    return {
        "dimension_keys": keys,
        "changed": changes,
        "summary": (
            f"{len(changes)} variant weight change(s)"
            if changes
            else "no allocation weight changes"
        ),
    }


def _kpi_counts(dials: int, connected: int, engaged: int, kpi: str) -> Proportion:
    if kpi == "pickup":
        return Proportion(successes=connected, trials=dials)
    if kpi == "engagement":
        return Proportion(successes=engaged, trials=connected)
    # conclusion at call level approximates with engaged/dials when lead-level join is unavailable per arm
    return Proportion(successes=engaged, trials=dials)


def _sample_n_for_kpi(dials: int, connected: int, kpi: str) -> int:
    if kpi == "engagement":
        return connected
    return dials


def experiment_id_for(workflow_id: str, cohort_id: str, dimension: str) -> str:
    return f"{workflow_id}|{cohort_id}|{dimension}"


def choose_control(observed: list[tuple[str, int, int, int]], allowed: list[str], current: dict[str, float]) -> str | None:
    """Prefer heaviest current allocation among allowed; else most dials; else first allowed."""
    if current:
        ranked = sorted(
            ((v, w) for v, w in current.items() if v in allowed),
            key=lambda item: (-item[1], item[0]),
        )
        if ranked:
            return ranked[0][0]
    for variant, dials, _c, _e in observed:
        if variant in allowed and dials > 0:
            return variant
    return allowed[0] if allowed else None


def select_dimension(
    store: Store,
    workflow: Workflow,
    cohort_id: str,
    *,
    prefer: str | None = None,
) -> str | None:
    """Pick welcome_message or voice with the most multi-variant traffic. One dim at a time."""
    existing = store.active_experiment_dimension(workflow.workflow_id, cohort_id)
    if existing:
        return existing
    candidates: list[tuple[int, str]] = []
    order = [prefer] + [d for d in EXPERIMENT_DIMS if d != prefer] if prefer in EXPERIMENT_DIMS else list(EXPERIMENT_DIMS)
    for dim in order:
        allowed = allowed_options(workflow, dim)
        if len(allowed) < 2:
            continue
        observed = store.observed_variants(workflow.workflow_id, dim, cohort_id=cohort_id)
        multi = sum(1 for v, dials, _c, _e in observed if v in allowed and dials > 0)
        score = multi if multi >= 2 else (1 if multi == 1 else 0)
        # Prefer dimensions that already show ≥2 arms in history.
        candidates.append((score, dim))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], EXPERIMENT_DIMS.index(item[1])))
    best_score, best_dim = candidates[0]
    if best_score <= 0 and prefer is None:
        # Still plan an experiment from options alone (planned allocations), starting with welcome_message.
        return candidates[0][1]
    return best_dim


def dry_run_impact(
    *,
    control_primary: float | None,
    winner_primary: float | None,
    control_guardrail: float | None,
    winner_guardrail: float | None,
    guardrail_max_drop: float,
    control_n: int,
    winner_n: int,
    min_sample: int,
    current_alloc: dict[str, float],
    proposed_alloc: dict[str, float],
) -> dict[str, Any]:
    """Historical replay estimate. Does not claim causal future impact."""
    lift = None
    if control_primary is not None and winner_primary is not None:
        lift = winner_primary - control_primary
    guardrail_drop = None
    if control_guardrail is not None and winner_guardrail is not None:
        guardrail_drop = control_guardrail - winner_guardrail
    sample_ok = control_n >= min_sample and winner_n >= min_sample
    # Impact in [0,1]: reward lift, penalize guardrail risk and small samples.
    score = 0.0
    if lift is not None and lift > 0 and sample_ok:
        score += min(0.6, lift * 10)
    if sample_ok:
        score += 0.2
    if guardrail_drop is not None and guardrail_drop <= guardrail_max_drop:
        score += 0.2
    elif guardrail_drop is not None:
        score = max(0.0, score - 0.3)
    score = max(0.0, min(1.0, score))
    return {
        "label": "historical_replay_estimate",
        "limitation": "Historical arm rates are observational; they do not prove future causal impact.",
        "baseline_primary": control_primary,
        "candidate_primary": winner_primary,
        "expected_primary_lift": lift,
        "baseline_guardrail": control_guardrail,
        "candidate_guardrail": winner_guardrail,
        "guardrail_drop": guardrail_drop,
        "guardrail_ok": guardrail_drop is None or guardrail_drop <= guardrail_max_drop,
        "control_n": control_n,
        "winner_n": winner_n,
        "sample_ok": sample_ok,
        "impact_score": round(score, 4),
        "allocation_diff": allocation_diff(current_alloc, proposed_alloc),
    }


def plan_experiment(
    store: Store,
    workflow: Workflow,
    config: WorkflowConfig,
    *,
    cohort_id: str = DEFAULT_COHORT_ID,
    dimension: str | None = None,
    min_sample: int = MIN_COHORT_SAMPLE,
    alpha: float = DEFAULT_ALPHA,
    min_effect: float = DEFAULT_MIN_EFFECT,
) -> ExperimentState | None:
    dim = dimension or select_dimension(store, workflow, cohort_id)
    if dim is None or dim not in EXPERIMENT_DIMS:
        return None
    allowed = allowed_options(workflow, dim)
    if len(allowed) < 2:
        return None

    # Conflict: another active dimension for this cohort.
    active_dim = store.active_experiment_dimension(workflow.workflow_id, cohort_id)
    eid = experiment_id_for(workflow.workflow_id, cohort_id, dim)
    existing = store.get_experiment(eid)
    if active_dim and active_dim != dim and existing is None:
        return None

    observed = store.observed_variants(workflow.workflow_id, dim, cohort_id=cohort_id)
    current = current_allocation(config.config, cohort_id, dim)
    # Only plan with allowed options; never invent variants.
    control = choose_control(observed, allowed, current)
    if control is None:
        return None
    challengers = [v for v in allowed if v != control]
    if not challengers:
        return None
    variants = [control, *challengers]
    allocation = balanced_allocation(variants, control)
    primary = normalize_primary_kpi(workflow.objective.primary)
    guardrail_kpi, guardrail_drop = parse_guardrail(workflow.objective.guardrail)
    now = _now()
    created = existing["created_at"] if existing is not None else now
    state = ExperimentState(
        experiment_id=eid,
        workflow_id=workflow.workflow_id,
        tenant_id=workflow.tenant_id,
        cohort_id=cohort_id,
        dimension=dim,
        primary_kpi=primary,
        guardrail_kpi=guardrail_kpi,
        guardrail_max_drop=guardrail_drop,
        status="planned",
        control_variant=control,
        planned_allocation=allocation,
        min_sample_per_arm=min_sample,
        alpha=alpha,
        min_effect=min_effect,
        created_at=created,
        updated_at=now,
        promotion_status="observe_only_not_promoted",
        evidence={
            "allowed_options": allowed,
            "current_allocation": current,
            "mid_flight": workflow.mid_flight,
            "observe_only": True,
        },
    )
    for variant in variants:
        dials, connected, engaged = store.variant_call_totals(
            workflow.workflow_id, dim, variant, cohort_id=cohort_id
        )
        primary_prop = _kpi_counts(dials, connected, engaged, primary)
        guard_prop = _kpi_counts(dials, connected, engaged, guardrail_kpi)
        sample_n = _sample_n_for_kpi(dials, connected, primary)
        state.arms.append(
            ExperimentArmState(
                experiment_id=eid,
                variant=variant,
                is_control=(variant == control),
                planned_weight=allocation.get(variant, 0.0),
                dials=dials,
                connected=connected,
                engaged=engaged,
                primary_rate=primary_prop.rate,
                guardrail_rate=guard_prop.rate,
                sample_n=sample_n,
                sample_ok=sample_n >= min_sample,
            )
        )
    return state


def evaluate_experiment(state: ExperimentState) -> ExperimentState:
    """Apply two-proportion z-tests with Holm correction. Deterministic."""
    control = next((a for a in state.arms if a.is_control), None)
    if control is None:
        state.status = "inconclusive"
        state.decision = "no_winner"
        state.decision_reason = "missing control arm"
        return state

    challengers = [a for a in state.arms if not a.is_control]
    if not challengers:
        state.status = "inconclusive"
        state.decision = "no_winner"
        state.decision_reason = "no challenger arms"
        return state

    control_primary = _kpi_counts(control.dials, control.connected, control.engaged, state.primary_kpi)
    control_guard = _kpi_counts(control.dials, control.connected, control.engaged, state.guardrail_kpi)

    comparisons = []
    for arm in challengers:
        treat_primary = _kpi_counts(arm.dials, arm.connected, arm.engaged, state.primary_kpi)
        treat_guard = _kpi_counts(arm.dials, arm.connected, arm.engaged, state.guardrail_kpi)
        cmp_ = two_proportion_ztest(
            control_primary,
            treat_primary,
            min_n=state.min_sample_per_arm,
            min_effect=state.min_effect,
        )
        comparisons.append((arm, cmp_, treat_guard.rate))

    underpowered = [arm for arm, cmp_, _g in comparisons if cmp_.underpowered]
    if underpowered or not control.sample_ok:
        state.status = "running"
        state.decision = "underpowered"
        missing = [a.variant for a in state.arms if not a.sample_ok]
        state.decision_reason = (
            f"insufficient samples for decision; need >= {state.min_sample_per_arm} "
            f"per arm on {state.primary_kpi}. short={missing or [control.variant]}"
        )
        for arm, cmp_, _g in comparisons:
            arm.verdict = "underpowered"
            arm.effect_size = cmp_.effect_size
            arm.p_value = cmp_.p_value
            arm.ci_low = cmp_.ci_low
            arm.ci_high = cmp_.ci_high
            arm.reason = cmp_.reason
            arm.adjusted_alpha = None
        control.verdict = "control"
        control.reason = f"control n={control.sample_n}"
        state.winner_variant = None
        state.evidence = {
            **state.evidence,
            "insufficient_data": True,
            "min_sample_per_arm": state.min_sample_per_arm,
            "warnings": [state.decision_reason],
        }
        return state

    p_values = [cmp_.p_value if cmp_.p_value is not None else 1.0 for _a, cmp_, _g in comparisons]
    adjusted = holm_adjust(p_values, alpha=state.alpha)

    winners: list[tuple[ExperimentArmState, float]] = []
    losers: list[str] = []
    guardrail_hits: list[str] = []
    inconclusive: list[str] = []

    for (arm, cmp_, guard_rate), adj_alpha in zip(comparisons, adjusted, strict=True):
        verdict, reason = decide_challenger(
            comparison=cmp_,
            adjusted_alpha=adj_alpha,
            min_effect=state.min_effect,
            guardrail_control=control_guard.rate,
            guardrail_treatment=guard_rate,
            guardrail_max_drop=state.guardrail_max_drop,
        )
        arm.verdict = verdict
        arm.effect_size = cmp_.effect_size
        arm.p_value = cmp_.p_value
        arm.adjusted_alpha = adj_alpha
        arm.ci_low = cmp_.ci_low
        arm.ci_high = cmp_.ci_high
        arm.reason = reason
        if verdict == "winner":
            winners.append((arm, cmp_.effect_size or 0.0))
        elif verdict == "loser":
            losers.append(arm.variant)
        elif verdict == "guardrail_violation":
            guardrail_hits.append(arm.variant)
        else:
            inconclusive.append(arm.variant)

    control.verdict = "control"
    control.reason = "baseline arm"

    # Deterministic winner: largest positive effect, then variant id.
    winners.sort(key=lambda item: (-item[1], item[0].variant))
    current_alloc = state.evidence.get("current_allocation") or {}
    if winners:
        winner_arm = winners[0][0]
        proposed = dict(state.planned_allocation)
        # Concentrate on winner while keeping control floor for safety preview.
        proposed = balanced_allocation([state.control_variant, winner_arm.variant], state.control_variant)
        proposed[winner_arm.variant] = max(proposed.get(winner_arm.variant, 0.0), 0.5)
        proposed[state.control_variant] = 1.0 - proposed[winner_arm.variant]
        dry = dry_run_impact(
            control_primary=control.primary_rate,
            winner_primary=winner_arm.primary_rate,
            control_guardrail=control.guardrail_rate,
            winner_guardrail=winner_arm.guardrail_rate,
            guardrail_max_drop=state.guardrail_max_drop,
            control_n=control.sample_n,
            winner_n=winner_arm.sample_n,
            min_sample=state.min_sample_per_arm,
            current_alloc=current_alloc if isinstance(current_alloc, dict) else {},
            proposed_alloc=proposed,
        )
        state.status = "winner"
        state.decision = "winner"
        state.winner_variant = winner_arm.variant
        state.decision_reason = (
            f"{winner_arm.variant} beats control {state.control_variant} on {state.primary_kpi}: "
            f"{winner_arm.reason}"
        )
        # Phase 5 does not promote or submit approvals; Phase 6 owns that path.
        state.promotion_status = "ready_to_propose_observe_only"
        state.evidence = {
            **state.evidence,
            "insufficient_data": False,
            "winners": [w.variant for w, _ in winners],
            "losers": losers,
            "inconclusive": inconclusive,
            "guardrail_violations": guardrail_hits,
            "holm_adjusted_alphas": adjusted,
            "dry_run": dry,
            "warnings": [],
        }
        return state

    if guardrail_hits and not winners:
        state.status = "guardrail_violation"
        state.decision = "guardrail_violation"
        state.winner_variant = None
        state.decision_reason = (
            f"challenger(s) {guardrail_hits} fail guardrail {state.guardrail_kpi} "
            f"max_drop={state.guardrail_max_drop}; no safe winner"
        )
        state.evidence = {
            **state.evidence,
            "insufficient_data": False,
            "losers": losers,
            "inconclusive": inconclusive,
            "guardrail_violations": guardrail_hits,
            "holm_adjusted_alphas": adjusted,
            "warnings": [state.decision_reason],
        }
        return state

    state.status = "inconclusive"
    state.decision = "no_winner"
    state.winner_variant = None
    state.decision_reason = (
        f"no significant practical lift after Holm correction "
        f"(losers={losers}, inconclusive={inconclusive})"
    )
    state.evidence = {
        **state.evidence,
        "insufficient_data": False,
        "losers": losers,
        "inconclusive": inconclusive,
        "guardrail_violations": guardrail_hits,
        "holm_adjusted_alphas": adjusted,
        "warnings": [],
    }
    return state


def persist_experiment(store: Store, state: ExperimentState) -> ExperimentState:
    state.updated_at = _now()
    with store.transaction():
        store.upsert_experiment(state)
        for arm in state.arms:
            store.upsert_experiment_arm(arm)
    return state


def run_workflow_experiments(
    store: Store,
    workflow: Workflow,
    config: WorkflowConfig,
    *,
    min_sample: int = MIN_COHORT_SAMPLE,
) -> list[ExperimentState]:
    """Plan/evaluate one dimension per cohort. Uses shared cohort IDs from call history."""
    cohort_ids = store.cohort_ids_for_workflow(workflow.workflow_id)
    if not cohort_ids:
        cohort_ids = [DEFAULT_COHORT_ID]
    results: list[ExperimentState] = []
    for cohort_id in cohort_ids:
        # Skip tiny cohorts for new experiments; still refresh an existing one.
        n_leads = store.leads_in_cohort(workflow.workflow_id, cohort_id)
        existing_dim = store.active_experiment_dimension(workflow.workflow_id, cohort_id)
        if existing_dim is None and n_leads > 0 and not cohort_is_actionable(n_leads, minimum=min_sample):
            # Cohort too small to start — record nothing fabricated.
            continue
        planned = plan_experiment(
            store,
            workflow,
            config,
            cohort_id=cohort_id,
            min_sample=min_sample,
        )
        if planned is None:
            continue
        evaluated = evaluate_experiment(planned)
        persist_experiment(store, evaluated)
        results.append(evaluated)
    return results


def run_all_experiments(
    store: Store,
    workflows: list[Workflow],
    configs: list[WorkflowConfig],
) -> list[ExperimentState]:
    by_id = {c.workflow_id: c for c in configs}
    out: list[ExperimentState] = []
    for workflow in workflows:
        cfg = by_id.get(workflow.workflow_id)
        if cfg is None:
            continue
        out.extend(run_workflow_experiments(store, workflow, cfg))
    return out


def experiment_as_dict(state: ExperimentState) -> dict[str, Any]:
    payload = asdict(state)
    payload["planned_allocation_json"] = json.dumps(state.planned_allocation, sort_keys=True)
    return payload
