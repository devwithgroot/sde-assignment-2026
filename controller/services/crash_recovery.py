"""Crash recovery / restart reconciliation (Phase 8).

Safe to run after a kill during ingestion or while an approval is pending:
- Re-open Store (schema/checkpoints intact)
- Re-ingest is idempotent (chunk unique + event_id unique)
- Reconcile controller approvals with simulator GET
- Resume proposed prompt attempts and apply approved approvals via governance
- Never advances simulated time
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from controller.api.client import SimulatorClient
from controller.api.models import Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.audit import audit
from controller.services.governance import apply_approved, reconcile_approvals
from controller.services.ingestion import ingest_available
from controller.services.observability import bump_counter, emit_alert
from controller.services.prompt_incident import run_prompt_incident_cycle


@dataclass
class RecoveryReport:
    ingested_kinds: list[dict[str, Any]] = field(default_factory=list)
    approvals_reconciled: int = 0
    applied: int = 0
    prompt_resumed_notes: list[str] = field(default_factory=list)
    duplicate_proposals: int = 0
    notes: list[str] = field(default_factory=list)


def run_crash_recovery(
    client: SimulatorClient,
    store: Store,
    workflows: list[Workflow],
    configs: list[WorkflowConfig],
    *,
    mutate: bool = True,
) -> RecoveryReport:
    report = RecoveryReport()
    bump_counter(store, "recovery.runs")

    # 1) Idempotent ingest resume from checkpoints.
    ingested = ingest_available(client, store)
    report.ingested_kinds = [
        {
            "kind": item.kind,
            "fetched": item.chunks_fetched,
            "committed": item.chunks_committed,
            "inserted": item.inserted,
            "duplicates": item.duplicates,
        }
        for item in ingested
    ]
    report.notes.append("ingest_reconciled")

    # 2) Approval reconciliation + apply (governed only when mutate).
    if mutate:
        reconciled = reconcile_approvals(client, store)
        report.approvals_reconciled = len(reconciled)
        applied = apply_approved(client, store)
        report.applied = len(applied)
        # Prompt cycle resumes proposed attempts and will not duplicate suggestions.
        from controller.api.models import SimState

        state = client.state()
        prompt = run_prompt_incident_cycle(
            client, store, workflows, configs, state, mutate=True
        )
        report.prompt_resumed_notes = list(prompt.notes)
        # Count proposals that remained unique by proposal_id primary key.
        props = store.list_proposals()
        ids = [p["proposal_id"] for p in props]
        report.duplicate_proposals = len(ids) - len(set(ids))
    else:
        report.notes.append("mutate_disabled_skip_approval_and_prompt_resume")

    audit(
        store,
        kind="crash_recovery_completed",
        summary="Crash recovery reconciliation finished",
        details={
            "approvals_reconciled": report.approvals_reconciled,
            "applied": report.applied,
            "mutate": mutate,
            "ingest": report.ingested_kinds,
        },
        idempotency_parts=["recovery", report.approvals_reconciled, report.applied, mutate],
    )
    emit_alert(
        store,
        kind="recovery_ran",
        severity="low",
        message="controller crash recovery completed",
        details={"mutate": mutate},
    )
    return report
