"""Build governed config proposals from experiment winners / rollback snapshots.

Never invents unsupported option values. Diff, dry-run, and impact are formula-based.
Prompt changes are out of scope here (Phase 7 requires validation evidence).
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from controller.api.models import Workflow, WorkflowConfig
from controller.domain.cohorts import DEFAULT_COHORT_ID, MIN_COHORT_SAMPLE
from controller.persistence.repositories import Store
from controller.services.detector import CALLER_ID_DROP_VS_WORKFLOW
from controller.services.experiments import (
    DEFAULT_MIN_EFFECT,
    ExperimentState,
    allocation_diff,
    balanced_allocation,
    dry_run_impact,
    normalize_primary_kpi,
    parse_guardrail,
)
from controller.services.incidents import Incident
from controller.services.metrics import KpiSlice


ALLOCATION_DIMS = ("welcome_message", "voice", "caller_id")
CALLER_ID_ROTATION_SOURCE = "caller_id_rotation"
# One proposal may move at most this much probability mass off the degraded caller id.
# 0.15 is enough that a later window can show whether pickup moved, and it leaves a
# residual when that id currently holds more than 0.15. It is not a full replacement.
# auto_apply still enforces the workflow's own max_shift; a tighter policy uses approval.
CALLER_ID_ROTATION_MAX_SHIFT = 0.15
# Same absolute pickup gap the detector uses. A replacement must clear this bar.
CALLER_ID_MIN_PICKUP_LIFT = CALLER_ID_DROP_VS_WORKFLOW


@dataclass
class ProposalDraft:
    proposal_id: str
    workflow_id: str
    base_version: int
    status: str
    source: str
    source_ref: str | None
    changed_dims: list[str]
    proposed_config: dict[str, Any]
    change_summary: str
    diff: dict[str, Any]
    dry_run: dict[str, Any]
    impact_score: float
    affected_leads: int
    rationale: str
    evidence: dict[str, Any] = field(default_factory=dict)
    approval_id: str | None = None
    route: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    created_at: str = ""
    updated_at: str = ""
    submitted_at: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def proposal_id_for(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return f"prop_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def next_winner_attempt_seq(store: Store, experiment_id: str) -> int:
    """1-based retry sequence from persisted winner proposals for this experiment.

    Stable across restart for the same next attempt: count existing rows, then +1.
    """
    n = 0
    for row in store.list_proposals():
        if row["source"] == "experiment_winner" and row["source_ref"] == experiment_id:
            n += 1
    return n + 1


def changed_dims(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    dims: list[str] = []
    if before.get("prompt_version") != after.get("prompt_version"):
        dims.append("prompt_version")
    if json.dumps(before.get("cohorts") or [], sort_keys=True) != json.dumps(after.get("cohorts") or [], sort_keys=True):
        dims.append("cohorts")
    if json.dumps(before.get("schedule") or {}, sort_keys=True) != json.dumps(after.get("schedule") or {}, sort_keys=True):
        dims.append("schedule")
    b_alloc = before.get("allocations") or {}
    a_alloc = after.get("allocations") or {}
    cohorts = set(b_alloc) | set(a_alloc)
    for cohort in cohorts:
        b_block = b_alloc.get(cohort) or {}
        a_block = a_alloc.get(cohort) or {}
        for dim in ALLOCATION_DIMS:
            if json.dumps(b_block.get(dim) or {}, sort_keys=True) != json.dumps(a_block.get(dim) or {}, sort_keys=True):
                if dim not in dims:
                    dims.append(dim)
    return dims


def total_variation(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) | set(b)
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys)


def max_allocation_shift(before: dict[str, Any], after: dict[str, Any]) -> float:
    b_alloc = before.get("allocations") or {}
    a_alloc = after.get("allocations") or {}
    cohorts = set(b_alloc) | set(a_alloc)
    worst = 0.0
    for cohort in cohorts:
        b_block = b_alloc.get(cohort) or {}
        a_block = a_alloc.get(cohort) or {}
        for dim in ALLOCATION_DIMS:
            worst = max(worst, total_variation(b_block.get(dim) or {}, a_block.get(dim) or {}))
    return worst


def set_allocation(
    config: dict[str, Any],
    *,
    cohort_id: str,
    dimension: str,
    weights: dict[str, float],
) -> dict[str, Any]:
    out = copy.deepcopy(config)
    allocations = out.setdefault("allocations", {})
    block = allocations.setdefault(cohort_id, {})
    total = sum(max(0.0, float(v)) for v in weights.values())
    if total <= 0:
        raise ValueError("allocation weights must sum to a positive value")
    block[dimension] = {k: float(v) / total for k, v in weights.items() if float(v) > 0}
    return out


def build_explore_proposal(
    store: Store,
    workflow: Workflow,
    config: WorkflowConfig,
    experiment: ExperimentState,
) -> ProposalDraft | None:
    """Propose the experiment's planned balanced allocation so underpowered arms can gather samples."""
    if experiment.decision not in {"underpowered", "inconclusive"} and experiment.status not in {
        "planned",
        "running",
    }:
        return None
    if experiment.decision == "winner":
        return None
    if experiment.dimension not in ("welcome_message", "voice"):
        return None
    planned = {str(k): float(v) for k, v in (experiment.planned_allocation or {}).items() if float(v) > 0}
    if len(planned) < 2:
        return None
    allowed = set(getattr(workflow.options, experiment.dimension) or [])
    if any(v not in allowed for v in planned):
        return None
    current = ((config.config.get("allocations") or {}).get(experiment.cohort_id) or {}).get(
        experiment.dimension
    ) or {}
    current_w = {str(k): float(v) for k, v in current.items()} if isinstance(current, dict) else {}
    if total_variation(current_w, planned) < 1e-6:
        return None
    proposed = set_allocation(
        config.config,
        cohort_id=experiment.cohort_id or DEFAULT_COHORT_ID,
        dimension=experiment.dimension,
        weights=planned,
    )
    dims = changed_dims(config.config, proposed)
    if not dims or any(d not in ALLOCATION_DIMS for d in dims):
        return None
    dry = {
        "label": "exploration_allocation",
        "limitation": (
            "Exploration reallocates traffic to gather samples; it does not claim a causal KPI lift."
        ),
        "current_allocation": current_w,
        "planned_allocation": planned,
        "sample_ok": False,
        "guardrail_ok": True,
        "impact_score": 0.25,
        "max_shift": max_allocation_shift(config.config, proposed),
    }
    affected = store.leads_in_cohort(workflow.workflow_id, experiment.cohort_id) or store.uploaded_leads(
        workflow.workflow_id
    )
    summary = (
        f"Explore {experiment.dimension} on {workflow.workflow_id}/{experiment.cohort_id} "
        f"with balanced allocation {planned}"
    )
    rationale = (
        f"Experiment {experiment.experiment_id} is {experiment.decision or experiment.status}; "
        f"submit planned allocation so each arm can reach min_sample={experiment.min_sample_per_arm}."
    )
    pid = proposal_id_for(
        "explore",
        experiment.experiment_id,
        config.version,
        json.dumps(planned, sort_keys=True),
    )
    now = _now()
    return ProposalDraft(
        proposal_id=pid,
        workflow_id=workflow.workflow_id,
        base_version=config.version,
        status="draft",
        source="experiment_explore",
        source_ref=experiment.experiment_id,
        changed_dims=dims,
        proposed_config=proposed,
        change_summary=summary,
        diff={
            "dimension": experiment.dimension,
            "cohort_id": experiment.cohort_id,
            "allocation": allocation_diff(current_w, planned),
            "changed_dims": dims,
            "max_shift": dry["max_shift"],
        },
        dry_run=dry,
        impact_score=0.25,
        affected_leads=int(affected),
        rationale=rationale,
        evidence={
            "experiment_id": experiment.experiment_id,
            "decision": experiment.decision,
            "min_sample_per_arm": experiment.min_sample_per_arm,
            "observe_note": "Exploration only; dry_run is not causal proof.",
        },
        created_at=now,
        updated_at=now,
    )


