"""KPI calculations. Pickup, engagement, and business outcome stay separate.

A missing denominator is None (no data), never 0.0. Attribution uses the call
event's cohort_id / caller_id / config_version.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from controller.domain.cohorts import MIN_COHORT_SAMPLE
from controller.persistence.repositories import Store

# Wilson z for ~95% confidence used only as a descriptive interval, not a winner test.
_Z = 1.96


@dataclass(frozen=True)
class Rate:
    numerator: int
    denominator: int

    @property
    def value(self) -> float | None:
        if self.denominator <= 0:
            return None
        return self.numerator / self.denominator

    def wilson(self) -> tuple[float | None, float | None]:
        n = self.denominator
        if n <= 0:
            return None, None
        p = self.numerator / n
        z2 = _Z * _Z
        denom = 1 + z2 / n
        centre = (p + z2 / (2 * n)) / denom
        half = (_Z * math.sqrt((p * (1 - p) / n) + z2 / (4 * n * n))) / denom
        return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class KpiSlice:
    workflow_id: str
    window_label: str
    window_start_ts: str | None
    window_end_ts: str | None
    cohort_id: str | None
    caller_id: str | None
    config_version: int | None
    dials: int
    connected: int
    engaged: int
    uploaded_leads: int | None
    concluded_leads: int
    halluc_flags: int
    connected_with_qa: int

    @property
    def pickup(self) -> float | None:
        return Rate(self.connected, self.dials).value

    @property
    def engagement(self) -> float | None:
        return Rate(self.engaged, self.connected).value

    @property
    def conclusion_rate(self) -> float | None:
        if self.uploaded_leads is None or self.uploaded_leads <= 0:
            return None
        return self.concluded_leads / self.uploaded_leads

    @property
    def halluc_rate(self) -> float | None:
        return Rate(self.halluc_flags, self.connected_with_qa).value

    def as_dict(self) -> dict[str, Any]:
        pickup_lo, pickup_hi = Rate(self.connected, self.dials).wilson()
        eng_lo, eng_hi = Rate(self.engaged, self.connected).wilson()
        return {
            **asdict(self),
            "pickup": self.pickup,
            "engagement": self.engagement,
            "conclusion_rate": self.conclusion_rate,
            "halluc_rate": self.halluc_rate,
            "pickup_ci95": [pickup_lo, pickup_hi],
            "engagement_ci95": [eng_lo, eng_hi],
            "sample_ok": self.dials >= MIN_COHORT_SAMPLE,
        }


def compute_workflow_kpis(
    store: Store,
    workflow_id: str,
    *,
    window_label: str = "all",
    start_ts: str | None = None,
    end_ts: str | None = None,
) -> KpiSlice:
    dials, connected, engaged, halluc_flags, connected_with_qa = store.call_totals(
        workflow_id, start_ts=start_ts, end_ts=end_ts
    )
    uploaded = store.uploaded_leads(workflow_id)
    concluded = store.concluded_leads_for_workflow(workflow_id, start_ts=start_ts, end_ts=end_ts)
    return KpiSlice(
        workflow_id=workflow_id,
        window_label=window_label,
        window_start_ts=start_ts,
        window_end_ts=end_ts,
        cohort_id=None,
        caller_id=None,
        config_version=None,
        dials=dials,
        connected=connected,
        engaged=engaged,
        uploaded_leads=uploaded,
        concluded_leads=concluded,
        halluc_flags=halluc_flags,
        connected_with_qa=connected_with_qa,
    )


def compute_cohort_kpis(
    store: Store,
    workflow_id: str,
    cohort_id: str,
    *,
    window_label: str = "all",
    start_ts: str | None = None,
    end_ts: str | None = None,
) -> KpiSlice:
    dials, connected, engaged, halluc_flags, connected_with_qa = store.call_totals(
        workflow_id, cohort_id=cohort_id, start_ts=start_ts, end_ts=end_ts
    )
    # Cohort uploaded estimate: leads assigned to this cohort under any stored config version.
    uploaded = store.leads_in_cohort(workflow_id, cohort_id)
    concluded = store.concluded_leads_for_workflow(
        workflow_id, cohort_id=cohort_id, start_ts=start_ts, end_ts=end_ts
    )
    return KpiSlice(
        workflow_id=workflow_id,
        window_label=window_label,
        window_start_ts=start_ts,
        window_end_ts=end_ts,
        cohort_id=cohort_id,
        caller_id=None,
        config_version=None,
        dials=dials,
        connected=connected,
        engaged=engaged,
        uploaded_leads=uploaded,
        concluded_leads=concluded,
        halluc_flags=halluc_flags,
        connected_with_qa=connected_with_qa,
    )


def compute_caller_id_kpis(
    store: Store,
    workflow_id: str,
    caller_id: str,
    *,
    window_label: str = "all",
    start_ts: str | None = None,
    end_ts: str | None = None,
) -> KpiSlice:
    dials, connected, engaged, halluc_flags, connected_with_qa = store.call_totals(
        workflow_id, caller_id=caller_id, start_ts=start_ts, end_ts=end_ts
    )
    return KpiSlice(
        workflow_id=workflow_id,
        window_label=window_label,
        window_start_ts=start_ts,
        window_end_ts=end_ts,
        cohort_id=None,
        caller_id=caller_id,
        config_version=None,
        dials=dials,
        connected=connected,
        engaged=engaged,
        uploaded_leads=None,
        concluded_leads=store.concluded_leads_for_workflow(
            workflow_id, caller_id=caller_id, start_ts=start_ts, end_ts=end_ts
        ),
        halluc_flags=halluc_flags,
        connected_with_qa=connected_with_qa,
    )


def day_bounds(store: Store, workflow_id: str) -> list[str]:
    """Distinct calendar days (YYYY-MM-DD) present in call timestamps for a workflow."""
    return store.call_days(workflow_id)


def split_baseline_recent(days: list[str], *, recent_days: int = 2) -> tuple[list[str], list[str]]:
    """Earlier days are baseline; the last recent_days are recent. Needs at least recent_days+1 days."""
    ordered = sorted(days)
    if len(ordered) <= recent_days:
        return [], ordered
    return ordered[:-recent_days], ordered[-recent_days:]


def window_from_days(days: list[str]) -> tuple[str | None, str | None]:
    if not days:
        return None, None
    start = min(days)
    end = max(days)
    return f"{start}T00:00:00", f"{end}T23:59:59.999999"


def persist_snapshot(store: Store, slice_: KpiSlice) -> None:
    store.upsert_metric_snapshot(slice_)


def compute_and_store_workflow_views(store: Store, workflow_ids: list[str]) -> list[KpiSlice]:
    out: list[KpiSlice] = []
    for wid in workflow_ids:
        overall = compute_workflow_kpis(store, wid, window_label="all")
        persist_snapshot(store, overall)
        out.append(overall)
        days = day_bounds(store, wid)
        baseline_days, recent_days = split_baseline_recent(days)
        if baseline_days:
            b0, b1 = window_from_days(baseline_days)
            baseline = compute_workflow_kpis(store, wid, window_label="baseline", start_ts=b0, end_ts=b1)
            persist_snapshot(store, baseline)
            out.append(baseline)
        if recent_days:
            r0, r1 = window_from_days(recent_days)
            recent = compute_workflow_kpis(store, wid, window_label="recent", start_ts=r0, end_ts=r1)
            persist_snapshot(store, recent)
            out.append(recent)
        for caller_id in store.caller_ids_for_workflow(wid):
            caller = compute_caller_id_kpis(store, wid, caller_id, window_label="all")
            persist_snapshot(store, caller)
            out.append(caller)
        for cohort_id in store.cohort_ids_for_workflow(wid):
            cohort = compute_cohort_kpis(store, wid, cohort_id, window_label="all")
            persist_snapshot(store, cohort)
            out.append(cohort)
    return out
