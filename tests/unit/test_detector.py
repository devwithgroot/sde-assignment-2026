"""Detection needs population samples. One call is never enough."""

from __future__ import annotations

import json

from controller.api.models import ChunkMeta
from controller.domain.cohorts import MIN_COHORT_SAMPLE
from controller.persistence.repositories import Store
from controller.services.detector import (
    CALLER_ID_DROP_VS_WORKFLOW,
    PICKUP_DROP,
    detect_workflow_issues,
)
from controller.services.joiner import project_events


def _meta(chunk: int, n: int) -> ChunkMeta:
    return ChunkMeta(
        chunk=chunk,
        events=n,
        first_event_id=1,
        kind="history",
        last_event_id=n,
        path="x",
        until_minute=0,
        until_ts="t",
    )


def _call(event_id: int, *, day: str, caller_id: str, connected: bool, engaged: bool = False, halluc: bool | None = None) -> dict:
    qa = None
    if connected:
        qa = {"hallucination_flag": bool(halluc), "flow_adherence": 0.9}
    return {
        "type": "call",
        "event_id": event_id,
        "workflow_id": "ks_cancel",
        "campaign_id": "c1",
        "lead_id": f"l{event_id}",
        "person_id": f"p{event_id}",
        "attempt_no": 1,
        "cohort_id": "default",
        "variant": {"caller_id": caller_id, "voice": "V1", "welcome_message": "W1", "prompt_version": "P1"},
        "connected": connected,
        "engaged": engaged,
        "disposition": "concluded_interested" if engaged else ("short_hangup" if connected else "no_answer"),
        "ts": f"{day}T12:00:00",
        "qa": qa,
    }


def test_no_signal_under_small_sample(tmp_path):
    store = Store(tmp_path / "d.sqlite")
    events = [_call(i, day="2027-01-04", caller_id="D1", connected=False) for i in range(1, 20)]
    store.commit_chunk(_meta(0, len(events)), "\n".join(json.dumps(e) for e in events))
    project_events(store)
    assert detect_workflow_issues(store, "ks_cancel") == []
    store.close()


def test_caller_id_health_on_known_gap(tmp_path):
    store = Store(tmp_path / "d.sqlite")
    events = []
    eid = 1
    # Healthy D1: ~50% pickup
    for _ in range(MIN_COHORT_SAMPLE):
        events.append(_call(eid, day="2027-01-04", caller_id="D1", connected=(eid % 2 == 0)))
        eid += 1
    # Bad D2: ~10% pickup, enough sample
    for i in range(MIN_COHORT_SAMPLE):
        events.append(_call(eid, day="2027-01-04", caller_id="D2", connected=(i < 10)))
        eid += 1
    store.commit_chunk(_meta(0, len(events)), "\n".join(json.dumps(e) for e in events))
    project_events(store)
    signals = detect_workflow_issues(store, "ks_cancel")
    kinds = {(s.signal_type, s.caller_id) for s in signals}
    assert ("caller_id_health", "D2") in kinds
    bad = next(s for s in signals if s.caller_id == "D2")
    assert bad.baseline_value - bad.recent_value >= CALLER_ID_DROP_VS_WORKFLOW
    # Rerun is deduped by fingerprint
    again = detect_workflow_issues(store, "ks_cancel")
    assert again == []
    assert len(store.list_signals("ks_cancel")) >= 1
    store.close()


def test_pickup_drift_baseline_vs_recent(tmp_path):
    store = Store(tmp_path / "d.sqlite")
    events = []
    eid = 1
    # Baseline days: strong pickup
    for day in ("2027-01-01", "2027-01-02", "2027-01-03"):
        for _ in range(MIN_COHORT_SAMPLE):
            events.append(_call(eid, day=day, caller_id="D1", connected=True))
            eid += 1
    # Recent days: weak pickup
    for day in ("2027-01-04", "2027-01-05"):
        for i in range(MIN_COHORT_SAMPLE):
            events.append(_call(eid, day=day, caller_id="D1", connected=(i < 20)))
            eid += 1
    store.commit_chunk(_meta(0, len(events)), "\n".join(json.dumps(e) for e in events))
    project_events(store)
    signals = detect_workflow_issues(store, "ks_cancel")
    pickup = [s for s in signals if s.signal_type == "pickup_drift"]
    assert len(pickup) == 1
    assert pickup[0].baseline_value - pickup[0].recent_value >= PICKUP_DROP
    store.close()


def test_separate_kpi_signals(tmp_path):
    store = Store(tmp_path / "d.sqlite")
    events = []
    eid = 1
    for day in ("2027-01-01", "2027-01-02", "2027-01-03"):
        for _ in range(MIN_COHORT_SAMPLE):
            events.append(_call(eid, day=day, caller_id="D1", connected=True, engaged=True))
            eid += 1
    for day in ("2027-01-04", "2027-01-05"):
        for _ in range(MIN_COHORT_SAMPLE):
            # Pickup stays high; engagement collapses.
            events.append(_call(eid, day=day, caller_id="D1", connected=True, engaged=False))
            eid += 1
    store.commit_chunk(_meta(0, len(events)), "\n".join(json.dumps(e) for e in events))
    project_events(store)
    signals = detect_workflow_issues(store, "ks_cancel")
    types = {s.signal_type for s in signals}
    assert "engagement_drift" in types
    assert "pickup_drift" not in types
    store.close()
