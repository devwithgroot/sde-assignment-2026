"""Structured counters, operational alerts, and supervisor summary (Phase 8)."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from controller.persistence.repositories import Store

log = logging.getLogger("controller.observability")


@dataclass
class SupervisorSummary:
    generated_at: str
    schema_version: int | None
    counts: dict[str, int] = field(default_factory=dict)
    actions_by_tenant: dict[str, int] = field(default_factory=dict)
    actions_by_workflow: dict[str, int] = field(default_factory=dict)
    wait_ticks_by_workflow: dict[str, int] = field(default_factory=dict)
    starvation_workflows: list[str] = field(default_factory=list)
    deferred_open: list[dict[str, Any]] = field(default_factory=list)
    credit_budget: dict[str, Any] = field(default_factory=dict)
    incidents_open: int = 0
    proposals_pending: int = 0
    approvals_pending: int = 0
    approvals_approved: int = 0
    experiments_running: int = 0
    alerts: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, float] = field(default_factory=dict)
    health: str = "ok"
    notes: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bump_counter(store: Store, name: str, delta: float = 1.0) -> None:
    store.bump_observability_counter(name, delta)


def emit_alert(
    store: Store,
    *,
    kind: str,
    severity: str,
    message: str,
    workflow_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> bool:
    """Idempotent alert by fingerprint. Returns True if newly inserted."""
    raw = f"{kind}|{workflow_id or ''}|{message}"
    alert_id = f"alert_{hashlib.sha256(raw.encode()).hexdigest()[:20]}"
    inserted = store.insert_operational_alert(
        alert_id=alert_id,
        kind=kind,
        severity=severity,
        workflow_id=workflow_id,
        message=message,
        details=details or {},
        created_at=_now(),
    )
    if inserted:
        log.warning(json.dumps({"event": "alert", "kind": kind, "severity": severity, "message": message}, sort_keys=True))
        bump_counter(store, f"alert.{kind}")
    return inserted


def build_supervisor_summary(store: Store, *, starvation_wait: int = 20) -> SupervisorSummary:
    schema = None
    try:
        row = store.conn.execute("SELECT version FROM schema_meta").fetchone()
        schema = int(row["version"]) if row else None
    except Exception:  # noqa: BLE001
        schema = None

    summary = SupervisorSummary(generated_at=_now(), schema_version=schema)
    for table in (
        "events",
        "calls",
        "incidents",
        "proposals",
        "controller_approvals",
        "experiments",
        "llm_suggestion_attempts",
        "deferred_actions",
        "operational_alerts",
    ):
        try:
            summary.counts[table] = store.table_count(table)
        except Exception:  # noqa: BLE001
            summary.counts[table] = 0

    for row in store.list_scheduler_state():
        summary.actions_by_workflow[row["workflow_id"]] = int(row["actions_taken"])
        summary.wait_ticks_by_workflow[row["workflow_id"]] = int(row["wait_ticks"])
        tenant = row["tenant_id"] or "unknown"
        summary.actions_by_tenant[tenant] = summary.actions_by_tenant.get(tenant, 0) + int(row["actions_taken"])
        if int(row["wait_ticks"]) >= starvation_wait:
            summary.starvation_workflows.append(row["workflow_id"])

    summary.deferred_open = [
        dict(r)
        for r in store.list_deferred_actions(status="deferred")
    ][:50]
    credit = store.get_credit_budget()
    if credit is not None:
        summary.credit_budget = dict(credit)

    summary.incidents_open = sum(
        1 for r in store.list_incidents() if r["status"] in {"detected", "investigating", "mitigating"}
    )
    summary.proposals_pending = sum(
        1 for r in store.list_proposals() if r["status"] in {"draft", "submitted", "approved"}
    )
    summary.approvals_pending = len(store.list_controller_approvals(status="pending"))
    summary.approvals_approved = len(store.list_controller_approvals(status="approved"))
    summary.experiments_running = sum(1 for r in store.list_experiments() if r["status"] == "running")
    summary.alerts = [dict(r) for r in store.list_operational_alerts(limit=50)]
    summary.counters = {r["name"]: float(r["value"]) for r in store.list_observability_counters()}

    if summary.starvation_workflows:
        summary.health = "degraded"
        summary.notes.append(f"starvation_wait>={starvation_wait}: {summary.starvation_workflows}")
        for wid in summary.starvation_workflows:
            emit_alert(
                store,
                kind="scheduler_starvation",
                severity="medium",
                message=f"workflow {wid} wait_ticks high",
                workflow_id=wid,
                details={"wait_ticks": summary.wait_ticks_by_workflow.get(wid)},
            )
    if summary.credit_budget:
        sim = summary.credit_budget.get("last_sim_credits")
        reserve = summary.credit_budget.get("emergency_reserve")
        if sim is not None and reserve is not None and float(sim) < float(reserve):
            summary.health = "degraded"
            summary.notes.append("sim credits below emergency reserve")
            emit_alert(
                store,
                kind="credits_low",
                severity="high",
                message=f"credits {sim} below emergency reserve {reserve}",
                details={"sim_credits": sim, "reserve": reserve},
            )
    if summary.incidents_open > 0:
        summary.notes.append(f"{summary.incidents_open} open incidents")

    bump_counter(store, "supervisor_summary.built")
    log.info(
        json.dumps(
            {
                "event": "supervisor_summary",
                "health": summary.health,
                "incidents_open": summary.incidents_open,
                "proposals_pending": summary.proposals_pending,
                "approvals_pending": summary.approvals_pending,
            },
            sort_keys=True,
        )
    )
    return summary


def summary_as_dict(summary: SupervisorSummary) -> dict[str, Any]:
    return asdict(summary)
