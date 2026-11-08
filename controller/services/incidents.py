"""Population-signal incident lifecycle (no LLM / validation — that is Phase 7).

Opens controller incidents from detection signals, links related experiments and
proposals, and advances status through investigation → mitigation → resolution.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from controller.persistence.repositories import Store
from controller.services.audit import audit
from controller.services.detector import Signal

OPEN_STATUSES = frozenset({"detected", "investigating", "mitigating"})
TERMINAL_STATUSES = frozenset({"resolved", "closed", "suppressed"})


@dataclass
class Incident:
    incident_id: str
    workflow_id: str
    signal_fingerprint: str
    signal_type: str
    severity: str
    status: str
    title: str
    created_at: str
    updated_at: str
    tenant_id: str | None = None
    message: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    related_experiment_id: str | None = None
    related_proposal_id: str | None = None
    resolution: str | None = None
    closed_at: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def incident_id_for(fingerprint: str) -> str:
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:20]
    return f"inc_{digest}"


def _severity_rank(severity: str) -> int:
    order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return order.get((severity or "").lower(), 2)


def open_or_refresh_incident(store: Store, signal: Signal, *, tenant_id: str | None = None) -> Incident:
    existing = store.get_incident_by_fingerprint(signal.fingerprint)
    now = _now()
    if existing is not None:
        status = existing["status"]
        if status in TERMINAL_STATUSES:
            # Re-open only if a new severity escalation is present on a still-open signal stream.
            # Fingerprint uniqueness means same signal won't re-fire; keep terminal.
            return Incident(
                incident_id=existing["incident_id"],
                workflow_id=existing["workflow_id"],
                tenant_id=existing["tenant_id"],
                signal_fingerprint=existing["signal_fingerprint"],
                signal_type=existing["signal_type"],
                severity=existing["severity"],
                status=existing["status"],
                title=existing["title"],
                message=existing["message"],
                evidence={},
                related_experiment_id=existing["related_experiment_id"],
                related_proposal_id=existing["related_proposal_id"],
                resolution=existing["resolution"],
                created_at=existing["created_at"],
                updated_at=existing["updated_at"],
                closed_at=existing["closed_at"],
            )
        incident = Incident(
            incident_id=existing["incident_id"],
            workflow_id=signal.workflow_id,
            tenant_id=tenant_id or existing["tenant_id"],
            signal_fingerprint=signal.fingerprint,
            signal_type=signal.signal_type,
            severity=signal.severity if _severity_rank(signal.severity) >= _severity_rank(existing["severity"]) else existing["severity"],
            status=existing["status"],
            title=existing["title"],
            message=signal.message,
            evidence={
                "signal_type": signal.signal_type,
                "kpi": signal.kpi,
                "baseline_value": signal.baseline_value,
                "recent_value": signal.recent_value,
                "baseline_n": signal.baseline_n,
                "recent_n": signal.recent_n,
                "threshold": signal.threshold,
                "caller_id": signal.caller_id,
                "cohort_id": signal.cohort_id,
                "detector_evidence": signal.evidence,
            },
            related_experiment_id=existing["related_experiment_id"],
            related_proposal_id=existing["related_proposal_id"],
            resolution=existing["resolution"],
            created_at=existing["created_at"],
            updated_at=now,
            closed_at=existing["closed_at"],
        )
        store.upsert_incident(incident)
        return incident

    incident = Incident(
        incident_id=incident_id_for(signal.fingerprint),
        workflow_id=signal.workflow_id,
        tenant_id=tenant_id,
        signal_fingerprint=signal.fingerprint,
        signal_type=signal.signal_type,
        severity=signal.severity,
        status="detected",
        title=f"{signal.signal_type} on {signal.workflow_id}",
        message=signal.message,
        evidence={
            "signal_type": signal.signal_type,
            "kpi": signal.kpi,
            "baseline_value": signal.baseline_value,
            "recent_value": signal.recent_value,
            "baseline_n": signal.baseline_n,
            "recent_n": signal.recent_n,
            "threshold": signal.threshold,
            "caller_id": signal.caller_id,
            "cohort_id": signal.cohort_id,
            "detector_evidence": signal.evidence,
        },
        created_at=now,
        updated_at=now,
    )
    store.upsert_incident(incident)
    audit(
        store,
        kind="incident_opened",
        summary=f"Opened incident {incident.incident_id} for {signal.signal_type}",
        details={"fingerprint": signal.fingerprint, "severity": signal.severity},
        workflow_id=signal.workflow_id,
        tenant_id=tenant_id,
        related_incident_id=incident.incident_id,
        sim_ts=signal.sim_ts,
        idempotency_parts=[incident.incident_id, "opened"],
    )
    return incident


def sync_incidents_from_signals(
    store: Store,
    signals: list[Signal],
    *,
    tenant_by_workflow: dict[str, str] | None = None,
) -> list[Incident]:
    tenants = tenant_by_workflow or {}
    out: list[Incident] = []
    for signal in signals:
        out.append(open_or_refresh_incident(store, signal, tenant_id=tenants.get(signal.workflow_id)))
    return out


def link_incident_experiment(store: Store, incident_id: str, experiment_id: str) -> None:
    row = store.get_incident(incident_id)
    if row is None:
        return
    incident = _row_to_incident(row)
    if incident.status == "detected":
        incident.status = "investigating"
    incident.related_experiment_id = experiment_id
    incident.updated_at = _now()
    store.upsert_incident(incident)
    audit(
        store,
        kind="incident_linked_experiment",
        summary=f"Linked incident {incident_id} to experiment {experiment_id}",
        details={"experiment_id": experiment_id},
        workflow_id=incident.workflow_id,
        related_incident_id=incident_id,
        related_experiment_id=experiment_id,
        idempotency_parts=[incident_id, experiment_id, "link_exp"],
    )


def mark_incident_mitigating(store: Store, incident_id: str, proposal_id: str) -> None:
    row = store.get_incident(incident_id)
    if row is None:
        return
    incident = _row_to_incident(row)
    incident.status = "mitigating"
    incident.related_proposal_id = proposal_id
    incident.updated_at = _now()
    store.upsert_incident(incident)
    audit(
        store,
        kind="incident_mitigating",
        summary=f"Incident {incident_id} moved to mitigating via proposal {proposal_id}",
        details={"proposal_id": proposal_id},
        workflow_id=incident.workflow_id,
        related_incident_id=incident_id,
        related_proposal_id=proposal_id,
        idempotency_parts=[incident_id, proposal_id, "mitigating"],
    )


def resolve_incident(store: Store, incident_id: str, resolution: str) -> None:
    row = store.get_incident(incident_id)
    if row is None:
        return
    incident = _row_to_incident(row)
    if incident.status in {"resolved", "closed"}:
        return
    incident.status = "resolved"
    incident.resolution = resolution
    incident.updated_at = _now()
    store.upsert_incident(incident)
    audit(
        store,
        kind="incident_resolved",
        summary=f"Resolved incident {incident_id}",
        details={"resolution": resolution},
        workflow_id=incident.workflow_id,
        related_incident_id=incident_id,
        idempotency_parts=[incident_id, "resolved", resolution[:80]],
    )


def close_incident(store: Store, incident_id: str, resolution: str | None = None) -> None:
    row = store.get_incident(incident_id)
    if row is None:
        return
    incident = _row_to_incident(row)
    if incident.status == "closed":
        return
    now = _now()
    incident.status = "closed"
    if resolution:
        incident.resolution = resolution
    incident.closed_at = now
    incident.updated_at = now
    store.upsert_incident(incident)
    audit(
        store,
        kind="incident_closed",
        summary=f"Closed incident {incident_id}",
        details={"resolution": incident.resolution},
        workflow_id=incident.workflow_id,
        related_incident_id=incident_id,
        idempotency_parts=[incident_id, "closed"],
    )


def find_open_incidents_for_workflow(store: Store, workflow_id: str) -> list[Incident]:
    rows = store.list_incidents(workflow_id=workflow_id)
    return [_row_to_incident(r) for r in rows if r["status"] in OPEN_STATUSES]


def _row_to_incident(row) -> Incident:
    import json

    evidence = row["evidence_json"]
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError:
            evidence = {"raw": evidence}
    return Incident(
        incident_id=row["incident_id"],
        workflow_id=row["workflow_id"],
        tenant_id=row["tenant_id"],
        signal_fingerprint=row["signal_fingerprint"],
        signal_type=row["signal_type"],
        severity=row["severity"],
        status=row["status"],
        title=row["title"],
        message=row["message"],
        evidence=evidence or {},
        related_experiment_id=row["related_experiment_id"],
        related_proposal_id=row["related_proposal_id"],
        resolution=row["resolution"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        closed_at=row["closed_at"],
    )
