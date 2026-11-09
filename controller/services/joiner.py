"""Project raw events into campaign / lead / call / config / approval rows.

Call → lead by lead_id. Lead → campaign by campaign_id. A call that arrives
before its lead is stored with lead_matched=0 and repaired when the lead lands.
Historical call cohort_id comes from the call event. Recomputed assignments use
the shared cohort engine against a provided config version.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from controller.domain.cohorts import assign_cohort, parse_cohort_definitions
from controller.persistence.repositories import Store

log = logging.getLogger("controller.joiner")

CONCLUDED_DISPOSITIONS = frozenset({"concluded_interested", "concluded_not_interested"})
BATCH = 1000


@dataclass(frozen=True)
class ProjectionStats:
    processed: int = 0
    campaigns: int = 0
    leads: int = 0
    calls: int = 0
    config_changes: int = 0
    approval_decisions: int = 0
    repaired_calls: int = 0
    cohort_assignments: int = 0
    last_event_id: int = 0


@dataclass(frozen=True)
class WorkflowConfigRef:
    workflow_id: str
    version: int
    config: Mapping[str, Any]


def project_events(store: Store, workflow_configs: list[WorkflowConfigRef] | None = None) -> ProjectionStats:
    """Normalize every event after the projection cursor. Idempotent on restart."""
    configs = {item.workflow_id: item for item in (workflow_configs or [])}
    cursor = store.projection_cursor()
    processed = campaigns = leads = calls = configs_n = approvals = repaired = assignments = 0
    last_id = cursor
    rows = store.iter_events_after(cursor)
    buffer: list[dict] = []
    for row in rows:
        buffer.append(row)
        if len(buffer) < BATCH:
            continue
        stats = _project_batch(store, buffer, configs)
        processed += stats.processed
        campaigns += stats.campaigns
        leads += stats.leads
        calls += stats.calls
        configs_n += stats.config_changes
        approvals += stats.approval_decisions
        repaired += stats.repaired_calls
        assignments += stats.cohort_assignments
        last_id = stats.last_event_id
        buffer.clear()
    if buffer:
        stats = _project_batch(store, buffer, configs)
        processed += stats.processed
        campaigns += stats.campaigns
        leads += stats.leads
        calls += stats.calls
        configs_n += stats.config_changes
        approvals += stats.approval_decisions
        repaired += stats.repaired_calls
        assignments += stats.cohort_assignments
        last_id = stats.last_event_id
    out = ProjectionStats(
        processed=processed,
        campaigns=campaigns,
        leads=leads,
        calls=calls,
        config_changes=configs_n,
        approval_decisions=approvals,
        repaired_calls=repaired,
        cohort_assignments=assignments,
        last_event_id=last_id,
    )
    log.info(
        "projected processed=%s campaigns=%s leads=%s calls=%s repaired=%s assignments=%s last_event_id=%s",
        out.processed,
        out.campaigns,
        out.leads,
        out.calls,
        out.repaired_calls,
        out.cohort_assignments,
        out.last_event_id,
    )
    return out


def _project_batch(store: Store, rows: list[dict], configs: dict[str, WorkflowConfigRef]) -> ProjectionStats:
    campaigns = leads = calls = configs_n = approvals = repaired = assignments = 0
    last_id = store.projection_cursor()
    with store.transaction():
        for row in rows:
            payload = json.loads(row["raw_json"])
            event_type = row["type"]
            event_id = int(row["event_id"])
            if event_type == "campaign_created":
                campaigns += int(_upsert_campaign(store, event_id, payload))
            elif event_type == "lead":
                wrote, assigned, fixed = _upsert_lead(store, event_id, payload, configs)
                leads += wrote
                assignments += assigned
                repaired += fixed
            elif event_type == "call":
                calls += int(_upsert_call(store, event_id, payload))
            elif event_type == "config_changed":
                configs_n += int(_upsert_config_change(store, event_id, payload))
            elif event_type == "approval_decided":
                approvals += int(_upsert_approval(store, event_id, payload))
            last_id = event_id
        store.set_projection_cursor(last_id)
    return ProjectionStats(
        processed=len(rows),
        campaigns=campaigns,
        leads=leads,
        calls=calls,
        config_changes=configs_n,
        approval_decisions=approvals,
        repaired_calls=repaired,
        cohort_assignments=assignments,
        last_event_id=last_id,
    )


def _wrote(store: Store, before: int) -> bool:
    return store.conn.total_changes > before


def _upsert_campaign(store: Store, event_id: int, payload: dict) -> bool:
    campaign_id = payload.get("campaign_id")
    workflow_id = payload.get("workflow_id")
    if not isinstance(campaign_id, str) or not isinstance(workflow_id, str):
        return False
    before = store.conn.total_changes
    store.conn.execute(
        """INSERT INTO campaigns (
               campaign_id, workflow_id, tenant_id, leads, config_version_at_creation, ts, event_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(campaign_id) DO NOTHING""",
        (
            campaign_id,
            workflow_id,
            payload.get("tenant_id"),
            payload.get("leads"),
            payload.get("config_version_at_creation"),
            payload.get("ts"),
            event_id,
        ),
    )
    return _wrote(store, before)


def _upsert_lead(
    store: Store,
    event_id: int,
    payload: dict,
    configs: dict[str, WorkflowConfigRef],
) -> tuple[int, int, int]:
    lead_id = payload.get("lead_id")
    person_id = payload.get("person_id")
    campaign_id = payload.get("campaign_id")
    workflow_id = payload.get("workflow_id")
    if not all(isinstance(x, str) for x in (lead_id, person_id, campaign_id, workflow_id)):
        return 0, 0, 0
    features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
    before = store.conn.total_changes
    store.conn.execute(
        """INSERT INTO leads (
               lead_id, person_id, campaign_id, workflow_id,
               lang, city_tier, age_band, value_band, features_json, ts, event_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(lead_id) DO NOTHING""",
        (
            lead_id,
            person_id,
            campaign_id,
            workflow_id,
            features.get("lang"),
            features.get("city_tier"),
            features.get("age_band"),
            features.get("value_band"),
            json.dumps(features, sort_keys=True),
            payload.get("ts"),
            event_id,
        ),
    )
    wrote = 1 if _wrote(store, before) else 0
    repaired = store.repair_calls_for_lead(lead_id, campaign_id)
    assigned = 0
    ref = configs.get(workflow_id)
    if ref is not None:
        assignment = assign_cohort(features, parse_cohort_definitions(ref.config))
        store.upsert_cohort_assignment(lead_id, workflow_id, ref.version, assignment.cohort_id, assignment.source)
        assigned = 1
    return wrote, assigned, repaired


def _upsert_call(store: Store, event_id: int, payload: dict) -> bool:
    lead_id = payload.get("lead_id") if isinstance(payload.get("lead_id"), str) else None
    campaign_id = payload.get("campaign_id") if isinstance(payload.get("campaign_id"), str) else None
    variant = payload.get("variant") if isinstance(payload.get("variant"), dict) else {}
    connected = bool(payload.get("connected"))
    engaged = bool(payload.get("engaged"))
    disposition = payload.get("disposition") if isinstance(payload.get("disposition"), str) else None
    concluded = int(engaged or (disposition in CONCLUDED_DISPOSITIONS))
    qa = payload.get("qa") if isinstance(payload.get("qa"), dict) else None
    halluc = None
    adherence = None
    if qa is not None:
        if "hallucination_flag" in qa:
            halluc = int(bool(qa.get("hallucination_flag")))
        if isinstance(qa.get("flow_adherence"), (int, float)):
            adherence = float(qa["flow_adherence"])
    lead_matched = int(lead_id is not None and store.lead_exists(lead_id))
    campaign_matched = int(campaign_id is not None and store.campaign_exists(campaign_id))
    before = store.conn.total_changes
    store.conn.execute(
        """INSERT INTO calls (
               event_id, lead_id, person_id, campaign_id, workflow_id, tenant_id,
               attempt_no, campaign_day_index, config_version, cohort_id,
               welcome_message, voice, caller_id, prompt_version,
               connected, engaged, concluded, disposition, duration_s, turns,
               halluc_flag, flow_adherence, ts, lead_matched, campaign_matched
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(event_id) DO NOTHING""",
        (
            event_id,
            lead_id,
            payload.get("person_id") if isinstance(payload.get("person_id"), str) else None,
            campaign_id,
            payload.get("workflow_id") if isinstance(payload.get("workflow_id"), str) else None,
            payload.get("tenant_id") if isinstance(payload.get("tenant_id"), str) else None,
            payload.get("attempt_no") if isinstance(payload.get("attempt_no"), int) else None,
            payload.get("campaign_day_index") if isinstance(payload.get("campaign_day_index"), int) else None,
            payload.get("config_version") if isinstance(payload.get("config_version"), int) else None,
            payload.get("cohort_id") if isinstance(payload.get("cohort_id"), str) else None,
            variant.get("welcome_message"),
            variant.get("voice"),
            variant.get("caller_id"),
            variant.get("prompt_version"),
            int(connected),
            int(engaged),
            concluded,
            disposition,
            payload.get("duration_s") if isinstance(payload.get("duration_s"), int) else None,
            payload.get("turns") if isinstance(payload.get("turns"), int) else None,
            halluc,
            adherence,
            payload.get("ts"),
            lead_matched,
            campaign_matched,
        ),
    )
    return _wrote(store, before)


def _upsert_config_change(store: Store, event_id: int, payload: dict) -> bool:
    workflow_id = payload.get("workflow_id")
    if not isinstance(workflow_id, str):
        return False
    dims = payload.get("changed_dims")
    before = store.conn.total_changes
    store.conn.execute(
        """INSERT INTO config_changes (
               event_id, workflow_id, version, actor, changed_dims_json, proposal_id, approval_id, ts
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(event_id) DO NOTHING""",
        (
            event_id,
            workflow_id,
            payload.get("version") if isinstance(payload.get("version"), int) else None,
            payload.get("actor") if isinstance(payload.get("actor"), str) else None,
            json.dumps(dims if isinstance(dims, list) else []),
            payload.get("proposal_id") if isinstance(payload.get("proposal_id"), str) else None,
            payload.get("approval_id") if isinstance(payload.get("approval_id"), str) else None,
            payload.get("ts"),
        ),
    )
    return _wrote(store, before)


def _upsert_approval(store: Store, event_id: int, payload: dict) -> bool:
    approval_id = payload.get("approval_id")
    workflow_id = payload.get("workflow_id")
    if not isinstance(approval_id, str) or not isinstance(workflow_id, str):
        return False
    reasons = payload.get("reasons")
    before = store.conn.total_changes
    store.conn.execute(
        """INSERT INTO approval_decisions (
               event_id, approval_id, workflow_id, status, reasons_json, ts
           ) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(event_id) DO NOTHING""",
        (
            event_id,
            approval_id,
            workflow_id,
            payload.get("status") if isinstance(payload.get("status"), str) else None,
            json.dumps(reasons if isinstance(reasons, list) else []),
            payload.get("ts"),
        ),
    )
    return _wrote(store, before)