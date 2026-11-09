"""Post-promotion decay detection. Rollback is proposed through governance, never forced."""

from __future__ import annotations

from typing import Any

from controller.domain.cohorts import MIN_COHORT_SAMPLE
from controller.persistence.repositories import Store
from controller.services.audit import audit
from controller.services.metrics import compute_workflow_kpis, day_bounds, split_baseline_recent, window_from_days

PRIMARY_DROP = 0.05
GUARDRAIL_DROP = 0.03
COOLDOWN_PROMOTION_STATUSES = frozenset({"promoted", "promoted_auto_apply"})


def detect_promoted_decay(store: Store) -> list[dict[str, Any]]:
    """Compare recent vs baseline windows for workflows with promoted experiments."""
    hits: list[dict[str, Any]] = []
    rows = store.list_experiments()
    seen_workflows: set[str] = set()
    for row in rows:
        if row["promotion_status"] not in COOLDOWN_PROMOTION_STATUSES:
            continue
        workflow_id = row["workflow_id"]
        if workflow_id in seen_workflows:
            continue
        seen_workflows.add(workflow_id)
        days = day_bounds(store, workflow_id)
        baseline_days, recent_days = split_baseline_recent(days)
        if not baseline_days or not recent_days:
            continue
        b0, b1 = window_from_days(baseline_days)
        r0, r1 = window_from_days(recent_days)
        baseline = compute_workflow_kpis(store, workflow_id, window_label="baseline", start_ts=b0, end_ts=b1)
        recent = compute_workflow_kpis(store, workflow_id, window_label="recent", start_ts=r0, end_ts=r1)
        primary = row["primary_kpi"]
        guardrail = row["guardrail_kpi"]
        reason = None
        if primary == "engagement":
            if (
                baseline.engagement is not None
                and recent.engagement is not None
                and baseline.connected >= MIN_COHORT_SAMPLE
                and recent.connected >= MIN_COHORT_SAMPLE
                and baseline.engagement - recent.engagement >= PRIMARY_DROP
            ):
                reason = (
                    f"promoted winner decay: engagement {baseline.engagement:.4f} → {recent.engagement:.4f}"
                )
        else:
            if (
                baseline.pickup is not None
                and recent.pickup is not None
                and baseline.dials >= MIN_COHORT_SAMPLE
                and recent.dials >= MIN_COHORT_SAMPLE
                and baseline.pickup - recent.pickup >= PRIMARY_DROP
            ):
                reason = f"promoted winner decay: pickup {baseline.pickup:.4f} → {recent.pickup:.4f}"
        if reason is None and guardrail == "pickup":
            if (
                baseline.pickup is not None
                and recent.pickup is not None
                and baseline.dials >= MIN_COHORT_SAMPLE
                and recent.dials >= MIN_COHORT_SAMPLE
                and baseline.pickup - recent.pickup >= GUARDRAIL_DROP
            ):
                reason = (
                    f"promoted winner guardrail break: pickup {baseline.pickup:.4f} → {recent.pickup:.4f}"
                )
        if reason is None:
            continue
        hit = {
            "workflow_id": workflow_id,
            "experiment_id": row["experiment_id"],
            "reason": reason,
            "primary_kpi": primary,
            "guardrail_kpi": guardrail,
        }
        hits.append(hit)
        store.update_experiment_promotion(row["experiment_id"], "decay_detected", decision_reason=reason)
        audit(
            store,
            kind="decay_detected",
            summary=reason,
            details=hit,
            workflow_id=workflow_id,
            related_experiment_id=row["experiment_id"],
            idempotency_parts=[row["experiment_id"], "decay", r0, r1],
        )
    return hits
