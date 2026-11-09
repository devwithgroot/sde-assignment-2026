"""Dashboard is read-only and distinguishes missing vs real data."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from controller.api.models import ChunkMeta, Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.audit import audit
from controller.services.change_builder import build_winner_proposal
from controller.services.detector import Signal, detect_workflow_issues
from controller.services.experiments import ExperimentArmState, ExperimentState, persist_experiment, run_workflow_experiments
from controller.services.incidents import open_or_refresh_incident
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


def _seed(db: Path, *, with_phase6: bool = False) -> None:
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
    if with_phase6:
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
    if with_phase6:
        run_workflow_experiments(store, _workflow(), _config(), min_sample=50)
        open_or_refresh_incident(
            store,
            Signal(
                fingerprint="nw|pickup_drift|dash",
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
                evidence={},
                sim_ts="t",
            ),
        )
        exp = ExperimentState(
            experiment_id="nw_renewal|default|welcome_message",
            workflow_id="nw_renewal",
            tenant_id="northwind",
            cohort_id="default",
            dimension="welcome_message",
            primary_kpi="engagement",
            guardrail_kpi="pickup",
            guardrail_max_drop=0.02,
            status="winner",
            control_variant="W1",
            planned_allocation={"W1": 0.45, "W2": 0.55},
            min_sample_per_arm=50,
            alpha=0.05,
            min_effect=0.02,
            created_at="t0",
            updated_at="t1",
            decision="winner",
            decision_reason="W2 wins",
            winner_variant="W2",
            promotion_status="ready_to_propose_observe_only",
            evidence={},
            arms=[
                ExperimentArmState(
                    experiment_id="nw_renewal|default|welcome_message",
                    variant="W1",
                    is_control=True,
                    planned_weight=0.45,
                    dials=60,
                    connected=60,
                    engaged=20,
                    primary_rate=0.33,
                    guardrail_rate=1.0,
                    sample_n=60,
                    sample_ok=True,
                ),
                ExperimentArmState(
                    experiment_id="nw_renewal|default|welcome_message",
                    variant="W2",
                    is_control=False,
                    planned_weight=0.55,
                    dials=60,
                    connected=60,
                    engaged=40,
                    primary_rate=0.66,
                    guardrail_rate=1.0,
                    sample_n=60,
                    sample_ok=True,
                ),
            ],
        )
        persist_experiment(store, exp)
        draft = build_winner_proposal(store, _workflow(), _config(), exp)
        assert draft is not None
        draft.status = "submitted"
        draft.approval_id = "apr_dash"
        draft.route = "approval"
        store.upsert_proposal(draft)
        from controller.services.governance import ControllerApproval

        store.upsert_controller_approval(
            ControllerApproval(
                approval_id="apr_dash",
                proposal_id=draft.proposal_id,
                workflow_id="nw_renewal",
                status="pending",
                base_version=1,
                changed_dims=["welcome_message"],
                updated_at="t",
                expires_at="t_exp",
            )
        )
        audit(
            store,
            kind="proposal_built",
            summary="dashboard seed proposal",
            workflow_id="nw_renewal",
            related_proposal_id=draft.proposal_id,
            idempotency_parts=["dash", draft.proposal_id],
        )
    store.close()


def test_missing_db_is_error_not_fake_success(tmp_path):
    snap = load_snapshot(tmp_path / "missing.sqlite")
    assert snap.ok is False
    assert snap.errors
    html = render_html(snap)
    assert "database not found" in html
    assert "not implemented" in html


def test_snapshot_phase5_without_fabricated_governance(tmp_path):
    db = tmp_path / "dash.sqlite"
    _seed(db, with_phase6=False)
    snap = load_snapshot(db)
    assert snap.ok is True
    assert snap.workflow_kpis
    assert snap.incidents_available is True
    assert snap.proposals_available is True
    assert snap.incidents == []
    assert snap.proposals == []
    assert "public_scenario_grading" in {u["key"] for u in snap.unimplemented}
    html = render_html(snap)
    assert "Incidents" in html
    assert "No incident rows" in html or "empty" in html.lower()
    assert "LLM suggestion" in html
    assert "Fairness" in html


def test_snapshot_shows_real_phase6_rows(tmp_path):
    db = tmp_path / "dash6.sqlite"
    _seed(db, with_phase6=True)
    snap = load_snapshot(db)
    assert snap.ok is True
    assert snap.incidents
    assert snap.proposals
    assert snap.controller_approvals
    assert snap.audit_decisions
    html = render_html(snap)
    assert snap.incidents[0]["incident_id"] in html
    assert snap.proposals[0]["proposal_id"] in html
    assert "apr_dash" in html
    assert "Audit decisions" in html
    assert snap.llm_attempts_available is True
    assert snap.validation_available is True


def test_http_endpoints_are_get_only(tmp_path):
    db = tmp_path / "dash.sqlite"
    _seed(db, with_phase6=True)
    app = create_app(str(db), sim_url=None)
    client = TestClient(app)
    assert client.get("/health").json()["readonly"] is True
    snap = client.get("/api/snapshot").json()
    assert snap["ok"] is True
    assert snap["incidents_available"] is True
    assert snap["proposals"]
    page = client.get("/")
    assert page.status_code == 200
    assert "Read-only" in page.text
    paths = {route.path for route in app.routes}
    assert "/approvals" not in paths
    assert "/sim/advance" not in paths
