"""Incident lifecycle from detection signals."""

from __future__ import annotations

from controller.persistence.repositories import Store
from controller.services.detector import Signal
from controller.services.incidents import (
    close_incident,
    mark_incident_mitigating,
    open_or_refresh_incident,
    resolve_incident,
    sync_incidents_from_signals,
)


def _signal(**overrides) -> Signal:
    base = dict(
        fingerprint="nw|pickup_drift|x",
        workflow_id="nw_renewal",
        signal_type="pickup_drift",
        kpi="pickup",
        dimension="workflow",
        cohort_id=None,
        caller_id=None,
        severity="high",
        baseline_value=0.3,
        recent_value=0.2,
        baseline_n=200,
        recent_n=200,
        threshold=0.05,
        message="pickup dropped",
        evidence={"window": "recent"},
        sim_ts="2027-01-06T12:00:00",
    )
    base.update(overrides)
    return Signal(**base)


def test_open_incident_idempotent(tmp_path):
    store = Store(tmp_path / "i.sqlite")
    a = open_or_refresh_incident(store, _signal())
    b = open_or_refresh_incident(store, _signal())
    assert a.incident_id == b.incident_id
    assert a.status == "detected"
    rows = store.list_incidents()
    assert len(rows) == 1
    audits = store.list_audit(workflow_id="nw_renewal")
    assert any(r["kind"] == "incident_opened" for r in audits)


def test_mitigate_resolve_close(tmp_path):
    store = Store(tmp_path / "i2.sqlite")
    incident = open_or_refresh_incident(store, _signal())
    mark_incident_mitigating(store, incident.incident_id, "prop_x")
    row = store.get_incident(incident.incident_id)
    assert row["status"] == "mitigating"
    assert row["related_proposal_id"] == "prop_x"
    resolve_incident(store, incident.incident_id, "applied fix")
    close_incident(store, incident.incident_id)
    row = store.get_incident(incident.incident_id)
    assert row["status"] == "closed"
    assert row["closed_at"]


def test_sync_multiple_signals(tmp_path):
    store = Store(tmp_path / "i3.sqlite")
    incidents = sync_incidents_from_signals(
        store,
        [
            _signal(fingerprint="a", signal_type="pickup_drift"),
            _signal(fingerprint="b", signal_type="engagement_drift", severity="medium"),
        ],
        tenant_by_workflow={"nw_renewal": "northwind"},
    )
    assert len(incidents) == 2
    assert all(i.tenant_id == "northwind" for i in incidents)
