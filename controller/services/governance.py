"""Governance: proposals, approval submission/reconciliation, apply, auto-apply.

Never bypasses approvals. Apply uses only ``{"approval_id": "..."}``.
Idempotent proposal_ids are persisted before network calls for crash recovery.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from controller.api.client import SimulatorClient, SimulatorError
from controller.api.models import Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.audit import audit
from controller.services.change_builder import (
    ProposalDraft,
    auto_apply_permitted,
    build_rollback_proposal,
    build_winner_proposal,
)
from controller.services.experiments import ExperimentState
from controller.services.incidents import (
    find_open_incidents_for_workflow,
    mark_incident_mitigating,
    resolve_incident,
)


@dataclass
class ControllerApproval:
    approval_id: str
    proposal_id: str
    workflow_id: str
    status: str
    updated_at: str
    base_version: int | None = None
    changed_dims: list[str] | None = None
    created_at: str | None = None
    expires_at: str | None = None
    apply_by: str | None = None
    decided_at: str | None = None
    reasons: list[Any] | None = None
    applied_version: int | None = None
    last_reconciled_at: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class ConfigSnapshot:
    snapshot_id: str
    workflow_id: str
    version: int
    config: dict[str, Any]
    source: str
    created_at: str
    related_proposal_id: str | None = None


@dataclass
class GovernanceReport:
    incidents_linked: int = 0
    proposals_built: int = 0
    proposals_submitted: int = 0
    auto_applied: int = 0
    approvals_reconciled: int = 0
    applied: int = 0
    rejected_or_expired: int = 0
    rollbacks_proposed: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _approval_from_api(payload: dict[str, Any], *, proposal_id: str | None = None) -> ControllerApproval:
    return ControllerApproval(
        approval_id=str(payload.get("approval_id")),
        proposal_id=str(payload.get("proposal_id") or proposal_id or ""),
        workflow_id=str(payload.get("workflow_id")),
        status=str(payload.get("status")),
        base_version=payload.get("base_version"),
        changed_dims=list(payload.get("changed_dims") or []),
        created_at=payload.get("created_at"),
        updated_at=_now(),
        expires_at=payload.get("expires_at"),
        apply_by=payload.get("apply_by"),
        decided_at=payload.get("decided_at"),
        reasons=list(payload.get("reasons") or []) if payload.get("reasons") is not None else None,
        applied_version=payload.get("applied_version"),
        last_reconciled_at=_now(),
        raw=payload,
    )


def persist_proposal(store: Store, draft: ProposalDraft) -> ProposalDraft:
    draft.updated_at = _now()
    if not draft.created_at:
        draft.created_at = draft.updated_at
    store.upsert_proposal(draft)
    return draft


def save_snapshot(
    store: Store,
    *,
    workflow_id: str,
    version: int,
    config: dict[str, Any],
    source: str,
    related_proposal_id: str | None = None,
) -> None:
    snap = ConfigSnapshot(
        snapshot_id=f"snap_{workflow_id}_{version}_{source}",
        workflow_id=workflow_id,
        version=version,
        config=config,
        source=source,
        related_proposal_id=related_proposal_id,
        created_at=_now(),
    )
    store.upsert_config_snapshot(snap)


def submit_proposal(
    client: SimulatorClient,
    store: Store,
    workflow: Workflow,
    current: WorkflowConfig,
    draft: ProposalDraft,
) -> ProposalDraft:
    """Persist draft, then route via auto-apply or approval. Idempotent on proposal_id."""
    existing = store.get_proposal(draft.proposal_id)
    if existing is not None and existing["status"] in {"submitted", "approved", "applied", "auto_applied"}:
        draft.status = existing["status"]
        draft.approval_id = existing["approval_id"]
        draft.route = existing["route"]
        return draft

    ok, problems = auto_apply_permitted(
        workflow, current.config, draft.proposed_config, impact_score=draft.impact_score
    )
    persist_proposal(store, draft)
    audit(
        store,
        kind="proposal_built",
        summary=f"Built proposal {draft.proposal_id} ({draft.source})",
        details={
            "changed_dims": draft.changed_dims,
            "impact_score": draft.impact_score,
            "auto_apply_ok": ok,
            "auto_apply_problems": problems,
        },
        workflow_id=draft.workflow_id,
        related_proposal_id=draft.proposal_id,
        related_experiment_id=draft.source_ref if draft.source == "experiment_winner" else None,
        idempotency_parts=[draft.proposal_id, "built"],
    )

    save_snapshot(
        store,
        workflow_id=current.workflow_id,
        version=current.version,
        config=current.config,
        source="pre_apply",
        related_proposal_id=draft.proposal_id,
    )
    # Keep last-known-safe as rollback candidate before mutation.
    save_snapshot(
        store,
        workflow_id=current.workflow_id,
        version=current.version,
        config=current.config,
        source="rollback_candidate",
        related_proposal_id=draft.proposal_id,
    )

    if ok:
        draft.route = "auto_apply"
        try:
            result = client.auto_apply(
                draft.workflow_id,
                {
                    "proposal_id": draft.proposal_id,
                    "base_version": draft.base_version,
                    "proposed_config": draft.proposed_config,
                    "rationale": draft.rationale,
                    "evidence": draft.evidence,
                },
            )
            draft.status = "auto_applied"
            draft.submitted_at = _now()
            draft.updated_at = draft.submitted_at
            draft.error_code = None
            draft.error_detail = None
            persist_proposal(store, draft)
            new_version = int(result.get("version"))
            save_snapshot(
                store,
                workflow_id=draft.workflow_id,
                version=new_version,
                config=draft.proposed_config,
                source="post_apply",
                related_proposal_id=draft.proposal_id,
            )
            save_snapshot(
                store,
                workflow_id=draft.workflow_id,
                version=new_version,
                config=draft.proposed_config,
                source="rollback_candidate",
                related_proposal_id=draft.proposal_id,
            )
            if draft.source_ref and draft.source == "experiment_winner":
                store.update_experiment_promotion(draft.source_ref, "promoted_auto_apply")
            audit(
                store,
                kind="auto_applied",
                summary=f"Auto-applied proposal {draft.proposal_id} → v{new_version}",
                details={"result": result},
                workflow_id=draft.workflow_id,
                related_proposal_id=draft.proposal_id,
                related_experiment_id=draft.source_ref,
                idempotency_parts=[draft.proposal_id, "auto_applied", new_version],
            )
            return draft
        except SimulatorError as exc:
            draft.error_code = exc.code
            draft.error_detail = str(exc.detail)
            draft.status = "failed" if exc.status not in {403, 409} else "blocked_auto_apply"
            draft.updated_at = _now()
            persist_proposal(store, draft)
            # Fall through to approval route when auto-apply is denied.
            if exc.status not in {403, 409}:
                audit(
                    store,
                    kind="auto_apply_failed",
                    summary=f"Auto-apply failed for {draft.proposal_id}: {exc.code}",
                    details={"status": exc.status, "detail": exc.detail},
                    workflow_id=draft.workflow_id,
                    related_proposal_id=draft.proposal_id,
                    idempotency_parts=[draft.proposal_id, "auto_fail", exc.code],
                )
                return draft

    draft.route = "approval"
    body = {
        "proposal_id": draft.proposal_id,
        "workflow_id": draft.workflow_id,
        "base_version": draft.base_version,
        "proposed_config": draft.proposed_config,
        "change_summary": draft.change_summary,
        "diff": draft.diff,
        "affected_leads": int(draft.affected_leads),
        "dry_run": draft.dry_run,
        "impact_score": float(draft.impact_score),
        "rationale": draft.rationale,
        "evidence": draft.evidence or {},
    }
    try:
        payload = client.request_approval(body)
    except SimulatorError as exc:
        if exc.code == "duplicate_proposal_id":
            # Crash recovery: proposal already accepted by simulator; reconcile via list.
            matches = [
                a
                for a in client.list_approvals(workflow_id=draft.workflow_id)
                if a.get("proposal_id") == draft.proposal_id
            ]
            if matches:
                payload = matches[0]
            else:
                draft.status = "submitted_unconfirmed"
                draft.error_code = exc.code
                draft.error_detail = str(exc.detail)
                draft.updated_at = _now()
                persist_proposal(store, draft)
                audit(
                    store,
                    kind="approval_submit_unconfirmed",
                    summary=f"Duplicate proposal_id {draft.proposal_id}; approval not listed yet",
                    details={"detail": exc.detail},
                    workflow_id=draft.workflow_id,
                    related_proposal_id=draft.proposal_id,
                    idempotency_parts=[draft.proposal_id, "dup_unconfirmed"],
                )
                return draft
        else:
            draft.status = "failed"
            draft.error_code = exc.code
            draft.error_detail = str(exc.detail)
            draft.updated_at = _now()
            persist_proposal(store, draft)
            audit(
                store,
                kind="approval_submit_failed",
                summary=f"Approval submit failed for {draft.proposal_id}: {exc.code}",
                details={"status": exc.status, "detail": exc.detail},
                workflow_id=draft.workflow_id,
                related_proposal_id=draft.proposal_id,
                idempotency_parts=[draft.proposal_id, "submit_fail", exc.code],
            )
            return draft

    approval = _approval_from_api(payload, proposal_id=draft.proposal_id)
    store.upsert_controller_approval(approval)
    draft.approval_id = approval.approval_id
    draft.status = "submitted"
    draft.submitted_at = _now()
    draft.updated_at = draft.submitted_at
    draft.error_code = None
    draft.error_detail = None
    persist_proposal(store, draft)
    if draft.source_ref and draft.source == "experiment_winner":
        store.update_experiment_promotion(draft.source_ref, "proposal_submitted")
    audit(
        store,
        kind="approval_submitted",
        summary=f"Submitted approval {approval.approval_id} for proposal {draft.proposal_id}",
        details={"status": approval.status},
        workflow_id=draft.workflow_id,
        related_proposal_id=draft.proposal_id,
        related_approval_id=approval.approval_id,
        related_experiment_id=draft.source_ref,
        idempotency_parts=[draft.proposal_id, approval.approval_id, "submitted"],
    )
    return draft


def reconcile_approvals(client: SimulatorClient, store: Store) -> list[ControllerApproval]:
    """Refresh local approval rows from simulator + ingested approval_decided events."""
    updated: list[ControllerApproval] = []
    local_rows = store.list_controller_approvals()
    for row in local_rows:
        if row["status"] in {"applied", "rejected", "expired", "lapsed"}:
            continue
        try:
            payload = client.get_approval(row["approval_id"])
        except SimulatorError as exc:
            audit(
                store,
                kind="approval_reconcile_error",
                summary=f"Failed to reconcile {row['approval_id']}: {exc.code}",
                details={"detail": exc.detail},
                workflow_id=row["workflow_id"],
                related_approval_id=row["approval_id"],
                related_proposal_id=row["proposal_id"],
                idempotency_parts=[row["approval_id"], "reconcile_err", exc.code, _now()[:16]],
            )
            continue
        approval = _approval_from_api(payload, proposal_id=row["proposal_id"])
        if row["status"] == "applied" and approval.status != "applied":
            # Keep terminal local apply; simulator poll may lag.
            continue
        store.upsert_controller_approval(approval)
        _sync_proposal_status_from_approval(store, approval)
        updated.append(approval)
        audit(
            store,
            kind="approval_reconciled",
            summary=f"Reconciled approval {approval.approval_id} → {approval.status}",
            details={"status": approval.status, "reasons": approval.reasons},
            workflow_id=approval.workflow_id,
            related_approval_id=approval.approval_id,
            related_proposal_id=approval.proposal_id,
            idempotency_parts=[approval.approval_id, approval.status, approval.decided_at or ""],
        )

    # Also fold ingested approval_decided events for approvals we already track.
    for event in store.recent_approval_decision_events():
        aid = event["approval_id"]
        local = store.get_controller_approval(aid)
        if local is None:
            continue
        if local["status"] == event["status"]:
            continue
        reasons = event["reasons_json"]
        if isinstance(reasons, str):
            try:
                reasons = json.loads(reasons)
            except json.JSONDecodeError:
                reasons = [reasons]
        approval = ControllerApproval(
            approval_id=aid,
            proposal_id=local["proposal_id"],
            workflow_id=event["workflow_id"],
            status=str(event["status"]),
            base_version=local["base_version"],
            changed_dims=json.loads(local["changed_dims_json"]) if local["changed_dims_json"] else [],
            created_at=local["created_at"],
            updated_at=_now(),
            expires_at=local["expires_at"],
            apply_by=local["apply_by"],
            decided_at=event["ts"],
            reasons=reasons if isinstance(reasons, list) else [reasons],
            applied_version=local["applied_version"],
            last_reconciled_at=_now(),
            raw={"from_event": True, "event_id": event["event_id"]},
        )
        store.upsert_controller_approval(approval)
        _sync_proposal_status_from_approval(store, approval)
        updated.append(approval)
    return updated


def _sync_proposal_status_from_approval(store: Store, approval: ControllerApproval) -> None:
    prop = store.get_proposal(approval.proposal_id)
    if prop is None:
        return
    # Never downgrade a locally applied proposal when the poll still says approved.
    if prop["status"] == "applied" and approval.status != "applied":
        return
    mapping = {
        "pending": "submitted",
        "approved": "approved",
        "rejected": "rejected",
        "expired": "expired",
        "lapsed": "lapsed",
        "applied": "applied",
    }
    new_status = mapping.get(approval.status)
    if not new_status:
        return
    draft = ProposalDraft(
        proposal_id=prop["proposal_id"],
        workflow_id=prop["workflow_id"],
        base_version=prop["base_version"],
        status=new_status,
        source=prop["source"],
        source_ref=prop["source_ref"],
        changed_dims=json.loads(prop["changed_dims_json"]),
        proposed_config=json.loads(prop["proposed_config_json"]),
        change_summary=prop["change_summary"],
        diff=json.loads(prop["diff_json"]),
        dry_run=json.loads(prop["dry_run_json"]),
        impact_score=prop["impact_score"],
        affected_leads=prop["affected_leads"],
        rationale=prop["rationale"],
        evidence=json.loads(prop["evidence_json"]) if prop["evidence_json"] else {},
        approval_id=approval.approval_id,
        route=prop["route"],
        error_code=prop["error_code"],
        error_detail=prop["error_detail"],
        created_at=prop["created_at"],
        updated_at=_now(),
        submitted_at=prop["submitted_at"],
    )
    persist_proposal(store, draft)
    if new_status == "rejected" and draft.source_ref and draft.source == "experiment_winner":
        store.update_experiment_promotion(draft.source_ref, "promotion_rejected")
    if new_status == "expired" and draft.source_ref and draft.source == "experiment_winner":
        store.update_experiment_promotion(draft.source_ref, "promotion_expired")


def apply_approved(client: SimulatorClient, store: Store) -> list[dict[str, Any]]:
    """Apply locally known approved approvals. Body is only approval_id."""
    results: list[dict[str, Any]] = []
    for row in store.list_controller_approvals(status="approved"):
        try:
            result = client.apply_approval(row["workflow_id"], row["approval_id"])
        except SimulatorError as exc:
            audit(
                store,
                kind="apply_failed",
                summary=f"Apply failed for {row['approval_id']}: {exc.code}",
                details={"status": exc.status, "detail": exc.detail},
                workflow_id=row["workflow_id"],
                related_approval_id=row["approval_id"],
                related_proposal_id=row["proposal_id"],
                idempotency_parts=[row["approval_id"], "apply_fail", exc.code],
            )
            if exc.code in {"approval_lapsed", "not_approved"}:
                try:
                    payload = client.get_approval(row["approval_id"])
                    approval = _approval_from_api(payload, proposal_id=row["proposal_id"])
                    store.upsert_controller_approval(approval)
                    _sync_proposal_status_from_approval(store, approval)
                except SimulatorError:
                    pass
            elif exc.code == "stale_base_version":
                prop = store.get_proposal(row["proposal_id"])
                if prop is not None:
                    draft = ProposalDraft(
                        proposal_id=prop["proposal_id"],
                        workflow_id=prop["workflow_id"],
                        base_version=prop["base_version"],
                        status="stale",
                        source=prop["source"],
                        source_ref=prop["source_ref"],
                        changed_dims=json.loads(prop["changed_dims_json"]),
                        proposed_config=json.loads(prop["proposed_config_json"]),
                        change_summary=prop["change_summary"],
                        diff=json.loads(prop["diff_json"]),
                        dry_run=json.loads(prop["dry_run_json"]),
                        impact_score=prop["impact_score"],
                        affected_leads=prop["affected_leads"],
                        rationale=prop["rationale"],
                        evidence=json.loads(prop["evidence_json"]) if prop["evidence_json"] else {},
                        approval_id=row["approval_id"],
                        route=prop["route"],
                        error_code=exc.code,
                        error_detail=str(exc.detail),
                        created_at=prop["created_at"],
                        updated_at=_now(),
                        submitted_at=prop["submitted_at"],
                    )
                    persist_proposal(store, draft)
            continue

        approval = ControllerApproval(
            approval_id=row["approval_id"],
            proposal_id=row["proposal_id"],
            workflow_id=row["workflow_id"],
            status="applied",
            base_version=row["base_version"],
            changed_dims=json.loads(row["changed_dims_json"]) if row["changed_dims_json"] else [],
            created_at=row["created_at"],
            updated_at=_now(),
            expires_at=row["expires_at"],
            apply_by=row["apply_by"],
            decided_at=row["decided_at"],
            reasons=json.loads(row["reasons_json"]) if row["reasons_json"] else None,
            applied_version=int(result.get("version")) if result.get("version") is not None else None,
            last_reconciled_at=_now(),
            raw=result,
        )
        store.upsert_controller_approval(approval)
        _sync_proposal_status_from_approval(store, approval)
        prop = store.get_proposal(row["proposal_id"])
        if prop is not None:
            save_snapshot(
                store,
                workflow_id=row["workflow_id"],
                version=int(result["version"]),
                config=json.loads(prop["proposed_config_json"]),
                source="post_apply",
                related_proposal_id=row["proposal_id"],
            )
            save_snapshot(
                store,
                workflow_id=row["workflow_id"],
                version=int(result["version"]),
                config=json.loads(prop["proposed_config_json"]),
                source="rollback_candidate",
                related_proposal_id=row["proposal_id"],
            )
            if prop["source"] == "experiment_winner" and prop["source_ref"]:
                store.update_experiment_promotion(prop["source_ref"], "promoted")
            if prop["source"] in {"experiment_winner", "decay_rollback", "incident_mitigation"}:
                for incident in find_open_incidents_for_workflow(store, row["workflow_id"]):
                    if incident.related_proposal_id == row["proposal_id"] or incident.status == "mitigating":
                        resolve_incident(
                            store,
                            incident.incident_id,
                            resolution=f"Applied approval {row['approval_id']} → v{result.get('version')}",
                        )
        audit(
            store,
            kind="applied",
            summary=f"Applied approval {row['approval_id']} → v{result.get('version')}",
            details={"result": result},
            workflow_id=row["workflow_id"],
            related_approval_id=row["approval_id"],
            related_proposal_id=row["proposal_id"],
            idempotency_parts=[row["approval_id"], "applied", result.get("version")],
        )
        results.append(result)
    return results


def _approver_attention_ok(store: Store, workflow: Workflow) -> tuple[bool, str]:
    """Local soft cap mirroring max_requests_per_24h to protect approver attention."""
    cap = int(workflow.governance.max_requests_per_24h)
    openish = [
        p
        for p in store.recent_proposals_for_workflow(workflow.workflow_id, limit=100)
        if p["status"] in {"draft", "submitted", "approved"}
    ]
    if len(openish) >= cap:
        return False, f"approver_attention_cap open={len(openish)} cap={cap}"
    return True, "ok"


def promote_experiment_winners(
    client: SimulatorClient,
    store: Store,
    workflows: list[Workflow],
    configs: list[WorkflowConfig],
    experiments: list[ExperimentState],
) -> list[ProposalDraft]:
    by_wf = {w.workflow_id: w for w in workflows}
    by_cfg = {c.workflow_id: c for c in configs}
    out: list[ProposalDraft] = []
    for exp in experiments:
        if exp.status != "winner" or not exp.winner_variant:
            continue
        if exp.promotion_status in {"promoted", "promoted_auto_apply", "proposal_submitted"}:
            continue
        workflow = by_wf.get(exp.workflow_id)
        config = by_cfg.get(exp.workflow_id)
        if workflow is None or config is None:
            continue
        ok_attn, attn_reason = _approver_attention_ok(store, workflow)
        if not ok_attn:
            from controller.services.fairness import ACTION_GOVERNANCE, FairScheduler

            FairScheduler(store).record_deferral(
                workflow_id=workflow.workflow_id,
                tenant_id=workflow.tenant_id,
                action_kind=ACTION_GOVERNANCE,
                priority=10,
                reason=attn_reason,
            )
            continue
        # Refresh live config before proposing (avoid stale base).
        try:
            live = client.workflow_config(exp.workflow_id)
        except SimulatorError:
            live = config
        draft = build_winner_proposal(store, workflow, live, exp)
        if draft is None:
            continue
        submitted = submit_proposal(client, store, workflow, live, draft)
        out.append(submitted)
        for incident in find_open_incidents_for_workflow(store, exp.workflow_id):
            mark_incident_mitigating(store, incident.incident_id, submitted.proposal_id)
    return out


def propose_decay_rollbacks(
    client: SimulatorClient,
    store: Store,
    workflows: list[Workflow],
    configs: list[WorkflowConfig],
    decay_hits: list[dict[str, Any]],
) -> list[ProposalDraft]:
    by_wf = {w.workflow_id: w for w in workflows}
    out: list[ProposalDraft] = []
    for hit in decay_hits:
        workflow = by_wf.get(hit["workflow_id"])
        if workflow is None:
            continue
        try:
            live = client.workflow_config(hit["workflow_id"])
        except SimulatorError:
            live = next((c for c in configs if c.workflow_id == hit["workflow_id"]), None)
            if live is None:
                continue
        draft = build_rollback_proposal(
            store,
            workflow,
            live,
            reason=hit.get("reason") or "Promoted winner decayed on primary/guardrail",
            source_ref=hit.get("experiment_id"),
        )
        if draft is None:
            continue
        submitted = submit_proposal(client, store, workflow, live, draft)
        out.append(submitted)
    return out


def run_governance_cycle(
    client: SimulatorClient,
    store: Store,
    workflows: list[Workflow],
    configs: list[WorkflowConfig],
    experiments: list[ExperimentState],
    *,
    decay_hits: list[dict[str, Any]] | None = None,
    mutate: bool = True,
) -> GovernanceReport:
    report = GovernanceReport()
    if not mutate:
        report.notes.append("governance mutate disabled; proposals/approvals not submitted")
        return report

    reconciled = reconcile_approvals(client, store)
    report.approvals_reconciled = len(reconciled)
    report.rejected_or_expired = sum(1 for a in reconciled if a.status in {"rejected", "expired", "lapsed"})

    applied = apply_approved(client, store)
    report.applied = len(applied)

    promotions = promote_experiment_winners(client, store, workflows, configs, experiments)
    report.proposals_built = len(promotions)
    report.proposals_submitted = sum(1 for p in promotions if p.status in {"submitted", "approved", "auto_applied", "applied"})
    report.auto_applied = sum(1 for p in promotions if p.status == "auto_applied")
    report.incidents_linked = sum(1 for p in promotions if p.approval_id or p.status == "auto_applied")

    rollbacks = propose_decay_rollbacks(client, store, workflows, configs, decay_hits or [])
    report.rollbacks_proposed = len(rollbacks)

    # Second reconcile/apply pass for anything that became approved mid-cycle (rare without advance).
    reconcile_approvals(client, store)
    report.applied += len(apply_approved(client, store))
    return report
