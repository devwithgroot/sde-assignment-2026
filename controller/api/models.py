"""Typed views of the simulator's public JSON.

OpenAPI marks every body as an untyped object. These models follow the fields
observed on the live server and in sim/server.py. Unknown fields are kept.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class Health(ApiModel):
    ok: bool


class SimState(ApiModel):
    scenario: str
    sim_minute: int
    ts: str
    end_minute: int
    finished: bool
    tick_minutes: int
    credits_remaining: float
    chunks: int
    last_event_id: int


class Objective(ApiModel):
    primary: str
    guardrail: dict[str, float] = Field(default_factory=dict)


class AutoApply(ApiModel):
    enabled: bool
    allowed_dims: list[str] = Field(default_factory=list)
    max_shift: float = 0


class Governance(ApiModel):
    auto_apply: AutoApply
    approver_hours: list[int]
    approver_latency_minutes: list[int]
    proposal_ttl_hours: float
    apply_window_hours: float
    max_requests_per_24h: int
    base_reject_rate: float
    min_validation_pass_rate: float


class WorkflowOptions(ApiModel):
    welcome_message: list[str]
    voice: list[str]
    caller_id: list[str]
    prompt_version: list[str]


class Workflow(ApiModel):
    workflow_id: str
    tenant_id: str
    use_case: str | None = None
    languages: list[str]
    dialling_window: list[int]
    dials_per_hour: int
    cadence: list[int]
    min_gap_minutes: int
    daily_batch: dict[str, Any]
    mid_flight: str
    objective: Objective
    options: WorkflowOptions
    prompt_catalog: dict[str, str]
    governance: Governance
    current_version: int


class WorkflowConfig(ApiModel):
    workflow_id: str
    version: int
    actor: str
    applied_at: str | None = None
    config: dict[str, Any]


class ChunkMeta(ApiModel):
    chunk: int
    events: int
    first_event_id: int
    kind: str
    last_event_id: int
    path: str
    until_minute: int
    until_ts: str
