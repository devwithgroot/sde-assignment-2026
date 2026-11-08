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
from controller.domain.cohorts import DEFAULT_COHORT_ID
from controller.persistence.repositories import Store
from controller.services.experiments import (
    ExperimentState,
    allocation_diff,
    balanced_allocation,
    dry_run_impact,
)


ALLOCATION_DIMS = ("welcome_message", "voice", "caller_id")


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
    pid = proposal_id_for(
        "winner",
        experiment.experiment_id,
        experiment.winner_variant,
        config.version,
        json.dumps(weights, sort_keys=True),
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
        diff=diff,
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


def build_rollback_proposal(
    store: Store,
    workflow: Workflow,
    config: WorkflowConfig,
    *,
    reason: str,
    source_ref: str | None = None,
) -> ProposalDraft | None:
    """Restore the config frozen before the monitored winner promotion.

    Uses pre_apply/rollback_candidate snapshots written when the winner proposal
    was submitted — never the post-apply promoted config.
    """
    snap = store.pre_promotion_rollback_snapshot(workflow.workflow_id, source_ref)
    if snap is None:
        return None
    prior = json.loads(snap["config_json"])
    dims = changed_dims(config.config, prior)
    if not dims:
        return None
    if "prompt_version" in dims:
        return None
    dry = {
        "label": "rollback_to_pre_promotion_snapshot",
        "limitation": (
            "Restores the configuration that existed before the monitored winner "
            "promotion; does not prove future KPI recovery."
        ),
        "snapshot_version": snap["version"],
        "snapshot_source": snap["source"],
        "related_proposal_id": snap["related_proposal_id"],
        "impact_score": 0.35,
        "sample_ok": True,
        "guardrail_ok": True,
    }
    pid = proposal_id_for("rollback", workflow.workflow_id, snap["version"], config.version, reason)
    now = _now()
    return ProposalDraft(
        proposal_id=pid,
        workflow_id=workflow.workflow_id,
        base_version=config.version,
        status="draft",
        source="decay_rollback",
        source_ref=source_ref,
        changed_dims=dims,
        proposed_config=prior,
        change_summary=f"Rollback {workflow.workflow_id} to pre-promotion config version {snap['version']}",
        diff={"changed_dims": dims, "to_version": snap["version"], "reason": reason},
        dry_run=dry,
        impact_score=0.35,
        affected_leads=store.uploaded_leads(workflow.workflow_id),
        rationale=reason,
        evidence={
            "rollback_from_version": config.version,
            "rollback_to_version": snap["version"],
            "snapshot_source": snap["source"],
            "related_proposal_id": snap["related_proposal_id"],
        },
        created_at=now,
        updated_at=now,
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
