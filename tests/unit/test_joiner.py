"""Normalize events, join out of order, and persist cohort assignments."""

from __future__ import annotations

import json

from controller.api.models import ChunkMeta
from controller.persistence.repositories import Store
from controller.services.joiner import WorkflowConfigRef, project_events


def _meta(chunk: int, events: int, kind: str = "history") -> ChunkMeta:
    return ChunkMeta(
        chunk=chunk,
        events=events,
        first_event_id=1,
        kind=kind,
        last_event_id=events,
        path="runs/secret",
        until_minute=0,
        until_ts="2027-01-04T00:00:00+05:30",
    )


def _dump(events: list[dict]) -> str:
    return "\n".join(json.dumps(ev) for ev in events)


def test_join_call_to_lead_and_campaign(tmp_path):
    store = Store(tmp_path / "j.sqlite")
    body = _dump(
        [
            {
                "type": "campaign_created",
                "event_id": 1,
                "ts": "t0",
                "tenant_id": "northwind_life",
                "workflow_id": "nw_renewal",
                "campaign_id": "nw_renewal:c0001",
                "leads": 2,
                "config_version_at_creation": 1,
            },
            {
                "type": "lead",
                "event_id": 2,
                "ts": "t0",
                "workflow_id": "nw_renewal",
                "campaign_id": "nw_renewal:c0001",
                "lead_id": "nw_renewal:l1",
                "person_id": "nw_renewal:p1",
                "features": {"lang": "hi", "city_tier": "2", "age_band": "46-60", "value_band": "mid"},
            },
            {
                "type": "call",
                "event_id": 3,
                "ts": "t1",
                "tenant_id": "northwind_life",
                "workflow_id": "nw_renewal",
                "campaign_id": "nw_renewal:c0001",
                "lead_id": "nw_renewal:l1",
                "person_id": "nw_renewal:p1",
                "attempt_no": 1,
                "campaign_day_index": 0,
                "config_version": 1,
                "cohort_id": "default",
                "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
                "connected": True,
                "engaged": True,
                "disposition": "concluded_interested",
                "duration_s": 80,
                "turns": 8,
                "qa": {"hallucination_flag": False, "flow_adherence": 0.9},
            },
        ]
    )
    store.commit_chunk(_meta(0, 3), body)
    stats = project_events(
        store,
        [WorkflowConfigRef("nw_renewal", 1, {"cohorts": [], "prompt_version": "P1"})],
    )
    assert stats.campaigns == 1
    assert stats.leads == 1
    assert stats.calls == 1
    assert store.unmatched_call_count() == 0
    call = store.conn.execute("SELECT * FROM calls WHERE event_id = 3").fetchone()
    assert call["lead_matched"] == 1
    assert call["campaign_matched"] == 1
    assert call["concluded"] == 1
    assert call["cohort_id"] == "default"
    assert store.concluded_lead_count() == 1
    store.close()


def test_call_before_lead_is_repaired(tmp_path):
    store = Store(tmp_path / "j.sqlite")
    first = _dump(
        [
            {
                "type": "call",
                "event_id": 1,
                "workflow_id": "nw_kyc",
                "campaign_id": "nw_kyc:c0001",
                "lead_id": "nw_kyc:l9",
                "person_id": "nw_kyc:p9",
                "attempt_no": 1,
                "config_version": 1,
                "cohort_id": "default",
                "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
                "connected": False,
                "engaged": False,
                "disposition": "no_answer",
            }
        ]
    )
    store.commit_chunk(_meta(0, 1), first)
    project_events(store)
    assert store.unmatched_call_count() == 1

    second = _dump(
        [
            {
                "type": "campaign_created",
                "event_id": 2,
                "workflow_id": "nw_kyc",
                "campaign_id": "nw_kyc:c0001",
                "leads": 1,
                "config_version_at_creation": 1,
            },
            {
                "type": "lead",
                "event_id": 3,
                "workflow_id": "nw_kyc",
                "campaign_id": "nw_kyc:c0001",
                "lead_id": "nw_kyc:l9",
                "person_id": "nw_kyc:p9",
                "features": {"lang": "en", "city_tier": "1", "age_band": "18-30", "value_band": "low"},
            },
        ]
    )
    store.commit_chunk(_meta(1, 2, kind="events"), second)
    stats = project_events(store, [WorkflowConfigRef("nw_kyc", 1, {"cohorts": []})])
    assert stats.repaired_calls == 1
    assert store.unmatched_call_count() == 0
    store.close()


