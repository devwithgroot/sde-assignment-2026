"""Caller-id rotation from an open caller_id_health incident. Synthetic ids only."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from controller.api.client import SimulatorClient
from controller.api.models import ChunkMeta, Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.change_builder import (
    CALLER_ID_ROTATION_MAX_SHIFT,
    build_caller_id_rotation_proposal,
    total_variation,
)
from controller.services.detector import Signal
from controller.services.governance import (
    apply_approved,
    persist_proposal,
    propose_caller_id_rotations,
    reconcile_approvals,
)
from controller.services.incidents import mark_incident_mitigating, open_or_refresh_incident
from controller.services.joiner import project_events

WF = "wf_rotate"


def _meta(n: int, *, chunk: int = 0, first: int = 1) -> ChunkMeta:
    return ChunkMeta(
        chunk=chunk,
        events=n,
        first_event_id=first,
        kind="history",
        last_event_id=first + n - 1,
        path="x",
        until_minute=0,
        until_ts="t",
    )


def _workflow(**overrides: Any) -> Workflow:
    base: dict[str, Any] = {
        "workflow_id": WF,
        "tenant_id": "tenant_test",
        "languages": ["en"],
        "dialling_window": [10, 18],
        "dials_per_hour": 80,
        "cadence": [0, 1],
        "min_gap_minutes": 60,
        "daily_batch": {},
        "mid_flight": "allowed",
        "objective": {"primary": "pickup_rate", "guardrail": {"pickup_rate_max_drop": 0.05}},
        "options": {
            "welcome_message": ["W_A"],
            "voice": ["V_A", "V_B"],
            "caller_id": ["CID_BAD", "CID_OK"],
            "prompt_version": ["P_A"],
        },
        "prompt_catalog": {"P_A": "hello"},
        "governance": {
            "auto_apply": {"enabled": False, "allowed_dims": ["caller_id"], "max_shift": 0.5},
            "approver_hours": [10, 18],
            "approver_latency_minutes": [30, 120],
            "proposal_ttl_hours": 24,
            "apply_window_hours": 2,
            "max_requests_per_24h": 5,
            "base_reject_rate": 0.0,
            "min_validation_pass_rate": 0.9,
        },
        "current_version": 1,
    }
    base.update(overrides)
    return Workflow.model_validate(base)


def _config(
    *,
    version: int = 1,
    caller: dict[str, float] | None = None,
    voice: dict[str, float] | None = None,
    extra_cohort: dict[str, Any] | None = None,
    include_default_caller: bool = True,
) -> WorkflowConfig:
    default: dict[str, Any] = {
        "welcome_message": {"W_A": 1.0},
        "voice": voice or {"V_A": 1.0},
    }
    if include_default_caller:
        default["caller_id"] = caller or {"CID_BAD": 0.5, "CID_OK": 0.5}
    allocations: dict[str, Any] = {"default": default}
    if extra_cohort is not None:
        allocations["cohort_north"] = extra_cohort
    return WorkflowConfig.model_validate(
        {
            "workflow_id": WF,
            "version": version,
            "actor": "system",
            "applied_at": None,
            "config": {
                "prompt_version": "P_A",
                "cohorts": [],
                "allocations": allocations,
                "schedule": {"hour_weights": {"default": {"11": 1}}},
            },
        }
    )


def _seed(
    store: Store,
    specs: list[tuple[str, int, int, int]],
    *,
    cohort_id: str = "default",
    start_id: int = 1,
    chunk: int = 0,
) -> int:
    events = []
    eid = start_id
    for caller_id, dials, connected, engaged in specs:
        for i in range(dials):
            is_conn = i < connected
            events.append(
                {
                    "type": "call",
                    "event_id": eid,
                    "workflow_id": WF,
                    "campaign_id": "camp",
                    "lead_id": f"l{eid}",
                    "person_id": f"p{eid}",
                    "attempt_no": 1,
                    "cohort_id": cohort_id,
                    "variant": {
                        "caller_id": caller_id,
                        "voice": "V_A",
                        "welcome_message": "W_A",
                        "prompt_version": "P_A",
                    },
                    "connected": is_conn,
                    "engaged": is_conn and i < engaged,
                    "disposition": "short_hangup" if is_conn else "no_answer",
                    "ts": "2027-04-02T12:00:00",
                    "qa": {"hallucination_flag": False, "flow_adherence": 0.9} if is_conn else None,
                }
            )
            eid += 1
    store.commit_chunk(
        _meta(len(events), chunk=chunk, first=start_id),
        "\n".join(json.dumps(event) for event in events),
    )
    project_events(store)
    return eid


def _open(store: Store, caller_id: str = "CID_BAD"):
    signal = Signal(
        fingerprint=f"{WF}|caller_id_health|{caller_id}|all",
        workflow_id=WF,
        signal_type="caller_id_health",
        kpi="pickup",
        dimension="caller_id",
        cohort_id=None,
        caller_id=caller_id,
        severity="high",
        baseline_value=0.4,
        recent_value=0.1,
        baseline_n=200,
        recent_n=200,
        threshold=0.08,
        message=f"caller_id {caller_id} pickup is below the workflow",
        evidence={"caller_dials": 200},
        sim_ts=None,
    )
    return open_or_refresh_incident(store, signal, tenant_id="tenant_test")


class _Sim:
    def __init__(
        self,
        config: WorkflowConfig,
        *,
        fail_config: bool = False,
        allow_auto_apply: bool = False,
        auto_apply_http_status: int | None = None,
        lose_auto_apply_response: bool = False,
    ):
        self.live = config.model_dump()
        self.approvals: dict[str, dict[str, Any]] = {}
        self.posts: list[str] = []
        self.fail_config = fail_config
        self.allow_auto_apply = allow_auto_apply
        self.auto_apply_http_status = auto_apply_http_status
        self.lose_auto_apply_response = lose_auto_apply_response

    def client(self, *, max_retries: int | None = 0) -> SimulatorClient:
        state = self

        def handler(request: httpx.Request) -> httpx.Response:
            state.posts.append(f"{request.method} {request.url.path}")
            if request.method == "GET" and request.url.path == f"/workflows/{WF}/config":
                if state.fail_config:
                    return httpx.Response(500, json={"error": "config_unavailable", "detail": "live config read failed"})
                return httpx.Response(200, json=json.loads(json.dumps(state.live)))
            if request.method == "PUT" and request.url.path == f"/workflows/{WF}/config":
                if not state.allow_auto_apply:
                    raise AssertionError("config PUT is not used for this rotation")
                body = json.loads(request.content.decode())
                new_version = int(state.live["version"]) + 1
                state.live = {
                    "workflow_id": WF,
                    "version": new_version,
                    "actor": "auto",
                    "applied_at": "t_auto",
                    "config": body["proposed_config"],
                }
                if state.lose_auto_apply_response:
                    raise httpx.RemoteProtocolError("connection lost")
                if state.auto_apply_http_status is not None:
                    return httpx.Response(
                        state.auto_apply_http_status,
                        json={"error": "upstream_error", "detail": "applied but the response failed"},
                    )
                return httpx.Response(200, json={"workflow_id": WF, "version": new_version})
            if request.method == "PUT":
                raise AssertionError("config PUT is not used for this rotation")
            if request.method == "POST" and request.url.path == "/approvals":
                body = json.loads(request.content.decode())
                aid = f"apr_{len(state.approvals) + 1:05d}"
                state.approvals[aid] = {
                    "approval_id": aid,
                    "proposal_id": body["proposal_id"],
                    "workflow_id": WF,
                    "status": "pending",
                    "base_version": body["base_version"],
                    "changed_dims": body.get("diff", {}).get("changed_dims") or ["caller_id"],
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
            if request.method == "GET" and request.url.path == "/approvals":
                return httpx.Response(
                    200,
                    json=[
                        {k: v for k, v in row.items() if not str(k).startswith("_")}
                        for row in state.approvals.values()
                    ],
                )
            if request.method == "GET" and request.url.path.startswith("/approvals/"):
                aid = request.url.path.rsplit("/", 1)[-1]
                row = state.approvals[aid]
                return httpx.Response(200, json={k: v for k, v in row.items() if not k.startswith("_")})
            if request.method == "POST" and request.url.path == f"/workflows/{WF}/apply":
                body = json.loads(request.content.decode())
                assert set(body.keys()) == {"approval_id"}
                ap = state.approvals[body["approval_id"]]
                if ap["status"] != "approved":
                    return httpx.Response(409, json={"error": "not_approved", "detail": {"status": ap["status"]}})
                new_version = int(state.live["version"]) + 1
                state.live = {
                    "workflow_id": WF,
                    "version": new_version,
                    "actor": "controller_approved",
                    "applied_at": "t_apply",
                    "config": ap["_proposed_config"],
                }
                ap["status"] = "applied"
                ap["applied_version"] = new_version
                return httpx.Response(
                    200, json={"workflow_id": WF, "version": new_version, "changed_dims": ap["changed_dims"]}
                )
            raise AssertionError(f"{request.method} {request.url.path}")

        options: dict[str, Any] = {
            "transport": httpx.MockTransport(handler),
            "sleeper": lambda _s: None,
        }
        # None keeps SimulatorClient's default (max_retries=2). Callers that
        # pass nothing still use one attempt, matching the older rotation tests.
        if max_retries is not None:
            options["max_retries"] = max_retries
        return SimulatorClient("http://sim.test", **options)

    def approve(self, approval_id: str) -> None:
        self.approvals[approval_id]["status"] = "approved"
        self.approvals[approval_id]["decided_at"] = "t_dec"


def _skip_reasons(store: Store) -> list[str]:
    reasons = []
    for row in store.list_audit(WF, limit=50):
        if row["kind"] != "caller_id_rotation_skipped":
            continue
        details = json.loads(row["details_json"])
        reasons.append(details.get("reason"))
    return reasons


def _propose(
    store: Store,
    sim: _Sim,
    workflow: Workflow | None = None,
    *,
    max_retries: int | None = 0,
):
    wf = workflow or _workflow()
    live = WorkflowConfig.model_validate(sim.live)
    return propose_caller_id_rotations(sim.client(max_retries=max_retries), store, [wf], [live])


def _weights(config: dict[str, Any], cohort: str = "default") -> dict[str, float]:
    return config["allocations"][cohort]["caller_id"]


def test_rotation_proposal_shifts_only_caller_id_within_limit(tmp_path):
    store = Store(tmp_path / "ok.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    _open(store)
    sim = _Sim(_config())
    proposed = _propose(store, sim)
    assert len(proposed) == 1
    draft = proposed[0]
    assert draft.status == "submitted"
    assert draft.route == "approval"
    assert draft.source == "caller_id_rotation"
    assert draft.changed_dims == ["caller_id"]
    assert draft.evidence["replacement_caller_id"] == "CID_OK"
    assert draft.evidence["replacement_dials"] >= 100
    assert draft.evidence["degraded_caller_id"] == "CID_BAD"
    assert draft.dry_run["rotation_limit"] == CALLER_ID_ROTATION_MAX_SHIFT
    weights = _weights(draft.proposed_config)
    assert weights["CID_BAD"] == pytest.approx(0.35)
    assert weights["CID_OK"] == pytest.approx(0.65)
    assert all(value >= 0 for value in weights.values())
    assert sum(weights.values()) == pytest.approx(1.0)
    before = _config().config["allocations"]["default"]["caller_id"]
    assert total_variation(before, weights) == pytest.approx(CALLER_ID_ROTATION_MAX_SHIFT)
    assert total_variation(before, weights) <= CALLER_ID_ROTATION_MAX_SHIFT + 1e-9
    assert draft.proposed_config["allocations"]["default"]["welcome_message"] == {"W_A": 1.0}
    assert draft.proposed_config["allocations"]["default"]["voice"] == {"V_A": 1.0}
    assert draft.proposed_config["prompt_version"] == "P_A"
    assert draft.proposed_config["schedule"] == {"hour_weights": {"default": {"11": 1}}}
    assert "PUT" not in " ".join(sim.posts)
    assert "POST /approvals" in sim.posts
    linked = store.get_incident(draft.source_ref)
    assert linked["status"] == "mitigating"
    assert linked["related_proposal_id"] == draft.proposal_id
    store.close()


def test_unsupported_caller_id_is_not_selected(tmp_path):
    store = Store(tmp_path / "secret.sqlite")
    _seed(
        store,
        [
            ("CID_BAD", 200, 20, 10),
            ("CID_OK", 200, 120, 60),
            ("CID_SECRET", 200, 190, 100),
        ],
    )
    _open(store)
    sim = _Sim(_config())
    proposed = _propose(store, sim)
    assert len(proposed) == 1
    weights = _weights(proposed[0].proposed_config)
    assert proposed[0].evidence["replacement_caller_id"] == "CID_OK"
    assert "CID_SECRET" not in weights
    assert set(weights) <= {"CID_BAD", "CID_OK"}
    store.close()


def test_insufficient_samples_block_rotation(tmp_path):
    thin_bad = Store(tmp_path / "thin_bad.sqlite")
    _seed(thin_bad, [("CID_BAD", 40, 4, 1), ("CID_OK", 200, 120, 80)])
    _open(thin_bad)
    sim = _Sim(_config())
    assert _propose(thin_bad, sim) == []
    assert "insufficient_samples" in _skip_reasons(thin_bad)
    assert "POST /approvals" not in sim.posts
    thin_bad.close()

    thin_repl = Store(tmp_path / "thin_repl.sqlite")
    # CID_OK is in the call history so the workflow gap is real, but it is not an allowed replacement.
    _seed(
        thin_repl,
        [("CID_BAD", 200, 20, 10), ("CID_THIN", 10, 8, 4), ("CID_OK", 200, 160, 80)],
    )
    _open(thin_repl)
    wf = _workflow(
        options={
            "welcome_message": ["W_A"],
            "voice": ["V_A"],
            "caller_id": ["CID_BAD", "CID_THIN"],
            "prompt_version": ["P_A"],
        }
    )
    sim2 = _Sim(_config(caller={"CID_BAD": 0.7, "CID_THIN": 0.3}))
    assert _propose(thin_repl, sim2, wf) == []
    assert "insufficient_samples" in _skip_reasons(thin_repl)
    assert "POST /approvals" not in sim2.posts
    thin_repl.close()


def test_no_supported_replacement_skips_rotation(tmp_path):
    store = Store(tmp_path / "none.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 160, 80)])
    _open(store)
    wf = _workflow(
        options={
            "welcome_message": ["W_A"],
            "voice": ["V_A"],
            "caller_id": ["CID_BAD"],
            "prompt_version": ["P_A"],
        }
    )
    sim = _Sim(_config(caller={"CID_BAD": 1.0}))
    assert _propose(store, sim, wf) == []
    assert "no_supported_replacement" in _skip_reasons(store)
    assert "POST /approvals" not in sim.posts
    store.close()


def test_guardrail_violation_is_not_proposed(tmp_path):
    store = Store(tmp_path / "guard.sqlite")
    _seed(store, [("CID_BAD", 500, 100, 95), ("CID_OK", 200, 140, 40)])
    _open(store)
    wf = _workflow(objective={"primary": "pickup_rate", "guardrail": {"engagement_rate_max_drop": 0.02}})
    sim = _Sim(_config())
    assert _propose(store, sim, wf) == []
    assert "guardrail_violation" in _skip_reasons(store)
    assert "POST /approvals" not in sim.posts
    store.close()


def test_cohort_specific_allocation_is_not_overwritten(tmp_path):
    store = Store(tmp_path / "cohort.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    _open(store)
    north = {
        "welcome_message": {"W_A": 1.0},
        "voice": {"V_A": 1.0},
        "caller_id": {"CID_BAD": 0.9, "CID_OK": 0.1},
    }
    sim = _Sim(_config(extra_cohort=north))
    proposed = _propose(store, sim)
    assert len(proposed) == 1
    draft = proposed[0]
    assert draft.changed_dims == ["caller_id"]
    assert _weights(draft.proposed_config)["CID_BAD"] == pytest.approx(0.35)
    assert draft.proposed_config["allocations"]["cohort_north"] == north
    store.close()

    specific = Store(tmp_path / "specific.sqlite")
    _seed(specific, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    _open(specific)
    only_north = _config(include_default_caller=False, extra_cohort=north)
    sim2 = _Sim(only_north)
    assert _propose(specific, sim2) == []
    assert "cohort_specific_only" in _skip_reasons(specific)
    assert sim2.live["config"]["allocations"]["cohort_north"]["caller_id"] == north["caller_id"]
    specific.close()


def test_pending_rotation_is_not_duplicated(tmp_path):
    store = Store(tmp_path / "dup.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    _open(store)
    sim = _Sim(_config())
    first = _propose(store, sim)
    assert len(first) == 1
    again = _propose(store, sim)
    assert again == []
    rows = [
        row
        for row in store.list_proposals(WF)
        if row["source"] == "caller_id_rotation"
    ]
    assert len(rows) == 1
    assert rows[0]["proposal_id"] == first[0].proposal_id
    assert rows[0]["status"] == "submitted"
    store.close()


def test_applied_rotation_resolves_linked_incident(tmp_path):
    store = Store(tmp_path / "apply.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident = _open(store)
    sim = _Sim(_config())
    proposed = _propose(store, sim)
    assert len(proposed) == 1
    draft = proposed[0]
    assert draft.approval_id
    sim.approve(draft.approval_id)
    client = sim.client()
    reconcile_approvals(client, store)
    applied = apply_approved(client, store)
    assert applied and applied[0]["version"] == 2
    assert store.get_proposal(draft.proposal_id)["status"] == "applied"
    row = store.get_incident(incident.incident_id)
    assert row["status"] == "resolved"
    assert row["related_proposal_id"] == draft.proposal_id
    assert "apr_00001" in (row["resolution"] or "")
    assert "PUT" not in " ".join(sim.posts)
    live_weights = sim.live["config"]["allocations"]["default"]["caller_id"]
    assert live_weights["CID_BAD"] == pytest.approx(0.35)
    assert sim.live["config"]["allocations"]["default"]["voice"] == {"V_A": 1.0}
    assert sim.live["config"]["schedule"] == {"hour_weights": {"default": {"11": 1}}}
    store.close()


def test_human_rejection_does_not_retry(tmp_path):
    store = Store(tmp_path / "reject.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident = _open(store)
    sim = _Sim(_config())
    proposed = _propose(store, sim)
    assert len(proposed) == 1
    aid = proposed[0].approval_id
    assert aid
    sim.approvals[aid]["status"] = "rejected"
    sim.approvals[aid]["decided_at"] = "t_no"
    reconcile_approvals(sim.client(), store)
    assert store.get_proposal(proposed[0].proposal_id)["status"] == "rejected"
    sim.posts.clear()
    assert _propose(store, sim) == []
    assert "POST /approvals" not in sim.posts
    assert "PUT" not in " ".join(sim.posts)
    assert store.get_incident(incident.incident_id)["status"] != "resolved"
    rows = [row for row in store.list_proposals(WF) if row["source"] == "caller_id_rotation"]
    assert len(rows) == 1
    assert "human_rejected" in _skip_reasons(store)
    store.close()


@pytest.mark.parametrize("terminal", ["stale", "lapsed", "expired"])
def test_terminal_retry_uses_fresh_id_and_live_config(tmp_path, terminal: str):
    store = Store(tmp_path / f"{terminal}.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    _open(store)
    sim = _Sim(_config())
    first = _propose(store, sim)
    assert len(first) == 1
    aid = first[0].approval_id
    assert aid
    sim.approvals[aid]["status"] = terminal
    sim.approvals[aid]["decided_at"] = "t_end"
    reconcile_approvals(sim.client(), store)
    assert store.get_proposal(first[0].proposal_id)["status"] == terminal

    sim.live["version"] = 2
    sim.live["config"]["allocations"]["default"]["voice"] = {"V_A": 0.25, "V_B": 0.75}
    sim.posts.clear()
    second = _propose(store, sim)
    assert len(second) == 1
    draft = second[0]
    assert draft.proposal_id != first[0].proposal_id
    assert draft.base_version == 2
    assert draft.status == "submitted"
    assert draft.changed_dims == ["caller_id"]
    assert draft.evidence["attempt"] == 2
    assert _weights(draft.proposed_config)["CID_BAD"] == pytest.approx(0.35)
    assert _weights(draft.proposed_config)["CID_OK"] == pytest.approx(0.65)
    assert draft.proposed_config["allocations"]["default"]["voice"] == {"V_A": 0.25, "V_B": 0.75}
    assert draft.proposed_config["prompt_version"] == "P_A"
    assert draft.proposed_config["schedule"] == {"hour_weights": {"default": {"11": 1}}}
    assert "PUT" not in " ".join(sim.posts)
    assert "POST /approvals" in sim.posts
    store.close()


def _gov(enabled: bool, max_shift: float) -> dict[str, Any]:
    return {
        "auto_apply": {"enabled": enabled, "allowed_dims": ["caller_id"], "max_shift": max_shift},
        "approver_hours": [10, 18],
        "approver_latency_minutes": [30, 120],
        "proposal_ttl_hours": 24,
        "apply_window_hours": 2,
        "max_requests_per_24h": 5,
        "base_reject_rate": 0.0,
        "min_validation_pass_rate": 0.9,
    }


def _skip_details(store: Store) -> list[dict[str, Any]]:
    details = []
    for row in store.list_audit(WF, limit=50):
        if row["kind"] != "caller_id_rotation_skipped":
            continue
        details.append(json.loads(row["details_json"]))
    return details


def _other_mitigating(store: Store):
    signal = Signal(
        fingerprint=f"{WF}|pickup_drift|unrelated",
        workflow_id=WF,
        signal_type="pickup_drift",
        kpi="pickup",
        dimension="workflow",
        cohort_id=None,
        caller_id=None,
        severity="medium",
        baseline_value=0.5,
        recent_value=0.3,
        baseline_n=200,
        recent_n=200,
        threshold=0.05,
        message="unrelated pickup drift",
        evidence={},
        sim_ts=None,
    )
    other = open_or_refresh_incident(store, signal, tenant_id="tenant_test")
    mark_incident_mitigating(store, other.incident_id, "prop_unrelated")
    return other


def test_named_cohort_traffic_does_not_rotate_default(tmp_path):
    sparse = Store(tmp_path / "sparse.sqlite")
    _seed(sparse, [("CID_BAD", 40, 4, 1), ("CID_OK", 40, 30, 10)], chunk=0)
    _seed(
        sparse,
        [("CID_BAD", 200, 20, 5), ("CID_OK", 200, 160, 80)],
        cohort_id="cohort_north",
        start_id=81,
        chunk=1,
    )
    _open(sparse)
    north = {
        "welcome_message": {"W_A": 1.0},
        "voice": {"V_A": 1.0},
        "caller_id": {"CID_BAD": 0.8, "CID_OK": 0.2},
    }
    sim = _Sim(_config(extra_cohort=north))
    assert _propose(sparse, sim) == []
    assert any(
        row["reason"] == "insufficient_samples" and row.get("cohort_id") == "default" for row in _skip_details(sparse)
    )
    assert "POST /approvals" not in sim.posts
    assert sim.live["config"]["allocations"]["cohort_north"]["caller_id"] == north["caller_id"]
    sparse.close()

    quiet = Store(tmp_path / "quiet.sqlite")
    _seed(quiet, [("CID_BAD", 150, 75, 40), ("CID_OK", 150, 80, 40)], chunk=0)
    _seed(
        quiet,
        [("CID_BAD", 200, 20, 5), ("CID_OK", 200, 170, 80)],
        cohort_id="cohort_north",
        start_id=301,
        chunk=1,
    )
    _open(quiet)
    sim2 = _Sim(_config(extra_cohort=north))
    assert _propose(quiet, sim2) == []
    confirmed = [row for row in _skip_details(quiet) if row["reason"] == "signal_not_confirmed"]
    assert confirmed and confirmed[0]["cohort_id"] == "default"
    assert sim2.live["config"]["allocations"]["cohort_north"]["caller_id"] == north["caller_id"]
    quiet.close()


@pytest.mark.parametrize("mode", ["approval", "auto_apply"])
def test_rotation_resolves_only_the_linked_incident(tmp_path, mode: str):
    store = Store(tmp_path / f"{mode}.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident = _open(store)
    other = _other_mitigating(store)
    if mode == "auto_apply":
        sim = _Sim(_config(), allow_auto_apply=True)
        proposed = _propose(store, sim, _workflow(governance=_gov(True, 0.5)))
        assert len(proposed) == 1
        assert proposed[0].status == "auto_applied"
        assert proposed[0].route == "auto_apply"
        assert any(post.startswith("PUT ") for post in sim.posts)
        assert "POST /approvals" not in sim.posts
        assert _weights(sim.live["config"])["CID_BAD"] == pytest.approx(0.35)
    else:
        sim = _Sim(_config())
        proposed = _propose(store, sim)
        assert len(proposed) == 1
        assert proposed[0].approval_id
        sim.approve(proposed[0].approval_id)
        client = sim.client()
        reconcile_approvals(client, store)
        applied = apply_approved(client, store)
        assert applied and applied[0]["version"] == 2
        assert "PUT" not in " ".join(sim.posts)
    assert store.get_incident(incident.incident_id)["status"] == "resolved"
    assert store.get_incident(incident.incident_id)["related_proposal_id"] == proposed[0].proposal_id
    assert store.get_incident(other.incident_id)["status"] == "mitigating"
    assert store.get_incident(other.incident_id)["related_proposal_id"] == "prop_unrelated"
    store.close()


def test_live_config_failure_submits_nothing(tmp_path):
    store = Store(tmp_path / "down.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    _open(store)
    stale = _config(version=9, voice={"V_A": 0.1, "V_B": 0.9})
    sim = _Sim(_config(), fail_config=True)
    proposed = propose_caller_id_rotations(sim.client(), store, [_workflow()], [stale])
    assert proposed == []
    assert store.list_proposals(WF) == []
    assert "POST /approvals" not in sim.posts
    assert "PUT" not in " ".join(sim.posts)
    assert "live_config_unavailable" in _skip_reasons(store)
    store.close()


def test_dry_run_reports_engagement_primary_not_pickup(tmp_path):
    store = Store(tmp_path / "eng.sqlite")
    _seed(store, [("CID_BAD", 400, 120, 40), ("CID_OK", 400, 200, 100)])
    _open(store)
    wf = _workflow(objective={"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.05}})
    sim = _Sim(_config())
    proposed = _propose(store, sim, wf)
    assert len(proposed) == 1
    dry = proposed[0].dry_run
    bad_engagement = 40 / 120
    ok_engagement = 100 / 200
    assert dry["baseline_primary"] == pytest.approx(bad_engagement)
    assert dry["candidate_primary"] == pytest.approx(ok_engagement)
    assert dry["expected_primary_lift"] == pytest.approx(ok_engagement - bad_engagement)
    assert dry["control_n"] == 120
    assert dry["winner_n"] == 200
    assert dry["baseline_pickup"] == pytest.approx(120 / 400)
    assert dry["candidate_pickup"] == pytest.approx(200 / 400)
    assert dry["pickup_lift"] == pytest.approx((200 / 400) - (120 / 400))
    assert dry["expected_primary_lift"] != pytest.approx(dry["pickup_lift"])
    assert proposed[0].evidence["primary_kpi"] == "engagement"
    assert proposed[0].evidence["replacement_caller_id"] == "CID_OK"
    assert proposed[0].evidence["replacement_dials"] >= 100
    assert proposed[0].changed_dims == ["caller_id"]
    store.close()


def test_tighter_max_shift_uses_approval(tmp_path):
    store = Store(tmp_path / "shift.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    _open(store)
    sim = _Sim(_config(), allow_auto_apply=True)
    proposed = _propose(store, sim, _workflow(governance=_gov(True, 0.10)))
    assert len(proposed) == 1
    assert proposed[0].route == "approval"
    assert proposed[0].status == "submitted"
    assert proposed[0].dry_run["shift"] == pytest.approx(CALLER_ID_ROTATION_MAX_SHIFT)
    assert "PUT" not in " ".join(sim.posts)
    assert "POST /approvals" in sim.posts
    store.close()


def test_stranded_draft_recovers_without_a_duplicate(tmp_path):
    store = Store(tmp_path / "resubmit.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident = _open(store)
    sim = _Sim(_config())
    wf = _workflow()
    live = WorkflowConfig.model_validate(sim.live)
    built = build_caller_id_rotation_proposal(store, wf, live, incident)
    assert built.draft is not None
    persist_proposal(store, built.draft)
    mark_incident_mitigating(store, incident.incident_id, built.draft.proposal_id)
    proposed = _propose(store, sim)
    assert len(proposed) == 1
    assert proposed[0].proposal_id == built.draft.proposal_id
    assert proposed[0].status == "submitted"
    rows = [row for row in store.list_proposals(WF) if row["source"] == "caller_id_rotation"]
    assert len(rows) == 1
    assert sim.posts.count("POST /approvals") == 1
    sim.posts.clear()
    assert _propose(store, sim) == []
    assert "POST /approvals" not in sim.posts
    store.close()

    store2 = Store(tmp_path / "resume.sqlite")
    _seed(store2, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident2 = _open(store2)
    sim2 = _Sim(_config())
    built2 = build_caller_id_rotation_proposal(store2, wf, live, incident2)
    assert built2.draft is not None
    persist_proposal(store2, built2.draft)
    mark_incident_mitigating(store2, incident2.incident_id, built2.draft.proposal_id)
    sim2.approvals["apr_remote"] = {
        "approval_id": "apr_remote",
        "proposal_id": built2.draft.proposal_id,
        "workflow_id": WF,
        "status": "pending",
        "base_version": 1,
        "changed_dims": ["caller_id"],
        "created_at": "t0",
        "expires_at": "t_exp",
        "apply_by": None,
        "decided_at": None,
        "reasons": [],
    }
    resumed = _propose(store2, sim2)
    assert len(resumed) == 1
    assert resumed[0].proposal_id == built2.draft.proposal_id
    assert resumed[0].status == "submitted"
    assert "POST /approvals" not in sim2.posts
    assert store2.get_proposal(built2.draft.proposal_id)["approval_id"] == "apr_remote"
    assert len([row for row in store2.list_proposals(WF) if row["source"] == "caller_id_rotation"]) == 1
    store2.close()

    store3 = Store(tmp_path / "rebuild.sqlite")
    _seed(store3, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident3 = _open(store3)
    sim3 = _Sim(_config())
    built3 = build_caller_id_rotation_proposal(store3, wf, live, incident3)
    assert built3.draft is not None
    persist_proposal(store3, built3.draft)
    mark_incident_mitigating(store3, incident3.incident_id, built3.draft.proposal_id)
    sim3.live["version"] = 4
    sim3.live["config"]["allocations"]["default"]["voice"] = {"V_A": 0.2, "V_B": 0.8}
    rebuilt = _propose(store3, sim3)
    assert len(rebuilt) == 1
    assert rebuilt[0].proposal_id != built3.draft.proposal_id
    assert rebuilt[0].base_version == 4
    assert rebuilt[0].status == "submitted"
    assert rebuilt[0].proposed_config["allocations"]["default"]["voice"] == {"V_A": 0.2, "V_B": 0.8}
    assert store3.get_proposal(built3.draft.proposal_id)["status"] == "stale"
    assert sim3.posts.count("POST /approvals") == 1
    store3.close()


def _rotation_rows(store: Store):
    return [row for row in store.list_proposals(WF) if row["source"] == "caller_id_rotation"]


def _plant_approval(sim: _Sim, proposal_id: str, *, status: str, applied_version: int | None = None) -> str:
    aid = f"apr_{status}"
    sim.approvals[aid] = {
        "approval_id": aid,
        "proposal_id": proposal_id,
        "workflow_id": WF,
        "status": status,
        "base_version": 1,
        "changed_dims": ["caller_id"],
        "created_at": "t0",
        "expires_at": "t_exp",
        "apply_by": None,
        "decided_at": "t_dec" if status != "pending" else None,
        "reasons": [],
        "applied_version": applied_version,
    }
    return aid


def _stranded_draft(store: Store):
    wf = _workflow()
    live = _config()
    incident = _open(store)
    built = build_caller_id_rotation_proposal(store, wf, live, incident)
    assert built.draft is not None
    persist_proposal(store, built.draft)
    mark_incident_mitigating(store, incident.incident_id, built.draft.proposal_id)
    return incident, built.draft


def test_stranded_applied_approval_resolves_without_a_second_proposal(tmp_path):
    store = Store(tmp_path / "applied.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident, draft = _stranded_draft(store)
    other = _other_mitigating(store)
    sim = _Sim(_config())
    _plant_approval(sim, draft.proposal_id, status="applied", applied_version=2)
    sim.live["version"] = 2
    sim.live["config"]["allocations"]["default"]["caller_id"] = dict(
        draft.proposed_config["allocations"]["default"]["caller_id"]
    )
    recovered = _propose(store, sim)
    assert len(recovered) == 1
    assert recovered[0].proposal_id == draft.proposal_id
    assert recovered[0].status == "applied"
    assert sim.posts.count("POST /approvals") == 0
    assert "PUT" not in " ".join(sim.posts)
    assert len(_rotation_rows(store)) == 1
    assert store.get_proposal(draft.proposal_id)["status"] == "applied"
    linked = store.get_incident(incident.incident_id)
    assert linked["status"] == "resolved"
    assert linked["related_proposal_id"] == draft.proposal_id
    assert store.get_incident(other.incident_id)["status"] == "mitigating"
    assert _weights(sim.live["config"])["CID_BAD"] == pytest.approx(0.35)
    store.close()


def test_stranded_auto_apply_is_recognized_from_the_live_allocation(tmp_path):
    store = Store(tmp_path / "auto.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident, draft = _stranded_draft(store)
    other = _other_mitigating(store)
    draft.route = "auto_apply"
    draft.status = "auto_apply_uncertain"
    persist_proposal(store, draft)
    sim = _Sim(_config())
    sim.live["version"] = 2
    sim.live["config"] = json.loads(json.dumps(draft.proposed_config))
    recovered = _propose(store, sim)
    assert len(recovered) == 1
    assert recovered[0].proposal_id == draft.proposal_id
    assert recovered[0].status == "auto_applied"
    assert sim.posts.count("POST /approvals") == 0
    assert "PUT" not in " ".join(sim.posts)
    assert len(_rotation_rows(store)) == 1
    saved = store.get_proposal(draft.proposal_id)
    assert saved["status"] == "auto_applied"
    assert _weights(sim.live["config"])["CID_BAD"] == pytest.approx(0.35)
    assert _weights(sim.live["config"])["CID_OK"] == pytest.approx(0.65)
    snapshot = store.snapshot_for_proposal(draft.proposal_id, "post_apply")
    assert snapshot is not None
    assert int(snapshot["version"]) == 2
    assert any(row["kind"] == "caller_id_auto_apply_recovered" for row in store.list_audit(WF))
    linked = store.get_incident(incident.incident_id)
    assert linked["status"] == "resolved"
    assert linked["related_proposal_id"] == draft.proposal_id
    assert store.get_incident(other.incident_id)["status"] == "mitigating"
    store.close()


def test_unconfirmed_submission_reconciles_when_found_remotely(tmp_path):
    store = Store(tmp_path / "found.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident, draft = _stranded_draft(store)
    draft.status = "submitted_unconfirmed"
    draft.error_code = "duplicate_proposal_id"
    persist_proposal(store, draft)
    sim = _Sim(_config())
    _plant_approval(sim, draft.proposal_id, status="pending")
    recovered = _propose(store, sim)
    assert len(recovered) == 1
    assert recovered[0].proposal_id == draft.proposal_id
    assert recovered[0].status == "submitted"
    assert recovered[0].approval_id == "apr_pending"
    assert sim.posts.count("POST /approvals") == 0
    assert len(_rotation_rows(store)) == 1
    assert store.get_incident(incident.incident_id)["status"] == "mitigating"
    store.close()


def test_unconfirmed_submission_is_not_replaced_when_missing(tmp_path):
    store = Store(tmp_path / "missing.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident, draft = _stranded_draft(store)
    draft.status = "submitted_unconfirmed"
    draft.error_code = "duplicate_proposal_id"
    persist_proposal(store, draft)
    sim = _Sim(_config())
    assert _propose(store, sim) == []
    assert _propose(store, sim) == []
    assert sim.posts.count("POST /approvals") == 0
    assert "PUT" not in " ".join(sim.posts)
    rows = _rotation_rows(store)
    assert len(rows) == 1
    assert rows[0]["proposal_id"] == draft.proposal_id
    assert rows[0]["status"] == "submitted_unconfirmed"
    assert store.get_incident(incident.incident_id)["status"] == "mitigating"
    assert any(row["kind"] == "caller_id_rotation_unconfirmed" for row in store.list_audit(WF))
    store.close()


def _put_count(sim: _Sim) -> int:
    return sum(1 for post in sim.posts if post.startswith("PUT "))


@pytest.mark.parametrize("failure", ["http_500", "lost_connection"])
def test_default_retries_do_not_repeat_an_uncertain_auto_apply(tmp_path, failure: str):
    store = Store(tmp_path / f"{failure}.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident = _open(store)
    sim = _Sim(
        _config(),
        allow_auto_apply=True,
        auto_apply_http_status=500 if failure == "http_500" else None,
        lose_auto_apply_response=failure == "lost_connection",
    )
    wf = _workflow(governance=_gov(True, 0.5))
    # Omit max_retries so SimulatorClient uses its default of 2.
    first = _propose(store, sim, wf, max_retries=None)
    assert len(first) == 1
    assert first[0].status == "auto_apply_uncertain"
    assert first[0].route == "auto_apply"
    assert _put_count(sim) == 1
    assert store.get_proposal(first[0].proposal_id)["status"] == "auto_apply_uncertain"
    assert store.get_incident(incident.incident_id)["status"] == "mitigating"

    recovered = _propose(store, sim, wf, max_retries=None)
    assert len(recovered) == 1
    assert recovered[0].proposal_id == first[0].proposal_id
    assert recovered[0].status == "auto_applied"
    assert _put_count(sim) == 1
    assert "POST /approvals" not in sim.posts
    assert _weights(sim.live["config"])["CID_BAD"] == pytest.approx(0.35)
    linked = store.get_incident(incident.incident_id)
    assert linked["status"] == "resolved"
    assert linked["related_proposal_id"] == first[0].proposal_id
    store.close()


def test_lost_auto_apply_response_recovers_the_same_proposal(tmp_path):
    store = Store(tmp_path / "lost.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident = _open(store)
    other = _other_mitigating(store)
    sim = _Sim(_config(), allow_auto_apply=True, auto_apply_http_status=500)
    first = _propose(store, sim, _workflow(governance=_gov(True, 0.5)))
    assert len(first) == 1
    proposal_id = first[0].proposal_id
    assert first[0].status == "auto_apply_uncertain"
    assert first[0].route == "auto_apply"
    assert _put_count(sim) == 1
    assert "POST /approvals" not in sim.posts
    assert store.get_incident(incident.incident_id)["status"] == "mitigating"
    assert store.snapshot_for_proposal(proposal_id, "post_apply") is None
    assert _weights(sim.live["config"])["CID_BAD"] == pytest.approx(0.35)

    recovered = _propose(store, sim, _workflow(governance=_gov(True, 0.5)))
    assert len(recovered) == 1
    assert recovered[0].proposal_id == proposal_id
    assert recovered[0].status == "auto_applied"
    assert _put_count(sim) == 1
    assert "POST /approvals" not in sim.posts
    assert len(_rotation_rows(store)) == 1
    assert _weights(sim.live["config"])["CID_BAD"] == pytest.approx(0.35)
    assert _weights(sim.live["config"])["CID_OK"] == pytest.approx(0.65)
    linked = store.get_incident(incident.incident_id)
    assert linked["status"] == "resolved"
    assert linked["related_proposal_id"] == proposal_id
    assert store.get_incident(other.incident_id)["status"] == "mitigating"
    snapshot = store.snapshot_for_proposal(proposal_id, "post_apply")
    assert snapshot is not None
    assert int(snapshot["version"]) == 2
    store.close()


def test_matching_caller_id_without_auto_apply_is_not_recovered(tmp_path):
    store = Store(tmp_path / "unrelated.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident, draft = _stranded_draft(store)
    other = _other_mitigating(store)
    sim = _Sim(_config())
    sim.live["version"] = 2
    sim.live["config"]["allocations"]["default"]["caller_id"] = dict(
        draft.proposed_config["allocations"]["default"]["caller_id"]
    )
    sim.live["config"]["allocations"]["default"]["voice"] = {"V_A": 0.2, "V_B": 0.8}
    _propose(store, sim)
    saved = store.get_proposal(draft.proposal_id)
    assert saved["status"] != "auto_applied"
    assert saved["route"] != "auto_apply"
    assert store.snapshot_for_proposal(draft.proposal_id, "post_apply") is None
    assert store.get_incident(incident.incident_id)["status"] != "resolved"
    assert store.get_incident(other.incident_id)["status"] == "mitigating"
    assert "PUT" not in " ".join(sim.posts)
    store.close()


def test_version_bump_with_other_differences_is_not_auto_apply(tmp_path):
    store = Store(tmp_path / "partial.sqlite")
    _seed(store, [("CID_BAD", 200, 20, 10), ("CID_OK", 200, 120, 80)])
    incident, draft = _stranded_draft(store)
    draft.route = "auto_apply"
    draft.status = "auto_apply_uncertain"
    persist_proposal(store, draft)
    sim = _Sim(_config())
    sim.live["version"] = 3
    sim.live["config"]["allocations"]["default"]["caller_id"] = dict(
        draft.proposed_config["allocations"]["default"]["caller_id"]
    )
    sim.live["config"]["prompt_version"] = "P_OTHER"
    assert _propose(store, sim) == []
    assert _propose(store, sim) == []
    saved = store.get_proposal(draft.proposal_id)
    assert saved["status"] == "auto_apply_uncertain"
    assert saved["route"] == "auto_apply"
    assert len(_rotation_rows(store)) == 1
    assert store.snapshot_for_proposal(draft.proposal_id, "post_apply") is None
    assert store.get_incident(incident.incident_id)["status"] == "mitigating"
    assert store.get_incident(incident.incident_id)["related_proposal_id"] == draft.proposal_id
    assert "POST /approvals" not in sim.posts
    assert "PUT" not in " ".join(sim.posts)
    assert any(row["kind"] == "caller_id_auto_apply_uncertain" for row in store.list_audit(WF))
    store.close()
