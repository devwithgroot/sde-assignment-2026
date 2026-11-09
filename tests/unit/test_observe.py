"""The observe pass reads and does not call a write endpoint."""

from __future__ import annotations

import httpx

from controller.api.client import SimulatorClient
from controller.main import main, observe
from controller.settings import Settings


WORKFLOW = {
    "workflow_id": "nw_renewal",
    "tenant_id": "northwind_life",
    "use_case": "renewal",
    "languages": ["hi"],
    "dialling_window": [9, 21],
    "dials_per_hour": 300,
    "cadence": [2, 3, 3],
    "min_gap_minutes": 120,
    "daily_batch": {"size": 1100, "at_hour": 8},
    "mid_flight": "allowed",
    "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
    "options": {
        "welcome_message": ["W1"],
        "voice": ["V1"],
        "caller_id": ["D1"],
        "prompt_version": ["P1"],
    },
    "prompt_catalog": {"P1": "current"},
    "governance": {
        "auto_apply": {"enabled": True, "allowed_dims": ["schedule", "caller_id"], "max_shift": 0.5},
        "approver_hours": [10, 19],
        "approver_latency_minutes": [30, 240],
        "proposal_ttl_hours": 24,
        "apply_window_hours": 2,
        "max_requests_per_24h": 3,
        "base_reject_rate": 0.1,
        "min_validation_pass_rate": 0.97,
    },
    "current_version": 1,
}


def test_observe_issues_only_gets():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/sim/state":
            return httpx.Response(
                200,
                json={
                    "scenario": "public_02_incidents",
                    "sim_minute": 0,
                    "ts": "2027-01-04T00:00:00+05:30",
                    "end_minute": 14400,
                    "finished": False,
                    "tick_minutes": 15,
                    "credits_remaining": 200,
                    "chunks": 1,
                    "last_event_id": 3,
                },
            )
        if request.url.path == "/workflows":
            return httpx.Response(200, json=[WORKFLOW])
        if request.url.path == "/workflows/nw_renewal/config":
            return httpx.Response(
                200,
                json={
                    "workflow_id": "nw_renewal",
                    "version": 1,
                    "actor": "default",
                    "applied_at": None,
                    "config": {"prompt_version": "P1", "cohorts": []},
                },
            )
        if request.url.path == "/events/chunks":
            kind = request.url.params["kind"]
            return httpx.Response(
                200,
                json=[
                    {
                        "chunk": 0,
                        "events": 4,
                        "first_event_id": 1,
                        "kind": kind,
                        "last_event_id": 4,
                        "path": "runs/secret",
                        "until_minute": 0,
                        "until_ts": "2027-01-04T00:00:00+05:30",
                    }
                ]
                if kind == "history"
                else [],
            )
        raise AssertionError(request.url.path)

    with SimulatorClient("http://sim.test", transport=httpx.MockTransport(handler), sleeper=lambda _s: None) as client:
        report = observe(client)
    assert [method for method, _path in seen] == ["GET"] * len(seen)
    assert report.state.scenario == "public_02_incidents"
    assert report.configs[0].version == 1
    assert len(report.history_chunks) == 1
    assert report.event_chunks == []
    assert "/sim/advance" not in {path for _method, path in seen}
    assert "/approvals" not in {path for _method, path in seen}


def test_main_missing_url(monkeypatch):
    monkeypatch.delenv("SIM_URL", raising=False)
    assert main([]) == 2


def test_settings_strips_slash():
    settings = Settings.from_env("http://127.0.0.1:8700/")
    assert settings.sim_url == "http://127.0.0.1:8700"