def build_winner_proposal(
    store: Store,
    workflow: Workflow,
    config: WorkflowConfig,
    experiment: ExperimentState,
) -> ProposalDraft | None:
    if experiment.status != "winner" or not experiment.winner_variant:
        return None
    if experiment.dimension not in ("welcome_message", "voice"):
        return None
    allowed = set(getattr(workflow.options, experiment.dimension) or [])
    if experiment.winner_variant not in allowed or experiment.control_variant not in allowed:
        return None

    weights = balanced_allocation(
        [experiment.control_variant, experiment.winner_variant],
        experiment.control_variant,
    )
    # Prefer the winner while keeping a control floor for safety.
    winner_share = max(weights.get(experiment.winner_variant, 0.5), 0.55)
    control_share = 1.0 - winner_share
    weights = {
        experiment.control_variant: control_share,
        experiment.winner_variant: winner_share,
    }
    proposed = set_allocation(
        config.config,
        cohort_id=experiment.cohort_id or DEFAULT_COHORT_ID,
        dimension=experiment.dimension,
        weights=weights,
    )
    dims = changed_dims(config.config, proposed)
    if not dims:
        return None
    if "prompt_version" in dims:
        # Phase 6 does not build prompt proposals without validation (Phase 7).
        return None

    control_arm = next((a for a in experiment.arms if a.is_control), None)
    winner_arm = next((a for a in experiment.arms if a.variant == experiment.winner_variant), None)
    current_alloc = ((config.config.get("allocations") or {}).get(experiment.cohort_id) or {}).get(
        experiment.dimension
    ) or {}
    dry = dry_run_impact(
        control_primary=control_arm.primary_rate if control_arm else None,
        winner_primary=winner_arm.primary_rate if winner_arm else None,
        control_guardrail=control_arm.guardrail_rate if control_arm else None,
        winner_guardrail=winner_arm.guardrail_rate if winner_arm else None,
        guardrail_max_drop=experiment.guardrail_max_drop,
        control_n=control_arm.sample_n if control_arm else 0,
        winner_n=winner_arm.sample_n if winner_arm else 0,
        min_sample=experiment.min_sample_per_arm,
        current_alloc={str(k): float(v) for k, v in current_alloc.items()} if isinstance(current_alloc, dict) else {},
        proposed_alloc=weights,
    )
    diff = {
        "dimension": experiment.dimension,
        "cohort_id": experiment.cohort_id,
        "allocation": allocation_diff(
            {str(k): float(v) for k, v in current_alloc.items()} if isinstance(current_alloc, dict) else {},
            weights,
        ),
        "changed_dims": dims,
        "max_shift": max_allocation_shift(config.config, proposed),
    }
    affected = store.leads_in_cohort(workflow.workflow_id, experiment.cohort_id) or store.uploaded_leads(
        workflow.workflow_id
    )
    summary = (
        f"Promote {experiment.dimension}={experiment.winner_variant} for {experiment.cohort_id} "
        f"on {workflow.workflow_id} (control={experiment.control_variant})"
    )
    rationale = (
        f"Experiment {experiment.experiment_id} selected {experiment.winner_variant} over "
        f"{experiment.control_variant} on {experiment.primary_kpi}. {experiment.decision_reason}"
    )
    # Attempt sequence from persisted history keeps retries unique even when base_version is unchanged,
    # while remaining stable across a crash/restart for the same next attempt.
    attempt_seq = next_winner_attempt_seq(store, experiment.experiment_id)
    pid = proposal_id_for(
        "winner",
        experiment.experiment_id,
        experiment.winner_variant,
        config.version,
        json.dumps(weights, sort_keys=True),
        f"attempt_{attempt_seq}",
    )
    now = _now()
    return ProposalDraft(
        proposal_id=pid,
        workflow_id=workflow.workflow_id,
        base_version=config.version,
        status="draft",
        source="experiment_winner",
        source_ref=experiment.experiment_id,
        changed_dims=dims,
        proposed_config=proposed,
        change_summary=summary,
        diff={**diff, "attempt_seq": attempt_seq},
        dry_run=dry,
        impact_score=float(dry.get("impact_score") or 0.0),
        affected_leads=int(affected),
        rationale=rationale,
        evidence={
            "experiment_id": experiment.experiment_id,
            "winner_variant": experiment.winner_variant,
            "control_variant": experiment.control_variant,
            "primary_kpi": experiment.primary_kpi,
            "decision_reason": experiment.decision_reason,
            "observe_note": "Historical arm rates are observational; dry_run is not causal proof.",
        },
        created_at=now,
        updated_at=now,
    )


