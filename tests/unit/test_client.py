"""HTTP client: retries, 4xx, bad JSON, and read-only surface."""

from __future__ import annotations

import httpx
import pytest

from controller.api.client import SimulatorClient, SimulatorError


def _client(handler, retries: int = 2) -> SimulatorClient:
    return SimulatorClient(
        "http://sim.test",
        max_retries=retries,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )


def test_get_parses_health():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        assert client.health().ok is True


def test_retries_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"error": "unavailable", "detail": "no"})
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        assert client.health().ok is True
    assert calls["n"] == 3


def test_respects_retry_after():
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if len(slept) == 0:
            return httpx.Response(503, headers={"Retry-After": "2"}, json={"error": "unavailable", "detail": "x"})
        return httpx.Response(200, json={"ok": True})

    client = SimulatorClient(
        "http://sim.test",
        max_retries=1,
        transport=httpx.MockTransport(handler),
        sleeper=slept.append,
    )
    with client:
        client.health()
    assert slept == [2.0]


def test_does_not_retry_4xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(409, json={"error": "finished", "detail": "scenario has ended"})

    with _client(handler) as client:
        with pytest.raises(SimulatorError) as caught:
            client.state()
    assert calls["n"] == 1
    assert caught.value.status == 409
    assert caught.value.code == "finished"
    assert caught.value.endpoint == "/sim/state"


def test_bad_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"})

    with _client(handler, retries=0) as client:
        with pytest.raises(SimulatorError) as caught:
            client.health()
    assert caught.value.code == "bad_json"


def test_auto_apply_does_not_retry_5xx_or_transport_errors():
    """Default max_retries is 2. The config PUT is still a single attempt."""

    def five_hundred(request: httpx.Request) -> httpx.Response:
        five_hundred.calls += 1
        assert request.method == "PUT"
        return httpx.Response(500, json={"error": "upstream_error", "detail": "applied"})

    five_hundred.calls = 0
    with _client(five_hundred) as client:
        with pytest.raises(SimulatorError) as caught:
            client.auto_apply("wf", {"proposal_id": "p", "base_version": 1, "proposed_config": {}})
    assert five_hundred.calls == 1
    assert caught.value.status == 500

    def lost(request: httpx.Request) -> httpx.Response:
        lost.calls += 1
        raise httpx.RemoteProtocolError("connection lost")

    lost.calls = 0
    with _client(lost) as client:
        with pytest.raises(SimulatorError) as caught:
            client.auto_apply("wf", {"proposal_id": "p", "base_version": 1, "proposed_config": {}})
    assert lost.calls == 1
    assert caught.value.status == 0
    assert caught.value.code == "transport_error"


def test_transport_error_after_retries():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    with _client(handler, retries=1) as client:
        with pytest.raises(SimulatorError) as caught:
            client.health()
    assert calls["n"] == 2
    assert caught.value.status == 0
    assert caught.value.code == "transport_error"


def test_client_has_advance_and_governance_methods():
    """Phase 9 continuous loop uses advance; Phase 6+ governance writes remain available."""
    names = set(dir(SimulatorClient))
    assert "advance" in names
    assert "request_approval" in names
    assert "apply_approval" in names
    assert "auto_apply" in names


def test_advance_treats_409_finished_as_terminal():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sim/advance"
        return httpx.Response(409, json={"error": "finished", "detail": "scenario has ended"})

    with _client(handler) as client:
        payload = client.advance(60)
    assert payload["finished"] is True
    assert payload["new_chunks"] == []