def test_returning_person_across_campaigns(tmp_path):
    store = Store(tmp_path / "j.sqlite")
    body = _dump(
        [
            {
                "type": "lead",
                "event_id": 1,
                "workflow_id": "ks_cancel",
                "campaign_id": "ks_cancel:c0001",
                "lead_id": "ks_cancel:l1",
                "person_id": "ks_cancel:p7",
                "features": {"lang": "hi"},
            },
            {
                "type": "lead",
                "event_id": 2,
                "workflow_id": "ks_cancel",
                "campaign_id": "ks_cancel:c0002",
                "lead_id": "ks_cancel:l2",
                "person_id": "ks_cancel:p7",
                "features": {"lang": "hi"},
            },
        ]
    )
    store.commit_chunk(_meta(0, 2), body)
    project_events(store)
    assert store.returning_person_count() == 1
    store.close()


def test_multiple_attempts_same_lead(tmp_path):
    store = Store(tmp_path / "j.sqlite")
    body = _dump(
        [
            {
                "type": "lead",
                "event_id": 1,
                "workflow_id": "nw_renewal",
                "campaign_id": "nw_renewal:c1",
                "lead_id": "nw_renewal:l1",
                "person_id": "nw_renewal:p1",
                "features": {"lang": "ta", "age_band": "60+"},
            },
            {
                "type": "call",
                "event_id": 2,
                "workflow_id": "nw_renewal",
                "campaign_id": "nw_renewal:c1",
                "lead_id": "nw_renewal:l1",
                "person_id": "nw_renewal:p1",
                "attempt_no": 1,
                "cohort_id": "default",
                "variant": {},
                "connected": False,
                "engaged": False,
                "disposition": "no_answer",
            },
            {
                "type": "call",
                "event_id": 3,
                "workflow_id": "nw_renewal",
                "campaign_id": "nw_renewal:c1",
                "lead_id": "nw_renewal:l1",
                "person_id": "nw_renewal:p1",
                "attempt_no": 2,
                "cohort_id": "senior_ta",
                "variant": {},
                "connected": True,
                "engaged": False,
                "disposition": "short_hangup",
                "qa": {"hallucination_flag": True, "flow_adherence": 0.8},
            },
        ]
    )
    store.commit_chunk(_meta(0, 3), body)
    project_events(
        store,
        [
            WorkflowConfigRef(
                "nw_renewal",
                4,
                {"cohorts": [{"id": "senior_ta", "where": {"age_band": ["60+"], "lang": ["ta"]}}]},
            )
        ],
    )
    assert store.table_count("calls") == 2
    assignment = store.conn.execute(
        "SELECT cohort_id, source, config_version FROM cohort_assignments WHERE lead_id = ?",
        ("nw_renewal:l1",),
    ).fetchone()
    assert assignment["cohort_id"] == "senior_ta"
    assert assignment["config_version"] == 4
    # Historical cohort on the call stays what the simulator dialled.
    second = store.conn.execute("SELECT cohort_id, halluc_flag FROM calls WHERE event_id = 3").fetchone()
    assert second["cohort_id"] == "senior_ta"
    assert second["halluc_flag"] == 1
    store.close()


def test_projection_is_restart_safe(tmp_path):
    path = tmp_path / "j.sqlite"
    store = Store(path)
    body = _dump(
        [
            {
                "type": "lead",
                "event_id": 1,
                "workflow_id": "nw_renewal",
                "campaign_id": "nw_renewal:c1",
                "lead_id": "nw_renewal:l1",
                "person_id": "nw_renewal:p1",
                "features": {"lang": "hi"},
            }
        ]
    )
    store.commit_chunk(_meta(0, 1), body)
    first = project_events(store, [WorkflowConfigRef("nw_renewal", 1, {"cohorts": []})])
    assert first.leads == 1
    store.close()

    again = Store(path)
    second = project_events(again, [WorkflowConfigRef("nw_renewal", 1, {"cohorts": []})])
    assert second.processed == 0
    assert again.table_count("leads") == 1
    assert again.projection_cursor() == 1
    again.close()


def test_config_and_approval_events(tmp_path):
    store = Store(tmp_path / "j.sqlite")
    body = _dump(
        [
            {
                "type": "config_changed",
                "event_id": 1,
                "workflow_id": "ks_cancel",
                "version": 2,
                "actor": "supervisor",
                "changed_dims": ["prompt_version"],
                "proposal_id": None,
                "approval_id": None,
            },
            {
                "type": "approval_decided",
                "event_id": 2,
                "approval_id": "apr_00001",
                "workflow_id": "ks_cancel",
                "status": "approved",
                "reasons": [],
            },
        ]
    )
    store.commit_chunk(_meta(0, 2, kind="events"), body)
    stats = project_events(store)
    assert stats.config_changes == 1
    assert stats.approval_decisions == 1
    store.close()