def build_prompt_proposal(
    store: Store,
    workflow: Workflow,
    config: WorkflowConfig,
    *,
    prompt_version: str,
    validation_run_id: str,
    pass_rate: float,
    incident_id: str,
    attempt_id: str,
    problem: str,
) -> ProposalDraft | None:
    """Prompt change proposal. Requires a validation_run_id; never invents prompt versions."""
    allowed = set(workflow.options.prompt_version or [])
    if prompt_version not in allowed:
        return None
    current_prompt = (config.config or {}).get("prompt_version")
    if current_prompt == prompt_version:
        return None
    proposed = copy.deepcopy(config.config)
    proposed["prompt_version"] = prompt_version
    dims = changed_dims(config.config, proposed)
    if dims != ["prompt_version"]:
        # Only the prompt dimension may change in this builder.
        return None
    threshold = float(workflow.governance.min_validation_pass_rate)
    dry = {
        "label": "prompt_validation_safety_only",
        "limitation": (
            "Validation pass_rate measures synthetic safety/hallucination only; "
            "it does not prove engagement or business-outcome improvement."
        ),
        "validation_run_id": validation_run_id,
        "pass_rate": pass_rate,
        "min_validation_pass_rate": threshold,
        "sample_ok": True,
        "guardrail_ok": True,
        "impact_score": round(min(1.0, max(0.0, 0.35 + max(0.0, pass_rate - threshold))), 4),
    }
    affected = store.uploaded_leads(workflow.workflow_id)
    summary = f"Replace prompt_version {current_prompt} → {prompt_version} on {workflow.workflow_id}"
    rationale = (
        f"Hallucination incident {incident_id}: validated candidate {prompt_version} "
        f"(pass_rate={pass_rate:.4f} ≥ {threshold:.4f}). Problem: {problem}"
    )
    pid = proposal_id_for(
        "prompt_incident",
        incident_id,
        attempt_id,
        prompt_version,
        validation_run_id,
        config.version,
    )
    now = _now()
    return ProposalDraft(
        proposal_id=pid,
        workflow_id=workflow.workflow_id,
        base_version=config.version,
        status="draft",
        source="prompt_incident",
        source_ref=incident_id,
        changed_dims=dims,
        proposed_config=proposed,
        change_summary=summary,
        diff={
            "changed_dims": dims,
            "prompt_version": {"from": current_prompt, "to": prompt_version},
            "validation_run_id": validation_run_id,
            "pass_rate": pass_rate,
        },
        dry_run=dry,
        impact_score=float(dry["impact_score"]),
        affected_leads=int(affected),
        rationale=rationale,
        evidence={
            "validation_run_id": validation_run_id,
            "incident_id": incident_id,
            "attempt_id": attempt_id,
            "pass_rate": pass_rate,
            "min_validation_pass_rate": threshold,
            "untrusted_llm_note": "LLM suggested candidates only; selection used validation threshold, not the LLM.",
        },
        created_at=now,
        updated_at=now,
    )


