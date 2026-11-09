"""Controller decision audit log. Idempotent inserts by decision_id."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from controller.persistence.repositories import Store


@dataclass
class AuditRecord:
    decision_id: str
    kind: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    workflow_id: str | None = None
    tenant_id: str | None = None
    cohort_id: str | None = None
    related_incident_id: str | None = None
    related_proposal_id: str | None = None
    related_approval_id: str | None = None
    related_experiment_id: str | None = None
    sim_ts: str | None = None
    wall_ts: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_decision_id(kind: str, *parts: object) -> str:
    raw = "|".join([kind, *[str(p) for p in parts]])
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"aud_{digest}"


def record_decision(store: Store, record: AuditRecord) -> bool:
    if not record.wall_ts:
        record.wall_ts = _now()
    before = store.conn.total_changes
    store.insert_audit(record)
    return store.conn.total_changes > before


def audit(
    store: Store,
    *,
    kind: str,
    summary: str,
    details: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    tenant_id: str | None = None,
    cohort_id: str | None = None,
    related_incident_id: str | None = None,
    related_proposal_id: str | None = None,
    related_approval_id: str | None = None,
    related_experiment_id: str | None = None,
    sim_ts: str | None = None,
    idempotency_parts: list[object] | None = None,
) -> AuditRecord:
    parts = idempotency_parts or [
        workflow_id or "",
        related_proposal_id or "",
        related_approval_id or "",
        related_incident_id or "",
        related_experiment_id or "",
        json.dumps(details or {}, sort_keys=True)[:200],
    ]
    rec = AuditRecord(
        decision_id=make_decision_id(kind, *parts),
        kind=kind,
        summary=summary,
        details=details or {},
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        cohort_id=cohort_id,
        related_incident_id=related_incident_id,
        related_proposal_id=related_proposal_id,
        related_approval_id=related_approval_id,
        related_experiment_id=related_experiment_id,
        sim_ts=sim_ts,
        wall_ts=_now(),
    )
    record_decision(store, rec)
    return rec
