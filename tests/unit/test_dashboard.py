"""Dashboard is read-only and distinguishes missing vs real data."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from controller.api.models import ChunkMeta, Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.detector import detect_workflow_issues
from controller.services.experiments import run_workflow_experiments
from controller.services.joiner import project_events
from controller.services.metrics import compute_and_store_workflow_views
from dashboard.app import create_app
from dashboard.queries import load_snapshot
from dashboard.render import render_html


def _workflow() -> Workflow:
    return Workflow.model_validate(
        {
            "workflow_id": "nw_renewal",
            "tenant_id": "northwind",
            "languages": ["hi"],
            "dialling_window": [10, 18],
            "dials_per_hour": 100,
            "cadence": [0, 1],
            "min_gap_minutes": 60,
            "daily_batch": {},
            "mid_flight": "allowed",
            "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
            "options": {
                "welcome_message": ["W1", "W2"],
                "voice": ["V1", "V2"],
                "caller_id": ["D1"],
                "prompt_version": ["P1"],
            },
            "prompt_catalog": {"P1": "ok"},
            "governance": {
                "auto_apply": {"enabled": False, "allowed_dims": [], "max_shift": 0},
                "approver_hours": [10, 18],
                "approver_latency_minutes": [30, 120],
                "proposal_ttl_hours": 24,
                "apply_window_hours": 2,
                "max_requests_per_24h": 5,
                "base_reject_rate": 0.1,
                "min_validation_pass_rate": 0.9,
            },
            "current_version": 1,
        }
    )


def _config() -> WorkflowConfig:
    return WorkflowConfig.model_validate(
        {
            "workflow_id": "nw_renewal",
            "version": 1,
            "actor": "system",
            "applied_at": None,
            "config": {
                "prompt_version": "P1",
                "cohorts": [],
                "allocations": {"default": {"welcome_message": {"W1": 0.5, "W2": 0.5}}},
                "schedule": {},
            },
        }
    )


def _seed(db: Path, *, with_experiments: bool = False) -> None:
    store = Store(db)
    events = [
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
            "type": "call",
            "event_id": 2,
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
    ]
    if with_experiments:
        # Enough connected dials on two welcome arms for an underpowered-or-decided experiment row.
        eid = 3
        for i in range(120):
            lead = f"a{i}"
            events.append(
                {
                    "type": "lead",
                    "event_id": eid,
                    "workflow_id": "nw_renewal",
                    "campaign_id": "c1",
                    "lead_id": lead,
                    "person_id": f"p-{lead}",
                    "features": {"lang": "hi"},
                    "ts": "2027-01-04T10:00:00",
                }
            )
            eid += 1
            events.append(
                {
                    "type": "call",
                    "event_id": eid,
                    "workflow_id": "nw_renewal",
                    "campaign_id": "c1",
                    "lead_id": lead,
                    "person_id": f"p-{lead}",
                    "attempt_no": 1,
                    "cohort_id": "default",
                    "variant": {
                        "caller_id": "D1",
                        "voice": "V1",
                        "welcome_message": "W1" if i < 60 else "W2",
                        "prompt_version": "P1",
                    },
                    "connected": True,
                    "engaged": i % 3 == 0 if i < 60 else i % 2 == 0,
                    "disposition": "concluded_interested",
                    "ts": "2027-01-04T11:00:00",
                    "qa": {"hallucination_flag": False, "flow_adherence": 0.9},
                }
            )
            eid += 1
    store.commit_chunk(
        ChunkMeta(
            chunk=0,
            events=len(events),
            first_event_id=1,
            kind="history",
            last_event_id=events[-1]["event_id"],
            path="x",
            until_minute=0,
            until_ts="t",
        ),
        "\n".join(json.dumps(e) for e in events),
    )
    project_events(store)
    compute_and_store_workflow_views(store, ["nw_renewal"])
    detect_workflow_issues(store, "nw_renewal")
    if with_experiments:
        run_workflow_experiments(store, _workflow(), _config(), min_sample=50)
    store.close()


def test_missing_db_is_error_not_fake_success(tmp_path):
    snap = load_snapshot(tmp_path / "missing.sqlite")
    assert snap.ok is False
    assert snap.errors
    html = render_html(snap)
    assert "database not found" in html
    assert "not implemented" in html


def test_snapshot_shows_kpis_and_experiment_unavailable_or_empty(tmp_path):
    db = tmp_path / "dash.sqlite"
    _seed(db, with_experiments=False)
    snap = load_snapshot(db)
    assert snap.ok is True
    assert snap.counts["calls"] == 1
    assert snap.workflow_kpis
    assert snap.workflow_kpis[0]["workflow_id"] == "nw_renewal"
    # Schema creates experiments table on Store init, so available=True but may be empty /
    # underpowered-only depending on whether evaluation ran.
    assert snap.experiments_available is True
    assert "proposals" in {u["key"] for u in snap.unimplemented}
    assert "experiments" not in {u["key"] for u in snap.unimplemented}
    html = render_html(snap)
    assert "nw_renewal" in html
    assert "Experiments" in html


def test_snapshot_shows_real_experiment_rows(tmp_path):
    db = tmp_path / "dash_exp.sqlite"
    _seed(db, with_experiments=True)
    snap = load_snapshot(db)
    assert snap.ok is True
    assert snap.experiments_available is True
    assert snap.experiments, "expected evaluated experiment rows, not fabricated placeholders"
    assert snap.experiment_arms
    exp = snap.experiments[0]
    assert exp["workflow_id"] == "nw_renewal"
    assert exp["dimension"] in {"welcome_message", "voice"}
    assert exp["min_sample_per_arm"] >= 50
    html = render_html(snap)
    assert exp["experiment_id"] in html
    assert "Variant arms" in html
    assert "Observational analysis only" in html


def test_http_endpoints_are_get_only(tmp_path):
    db = tmp_path / "dash.sqlite"
    _seed(db, with_experiments=True)
    app = create_app(str(db), sim_url=None)
    client = TestClient(app)
    assert client.get("/health").json()["readonly"] is True
    snap = client.get("/api/snapshot").json()
    assert snap["ok"] is True
    assert snap["experiments_available"] is True
    assert snap["experiments"]
    assert "proposals" in {u["key"] for u in snap["unimplemented"]}
    page = client.get("/")
    assert page.status_code == 200
    assert "Read-only" in page.text
    paths = {route.path for route in app.routes}
    assert "/approvals" not in paths
    assert "/sim/advance" not in paths
