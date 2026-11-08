"""Phase 7 prompt incident loop: suggest → validate → propose (mocked HTTP)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from controller.api.client import SimulatorClient
from controller.api.models import SimState, Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.detector import Signal
from controller.services.incidents import open_or_refresh_incident
from controller.services.prompt_incident import (
    run_prompt_incident_cycle,
    select_validated_candidate,
    should_start_attempt,
    ValidationRecord,
)


def _workflow(**overrides) -> Workflow:
    base = {
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
            "welcome_message": ["W1"],
            "voice": ["V1"],
            "caller_id": ["D1"],
            "prompt_version": ["P1", "P2", "P3"],
        },
        "prompt_catalog": {"P1": "current", "P2": "alt", "P3": "alt2"},
        "governance": {
            "auto_apply": {"enabled": False, "allowed_dims": [], "max_shift": 0},
            "approver_hours": [10, 18],
            "approver_latency_minutes": [30, 120],
            "proposal_ttl_hours": 24,
            "apply_window_hours": 2,
            "max_requests_per_24h": 5,
            "base_reject_rate": 0.0,
            "min_validation_pass_rate": 0.90,
        },
        "current_version": 1,
    }
    base.update(overrides)
    return Workflow.model_validate(base)


def _config(prompt: str = "P1") -> WorkflowConfig:
    return WorkflowConfig.model_validate(
        {
            "workflow_id": "nw_renewal",
            "version": 1,
            "actor": "system",
            "applied_at": None,
            "config": {
                "prompt_version": prompt,
                "cohorts": [],
                "allocations": {"default": {"welcome_message": {"W1": 1.0}}},
                "schedule": {},
            },
        }
    )


def _state(credits: float = 50.0) -> SimState:
    return SimState.model_validate(
        {
            "scenario": "public_02_incidents",
            "sim_minute": 0,
            "ts": "2027-01-04T00:00:00+05:30",
            "end_minute": 14400,
            "finished": False,
            "tick_minutes": 15,
            "credits_remaining": credits,
            "chunks": 1,
            "last_event_id": 1,
        }
    )


def _open_halluc(store: Store):
    return open_or_refresh_incident(
        store,
        Signal(
            fingerprint="nw|hallucination_spike|t",
            workflow_id="nw_renewal",
            signal_type="hallucination_spike",
            kpi="halluc_rate",
            dimension="workflow",
            cohort_id=None,
            caller_id=None,
            severity="high",
            baseline_value=0.01,
            recent_value=0.08,
            baseline_n=200,
            recent_n=200,
            threshold=0.02,
            message="hallucination flag rate rose",
            evidence={},
            sim_ts="t",
        ),
    )


def _client(handler) -> SimulatorClient:
    return SimulatorClient(
        "http://sim.test", transport=httpx.MockTransport(handler), sleeper=lambda _s: None, max_retries=0
    )


def test_select_validated_candidate_deterministic():
    records = [
        ValidationRecord(
            record_id="a",
            attempt_id="x",
            incident_id="i",
            workflow_id="w",
            prompt_version="P3",
            n=200,
            status="completed",
            pass_rate=0.95,
            validation_run_id="val_1",
            meets_threshold=True,
            created_at="t",
            updated_at="t",
        ),
        ValidationRecord(
            record_id="b",
            attempt_id="x",
            incident_id="i",
            workflow_id="w",
            prompt_version="P2",
            n=200,
            status="completed",
            pass_rate=0.95,
            validation_run_id="val_2",
            meets_threshold=True,
            created_at="t",
            updated_at="t",
        ),
        ValidationRecord(
            record_id="c",
            attempt_id="x",
            incident_id="i",
            workflow_id="w",
            prompt_version="P1",
            n=200,
            status="completed",
            pass_rate=0.80,
            validation_run_id="val_3",
            meets_threshold=False,
            created_at="t",
            updated_at="t",
        ),
    ]
    chosen = select_validated_candidate(records, min_pass_rate=0.90)
    assert chosen is not None
    assert chosen.prompt_version == "P2"  # tie on rate → lex smaller


def test_no_mutate_skips_paid_calls(tmp_path):
    store = Store(tmp_path / "p7.sqlite")
    _open_halluc(store)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        raise AssertionError("no network when mutate=False")

    report = run_prompt_incident_cycle(
        _client(handler),
        store,
        [_workflow()],
        [_config()],
        _state(),
        mutate=False,
    )
    assert report.eligible == 1
    assert report.skipped_no_mutate == 1
    assert report.attempted == 0
    assert calls == []


def test_happy_path_suggest_validate_propose(tmp_path):
    store = Store(tmp_path / "p7b.sqlite")
    incident = _open_halluc(store)
    approvals: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/llm/suggest-fixes":
            body = json.loads(request.content.decode())
            assert body["workflow_id"] == "nw_renewal"
            assert body["problem"]
            return httpx.Response(
                200,
                json={
                    "suggestions": [
                        {"prompt_version": "P2", "summary": "maybe safer"},
                        {"prompt_version": "P9", "summary": "unsupported — ignored"},
                        {"prompt_version": "P3", "summary": "also candidate"},
                    ],
                    "credits_remaining": 47,
                },
            )
        if request.method == "POST" and path == "/validation/runs":
            body = json.loads(request.content.decode())
            pv = body["prompt_version"]
            rate = 0.97 if pv == "P2" else 0.91
            return httpx.Response(
                200,
                json={
                    "validation_run_id": f"val_{pv}",
                    "workflow_id": "nw_renewal",
                    "prompt_version": pv,
                    "n": body["n"],
                    "failures": 1,
                    "pass_rate": rate,
                    "credits_remaining": 45 if pv == "P2" else 43,
                },
            )
        if request.method == "GET" and path == "/workflows/nw_renewal/config":
            return httpx.Response(200, json=_config().model_dump())
        if request.method == "POST" and path == "/approvals":
            body = json.loads(request.content.decode())
            assert body["evidence"]["validation_run_id"] == "val_P2"
            assert body["proposed_config"]["prompt_version"] == "P2"
            assert body["diff"]["changed_dims"] == ["prompt_version"]
            aid = "apr_p7"
            approvals[aid] = {
                "approval_id": aid,
                "proposal_id": body["proposal_id"],
                "workflow_id": "nw_renewal",
                "status": "pending",
                "base_version": 1,
                "changed_dims": ["prompt_version"],
            }
            return httpx.Response(200, json=approvals[aid])
        raise AssertionError(f"{request.method} {path}")

    report = run_prompt_incident_cycle(
        _client(handler),
        store,
        [_workflow()],
        [_config()],
        _state(50),
        mutate=True,
        validation_n=200,
    )
    assert report.attempted == 1
    assert report.suggested == 1
    assert report.validated >= 1
    assert report.proposed == 1
    assert report.submitted == 1
    attempts = store.list_llm_attempts(incident.incident_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "submitted"
    assert attempts[0]["selected_prompt_version"] == "P2"
    assert attempts[0]["selected_validation_run_id"] == "val_P2"
    vals = store.list_validation_records(attempt_id=attempts[0]["attempt_id"])
    assert any(v["prompt_version"] == "P2" and v["meets_threshold"] == 1 for v in vals)
    props = store.list_proposals(workflow_id="nw_renewal")
    assert props and props[0]["source"] == "prompt_incident"
    evidence = json.loads(props[0]["evidence_json"])
    assert evidence["validation_run_id"] == "val_P2"

    # Duplicate prevention on second cycle
    report2 = run_prompt_incident_cycle(
        _client(handler),
        store,
        [_workflow()],
        [_config()],
        _state(50),
        mutate=True,
    )
    assert report2.attempted == 0
    assert report2.skipped_duplicate >= 1
    assert len(store.list_llm_attempts(incident.incident_id)) == 1


def test_validation_below_threshold_no_proposal(tmp_path):
    store = Store(tmp_path / "p7c.sqlite")
    _open_halluc(store)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/llm/suggest-fixes":
            return httpx.Response(
                200,
                json={"suggestions": [{"prompt_version": "P2", "summary": "x"}], "credits_remaining": 47},
            )
        if request.url.path == "/validation/runs":
            return httpx.Response(
                200,
                json={
                    "validation_run_id": "val_low",
                    "workflow_id": "nw_renewal",
                    "prompt_version": "P2",
                    "n": 200,
                    "failures": 50,
                    "pass_rate": 0.50,
                    "credits_remaining": 45,
                },
            )
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json=_config().model_dump())
        raise AssertionError(request.url.path)

    report = run_prompt_incident_cycle(
        _client(handler), store, [_workflow()], [_config()], _state(), mutate=True
    )
    assert report.proposed == 0
    assert report.submitted == 0
    attempt = store.list_llm_attempts()[0]
    assert attempt["status"] == "no_safe_candidate"
    assert store.list_proposals() == []


def test_credit_exhaustion_on_suggest(tmp_path):
    store = Store(tmp_path / "p7d.sqlite")
    incident = _open_halluc(store)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/llm/suggest-fixes":
            return httpx.Response(402, json={"error": "credits_exhausted", "detail": "no credits"})
        raise AssertionError(request.url.path)

    report = run_prompt_incident_cycle(
        _client(handler), store, [_workflow()], [_config()], _state(10), mutate=True
    )
    assert report.failures == 1
    assert store.list_llm_attempts(incident.incident_id)[0]["status"] == "credit_exhausted"
    # Retryable after credits return
    ok, reason = should_start_attempt(store, incident)
    assert ok is True
    assert "retry" in reason


def test_invalid_validation_payload(tmp_path):
    store = Store(tmp_path / "p7e.sqlite")
    _open_halluc(store)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/llm/suggest-fixes":
            return httpx.Response(
                200,
                json={"suggestions": [{"prompt_version": "P2", "summary": "x"}], "credits_remaining": 40},
            )
        if request.url.path == "/validation/runs":
            return httpx.Response(200, json={"oops": True, "credits_remaining": 38})
        raise AssertionError(request.url.path)

    report = run_prompt_incident_cycle(
        _client(handler), store, [_workflow()], [_config()], _state(), mutate=True
    )
    assert report.proposed == 0
    vals = store.list_validation_records()
    assert vals and vals[0]["status"] == "invalid_response"


def test_malformed_suggestions_ignored(tmp_path):
    store = Store(tmp_path / "p7f.sqlite")
    _open_halluc(store)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/llm/suggest-fixes":
            return httpx.Response(
                200,
                json={"suggestions": "not-a-list", "credits_remaining": 40},
            )
        raise AssertionError(request.url.path)

    report = run_prompt_incident_cycle(
        _client(handler), store, [_workflow()], [_config()], _state(), mutate=True
    )
    assert report.proposed == 0
    assert store.list_llm_attempts()[0]["status"] == "no_safe_candidate"
