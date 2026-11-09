"""KPI denominators: missing data is None, never a fake 0.0 rate."""

from __future__ import annotations

import json

from controller.api.models import ChunkMeta
from controller.persistence.repositories import Store
from controller.services.joiner import project_events
from controller.services.metrics import Rate, compute_caller_id_kpis, compute_workflow_kpis


def _meta(n: int) -> ChunkMeta:
    return ChunkMeta(
        chunk=0,
        events=n,
        first_event_id=1,
        kind="history",
        last_event_id=n,
        path="x",
        until_minute=0,
        until_ts="t",
    )


def _load(store: Store, events: list[dict]) -> None:
    store.commit_chunk(_meta(len(events)), "\n".join(json.dumps(e) for e in events))
    project_events(store)


def test_rate_none_on_zero_denominator():
    assert Rate(0, 0).value is None
    assert Rate(3, 0).value is None
    assert Rate(1, 4).value == 0.25


def test_hand_calculated_workflow_kpis(tmp_path):
    store = Store(tmp_path / "m.sqlite")
    _load(
        store,
        [
            {
                "type": "lead",
                "event_id": 1,
                "workflow_id": "nw_renewal",
                "campaign_id": "c1",
                "lead_id": "l1",
                "person_id": "p1",
                "features": {"lang": "hi"},
                "ts": "2027-01-04T10:00:00",
            },
            {
                "type": "lead",
                "event_id": 2,
                "workflow_id": "nw_renewal",
                "campaign_id": "c1",
                "lead_id": "l2",
                "person_id": "p2",
                "features": {"lang": "hi"},
                "ts": "2027-01-04T10:00:00",
            },
            {
                "type": "call",
                "event_id": 3,
                "workflow_id": "nw_renewal",
                "campaign_id": "c1",
                "lead_id": "l1",
                "person_id": "p1",
                "attempt_no": 1,
                "cohort_id": "default",
                "variant": {"caller_id": "D1", "voice": "V1", "welcome_message": "W1", "prompt_version": "P1"},
                "connected": True,
                "engaged": True,
                "disposition": "concluded_interested",
                "ts": "2027-01-04T11:00:00",
                "qa": {"hallucination_flag": False, "flow_adherence": 0.9},
            },
            {
                "type": "call",
                "event_id": 4,
                "workflow_id": "nw_renewal",
                "campaign_id": "c1",
                "lead_id": "l1",
                "person_id": "p1",
                "attempt_no": 2,
                "cohort_id": "default",
                "variant": {"caller_id": "D2", "voice": "V1", "welcome_message": "W1", "prompt_version": "P1"},
                "connected": True,
                "engaged": False,
                "disposition": "short_hangup",
                "ts": "2027-01-05T11:00:00",
                "qa": {"hallucination_flag": True, "flow_adherence": 0.7},
            },
            {
                "type": "call",
                "event_id": 5,
                "workflow_id": "nw_renewal",
                "campaign_id": "c1",
                "lead_id": "l2",
                "person_id": "p2",
                "attempt_no": 1,
                "cohort_id": "default",
                "variant": {"caller_id": "D1", "voice": "V1", "welcome_message": "W1", "prompt_version": "P1"},
                "connected": False,
                "engaged": False,
                "disposition": "no_answer",
                "ts": "2027-01-05T12:00:00",
            },
        ],
    )
    kpi = compute_workflow_kpis(store, "nw_renewal")
    assert kpi.dials == 3
    assert kpi.connected == 2
    assert kpi.engaged == 1
    assert kpi.pickup == 2 / 3
    assert kpi.engagement == 0.5
    assert kpi.uploaded_leads == 2
    # l1 concluded once despite two attempts
    assert kpi.concluded_leads == 1
    assert kpi.conclusion_rate == 0.5
    assert kpi.halluc_flags == 1
    assert kpi.connected_with_qa == 2
    assert kpi.halluc_rate == 0.5

    d1 = compute_caller_id_kpis(store, "nw_renewal", "D1")
    assert d1.dials == 2
    assert d1.connected == 1
    assert d1.pickup == 0.5
    store.close()


def test_engagement_none_when_no_connects(tmp_path):
    store = Store(tmp_path / "m.sqlite")
    _load(
        store,
        [
            {
                "type": "call",
                "event_id": 1,
                "workflow_id": "nw_kyc",
                "campaign_id": "c1",
                "lead_id": "l1",
                "person_id": "p1",
                "attempt_no": 1,
                "cohort_id": "default",
                "variant": {"caller_id": "D1"},
                "connected": False,
                "engaged": False,
                "disposition": "no_answer",
                "ts": "2027-01-04T11:00:00",
            }
        ],
    )
    kpi = compute_workflow_kpis(store, "nw_kyc")
    assert kpi.pickup == 0.0
    assert kpi.engagement is None
    assert kpi.conclusion_rate is None  # no uploaded leads row
    store.close()
