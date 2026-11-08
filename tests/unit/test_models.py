"""Models accept the public payload shapes seen on the live simulator."""

from controller.api.models import ChunkMeta, SimState, Workflow, WorkflowConfig


def test_state_and_chunk():
    state = SimState.model_validate(
        {
            "scenario": "public_02_incidents",
            "sim_minute": 0,
            "ts": "2027-01-04T00:00:00+05:30",
            "end_minute": 14400,
            "finished": False,
            "tick_minutes": 15,
            "credits_remaining": 200.0,
            "chunks": 1,
            "last_event_id": 187274,
        }
    )
    assert state.tick_minutes == 15
    chunk = ChunkMeta.model_validate(
        {
            "chunk": 0,
            "events": 10,
            "first_event_id": 1,
            "kind": "history",
            "last_event_id": 10,
            "path": "ignored-by-controller",
            "until_minute": 0,
            "until_ts": "2027-01-04T00:00:00+05:30",
            "extra_field": True,
        }
    )
    assert chunk.kind == "history"
    assert chunk.model_extra["extra_field"] is True


def test_workflow_and_config():
    workflow = Workflow.model_validate(
        {
            "workflow_id": "nw_kyc",
            "tenant_id": "northwind_life",
            "use_case": "KYC",
            "languages": ["en", "hi"],
            "dialling_window": [10, 20],
            "dials_per_hour": 200,
            "cadence": [2, 2, 3],
            "min_gap_minutes": 120,
            "daily_batch": {"size": 700, "at_hour": 8, "returning_share": 0.2},
            "mid_flight": "next_campaign_only",
            "objective": {"primary": "pickup_rate", "guardrail": {"engagement_rate_max_drop": 0.02}},
            "options": {
                "welcome_message": ["W1", "W2"],
                "voice": ["V1", "V2"],
                "caller_id": ["D1", "D2", "D3"],
                "prompt_version": ["P1", "P3"],
            },
            "prompt_catalog": {"P1": "Current production prompt"},
            "governance": {
                "auto_apply": {"enabled": False, "allowed_dims": [], "max_shift": 0},
                "approver_hours": [10, 18],
                "approver_latency_minutes": [60, 480],
                "proposal_ttl_hours": 24,
                "apply_window_hours": 2,
                "max_requests_per_24h": 2,
                "base_reject_rate": 0.15,
                "min_validation_pass_rate": 0.97,
            },
            "current_version": 1,
        }
    )
    assert workflow.governance.auto_apply.enabled is False
    config = WorkflowConfig.model_validate(
        {
            "workflow_id": "nw_kyc",
            "version": 1,
            "actor": "default",
            "applied_at": None,
            "config": {"prompt_version": "P1", "cohorts": [], "allocations": {}, "schedule": {}},
        }
    )
    assert config.applied_at is None
    assert config.config["prompt_version"] == "P1"
