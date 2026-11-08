"""SQLite schema, idempotent inserts, and transaction rollback."""

from __future__ import annotations

import json

import pytest

from controller.api.models import ChunkMeta
from controller.persistence.repositories import IngestIntegrityError, Store


def _meta(chunk: int, kind: str, events: int, first: int = 1, last: int = 1) -> ChunkMeta:
    return ChunkMeta(
        chunk=chunk,
        events=events,
        first_event_id=first,
        kind=kind,
        last_event_id=last,
        path="runs/do-not-open.jsonl",
        until_minute=0,
        until_ts="2027-01-04T00:00:00+05:30",
    )


def _line(event_id: int, event_type: str = "call", **extra: object) -> str:
    body = {
        "type": event_type,
        "event_id": event_id,
        "ts": "2027-01-04T00:00:00+05:30",
        "workflow_id": "nw_renewal",
        "lead_id": "nw_renewal:l00000001",
        "person_id": "nw_renewal:p0000001",
    }
    body.update(extra)
    return json.dumps(body)


def test_schema_initializes(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    names = {row["name"] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"schema_meta", "events", "chunks", "checkpoints", "ingest_issues"} <= names
    store.close()


def test_insert_and_duplicate_event_id(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    body = "\n".join([_line(1), _line(1), _line(2)])
    commit = store.commit_chunk(_meta(0, "history", 3, last=2), body)
    assert commit.inserted == 2
    assert commit.duplicates == 1
    assert store.event_count() == 2
    assert store.distinct_event_ids() == 2
    store.close()


def test_duplicate_chunk_does_not_insert_again(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    meta = _meta(0, "history", 1)
    first = store.commit_chunk(meta, _line(7))
    second = store.commit_chunk(meta, _line(7))
    assert first.inserted == 1
    assert second.already_committed is True
    assert second.inserted == 0
    assert store.event_count() == 1
    store.close()


def test_malformed_line_is_recorded(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    body = "\n".join([_line(1), "not-json", _line(2)])
    commit = store.commit_chunk(_meta(3, "events", 3, first=1, last=2), body)
    assert commit.inserted == 2
    assert commit.issues == 1
    assert store.issue_count() == 1
    reason = store.conn.execute("SELECT reason FROM ingest_issues").fetchone()["reason"]
    assert reason.startswith("malformed_json")
    store.close()


def test_mismatch_rolls_back(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    with pytest.raises(IngestIntegrityError):
        store.commit_chunk(_meta(0, "history", 5), _line(1))
    assert store.event_count() == 0
    assert store.issue_count() == 0
    assert store.checkpoint("history") is None
    assert store.is_committed(0) is False
    store.close()


def test_explicit_transaction_rollback(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.conn.execute(
                """INSERT INTO events (
                       event_id, chunk, kind, type, raw_json
                   ) VALUES (1, 0, 'history', 'call', '{}')"""
            )
            raise RuntimeError("boom")
    assert store.event_count() == 0
    store.close()


def test_empty_chunk(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    commit = store.commit_chunk(_meta(4, "events", 0, first=0, last=0), "")
    assert commit.inserted == 0
    assert store.is_committed(4) is True
    checkpoint = store.checkpoint("events")
    assert checkpoint is not None
    assert checkpoint.last_chunk == 4
    store.close()


def test_checkpoint_survives_reopen(tmp_path):
    path = tmp_path / "c.sqlite"
    store = Store(path)
    store.commit_chunk(_meta(0, "history", 1), _line(9, tenant_id="northwind_life"))
    store.close()
    again = Store(path)
    checkpoint = again.checkpoint("history")
    assert checkpoint is not None
    assert checkpoint.last_chunk == 0
    assert checkpoint.last_event_id == 9
    row = again.conn.execute("SELECT tenant_id, workflow_id FROM events WHERE event_id = 9").fetchone()
    assert row["tenant_id"] == "northwind_life"
    assert row["workflow_id"] == "nw_renewal"
    again.close()
