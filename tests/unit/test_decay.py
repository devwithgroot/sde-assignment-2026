"""Promoted winners survive replanning, then decay can roll back once."""

from __future__ import annotations

import json
from typing import Any

import httpx

from controller.api.client import SimulatorClient
from controller.api.models import ChunkMeta, Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.decay import detect_promoted_decay
from controller.services.experiments import run_workflow_experiments
from controller.services.governance import ConfigSnapshot, propose_decay_rollbacks
from controller.services.joiner import project_events


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
                "base_reject_rate": 0.0,
                "min_validation_pass_rate": 0.9,
            },
            "current_version": 2,
        }
    )


def _control_config() -> WorkflowConfig:
    """W1 still holds the allocation, so a stronger W2 is the challenger."""
    return WorkflowConfig.model_validate(
        {
            "workflow_id": "nw_renewal",
            "version": 1,
            "actor": "business_default",
            "applied_at": None,
            "config": {
                "prompt_version": "P1",
                "cohorts": [],
                "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0}}},
                "schedule": {},
            },
        }
    )


def _live_config() -> WorkflowConfig:
    return WorkflowConfig.model_validate(
        {
            "workflow_id": "nw_renewal",
            "version": 2,
            "actor": "controller_approved",
            "applied_at": "2027-01-05T12:00:00+05:30",
            "config": {
                "prompt_version": "P1",
                "cohorts": [],
                "allocations": {
                    "default": {"welcome_message": {"W1": 0.45, "W2": 0.55}, "voice": {"V1": 1.0}}
                },
                "schedule": {},
            },
        }
    )


def _prior_config() -> dict[str, Any]:
    return {
        "prompt_version": "P1",
        "cohorts": [],
        "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0}}},
        "schedule": {},
    }


def _meta(n: int) -> ChunkMeta:
    return ChunkMeta(
        chunk=0,
        events=n,
        first_event_id=1,
        kind="history",
        last_event_id=n,
        path="x",
        until_minute=0,
        until_ts="t",
    )


def _seed_winner(store: Store) -> None:
    events: list[dict] = []
    eid = 1
    for welcome, rate in (("W1", 0.30), ("W2", 0.70)):
        for i in range(150):
            lead = f"{welcome}-{i}"
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
                        "welcome_message": welcome,
                        "prompt_version": "P1",
                    },
                    "connected": True,
                    "engaged": (i / 150) < rate,
                    "disposition": "concluded_interested" if (i / 150) < rate else "short_hangup",
                    "ts": "2027-01-04T11:00:00",
                }
            )
            eid += 1
    store.commit_chunk(_meta(len(events)), "\n".join(json.dumps(e) for e in events))
    project_events(store)


def _seed_decay_window(store: Store) -> None:
    """Baseline days engage ~0.85; the last two days engage ~0.30."""
    events: list[dict] = []
    eid = 1
    for day, rate in (
        ("2027-01-04", 0.85),
        ("2027-01-05", 0.85),
        ("2027-01-06", 0.30),
        ("2027-01-07", 0.30),
    ):
        for i in range(120):
            lead = f"{day}-{i}"
            welcome = "W1" if i % 2 == 0 else "W2"
            engaged = (i / 120) < rate
            events.append(
                {
                    "type": "lead",
                    "event_id": eid,
                    "workflow_id": "nw_renewal",
                    "campaign_id": "c1",
                    "lead_id": lead,
                    "person_id": f"p-{lead}",
                    "features": {"lang": "hi"},
                    "ts": f"{day}T10:00:00",
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
                        "welcome_message": welcome,
                        "prompt_version": "P1",
                    },
                    "connected": True,
                    "engaged": engaged,
                    "disposition": "concluded_interested" if engaged else "short_hangup",
                    "ts": f"{day}T11:00:00",
                }
            )
            eid += 1
    store.commit_chunk(_meta(len(events)), "\n".join(json.dumps(e) for e in events))
    project_events(store)


