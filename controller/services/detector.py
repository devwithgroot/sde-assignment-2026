"""Population-level issue detection. Produces signals only; never changes config.

A single call cannot open a signal. Small samples are insufficient, not evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from controller.domain.cohorts import MIN_COHORT_SAMPLE
from controller.persistence.repositories import Store
from controller.services.metrics import (
    compute_caller_id_kpis,
    compute_workflow_kpis,
    day_bounds,
    split_baseline_recent,
    window_from_days,
)

# Absolute drop (or rise for hallucination) required once sample minimums are met.
PICKUP_DROP = 0.05
ENGAGEMENT_DROP = 0.05
CALLER_ID_DROP_VS_WORKFLOW = 0.08
HALLUC_RISE = 0.02
HALLUC_ABS_FLOOR = 0.03


@dataclass(frozen=True)
class Signal:
    fingerprint: str
    workflow_id: str
    signal_type: str
    kpi: str
    dimension: str | None
    cohort_id: str | None
    caller_id: str | None
    severity: str
    baseline_value: float | None
    recent_value: float | None
    baseline_n: int
    recent_n: int
    threshold: float
    message: str
    evidence: dict[str, Any]
    sim_ts: str | None


def detect_workflow_issues(store: Store, workflow_id: str) -> list[Signal]:
    signals: list[Signal] = []
    days = day_bounds(store, workflow_id)
    baseline_days, recent_days = split_baseline_recent(days)
    if not baseline_days or not recent_days:
        # Not enough calendar span to compare populations.
        signals.extend(_caller_id_signals(store, workflow_id, window="all"))
        return _persist(store, signals)

    b0, b1 = window_from_days(baseline_days)
    r0, r1 = window_from_days(recent_days)
    baseline = compute_workflow_kpis(store, workflow_id, window_label="baseline", start_ts=b0, end_ts=b1)
    recent = compute_workflow_kpis(store, workflow_id, window_label="recent", start_ts=r0, end_ts=r1)
    sim_ts = recent.window_end_ts

    if (
        baseline.pickup is not None
        and recent.pickup is not None
        and baseline.dials >= MIN_COHORT_SAMPLE
        and recent.dials >= MIN_COHORT_SAMPLE
        and baseline.pickup - recent.pickup >= PICKUP_DROP
    ):
        drop = baseline.pickup - recent.pickup
        signals.append(
            Signal(
                fingerprint=f"{workflow_id}|pickup_drift|{recent.window_label}|{r0}|{r1}",
                workflow_id=workflow_id,
                signal_type="pickup_drift",
                kpi="pickup",
                dimension="workflow",
                cohort_id=None,
                caller_id=None,
                severity="high" if drop >= 2 * PICKUP_DROP else "medium",
                baseline_value=baseline.pickup,
                recent_value=recent.pickup,
                baseline_n=baseline.dials,
                recent_n=recent.dials,
                threshold=PICKUP_DROP,
                message=f"pickup fell {drop:.3f} vs baseline on {recent.dials} dials",
                evidence={
                    "baseline_days": baseline_days,
                    "recent_days": recent_days,
                    "baseline_connected": baseline.connected,
                    "recent_connected": recent.connected,
                },
                sim_ts=sim_ts,
            )
        )

    if (
        baseline.engagement is not None
        and recent.engagement is not None
        and baseline.connected >= MIN_COHORT_SAMPLE
        and recent.connected >= MIN_COHORT_SAMPLE
        and baseline.engagement - recent.engagement >= ENGAGEMENT_DROP
    ):
        drop = baseline.engagement - recent.engagement
        signals.append(
            Signal(
                fingerprint=f"{workflow_id}|engagement_drift|{recent.window_label}|{r0}|{r1}",
                workflow_id=workflow_id,
                signal_type="engagement_drift",
                kpi="engagement",
                dimension="workflow",
                cohort_id=None,
                caller_id=None,
                severity="high" if drop >= 2 * ENGAGEMENT_DROP else "medium",
                baseline_value=baseline.engagement,
                recent_value=recent.engagement,
                baseline_n=baseline.connected,
                recent_n=recent.connected,
                threshold=ENGAGEMENT_DROP,
                message=f"engagement fell {drop:.3f} vs baseline on {recent.connected} connected calls",
                evidence={
                    "baseline_days": baseline_days,
                    "recent_days": recent_days,
                    "baseline_engaged": baseline.engaged,
                    "recent_engaged": recent.engaged,
                },
                sim_ts=sim_ts,
            )
        )

    if (
        baseline.halluc_rate is not None
        and recent.halluc_rate is not None
        and baseline.connected_with_qa >= MIN_COHORT_SAMPLE
        and recent.connected_with_qa >= MIN_COHORT_SAMPLE
        and recent.halluc_rate >= HALLUC_ABS_FLOOR
        and recent.halluc_rate - baseline.halluc_rate >= HALLUC_RISE
    ):
        rise = recent.halluc_rate - baseline.halluc_rate
        signals.append(
            Signal(
                fingerprint=f"{workflow_id}|hallucination_spike|{recent.window_label}|{r0}|{r1}",
                workflow_id=workflow_id,
                signal_type="hallucination_spike",
                kpi="halluc_rate",
                dimension="workflow",
                cohort_id=None,
                caller_id=None,
                severity="high",
                baseline_value=baseline.halluc_rate,
                recent_value=recent.halluc_rate,
                baseline_n=baseline.connected_with_qa,
                recent_n=recent.connected_with_qa,
                threshold=HALLUC_RISE,
                message=f"hallucination flag rate rose {rise:.3f} to {recent.halluc_rate:.3f}",
                evidence={
                    "baseline_flags": baseline.halluc_flags,
                    "recent_flags": recent.halluc_flags,
                    "note": "QA flag is noisy; threshold includes absolute floor",
                },
                sim_ts=sim_ts,
            )
        )

    signals.extend(_caller_id_signals(store, workflow_id, window="all"))
    return _persist(store, signals)


def _caller_id_signals(store: Store, workflow_id: str, *, window: str) -> list[Signal]:
    overall = compute_workflow_kpis(store, workflow_id, window_label=window)
    if overall.pickup is None or overall.dials < MIN_COHORT_SAMPLE:
        return []
    out: list[Signal] = []
    for caller_id in store.caller_ids_for_workflow(workflow_id):
        slice_ = compute_caller_id_kpis(store, workflow_id, caller_id, window_label=window)
        if slice_.pickup is None or slice_.dials < MIN_COHORT_SAMPLE:
            continue
        drop = overall.pickup - slice_.pickup
        if drop < CALLER_ID_DROP_VS_WORKFLOW:
            continue
        out.append(
            Signal(
                fingerprint=f"{workflow_id}|caller_id_health|{caller_id}|{window}",
                workflow_id=workflow_id,
                signal_type="caller_id_health",
                kpi="pickup",
                dimension="caller_id",
                cohort_id=None,
                caller_id=caller_id,
                severity="high" if drop >= 2 * CALLER_ID_DROP_VS_WORKFLOW else "medium",
                baseline_value=overall.pickup,
                recent_value=slice_.pickup,
                baseline_n=overall.dials,
                recent_n=slice_.dials,
                threshold=CALLER_ID_DROP_VS_WORKFLOW,
                message=f"caller_id {caller_id} pickup {slice_.pickup:.3f} is {drop:.3f} below workflow {overall.pickup:.3f}",
                evidence={"caller_dials": slice_.dials, "caller_connected": slice_.connected},
                sim_ts=slice_.window_end_ts,
            )
        )
    return out


def _persist(store: Store, signals: list[Signal]) -> list[Signal]:
    kept: list[Signal] = []
    for signal in signals:
        inserted = store.insert_signal(signal)
        if inserted:
            kept.append(signal)
    return kept


def detect_all(store: Store, workflow_ids: list[str]) -> list[Signal]:
    out: list[Signal] = []
    for wid in workflow_ids:
        out.extend(detect_workflow_issues(store, wid))
    return out