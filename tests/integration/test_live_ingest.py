"""Ingest the running public simulator without moving its clock.

Skipped only when nothing is listening on 127.0.0.1:8700.
"""

from __future__ import annotations

import json

import httpx
import pytest

from controller.api.client import SimulatorClient
from controller.persistence.repositories import Store
from controller.services.ingestion import ingest_available


@pytest.fixture()
def live_client():
    try:
        client = SimulatorClient(
            "http://127.0.0.1:8700",
            timeout_seconds=30,
            chunk_timeout_seconds=180,
            max_retries=1,
        )
        client.health()
    except (httpx.TransportError, OSError):
        pytest.skip("simulator is not running at http://127.0.0.1:8700")
    yield client
    client.close()


def _configs(client: SimulatorClient) -> list[tuple]:
    rows = []
    for workflow in client.workflows():
        config = client.workflow_config(workflow.workflow_id)
        rows.append(
            (
                workflow.workflow_id,
                config.version,
                config.actor,
                json.dumps(config.config, sort_keys=True),
            )
        )
    return rows


def test_live_history_ingest_is_idempotent_and_does_not_advance(tmp_path, live_client):
    before_state = live_client.state()
    before_configs = _configs(live_client)
    path = tmp_path / "live.sqlite"
    store = Store(path)
    first = ingest_available(live_client, store)
    history, live = first
    assert history.chunks_committed >= 1
    assert history.issues == 0
    assert store.event_count() == store.distinct_event_ids()
    assert store.event_count("history") == history.inserted
    declared = live_client.chunks("history")
    assert store.event_count("history") == sum(chunk.events for chunk in declared)
    checkpoint = store.checkpoint("history")
    assert checkpoint is not None
    assert checkpoint.last_chunk == max(chunk.chunk for chunk in declared)
    store.close()

    restarted = Store(path)
    second = ingest_available(live_client, restarted)
    assert second[0].inserted == 0
    assert second[1].inserted == 0
    assert restarted.event_count() == history.inserted + live.inserted
    restarted.close()

    after_state = live_client.state()
    assert after_state.sim_minute == before_state.sim_minute
    assert after_state.scenario == before_state.scenario
    assert _configs(live_client) == before_configs