def _allocation_weights(config: dict[str, Any], cohort_id: str, dimension: str) -> dict[str, float]:
    """Normalised positive weights for one cohort dimension. Missing block is empty."""
    raw = ((config.get("allocations") or {}).get(cohort_id) or {}).get(dimension) or {}
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, float] = {}
    for key, value in raw.items():
        try:
            weight = float(value)
        except (TypeError, ValueError):
            continue
        if weight > 0:
            cleaned[str(key)] = weight
    total = sum(cleaned.values())
    if total <= 0:
        return {}
    return {key: weight / total for key, weight in cleaned.items()}


def _weights_match(left: dict[str, float], right: dict[str, float], *, tol: float = 1e-6) -> bool:
    keys = set(left) | set(right)
    return all(abs(left.get(key, 0.0) - right.get(key, 0.0)) <= tol for key in keys)


def _applied_winner_proposal_id(store: Store, workflow_id: str, experiment_id: str) -> str | None:
    """Most recent applied winner proposal for this experiment, if one exists."""
    for prop in store.recent_proposals_for_workflow(workflow_id, limit=100):
        if prop["source"] != "experiment_winner" or prop["source_ref"] != experiment_id:
            continue
        if prop["status"] in {"applied", "auto_applied"}:
            return str(prop["proposal_id"])
    return None