def _client(config: WorkflowConfig, posts: list[str]) -> SimulatorClient:
    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(f"{request.method} {request.url.path}")
        if request.method == "PUT":
            raise AssertionError("rollback must not auto-apply when policy disables it")
        if request.method == "GET" and request.url.path == "/workflows/nw_renewal/config":
            return httpx.Response(200, json=config.model_dump())
        if request.method == "POST" and request.url.path == "/approvals":
            body = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "approval_id": "apr_rollback",
                    "proposal_id": body["proposal_id"],
                    "workflow_id": "nw_renewal",
                    "status": "pending",
                    "base_version": body["base_version"],
                    "changed_dims": body.get("diff", {}).get("changed_dims", []),
                },
            )
        raise AssertionError(f"{request.method} {request.url.path}")

    return SimulatorClient("http://sim.test", transport=httpx.MockTransport(handler), sleeper=lambda _s: None, max_retries=0)


def test_replanning_preserves_promoted_status(tmp_path):
    store = Store(tmp_path / "promoted.sqlite")
    wf, cfg = _workflow(), _control_config()
    _seed_winner(store)
    first = run_workflow_experiments(store, wf, cfg, min_sample=100)
    assert first and first[0].status == "winner"
    assert first[0].promotion_status == "ready_to_propose_observe_only"
    store.update_experiment_promotion(first[0].experiment_id, "promoted")

    again = run_workflow_experiments(store, wf, cfg, min_sample=100)
    assert again[0].promotion_status == "promoted"
    assert again[0].status == "winner"
    row = store.get_experiment(first[0].experiment_id)
    assert row is not None
    assert row["promotion_status"] == "promoted"
    store.close()


def test_rejected_promotion_can_be_proposed_again(tmp_path):
    store = Store(tmp_path / "rejected.sqlite")
    wf, cfg = _workflow(), _control_config()
    _seed_winner(store)
    first = run_workflow_experiments(store, wf, cfg, min_sample=100)
    store.update_experiment_promotion(first[0].experiment_id, "promotion_rejected")

    again = run_workflow_experiments(store, wf, cfg, min_sample=100)
    assert again[0].status == "winner"
    assert again[0].promotion_status == "ready_to_propose_observe_only"
    store.close()


def test_decay_after_replan_proposes_one_rollback(tmp_path):
    store = Store(tmp_path / "decay.sqlite")
    wf, cfg = _workflow(), _live_config()
    _seed_decay_window(store)
    planned = run_workflow_experiments(store, wf, cfg, min_sample=100)
    assert planned
    experiment_id = planned[0].experiment_id
    store.update_experiment_promotion(experiment_id, "promoted")

    replanned = run_workflow_experiments(store, wf, cfg, min_sample=100)
    assert replanned[0].promotion_status == "promoted"
    assert store.get_experiment(experiment_id)["promotion_status"] == "promoted"

    store.upsert_config_snapshot(
        ConfigSnapshot(
            snapshot_id="snap_nw_renewal_1_rollback_candidate",
            workflow_id="nw_renewal",
            version=1,
            config=_prior_config(),
            source="rollback_candidate",
            created_at="2027-01-05T00:00:00+00:00",
        )
    )

    hits = detect_promoted_decay(store)
    assert len(hits) == 1
    assert hits[0]["workflow_id"] == "nw_renewal"
    assert hits[0]["experiment_id"] == experiment_id
    assert store.get_experiment(experiment_id)["promotion_status"] == "decay_detected"

    posts: list[str] = []
    client = _client(cfg, posts)
    proposed = propose_decay_rollbacks(client, store, [wf], [cfg], hits)
    assert len(proposed) == 1
    draft = proposed[0]
    assert draft.workflow_id == "nw_renewal"
    assert draft.source == "decay_rollback"
    assert draft.source_ref == experiment_id
    assert draft.route == "approval"
    assert draft.status == "submitted"
    assert draft.proposed_config["allocations"]["default"]["welcome_message"] == {"W1": 1.0}
    assert "welcome_message" in draft.changed_dims
    assert "PUT" not in " ".join(posts)

    # Same cycle again: replanning keeps decay_detected, so detection does not fire twice.
    run_workflow_experiments(store, wf, cfg, min_sample=100)
    assert store.get_experiment(experiment_id)["promotion_status"] == "decay_detected"
    assert detect_promoted_decay(store) == []
    again = propose_decay_rollbacks(client, store, [wf], [cfg], hits)
    assert again == []
    rollbacks = [p for p in store.list_proposals() if p["source"] == "decay_rollback"]
    assert len(rollbacks) == 1
    assert posts.count("POST /approvals") == 1
    store.close()
