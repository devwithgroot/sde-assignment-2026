"""Ingestion uses chunk metadata and bodies, and does not advance the clock."""

from __future__ import annotations

import json

import httpx
import pytest

from controller.api.client import SimulatorClient, SimulatorError
from controller.api.models import ChunkMeta
from controller.persistence.repositories import Store
from controller.services.ingestion import ingest_available, ingest_kind


def _meta(chunk: int, kind: str, events: int, first: int, last: int) -> ChunkMeta:
    return ChunkMeta(
        chunk=chunk,
        events=events,
        first_event_id=first,
        kind=kind,
        last_event_id=last,
        path="runs/secret.jsonl",
        until_minute=0,
        until_ts="2027-01-04T00:00:00+05:30",
    )


def _line(event_id: int, event_type: str = "lead") -> str:
    return json.dumps(
        {
            "type": event_type,
            "event_id": event_id,
            "ts": "2027-01-04T08:00:00+05:30",
            "workflow_id": "ks_cancel",
            "campaign_id": "ks_cancel:c0001",
        }
    )


class FakeSim:
    def __init__(self, history: dict[int, tuple[ChunkMeta, str]], live: dict[int, tuple[ChunkMeta, str]]):
        self.history = history
        self.live = live
        self.calls: list[tuple] = []
        self.fail_chunk: int | None = None

    def chunks(self, kind: str, *, after: int = -1) -> list[ChunkMeta]:
        self.calls.append(("chunks", kind, after))
        source = self.history if kind == "history" else self.live
        return [meta for meta, _body in source.values() if meta.chunk > after]

    def chunk_text(self, n: int) -> str:
        self.calls.append(("chunk_text", n))
        if n == self.fail_chunk:
            raise SimulatorError(status=500, code="unavailable", detail="down", endpoint=f"/events/chunks/{n}")
        for source in (self.history, self.live):
            if n in source:
                return source[n][1]
        raise AssertionError(n)


def test_history_then_live_and_restart(tmp_path):
    history_body = "\n".join(_line(i, "campaign_created") for i in (1, 2))
    live_body = _line(3, "call")
    sim = FakeSim(
        {0: (_meta(0, "history", 2, 1, 2), history_body)},
        {1: (_meta(1, "events", 1, 3, 3), live_body)},
    )
    path = tmp_path / "c.sqlite"
    store = Store(path)
    stats = ingest_available(sim, store)
    assert [item.kind for item in stats] == ["history", "events"]
    assert stats[0].inserted == 2
    assert stats[1].inserted == 1
    assert store.event_count("history") == 2
    assert store.event_count("events") == 1
    assert store.distinct_event_ids() == 3
    store.close()

    reopened = Store(path)
    again = FakeSim(sim.history, sim.live)
    second = ingest_available(again, reopened)
    assert second[0].chunks_fetched == 0
    assert second[1].chunks_fetched == 0
    assert reopened.event_count() == 3
    assert all(call[0] != "advance" for call in again.calls)
    assert not any(isinstance(call[1], str) and "secret" in call[1] for call in again.calls)
    reopened.close()


def test_empty_chunk_list(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    stats = ingest_kind(FakeSim({}, {}), store, "history")
    assert stats.chunks_fetched == 0
    assert store.event_count() == 0
    store.close()


def test_api_failure_does_not_advance_checkpoint(tmp_path):
    sim = FakeSim({0: (_meta(0, "history", 1, 1, 1), _line(1))}, {})
    sim.fail_chunk = 0
    store = Store(tmp_path / "c.sqlite")
    with pytest.raises(SimulatorError):
        ingest_kind(sim, store, "history")
    assert store.checkpoint("history") is None
    assert store.event_count() == 0
    store.close()


def test_replay_same_chunk_text_is_idempotent(tmp_path):
    body = _line(4, "config_changed")
    meta = _meta(2, "events", 1, 4, 4)
    sim = FakeSim({}, {2: (meta, body)})
    store = Store(tmp_path / "c.sqlite")
    ingest_kind(sim, store, "events")
    # Checkpoint still points at this chunk, so a client that ignores `after` must not duplicate.
    sim.chunks = lambda kind, after=-1: [meta]  # type: ignore[method-assign]
    ingest_kind(sim, store, "events")
    assert store.event_count() == 1
    store.close()


def test_chunk_text_is_a_get():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/events/chunks/0"
        return httpx.Response(200, text='{"type":"call","event_id":1}\n')

    with SimulatorClient("http://sim.test", transport=httpx.MockTransport(handler), sleeper=lambda _s: None) as client:
        assert '"event_id": 1' in client.chunk_text(0) or '"event_id":1' in client.chunk_text(0)