def build_rollback_proposal(
    store: Store,
    workflow: Workflow,
    config: WorkflowConfig,
    *,
    reason: str,
    source_ref: str | None = None,
) -> ProposalDraft | None:
    """Restore only the promoted experiment's allocation.

    Starts from the live config. Copies the pre-promotion weight for that one
    cohort and dimension. Leaves every other setting in place.

    If the live weight for that cell no longer matches the winner's post_apply
    snapshot, someone changed it after promotion. The draft status is then
    ``conflict`` and the live config is not rewritten. Callers must not submit
    that draft.
    """
    if not source_ref:
        return None
    experiment = store.get_experiment(source_ref)
    if experiment is None:
        return None
    dimension = str(experiment["dimension"])
    cohort_id = str(experiment["cohort_id"] or DEFAULT_COHORT_ID)
    if dimension not in ALLOCATION_DIMS:
        return None

    snap = store.pre_promotion_rollback_snapshot(workflow.workflow_id, source_ref)
    if snap is None:
        return None
    prior = json.loads(snap["config_json"])
    prior_weights = _allocation_weights(prior, cohort_id, dimension)
    if not prior_weights:
        return None

    live_weights = _allocation_weights(config.config, cohort_id, dimension)
    winner_proposal_id = _applied_winner_proposal_id(store, workflow.workflow_id, source_ref)
    post = (
        store.snapshot_for_proposal(winner_proposal_id, "post_apply")
        if winner_proposal_id
        else None
    )
    promoted_weights = (
        _allocation_weights(json.loads(post["config_json"]), cohort_id, dimension) if post is not None else {}
    )
    now = _now()
    affected = store.uploaded_leads(workflow.workflow_id)

    if post is None or not _weights_match(live_weights, promoted_weights):
        detail = (
            "no post_apply snapshot for the winner promotion"
            if post is None
            else (
                f"live {dimension} on {cohort_id} no longer matches the promoted allocation; "
                "a later edit may own this setting"
            )
        )
        pid = proposal_id_for(
            "rollback_conflict",
            workflow.workflow_id,
            source_ref,
            config.version,
            json.dumps(live_weights, sort_keys=True),
            json.dumps(promoted_weights, sort_keys=True),
        )
        return ProposalDraft(
            proposal_id=pid,
            workflow_id=workflow.workflow_id,
            base_version=config.version,
            status="conflict",
            source="decay_rollback",
            source_ref=source_ref,
            changed_dims=[],
            proposed_config=copy.deepcopy(config.config),
            change_summary=(
                f"Rollback of {dimension} on {workflow.workflow_id}/{cohort_id} needs review: {detail}"
            ),
            diff={
                "conflict": True,
                "dimension": dimension,
                "cohort_id": cohort_id,
                "live_allocation": live_weights,
                "promoted_allocation": promoted_weights,
                "pre_promotion_allocation": prior_weights,
                "reason": reason,
            },
            dry_run={
                "label": "rollback_conflict_needs_review",
                "limitation": "No configuration was changed. A person must review this before any rollback.",
                "conflict": True,
                "impact_score": 0.0,
            },
            impact_score=0.0,
            affected_leads=int(affected),
            rationale=f"{reason} Rollback not submitted: {detail}",
            evidence={
                "conflict": True,
                "dimension": dimension,
                "cohort_id": cohort_id,
                "live_allocation": live_weights,
                "promoted_allocation": promoted_weights,
                "pre_promotion_allocation": prior_weights,
                "winner_proposal_id": winner_proposal_id,
                "snapshot_version": snap["version"],
            },
            created_at=now,
            updated_at=now,
        )

    proposed = set_allocation(
        config.config,
        cohort_id=cohort_id,
        dimension=dimension,
        weights=prior_weights,
    )
    dims = changed_dims(config.config, proposed)
    if not dims:
        return None
    if dims != [dimension]:
        return None
    dry = {
        "label": "rollback_one_allocation",
        "limitation": (
            "Restores only the promoted experiment's allocation from the pre-promotion "
            "snapshot. Other settings stay as they are on the live config. This does not "
            "prove future KPI recovery."
        ),
        "snapshot_version": snap["version"],
        "snapshot_source": snap["source"],
        "related_proposal_id": snap["related_proposal_id"],
        "dimension": dimension,
        "cohort_id": cohort_id,
        "impact_score": 0.35,
        "sample_ok": True,
        "guardrail_ok": True,
    }
    pid = proposal_id_for(
        "rollback",
        workflow.workflow_id,
        source_ref,
        dimension,
        cohort_id,
        snap["version"],
        config.version,
        reason,
    )
    return ProposalDraft(
        proposal_id=pid,
        workflow_id=workflow.workflow_id,
        base_version=config.version,
        status="draft",
        source="decay_rollback",
        source_ref=source_ref,
        changed_dims=dims,
        proposed_config=proposed,
        change_summary=(
            f"Rollback {dimension} on {workflow.workflow_id}/{cohort_id} "
            f"to the pre-promotion allocation from config version {snap['version']}"
        ),
        diff={
            "changed_dims": dims,
            "dimension": dimension,
            "cohort_id": cohort_id,
            "allocation": allocation_diff(live_weights, prior_weights),
            "to_snapshot_version": snap["version"],
            "reason": reason,
            "targeted": True,
        },
        dry_run=dry,
        impact_score=0.35,
        affected_leads=int(affected),
        rationale=(
            f"{reason} Restoring only {dimension} for cohort {cohort_id}. "
            "Other dimensions, cohorts, and schedule values are left unchanged."
        ),
        evidence={
            "rollback_from_version": config.version,
            "rollback_to_snapshot_version": snap["version"],
            "snapshot_source": snap["source"],
            "related_proposal_id": snap["related_proposal_id"],
            "winner_proposal_id": winner_proposal_id,
            "dimension": dimension,
            "cohort_id": cohort_id,
            "restored_allocation": prior_weights,
            "promoted_allocation": promoted_weights,
            "targeted": True,
        },
        created_at=now,
        updated_at=now,
    )


@dataclass(frozen=True)
class CallerIdRotationBuild:
    draft: ProposalDraft | None
    reason: str | None
    details: dict[str, Any]


def _caller_rate(slice_, kpi: str) -> tuple[float | None, int]:
    if kpi == "pickup":
        return slice_.pickup, slice_.dials
    if kpi == "engagement":
        return slice_.engagement, slice_.connected
    # Conclusion needs uploaded leads. Caller-id slices do not have that denominator.
    return None, 0


def _rate_ok(rate: float | None, n: int) -> bool:
    return rate is not None and n >= MIN_COHORT_SAMPLE


def _cohort_slice(
    store: Store,
    workflow_id: str,
    *,
    cohort_id: str,
    caller_id: str | None,
) -> KpiSlice:
    """Call totals for one cohort, optionally one caller id. Both filters are applied together."""
    dials, connected, engaged, halluc_flags, connected_with_qa = store.call_totals(
        workflow_id,
        cohort_id=cohort_id,
        caller_id=caller_id,
    )
    return KpiSlice(
        workflow_id=workflow_id,
        window_label="all",
        window_start_ts=None,
        window_end_ts=None,
        cohort_id=cohort_id,
        caller_id=caller_id,
        config_version=None,
        dials=dials,
        connected=connected,
        engaged=engaged,
        uploaded_leads=None,
        concluded_leads=0,
        halluc_flags=halluc_flags,
        connected_with_qa=connected_with_qa,
    )


def _cohorts_holding_caller(config: dict[str, Any], caller_id: str) -> list[str]:
    allocs = config.get("allocations") or {}
    if not isinstance(allocs, dict):
        return []
    found: list[str] = []
    for cohort in allocs:
        weights = _allocation_weights(config, str(cohort), "caller_id")
        if weights.get(caller_id, 0.0) > 0:
            found.append(str(cohort))
    return found


