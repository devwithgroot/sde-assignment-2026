"""Promoted winners survive replanning; decay rolls back to the pre-promotion config."""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx

from controller.api.client import SimulatorClient
from controller.api.models import ChunkMeta, Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.change_builder import build_winner_proposal
from controller.services.decay import detect_promoted_decay
from controller.services.experiments import persist_experiment, run_workflow_experiments
from controller.services.governance import (
    apply_approved,
    promote_experiment_winners,
    propose_decay_rollbacks,
    reconcile_approvals,
    save_snapshot,
    submit_proposal,
)
from controller.services.joiner import project_events


def _workflow(*, version: int = 1) -> Workflow:
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
            "current_version": version,
        }
    )


def _v1_config() -> WorkflowConfig:
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


def _clear_facts(store: Store) -> None:
    # Wipe projected facts and ingest checkpoints so a fresh chunk can be committed.
    for table in ("events", "calls", "leads", "campaigns", "cohort_assignments", "chunks"):
        store.conn.execute(f"DELETE FROM {table}")
    store.conn.commit()
    # New seeded event_ids restart at 1; without resetting the cursor, project_events skips them.
    store.set_projection_cursor(0)


class _SimState:
    """Shared mutable world for lifecycle mocks (config + approvals)."""

    def __init__(self, config: WorkflowConfig):
        self.live = config.model_dump()
        self.approvals: dict[str, dict[str, Any]] = {}
        self.posts: list[str] = []

    def client(self) -> SimulatorClient:
        state = self

        def handler(request: httpx.Request) -> httpx.Response:
            state.posts.append(f"{request.method} {request.url.path}")
            if request.method == "GET" and request.url.path == "/workflows/nw_renewal/config":
                return httpx.Response(200, json=copy.deepcopy(state.live))
            if request.method == "PUT":
                raise AssertionError("auto-apply not expected; approval path required")
            if request.method == "POST" and request.url.path == "/approvals":
                body = json.loads(request.content.decode())
                aid = f"apr_{len(state.approvals) + 1:05d}"
                dims = body.get("diff", {}).get("changed_dims") or ["welcome_message"]
                state.approvals[aid] = {
                    "approval_id": aid,
                    "proposal_id": body["proposal_id"],
                    "workflow_id": "nw_renewal",
                    "status": "pending",
                    "base_version": body["base_version"],
                    "changed_dims": dims,
                    "created_at": "t0",
                    "expires_at": "t_exp",
                    "apply_by": None,
                    "decided_at": None,
                    "reasons": [],
                    "_proposed_config": body["proposed_config"],
                }
                return httpx.Response(
                    200, json={k: v for k, v in state.approvals[aid].items() if not k.startswith("_")}
                )
            if request.method == "GET" and request.url.path.startswith("/approvals/"):
                aid = request.url.path.rsplit("/", 1)[-1]
                return httpx.Response(
                    200, json={k: v for k, v in state.approvals[aid].items() if not k.startswith("_")}
                )
            if request.method == "GET" and request.url.path == "/approvals":
                return httpx.Response(
                    200,
                    json=[
                        {k: v for k, v in a.items() if not k.startswith("_")}
                        for a in state.approvals.values()
                    ],
                )
            if request.method == "POST" and request.url.path == "/workflows/nw_renewal/apply":
                body = json.loads(request.content.decode())
                assert set(body.keys()) == {"approval_id"}
                ap = state.approvals[body["approval_id"]]
                if ap["status"] != "approved":
                    return httpx.Response(
                        409, json={"error": "not_approved", "detail": {"status": ap["status"]}}
                    )
                new_version = int(state.live["version"]) + 1
                state.live = {
                    "workflow_id": "nw_renewal",
                    "version": new_version,
                    "actor": "controller_approved",
                    "applied_at": "t_apply",
                    "config": ap["_proposed_config"],
                }
                ap["status"] = "applied"
                ap["applied_version"] = new_version
                return httpx.Response(
                    200,
                    json={
                        "workflow_id": "nw_renewal",
                        "version": new_version,
                        "changed_dims": ap["changed_dims"],
                    },
                )
            raise AssertionError(f"{request.method} {request.url.path}")

        return SimulatorClient(
            "http://sim.test",
            transport=httpx.MockTransport(handler),
            sleeper=lambda _s: None,
            max_retries=0,
        )

    def approve(self, approval_id: str) -> None:
        ap = self.approvals[approval_id]
        ap["status"] = "approved"
        ap["decided_at"] = "t_dec"
        ap["apply_by"] = "t_by"


def _promote_winner_via_approval(store: Store, sim: _SimState, experiment) -> Any:
    wf = _workflow(version=int(sim.live["version"]))
    cfg = WorkflowConfig.model_validate(sim.live)
    draft = build_winner_proposal(store, wf, cfg, experiment)
    assert draft is not None
    client = sim.client()
    submitted = submit_proposal(client, store, wf, cfg, draft)
    assert submitted.status == "submitted"
    assert submitted.route == "approval"
    assert submitted.approval_id
    sim.approve(submitted.approval_id)
    reconcile_approvals(client, store)
    assert store.get_proposal(draft.proposal_id)["status"] == "approved"
    applied = apply_approved(client, store)
    assert applied and applied[0]["version"] == int(sim.live["version"])
    assert store.get_proposal(draft.proposal_id)["status"] == "applied"
    return draft


