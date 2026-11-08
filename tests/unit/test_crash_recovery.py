"""Crash/restart idempotency for ingest and pending approvals (mocked)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from controller.api.client import SimulatorClient
from controller.api.models import ChunkMeta, Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.crash_recovery import run_crash_recovery
from controller.services.change_builder import ProposalDraft
from controller.services.governance import ControllerApproval, persist_proposal
from controller.services.ingestion import ingest_available


def _wf() -> Workflow:
    return Workflow.model_validate(
        {
            "workflow_id": "nw_renewal",
            "tenant_id": "northwind",
            "languages": ["hi"],
            "dialling_window": [10, 18],
            "dials_per_hour": 100,
            "cadence": [0],
            "min_gap_minutes": 60,
            "daily_batch": {},
            "mid_flight": "allowed",
            "objective": {"primary": "engagement_rate", "guardrail": {}},
            "options": {
                "welcome_message": ["W1", "W2"],
                "voice": ["V1"],
                "caller_id": ["D1"],
                "prompt_version": ["P1"],
            },
            "prompt_catalog": {"P1": "x"},
            "governance": {
                "auto_apply": {"enabled": False, "allowed_dims": [], "max_shift": 0},
                "approver_hours": [10, 18],
                "approver_latency_minutes": [30, 60],
                "proposal_ttl_hours": 24,
                "apply_window_hours": 2,
                "max_requests_per_24h": 5,
                "base_reject_rate": 0.0,
                "min_validation_pass_rate": 0.9,
            },
            "current_version": 1,
        }
    )


def _cfg() -> WorkflowConfig:
    return WorkflowConfig.model_validate(
        {
            "workflow_id": "nw_renewal",
            "version": 1,
            "actor": "system",
            "applied_at": None,
            "config": {
                "prompt_version": "P1",
                "cohorts": [],
                "allocations": {"default": {"welcome_message": {"W1": 1.0}}},
                "schedule": {},
            },
        }
    )


def _client(handler) -> SimulatorClient:
    return SimulatorClient(
        "http://sim.test", transport=httpx.MockTransport(handler), sleeper=lambda _s: None, max_retries=0
    )


def test_ingest_crash_replay_is_idempotent(tmp_path):
    store = Store(tmp_path / "cr.sqlite")
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
        }
    ]
    body = "\n".join(json.dumps(e) for e in events)
    meta = ChunkMeta(
        chunk=0,
        events=1,
        first_event_id=1,
        kind="history",
        last_event_id=1,
        path="x",
        until_minute=0,
        until_ts="t",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/events/chunks" and request.url.params.get("kind") == "history":
            return httpx.Response(200, json=[meta.model_dump()])
        if request.url.path == "/events/chunks" and request.url.params.get("kind") == "events":
            return httpx.Response(200, json=[])
        if request.url.path == "/events/chunks/0":
            return httpx.Response(200, text=body)
        if request.url.path == "/sim/state":
            return httpx.Response(
                200,
                json={
                    "scenario": "x",
                    "sim_minute": 0,
                    "ts": "t",
                    "end_minute": 100,
                    "finished": False,
                    "tick_minutes": 15,
                    "credits_remaining": 100,
                    "chunks": 1,
                    "last_event_id": 1,
                },
            )
        if request.url.path == "/workflows":
            return httpx.Response(200, json=[_wf().model_dump()])
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json=_cfg().model_dump())
        if request.url.path == "/approvals":
            return httpx.Response(200, json=[])
        raise AssertionError(request.url.path)

    client = _client(handler)
    first = ingest_available(client, store)
    assert first[0].inserted == 1
    # Simulate crash-restart: ingest again — duplicates, no double insert.
    second = ingest_available(client, store)
    assert second[0].inserted == 0
    assert store.event_count() == 1

    recovery = run_crash_recovery(client, store, [_wf()], [_cfg()], mutate=False)
    assert recovery.notes
    assert store.event_count() == 1


def test_pending_approval_reconcile_no_duplicate_apply(tmp_path):
    store = Store(tmp_path / "cr2.sqlite")
    draft = ProposalDraft(
        proposal_id="prop_cr",
        workflow_id="nw_renewal",
        base_version=1,
        status="submitted",
        source="experiment_winner",
        source_ref="exp1",
        changed_dims=["welcome_message"],
        proposed_config=_cfg().config | {"allocations": {"default": {"welcome_message": {"W2": 1.0}}}},
        change_summary="test",
        diff={"changed_dims": ["welcome_message"]},
        dry_run={"limitation": "x", "impact_score": 0.4},
        impact_score=0.4,
        affected_leads=1,
        rationale="test",
        evidence={},
        approval_id="apr_cr",
        route="approval",
        created_at="t",
        updated_at="t",
        submitted_at="t",
    )
    persist_proposal(store, draft)
    store.upsert_controller_approval(
        ControllerApproval(
            approval_id="apr_cr",
            proposal_id="prop_cr",
            workflow_id="nw_renewal",
            status="approved",
            base_version=1,
            changed_dims=["welcome_message"],
            updated_at="t",
        )
    )
    apply_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/events/chunks":
            return httpx.Response(200, json=[])
        if request.url.path == "/sim/state":
            return httpx.Response(
                200,
                json={
                    "scenario": "x",
                    "sim_minute": 0,
                    "ts": "t",
                    "end_minute": 100,
                    "finished": False,
                    "tick_minutes": 15,
                    "credits_remaining": 100,
                    "chunks": 0,
                    "last_event_id": 0,
                },
            )
        if request.url.path == "/approvals/apr_cr":
            return httpx.Response(
                200,
                json={
                    "approval_id": "apr_cr",
                    "proposal_id": "prop_cr",
                    "workflow_id": "nw_renewal",
                    "status": "approved",
                    "base_version": 1,
                    "changed_dims": ["welcome_message"],
                },
            )
        if request.url.path == "/workflows/nw_renewal/apply":
            apply_calls["n"] += 1
            body = json.loads(request.content.decode())
            assert body == {"approval_id": "apr_cr"}
            return httpx.Response(
                200, json={"workflow_id": "nw_renewal", "version": 2, "changed_dims": ["welcome_message"]}
            )
        if request.url.path == "/workflows/nw_renewal/config":
            return httpx.Response(200, json=_cfg().model_dump())
        if request.url.path == "/approvals":
            return httpx.Response(200, json=[])
        if request.url.path == "/llm/suggest-fixes":
            return httpx.Response(200, json={"suggestions": [], "credits_remaining": 100})
        raise AssertionError(f"{request.method} {request.url.path}")

    client = _client(handler)
    r1 = run_crash_recovery(client, store, [_wf()], [_cfg()], mutate=True)
    assert r1.applied == 1
    assert apply_calls["n"] == 1
    # Second recovery: approval already applied locally after first pass — should not re-apply.
    # Reconcile will see approved still if mock always returns approved; mark local as applied already.
    assert store.get_controller_approval("apr_cr")["status"] == "applied"
    r2 = run_crash_recovery(client, store, [_wf()], [_cfg()], mutate=True)
    assert r2.applied == 0
    assert apply_calls["n"] == 1
    assert store.get_proposal("prop_cr")["status"] == "applied"
