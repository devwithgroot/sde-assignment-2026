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


def test_client_has_no_mutation_methods():
    names = set(dir(SimulatorClient))
    for forbidden in ("advance", "request_approval", "apply", "auto_apply", "put_config"):
        assert forbidden not in names