def test_replanning_preserves_promoted_status(tmp_path):
    store = Store(tmp_path / "promoted.sqlite")
    wf, cfg = _workflow(), _v1_config()
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
    wf, cfg = _workflow(), _v1_config()
    _seed_winner(store)
    first = run_workflow_experiments(store, wf, cfg, min_sample=100)
    store.update_experiment_promotion(first[0].experiment_id, "promotion_rejected")

    again = run_workflow_experiments(store, wf, cfg, min_sample=100)
    assert again[0].status == "winner"
    assert again[0].promotion_status == "ready_to_propose_observe_only"
    store.close()


def test_winner_apply_keeps_pre_promotion_rollback_snapshot(tmp_path):
    """post_apply must not overwrite rollback_candidate with the newly applied config."""
    store = Store(tmp_path / "snap.sqlite")
    v1 = _v1_config()
    _seed_winner(store)
    experiments = run_workflow_experiments(store, _workflow(), v1, min_sample=100)
    assert experiments[0].status == "winner"
    persist_experiment(store, experiments[0])

    sim = _SimState(v1)
    draft = _promote_winner_via_approval(store, sim, experiments[0])

    snap = store.pre_promotion_rollback_snapshot("nw_renewal", experiments[0].experiment_id)
    assert snap is not None
    assert snap["version"] == 1
    assert snap["source"] in {"pre_apply", "rollback_candidate"}
    prior = json.loads(snap["config_json"])
    assert prior["allocations"]["default"]["welcome_message"] == {"W1": 1.0}

    post = store.snapshot_for_proposal(draft.proposal_id, "post_apply")
    assert post is not None
    assert post["version"] == 2
    assert json.loads(post["config_json"])["allocations"]["default"]["welcome_message"]["W2"] == 0.55

    # Unrelated later snapshot must not become the decay rollback target for this experiment.
    save_snapshot(
        store,
        workflow_id="nw_renewal",
        version=2,
        config=sim.live["config"],
        source="post_apply",
        related_proposal_id="prop_unrelated",
    )
    snap2 = store.pre_promotion_rollback_snapshot("nw_renewal", experiments[0].experiment_id)
    assert json.loads(snap2["config_json"])["allocations"]["default"]["welcome_message"] == {"W1": 1.0}
    store.close()


def test_decay_lifecycle_rolls_back_to_v1_and_blocks_repromote(tmp_path):
    """v1 → winner promote/apply v2 → decay → rollback to v1; no re-promote; no duplicate rollback."""
    store = Store(tmp_path / "life.sqlite")
    v1 = _v1_config()
    _seed_winner(store)
    planned = run_workflow_experiments(store, _workflow(), v1, min_sample=100)
    assert planned and planned[0].status == "winner"
    experiment_id = planned[0].experiment_id
    persist_experiment(store, planned[0])

    sim = _SimState(v1)
    _promote_winner_via_approval(store, sim, planned[0])
    assert sim.live["version"] == 2
    assert store.get_experiment(experiment_id)["promotion_status"] == "promoted"

    # Replace traffic with a decaying window; sticky promotion survives replan.
    _clear_facts(store)
    _seed_decay_window(store)
    v2 = WorkflowConfig.model_validate(sim.live)
    replanned = run_workflow_experiments(store, _workflow(version=2), v2, min_sample=100)
    assert store.get_experiment(experiment_id)["promotion_status"] == "promoted"
    assert replanned[0].promotion_status == "promoted"

    hits = detect_promoted_decay(store)
    assert len(hits) == 1
    assert hits[0]["experiment_id"] == experiment_id
    assert store.get_experiment(experiment_id)["promotion_status"] == "decay_detected"

    sim.posts.clear()
    client = sim.client()
    proposed = propose_decay_rollbacks(client, store, [_workflow(version=2)], [v2], hits)
    assert len(proposed) == 1
    rb = proposed[0]
    assert rb.source == "decay_rollback"
    assert rb.source_ref == experiment_id
    assert rb.route == "approval"
    assert rb.status == "submitted"
    assert rb.proposed_config["allocations"]["default"]["welcome_message"] == {"W1": 1.0}
    assert rb.changed_dims
    assert "welcome_message" in rb.changed_dims
    assert "PUT" not in " ".join(sim.posts)

    # Next experiment cycle must not re-promote the decayed winner.
    again = run_workflow_experiments(store, _workflow(version=2), v2, min_sample=100)
    assert store.get_experiment(experiment_id)["promotion_status"] == "decay_detected"
    sim.posts.clear()
    assert promote_experiment_winners(client, store, [_workflow(version=2)], [v2], again) == []
    assert not any(p == "POST /approvals" for p in sim.posts)

    # No duplicate rollback while one is already pending.
    assert propose_decay_rollbacks(client, store, [_workflow(version=2)], [v2], hits) == []
    assert len([p for p in store.list_proposals() if p["source"] == "decay_rollback"]) == 1

    # Apply rollback via the normal approval path.
    assert rb.approval_id
    sim.approve(rb.approval_id)
    reconcile_approvals(client, store)
    rb_applied = apply_approved(client, store)
    assert rb_applied and rb_applied[0]["version"] == 3
    assert store.get_experiment(experiment_id)["promotion_status"] == "rolled_back"
    assert sim.live["config"]["allocations"]["default"]["welcome_message"] == {"W1": 1.0}

    # After rollback, still no automatic re-promotion of the same evidence.
    v3 = WorkflowConfig.model_validate(sim.live)
    after = run_workflow_experiments(store, _workflow(version=3), v3, min_sample=100)
    assert store.get_experiment(experiment_id)["promotion_status"] == "rolled_back"
    assert promote_experiment_winners(client, store, [_workflow(version=3)], [v3], after) == []
    assert propose_decay_rollbacks(client, store, [_workflow(version=3)], [v3], hits) == []
    store.close()