def next_caller_id_attempt_seq(store: Store, incident_id: str) -> int:
    """1-based retry count. A stale or lapsed row still counts, so the next id is new."""
    n = 0
    for row in store.list_proposals():
        if row["source"] == CALLER_ID_ROTATION_SOURCE and row["source_ref"] == incident_id:
            n += 1
    return n + 1


def build_caller_id_rotation_proposal(
    store: Store,
    workflow: Workflow,
    config: WorkflowConfig,
    incident: Incident,
) -> CallerIdRotationBuild:
    """Shift a limited share of the default cohort off one degraded caller id.

    The incident names the degraded id. Pickup, sample size, primary KPI, and the
    guardrail are read from calls in the default cohort only. A workflow-wide gap
    that lives on a named cohort does not justify this edit. Only ids listed on
    the workflow are eligible. Named-cohort allocations are left unchanged.
    """
    evidence = incident.evidence if isinstance(incident.evidence, dict) else {}
    degraded = evidence.get("caller_id")
    if not degraded:
        return CallerIdRotationBuild(None, "missing_caller_id", {})
    degraded = str(degraded)
    allowed = [str(v) for v in (workflow.options.caller_id or []) if v is not None and str(v)]
    if degraded not in allowed:
        return CallerIdRotationBuild(
            None,
            "degraded_not_supported",
            {"degraded_caller_id": degraded, "options": allowed},
        )
    alternatives = [cid for cid in allowed if cid != degraded]
    if not alternatives:
        return CallerIdRotationBuild(
            None,
            "no_supported_replacement",
            {"degraded_caller_id": degraded, "options": allowed},
        )

    holders = _cohorts_holding_caller(config.config, degraded)
    current = _allocation_weights(config.config, DEFAULT_COHORT_ID, "caller_id")
    if degraded not in current:
        reason = "cohort_specific_only" if any(c != DEFAULT_COHORT_ID for c in holders) else "degraded_not_allocated"
        return CallerIdRotationBuild(
            None,
            reason,
            {"degraded_caller_id": degraded, "cohorts_holding": holders},
        )

    bad = _cohort_slice(store, workflow.workflow_id, cohort_id=DEFAULT_COHORT_ID, caller_id=degraded)
    if not _rate_ok(bad.pickup, bad.dials):
        return CallerIdRotationBuild(
            None,
            "insufficient_samples",
            {
                "which": "degraded",
                "cohort_id": DEFAULT_COHORT_ID,
                "degraded_caller_id": degraded,
                "dials": bad.dials,
            },
        )
    cohort = _cohort_slice(store, workflow.workflow_id, cohort_id=DEFAULT_COHORT_ID, caller_id=None)
    if not _rate_ok(cohort.pickup, cohort.dials):
        return CallerIdRotationBuild(
            None,
            "insufficient_samples",
            {"which": "default_cohort", "cohort_id": DEFAULT_COHORT_ID, "dials": cohort.dials},
        )
    drop = float(cohort.pickup) - float(bad.pickup)
    if drop < CALLER_ID_DROP_VS_WORKFLOW:
        return CallerIdRotationBuild(
            None,
            "signal_not_confirmed",
            {
                "degraded_caller_id": degraded,
                "cohort_id": DEFAULT_COHORT_ID,
                "degraded_pickup": bad.pickup,
                "cohort_pickup": cohort.pickup,
                "drop": drop,
            },
        )

    primary = normalize_primary_kpi(workflow.objective.primary)
    guard_kpi, guard_max_drop = parse_guardrail(workflow.objective.guardrail)
    bad_guard, bad_guard_n = _caller_rate(bad, guard_kpi)
    bad_primary, bad_primary_n = _caller_rate(bad, primary)
    evaluated: list[dict[str, Any]] = []
    for cid in alternatives:
        sl = _cohort_slice(store, workflow.workflow_id, cohort_id=DEFAULT_COHORT_ID, caller_id=cid)
        row: dict[str, Any] = {
            "caller_id": cid,
            "dials": sl.dials,
            "connected": sl.connected,
            "pickup": sl.pickup,
            "reason": None,
        }
        if not _rate_ok(sl.pickup, sl.dials):
            row["reason"] = "insufficient_samples"
            evaluated.append(row)
            continue
        lift = float(sl.pickup) - float(bad.pickup)
        row["pickup_lift"] = lift
        if lift < CALLER_ID_MIN_PICKUP_LIFT:
            row["reason"] = "no_meaningful_pickup_lift"
            evaluated.append(row)
            continue
        cand_guard, cand_guard_n = _caller_rate(sl, guard_kpi)
        if not _rate_ok(bad_guard, bad_guard_n) or not _rate_ok(cand_guard, cand_guard_n):
            row["reason"] = "guardrail_not_evaluable"
            row["guardrail_kpi"] = guard_kpi
            evaluated.append(row)
            continue
        guard_drop = float(bad_guard) - float(cand_guard)
        row["guardrail_drop"] = guard_drop
        if guard_drop > guard_max_drop + 1e-9:
            row["reason"] = "guardrail_violation"
            row["guardrail_kpi"] = guard_kpi
            evaluated.append(row)
            continue
        if primary != "pickup":
            cand_primary, cand_primary_n = _caller_rate(sl, primary)
            if not _rate_ok(bad_primary, bad_primary_n) or not _rate_ok(cand_primary, cand_primary_n):
                row["reason"] = "primary_not_evaluable"
                row["primary_kpi"] = primary
                evaluated.append(row)
                continue
            if float(cand_primary) < float(bad_primary) - DEFAULT_MIN_EFFECT:
                row["reason"] = "primary_regression"
                row["primary_kpi"] = primary
                row["primary_drop"] = float(bad_primary) - float(cand_primary)
                evaluated.append(row)
                continue
            row["primary_rate"] = cand_primary
        row["slice_connected"] = sl.connected
        row["slice_engaged"] = sl.engaged
        evaluated.append(row)

    passed = [row for row in evaluated if row["reason"] is None]
    if not passed:
        ranked = sorted(
            evaluated,
            key=lambda row: (-(row["pickup"] if isinstance(row.get("pickup"), float) else -1.0), row["caller_id"]),
        )
        reason = str(ranked[0]["reason"]) if ranked else "no_supported_replacement"
        return CallerIdRotationBuild(
            None,
            reason,
            {"degraded_caller_id": degraded, "candidates": evaluated},
        )
    passed.sort(key=lambda row: (-float(row["pickup"]), row["caller_id"]))
    chosen = passed[0]
    replacement = str(chosen["caller_id"])
    if replacement not in allowed:
        return CallerIdRotationBuild(None, "no_supported_replacement", {"replacement": replacement})

    move = min(CALLER_ID_ROTATION_MAX_SHIFT, float(current[degraded]))
    if move <= 1e-9:
        return CallerIdRotationBuild(None, "no_allocation_change", {"degraded_weight": current.get(degraded)})
    proposed_weights = dict(current)
    proposed_weights[degraded] = float(current[degraded]) - move
    proposed_weights[replacement] = float(proposed_weights.get(replacement, 0.0)) + move
    proposed_weights = {key: weight for key, weight in proposed_weights.items() if weight > 1e-12}
    proposed = set_allocation(
        config.config,
        cohort_id=DEFAULT_COHORT_ID,
        dimension="caller_id",
        weights=proposed_weights,
    )
    dims = changed_dims(config.config, proposed)
    shift = max_allocation_shift(config.config, proposed)
    if dims != ["caller_id"] or shift > CALLER_ID_ROTATION_MAX_SHIFT + 1e-6:
        return CallerIdRotationBuild(
            None,
            "rotation_not_isolated",
            {"changed_dims": dims, "shift": shift},
        )
    repl_slice = _cohort_slice(
        store, workflow.workflow_id, cohort_id=DEFAULT_COHORT_ID, caller_id=replacement
    )
    repl_primary, repl_primary_n = _caller_rate(repl_slice, primary)
    bad_primary_rate, bad_primary_n = _caller_rate(bad, primary)
    repl_guard, _repl_guard_n = _caller_rate(repl_slice, guard_kpi)
    dry = dry_run_impact(
        control_primary=bad_primary_rate,
        winner_primary=repl_primary,
        control_guardrail=bad_guard if _rate_ok(bad_guard, bad_guard_n) else None,
        winner_guardrail=repl_guard,
        guardrail_max_drop=guard_max_drop,
        control_n=bad_primary_n,
        winner_n=repl_primary_n,
        min_sample=MIN_COHORT_SAMPLE,
        current_alloc=current,
        proposed_alloc=proposed_weights,
    )
    # Score the traffic that actually moves, not a pretend full swap.
    arm_lift = float(repl_slice.pickup) - float(bad.pickup)
    expected = arm_lift * shift
    score = 0.0
    if dry.get("sample_ok") and arm_lift > 0:
        score += min(0.5, expected * 8)
    if dry.get("sample_ok"):
        score += 0.25
    if dry.get("guardrail_ok"):
        score += 0.2
    score = max(0.0, min(1.0, score))
    dry = {
        **dry,
        "label": "caller_id_rotation",
        "limitation": (
            "Moves a limited share of the default cohort off the degraded caller id. "
            "Other cohorts, dimensions, prompt, and schedule stay as they are. "
            "Historical pickup does not prove the next window will recover."
        ),
        "rotation_limit": CALLER_ID_ROTATION_MAX_SHIFT,
        "rotation_limit_reason": (
            f"At most {CALLER_ID_ROTATION_MAX_SHIFT:.2f} of allocation mass moves in one proposal "
            "so a later window can measure pickup. A heavier current weight keeps a residual. "
            "The workflow max_shift still decides auto-apply versus approval."
        ),
        "shift": shift,
        "baseline_pickup": bad.pickup,
        "candidate_pickup": repl_slice.pickup,
        "pickup_lift": arm_lift,
        "expected_pickup_lift": expected,
        "impact_score": score,
        "impact_basis": "bounded_default_cohort_pickup_shift",
    }
    untouched = [
        cohort
        for cohort in (config.config.get("allocations") or {})
        if str(cohort) != DEFAULT_COHORT_ID
    ]
    attempt = next_caller_id_attempt_seq(store, incident.incident_id)
    pid = proposal_id_for(
        "caller_id_rotation",
        incident.incident_id,
        attempt,
        config.version,
        degraded,
        replacement,
        json.dumps(proposed_weights, sort_keys=True),
    )
    now = _now()
    affected = store.uploaded_leads(workflow.workflow_id)
    summary = (
        f"Rotate {shift:.2f} of {workflow.workflow_id}/{DEFAULT_COHORT_ID} caller_id "
        f"from {degraded} to {replacement}"
    )
    rationale = (
        f"Caller id {degraded} pickup {float(bad.pickup):.3f} is {drop:.3f} below the default cohort "
        f"on {bad.dials} dials in that cohort. {replacement} pickup is {float(repl_slice.pickup):.3f} "
        f"on {repl_slice.dials} dials. Moving {shift:.2f} of the default-cohort allocation "
        f"(cap {CALLER_ID_ROTATION_MAX_SHIFT:.2f}) leaves the rest of the config in place "
        "so the next window can show whether pickup recovers."
    )
    return CallerIdRotationBuild(
        ProposalDraft(
            proposal_id=pid,
            workflow_id=workflow.workflow_id,
            base_version=config.version,
            status="draft",
            source=CALLER_ID_ROTATION_SOURCE,
            source_ref=incident.incident_id,
            changed_dims=dims,
            proposed_config=proposed,
            change_summary=summary,
            diff={
                "changed_dims": dims,
                "dimension": "caller_id",
                "cohort_id": DEFAULT_COHORT_ID,
                "allocation": allocation_diff(current, proposed_weights),
                "degraded_caller_id": degraded,
                "replacement_caller_id": replacement,
                "rotation_limit": CALLER_ID_ROTATION_MAX_SHIFT,
                "shift": shift,
                "targeted": True,
                "cohorts_left_unchanged": untouched,
            },
            dry_run=dry,
            impact_score=score,
            affected_leads=int(affected),
            rationale=rationale,
            evidence={
                "incident_id": incident.incident_id,
                "signal_type": incident.signal_type,
                "degraded_caller_id": degraded,
                "replacement_caller_id": replacement,
                "degraded_pickup": bad.pickup,
                "degraded_dials": bad.dials,
                "replacement_pickup": repl_slice.pickup,
                "replacement_dials": repl_slice.dials,
                "cohort_id": DEFAULT_COHORT_ID,
                "cohort_pickup": cohort.pickup,
                "cohort_dials": cohort.dials,
                "pickup_drop": drop,
                "pickup_lift": arm_lift,
                "primary_kpi": primary,
                "degraded_primary": bad_primary_rate,
                "degraded_primary_n": bad_primary_n,
                "replacement_primary": repl_primary,
                "replacement_primary_n": repl_primary_n,
                "guardrail_kpi": guard_kpi,
                "guardrail_max_drop": guard_max_drop,
                "rotation_limit": CALLER_ID_ROTATION_MAX_SHIFT,
                "shift": shift,
                "attempt": attempt,
                "targeted": True,
                "candidates_rejected": [row for row in evaluated if row["reason"] is not None],
            },
            created_at=now,
            updated_at=now,
        ),
        None,
        {"replacement_caller_id": replacement, "shift": shift},
    )


def auto_apply_permitted(
    workflow: Workflow,
    current_config: dict[str, Any],
    proposed_config: dict[str, Any],
    *,
    impact_score: float,
    min_impact: float = 0.25,
) -> tuple[bool, list[str]]:
    """Check simulator auto-apply policy locally before attempting PUT."""
    problems: list[str] = []
    pol = workflow.governance.auto_apply
    dims = changed_dims(current_config, proposed_config)
    if not pol.enabled:
        problems.append("auto-apply is not enabled for this workflow")
    bad = [d for d in dims if d not in set(pol.allowed_dims)]
    if bad:
        problems.append(f"dims not permitted for auto-apply: {bad}")
    shift = max_allocation_shift(current_config, proposed_config)
    if shift > pol.max_shift + 1e-9:
        problems.append(f"allocation shift {shift:.3f} exceeds max_shift {pol.max_shift}")
    if not dims:
        problems.append("no change")
    if impact_score < min_impact:
        problems.append(f"impact_score {impact_score:.3f} below local auto-apply confidence floor {min_impact}")
    return (len(problems) == 0, problems)
