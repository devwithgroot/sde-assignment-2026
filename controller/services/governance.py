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
    ALLOCATION_DIMS,
    CALLER_ID_ROTATION_SOURCE,
    ProposalDraft,
    _allocation_weights,
    _weights_match,
    auto_apply_permitted,
    build_caller_id_rotation_proposal,
    build_explore_proposal,
    build_rollback_proposal,
    build_winner_proposal,
)
from controller.services.experiments import STICKY_PROMOTION_STATUSES, ExperimentState
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


def _auto_apply_outcome_ambiguous(exc: SimulatorError) -> bool:
    """True when the PUT may already have been committed.

    A timeout, a dropped connection, an HTTP 408, or a 5xx can happen after the
    simulator applied the config. Other 4xx responses, including 403 and 409,
    are definite refusals and did not apply.
    """
    if exc.status == 408:
        return True
    if 400 <= exc.status < 500:
        return False
    return True


def submit_proposal(
    client: SimulatorClient,
    store: Store,
    workflow: Workflow,
    current: WorkflowConfig,
    draft: ProposalDraft,
) -> ProposalDraft:
    """Persist draft, then route via auto-apply or approval. Idempotent on proposal_id."""
    existing = store.get_proposal(draft.proposal_id)
    if existing is not None and existing["status"] in {
        "submitted",
        "approved",
        "applied",
        "auto_applied",
        "auto_apply_uncertain",
    }:
        draft.status = existing["status"]
        draft.approval_id = existing["approval_id"]
        draft.route = existing["route"]
        return draft

    ok, problems = auto_apply_permitted(
        workflow, current.config, draft.proposed_config, impact_score=draft.impact_score
    )
    if ok:
        # Record the route before the PUT. A crash or a lost response must not
        # look like an approval draft, and must not be retried as a new id.
        draft.route = "auto_apply"
        draft.status = "auto_apply_uncertain"
        draft.error_code = None
        draft.error_detail = None
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
        related_experiment_id=(
            draft.source_ref if draft.source in {"experiment_winner", "experiment_explore"} else None
        ),
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
    # Freeze the pre-promotion config as the decay rollback target.
    # Only winner promotions are monitored for decay; explore/prompt/rollback
    # must not overwrite that meaning with the live (possibly already promoted) config.
    if draft.source == "experiment_winner":
        save_snapshot(
            store,
            workflow_id=current.workflow_id,
            version=current.version,
            config=current.config,
            source="rollback_candidate",
            related_proposal_id=draft.proposal_id,
        )

    if ok:
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
            # Do not replace rollback_candidate with the newly applied config —
            # decay must restore the pre-promotion snapshot frozen at submit time.
            if draft.source_ref and draft.source == "experiment_winner":
                store.update_experiment_promotion(draft.source_ref, "promoted_auto_apply")
            if draft.source_ref and draft.source == "decay_rollback":
                store.update_experiment_promotion(draft.source_ref, "rolled_back")
            if draft.source == CALLER_ID_ROTATION_SOURCE:
                _resolve_matching_caller_rotation(
                    store,
                    draft.workflow_id,
                    draft.proposal_id,
                    resolution=f"Auto-applied caller-id rotation {draft.proposal_id} → v{new_version}",
                )
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
            draft.updated_at = _now()
            if _auto_apply_outcome_ambiguous(exc):
                draft.status = "auto_apply_uncertain"
                draft.route = "auto_apply"
                persist_proposal(store, draft)
                audit(
                    store,
                    kind="auto_apply_uncertain",
                    summary=f"Auto-apply outcome unknown for {draft.proposal_id}: {exc.code}",
                    details={"status": exc.status, "detail": exc.detail},
                    workflow_id=draft.workflow_id,
                    related_proposal_id=draft.proposal_id,
                    idempotency_parts=[draft.proposal_id, "auto_apply_uncertain", exc.code],
                )
                return draft
            draft.status = "failed" if exc.status not in {403, 409} else "blocked_auto_apply"
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
                prior_status = str(payload.get("status") or "")
                # An old lapsed/expired/rejected/applied approval must not be recorded as a
                # freshly submitted active approval for a retry that reused an ID.
                if prior_status in {"lapsed", "expired", "rejected", "applied", "stale"}:
                    approval = _approval_from_api(payload, proposal_id=draft.proposal_id)
                    store.upsert_controller_approval(approval)
                    _sync_proposal_status_from_approval(store, approval)
                    draft.approval_id = approval.approval_id
                    draft.status = prior_status if prior_status != "applied" else "applied"
                    draft.error_code = "duplicate_terminal_approval"
                    draft.error_detail = f"duplicate_proposal_id matched terminal status={prior_status}"
                    draft.updated_at = _now()
                    persist_proposal(store, draft)
                    audit(
                        store,
                        kind="approval_duplicate_terminal",
                        summary=(
                            f"Duplicate proposal_id {draft.proposal_id} matched "
                            f"terminal approval {approval.approval_id} ({prior_status})"
                        ),
                        details={"status": prior_status},
                        workflow_id=draft.workflow_id,
                        related_proposal_id=draft.proposal_id,
                        related_approval_id=approval.approval_id,
                        idempotency_parts=[draft.proposal_id, approval.approval_id, prior_status],
                    )
                    return draft
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
        if row["status"] in TERMINAL_APPROVAL_STATUSES:
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


TERMINAL_APPROVAL_STATUSES = frozenset(
    {"applied", "rejected", "expired", "lapsed", "stale"}
)


def next_advance_minutes(store: Store, configured: int) -> int:
    """Cap advance at 15 minutes while approvals are pending or approved awaiting apply.

    Never increases the configured interval: a 5-minute setting stays 5.
    """
    pending = store.list_controller_approvals(status="pending")
    approved = store.list_controller_approvals(status="approved")
    if pending or approved:
        return min(configured, 15)
    return configured


def _proposal_draft_from_row(
    prop,
    *,
    status: str | None = None,
    approval_id: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> ProposalDraft:
    return ProposalDraft(
        proposal_id=prop["proposal_id"],
        workflow_id=prop["workflow_id"],
        base_version=prop["base_version"],
        status=status if status is not None else prop["status"],
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
        approval_id=approval_id if approval_id is not None else prop["approval_id"],
        route=prop["route"],
        error_code=error_code if error_code is not None else prop["error_code"],
        error_detail=error_detail if error_detail is not None else prop["error_detail"],
        created_at=prop["created_at"],
        updated_at=_now(),
        submitted_at=prop["submitted_at"],
    )


def _mark_experiment_retryable(store: Store, prop, *, status: str) -> None:
    if prop["source"] == "experiment_winner" and prop["source_ref"]:
        store.update_experiment_promotion(prop["source_ref"], status)


def _sync_prompt_attempt_for_proposal(store: Store, proposal_id: str, proposal_status: str) -> None:
    """Release blocking LLM attempts when their related proposal becomes terminal."""
    from controller.services.prompt_incident import sync_attempt_with_proposal_status

    sync_attempt_with_proposal_status(store, proposal_id, proposal_status)


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
        "stale": "stale",
    }
    new_status = mapping.get(approval.status)
    if not new_status:
        return
    draft = _proposal_draft_from_row(prop, status=new_status, approval_id=approval.approval_id)
    persist_proposal(store, draft)
    if draft.source == "experiment_winner" and draft.source_ref:
        if new_status == "rejected":
            store.update_experiment_promotion(draft.source_ref, "promotion_rejected")
        elif new_status == "expired":
            store.update_experiment_promotion(draft.source_ref, "promotion_expired")
        elif new_status == "lapsed":
            store.update_experiment_promotion(draft.source_ref, "promotion_lapsed")
        elif new_status == "stale":
            store.update_experiment_promotion(draft.source_ref, "promotion_stale")
    if new_status in {"stale", "lapsed", "expired", "rejected"}:
        _sync_prompt_attempt_for_proposal(store, draft.proposal_id, new_status)


def _mark_approval_terminal(
    store: Store,
    row,
    *,
    status: str,
    error_code: str | None = None,
    error_detail: str | None = None,
    raw: dict[str, Any] | None = None,
) -> ControllerApproval:
    approval = ControllerApproval(
        approval_id=row["approval_id"],
        proposal_id=row["proposal_id"],
        workflow_id=row["workflow_id"],
        status=status,
        base_version=row["base_version"],
        changed_dims=json.loads(row["changed_dims_json"]) if row["changed_dims_json"] else [],
        created_at=row["created_at"],
        updated_at=_now(),
        expires_at=row["expires_at"],
        apply_by=row["apply_by"],
        decided_at=row["decided_at"],
        reasons=json.loads(row["reasons_json"]) if row["reasons_json"] else None,
        applied_version=row["applied_version"],
        last_reconciled_at=_now(),
        raw=raw,
    )
    store.upsert_controller_approval(approval)
    prop = store.get_proposal(row["proposal_id"])
    if prop is not None:
        draft = _proposal_draft_from_row(
            prop,
            status=status,
            approval_id=row["approval_id"],
            error_code=error_code,
            error_detail=error_detail,
        )
        persist_proposal(store, draft)
        if status == "stale":
            _mark_experiment_retryable(store, prop, status="promotion_stale")
        elif status == "lapsed":
            _mark_experiment_retryable(store, prop, status="promotion_lapsed")
        elif status == "expired":
            _mark_experiment_retryable(store, prop, status="promotion_expired")
        if status in {"stale", "lapsed", "expired", "rejected"}:
            _sync_prompt_attempt_for_proposal(store, prop["proposal_id"], status)
    return approval


def apply_approved(client: SimulatorClient, store: Store) -> list[dict[str, Any]]:
    """Apply locally known approved approvals. Body is only approval_id.

    Processes earliest ``apply_by`` first. Simulator remains the authority on lapse/expiry.
    """
    results: list[dict[str, Any]] = []
    rows = list(store.list_controller_approvals(status="approved"))

    def _apply_by_key(row) -> tuple[int, str]:
        apply_by = row["apply_by"]
        # Missing apply_by sorts last; ISO timestamps sort lexicographically.
        return (1 if not apply_by else 0, str(apply_by or ""))

    rows.sort(key=_apply_by_key)
    for row in rows:
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
            if exc.code == "approval_lapsed":
                try:
                    payload = client.get_approval(row["approval_id"])
                    approval = _approval_from_api(payload, proposal_id=row["proposal_id"])
                    store.upsert_controller_approval(approval)
                    _sync_proposal_status_from_approval(store, approval)
                except SimulatorError:
                    # Simulator said lapsed; do not leave local row stuck on approved.
                    _mark_approval_terminal(
                        store,
                        row,
                        status="lapsed",
                        error_code=exc.code,
                        error_detail=str(exc.detail),
                        raw={"from_apply_error": True},
                    )
            elif exc.code == "not_approved":
                # Trust the simulator's current status — never invent lapsed here.
                try:
                    payload = client.get_approval(row["approval_id"])
                    approval = _approval_from_api(payload, proposal_id=row["proposal_id"])
                    store.upsert_controller_approval(approval)
                    _sync_proposal_status_from_approval(store, approval)
                except SimulatorError:
                    pass
            elif exc.code == "stale_base_version":
                detail = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
                current_version = detail.get("current_version") if isinstance(detail, dict) else None
                _mark_approval_terminal(
                    store,
                    row,
                    status="stale",
                    error_code=exc.code,
                    error_detail=str(exc.detail),
                    raw={"from_apply_error": True, "detail": detail},
                )
                audit(
                    store,
                    kind="approval_stale",
                    summary=(
                        f"Approval {row['approval_id']} stale vs live config "
                        f"(base={row['base_version']} current={current_version})"
                    ),
                    details={
                        "approval_id": row["approval_id"],
                        "proposal_id": row["proposal_id"],
                        "old_base_version": row["base_version"],
                        "current_version": current_version,
                        "detail": detail,
                    },
                    workflow_id=row["workflow_id"],
                    related_approval_id=row["approval_id"],
                    related_proposal_id=row["proposal_id"],
                    idempotency_parts=[row["approval_id"], "stale", row["base_version"], current_version],
                )
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
            # post_apply records what went live; rollback_candidate stays the pre-promotion safe config.
            if prop["source"] == "experiment_winner" and prop["source_ref"]:
                store.update_experiment_promotion(prop["source_ref"], "promoted")
            if prop["source"] == "decay_rollback" and prop["source_ref"]:
                store.update_experiment_promotion(prop["source_ref"], "rolled_back")
            if prop["source"] == CALLER_ID_ROTATION_SOURCE:
                _resolve_matching_caller_rotation(
                    store,
                    row["workflow_id"],
                    row["proposal_id"],
                    resolution=f"Applied approval {row['approval_id']} → v{result.get('version')}",
                )
            elif prop["source"] in {
                "experiment_winner",
                "experiment_explore",
                "decay_rollback",
                "incident_mitigation",
            }:
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


def propose_experiment_exploration(
    client: SimulatorClient,
    store: Store,
    workflows: list[Workflow],
    configs: list[WorkflowConfig],
    experiments: list[ExperimentState],
) -> list[ProposalDraft]:
    """Submit planned exploration allocations for underpowered experiments (one open explore per workflow)."""
    by_wf = {w.workflow_id: w for w in workflows}
    by_cfg = {c.workflow_id: c for c in configs}
    out: list[ProposalDraft] = []
    seen_wf: set[str] = set()
    for exp in experiments:
        if workflow_has_uncertain_auto_apply(store, exp.workflow_id):
            seen_wf.add(exp.workflow_id)
            continue
        if exp.workflow_id in seen_wf:
            continue
        if exp.decision not in {"underpowered", "inconclusive"} and exp.status not in {"planned", "running"}:
            continue
        if exp.decision == "winner":
            continue
        # A promoted or decaying experiment is not an exploration candidate.
        # Replanning must not replace that status with exploration_submitted.
        if exp.promotion_status in STICKY_PROMOTION_STATUSES:
            continue
        # Skip if an explore/winner proposal is already open for this experiment.
        open_props = [
            p
            for p in store.recent_proposals_for_workflow(exp.workflow_id, limit=50)
            if p["status"] in {"draft", "submitted", "approved"}
            and p["source"] in {"experiment_explore", "experiment_winner"}
            and p["source_ref"] == exp.experiment_id
        ]
        if open_props:
            seen_wf.add(exp.workflow_id)
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
                priority=5,
                reason=attn_reason,
            )
            continue
        try:
            live = client.workflow_config(exp.workflow_id)
        except SimulatorError:
            live = config
        draft = build_explore_proposal(store, workflow, live, exp)
        if draft is None:
            continue
        submitted = submit_proposal(client, store, workflow, live, draft)
        out.append(submitted)
        seen_wf.add(exp.workflow_id)
        if submitted.status in {"submitted", "approved", "auto_applied", "applied"}:
            exp.promotion_status = "exploration_submitted"
            store.upsert_experiment(exp)
    return out


# Once an experiment is promoted, decaying, or rolled back, do not re-submit the
# same winner evidence as a new promotion. Stale/lapsed/expired are retryable and
# intentionally omitted so a fresh proposal can be built after replan.
BLOCK_WINNER_REPROMOTE_STATUSES = frozenset(
    {
        "promoted",
        "promoted_auto_apply",
        "proposal_submitted",
        "decay_detected",
        "rolled_back",
        "promotion_rejected",
    }
)

ACTIVE_WINNER_PROPOSAL_STATUSES = frozenset({"draft", "submitted", "approved"})


def _active_winner_proposal(store: Store, experiment_id: str):
    for prop in store.list_proposals():
        if (
            prop["source"] == "experiment_winner"
            and prop["source_ref"] == experiment_id
            and prop["status"] in ACTIVE_WINNER_PROPOSAL_STATUSES
        ):
            return prop
    return None


def _draft_from_stored_proposal(prop) -> ProposalDraft:
    return _proposal_draft_from_row(prop)


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
        if workflow_has_uncertain_auto_apply(store, exp.workflow_id):
            continue
        if exp.promotion_status in BLOCK_WINNER_REPROMOTE_STATUSES:
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
        # Only one active winner proposal per experiment (crash-safe resume).
        try:
            live = client.workflow_config(exp.workflow_id)
        except SimulatorError:
            live = config
        active = _active_winner_proposal(store, exp.experiment_id)
        if active is not None:
            if active["status"] == "draft":
                if int(active["base_version"]) != int(live.version):
                    # Live config moved on — do not POST the outdated draft body.
                    outdated = _draft_from_stored_proposal(active)
                    outdated.status = "stale"
                    outdated.error_code = "stale_base_version"
                    outdated.error_detail = (
                        f"draft base_version={active['base_version']} live={live.version}"
                    )
                    outdated.updated_at = _now()
                    persist_proposal(store, outdated)
                    # Fall through and rebuild against live config with a fresh proposal ID.
                else:
                    submitted = submit_proposal(
                        client, store, workflow, live, _draft_from_stored_proposal(active)
                    )
                    out.append(submitted)
                    continue
            else:
                # submitted/approved — leave the in-flight proposal alone.
                continue
        # Always rebuild against the live config — never reuse a stale/lapsed body.
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
        if workflow_has_uncertain_auto_apply(store, hit["workflow_id"]):
            continue
        already = [
            p
            for p in store.recent_proposals_for_workflow(hit["workflow_id"], limit=50)
            if p["source"] == "decay_rollback"
            and p["source_ref"] == hit.get("experiment_id")
            and p["status"] in {"draft", "submitted", "approved", "applied", "auto_applied"}
        ]
        if already:
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
        if draft.status == "conflict":
            # Record the clash for a person to review. Do not POST a config change.
            persist_proposal(store, draft)
            audit(
                store,
                kind="rollback_conflict",
                summary=draft.change_summary,
                details=draft.diff,
                workflow_id=draft.workflow_id,
                related_proposal_id=draft.proposal_id,
                related_experiment_id=draft.source_ref,
                idempotency_parts=[draft.proposal_id, "conflict"],
            )
            out.append(draft)
            continue
        submitted = submit_proposal(client, store, workflow, live, draft)
        out.append(submitted)
    return out


_ACTIVE_CALLER_ROTATION = frozenset({"submitted", "approved"})


def _resolve_matching_caller_rotation(
    store: Store,
    workflow_id: str,
    proposal_id: str,
    *,
    resolution: str,
) -> None:
    """Close only the incident linked to this rotation. Other open incidents stay open."""
    for incident in find_open_incidents_for_workflow(store, workflow_id):
        if incident.related_proposal_id == proposal_id:
            resolve_incident(store, incident.incident_id, resolution=resolution)


def _caller_rotation_rows(store: Store, incident_id: str) -> list[Any]:
    return [
        prop
        for prop in store.list_proposals()
        if prop["source"] == CALLER_ID_ROTATION_SOURCE and prop["source_ref"] == incident_id
    ]


def _workflow_has_active_caller_rotation(store: Store, workflow_id: str) -> bool:
    return any(
        prop["source"] == CALLER_ID_ROTATION_SOURCE and prop["status"] in _ACTIVE_CALLER_ROTATION
        for prop in store.list_proposals(workflow_id)
    )


def _skip_caller_rotation(
    store: Store,
    workflow: Workflow,
    incident_id: str,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    audit(
        store,
        kind="caller_id_rotation_skipped",
        summary=f"Skipped caller-id rotation for {incident_id}: {reason}",
        details={"reason": reason, **(details or {})},
        workflow_id=workflow.workflow_id,
        tenant_id=workflow.tenant_id,
        related_incident_id=incident_id,
        idempotency_parts=[incident_id, reason],
    )


def _release_stale_caller_draft(store: Store, prop, live: WorkflowConfig) -> None:
    outdated = _draft_from_stored_proposal(prop)
    outdated.status = "stale"
    outdated.error_code = "stale_base_version"
    outdated.error_detail = (
        f"draft base_version={prop['base_version']} live={live.version}; proposal was not on the simulator"
    )
    outdated.updated_at = _now()
    persist_proposal(store, outdated)


def _configs_match(proposed: dict[str, Any], live: dict[str, Any]) -> bool:
    """Full-config equality using the same fields and weight normalisation as diffs.

    Allocation cells are compared after dropping non-positive weights and
    renormalising, within the same tolerance used for rollback conflict checks.
    Prompt, cohorts, and schedule stay exact.
    """
    if proposed.get("prompt_version") != live.get("prompt_version"):
        return False
    if json.dumps(proposed.get("cohorts") or [], sort_keys=True) != json.dumps(
        live.get("cohorts") or [], sort_keys=True
    ):
        return False
    if json.dumps(proposed.get("schedule") or {}, sort_keys=True) != json.dumps(
        live.get("schedule") or {}, sort_keys=True
    ):
        return False
    proposed_alloc = proposed.get("allocations") or {}
    live_alloc = live.get("allocations") or {}
    if not isinstance(proposed_alloc, dict) or not isinstance(live_alloc, dict):
        return False
    if set(proposed_alloc) != set(live_alloc):
        return False
    for cohort in proposed_alloc:
        for dim in ALLOCATION_DIMS:
            if not _weights_match(
                _allocation_weights(proposed, str(cohort), dim),
                _allocation_weights(live, str(cohort), dim),
            ):
                return False
    return True


def _auto_apply_is_proven(live: WorkflowConfig, prop) -> bool:
    """True only when this row was sent through auto-apply and the live config is that proposal.

    A newer version, or a matching caller-id cell, is not enough. An approval
    draft is never treated as auto-applied.
    """
    if prop["route"] != "auto_apply":
        return False
    try:
        if int(live.version) <= int(prop["base_version"]):
            return False
    except (TypeError, ValueError):
        return False
    try:
        proposed = json.loads(prop["proposed_config_json"])
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(proposed, dict):
        return False
    return _configs_match(proposed, live.config)


def _snapshot_recovered_caller_apply(
    store: Store,
    workflow: Workflow,
    proposal_id: str,
    version: int,
    config: dict[str, Any],
) -> None:
    save_snapshot(
        store,
        workflow_id=workflow.workflow_id,
        version=int(version),
        config=config,
        source="post_apply",
        related_proposal_id=proposal_id,
    )


def _finish_recovered_remote_apply(
    store: Store,
    workflow: Workflow,
    prop,
    approval: ControllerApproval,
    live: WorkflowConfig,
) -> None:
    """A remote approval already applied; finish the local proposal and its incident."""
    version = approval.applied_version
    if version is None and int(live.version) > int(prop["base_version"]):
        version = int(live.version)
    proposed = json.loads(prop["proposed_config_json"])
    if version is not None and isinstance(proposed, dict):
        _snapshot_recovered_caller_apply(
            store, workflow, str(prop["proposal_id"]), int(version), proposed
        )
    _resolve_matching_caller_rotation(
        store,
        workflow.workflow_id,
        str(prop["proposal_id"]),
        resolution=f"Recovered applied approval {approval.approval_id} → v{version}",
    )


def _record_recovered_auto_apply(
    store: Store,
    workflow: Workflow,
    prop,
    live: WorkflowConfig,
) -> ProposalDraft:
    """The simulator already has this draft's rotation; record it without shifting again."""
    draft = _draft_from_stored_proposal(prop)
    draft.status = "auto_applied"
    draft.route = "auto_apply"
    draft.error_code = None
    draft.error_detail = None
    draft.submitted_at = draft.submitted_at or _now()
    draft.updated_at = _now()
    persist_proposal(store, draft)
    _snapshot_recovered_caller_apply(
        store, workflow, draft.proposal_id, int(live.version), live.config
    )
    _resolve_matching_caller_rotation(
        store,
        workflow.workflow_id,
        draft.proposal_id,
        resolution=f"Recovered auto-applied caller-id rotation {draft.proposal_id} → v{live.version}",
    )
    audit(
        store,
        kind="caller_id_auto_apply_recovered",
        summary=(
            f"Recovered auto-applied caller-id rotation {draft.proposal_id} "
            f"already on v{live.version}"
        ),
        details={"version": int(live.version), "base_version": prop["base_version"]},
        workflow_id=workflow.workflow_id,
        tenant_id=workflow.tenant_id,
        related_proposal_id=draft.proposal_id,
        related_incident_id=prop["source_ref"],
        idempotency_parts=[draft.proposal_id, "auto_apply_recovered", int(live.version)],
    )
    return draft


def _sync_remote_caller_proposal(
    store: Store,
    workflow: Workflow,
    prop,
    remote: list[dict[str, Any]],
    live: WorkflowConfig,
) -> ProposalDraft | None:
    matches = [row for row in remote if row.get("proposal_id") == prop["proposal_id"]]
    if not matches:
        return None
    approval = _approval_from_api(matches[0], proposal_id=str(prop["proposal_id"]))
    store.upsert_controller_approval(approval)
    _sync_proposal_status_from_approval(store, approval)
    if approval.status == "applied":
        _finish_recovered_remote_apply(store, workflow, prop, approval, live)
    stored = store.get_proposal(prop["proposal_id"])
    if stored is None:
        return None
    audit(
        store,
        kind="caller_id_rotation_resumed",
        summary=f"Resumed caller-id proposal {prop['proposal_id']} from simulator status {approval.status}",
        details={"approval_id": approval.approval_id, "status": approval.status},
        workflow_id=workflow.workflow_id,
        tenant_id=workflow.tenant_id,
        related_proposal_id=str(prop["proposal_id"]),
        related_approval_id=approval.approval_id,
        related_incident_id=prop["source_ref"],
        idempotency_parts=[prop["proposal_id"], approval.approval_id, "resumed"],
    )
    return _draft_from_stored_proposal(stored)


def _recover_stranded_caller_drafts(
    client: SimulatorClient,
    store: Store,
    workflow: Workflow,
    live: WorkflowConfig,
    drafts: list[Any],
    unconfirmed: list[Any] | None = None,
) -> tuple[str, list[ProposalDraft], str | None]:
    """Resume a local draft or an uncertain submission, or resubmit the same id.

    Outcome is "resumed" when an in-flight or already-applied proposal exists,
    "rejected" when the simulator already rejected that id, "unconfirmed" when a
    submission is still missing remotely and must not be replaced, "released"
    when a draft was stale and a fresh id may be built, or "lookup_failed" when
    the remote list could not be read.
    """
    try:
        remote = client.list_approvals(workflow_id=workflow.workflow_id)
    except SimulatorError as exc:
        return "lookup_failed", [], exc.code
    resumed: list[ProposalDraft] = []
    for prop in unconfirmed or []:
        synced = _sync_remote_caller_proposal(store, workflow, prop, remote, live)
        if synced is None:
            audit(
                store,
                kind="caller_id_rotation_unconfirmed",
                summary=(
                    f"Caller-id proposal {prop['proposal_id']} was submitted but is "
                    "not on the simulator yet"
                ),
                details={"proposal_id": prop["proposal_id"], "status": "submitted_unconfirmed"},
                workflow_id=workflow.workflow_id,
                tenant_id=workflow.tenant_id,
                related_proposal_id=str(prop["proposal_id"]),
                related_incident_id=prop["source_ref"],
                idempotency_parts=[prop["proposal_id"], "unconfirmed"],
            )
            return "unconfirmed", resumed, None
        resumed.append(synced)
    if any(draft.status == "rejected" for draft in resumed):
        return "rejected", resumed, None
    if any(draft.status in {"submitted", "approved", "applied", "auto_applied"} for draft in resumed):
        return "resumed", resumed, None
    for prop in drafts:
        synced = _sync_remote_caller_proposal(store, workflow, prop, remote, live)
        if synced is not None:
            resumed.append(synced)
            continue
        if int(prop["base_version"]) != int(live.version):
            _release_stale_caller_draft(store, prop, live)
            continue
        submitted = submit_proposal(client, store, workflow, live, _draft_from_stored_proposal(prop))
        resumed.append(submitted)
    if any(draft.status == "rejected" for draft in resumed):
        return "rejected", resumed, None
    if any(draft.status in {"submitted", "approved", "applied", "auto_applied"} for draft in resumed):
        return "resumed", resumed, None
    return "released", [], None


def propose_caller_id_rotations(
    client: SimulatorClient,
    store: Store,
    workflows: list[Workflow],
    configs: list[WorkflowConfig],
) -> list[ProposalDraft]:
    """One governed rotation per workflow for an open caller_id_health incident.

    A pending proposal is left alone. A local draft is resumed from the simulator
    when that proposal id was already accepted. An approval that is already
    applied finishes the local proposal and only the linked incident. An
    auto-apply whose outcome is still unknown is reconciled against the live
    config and is not retried or replaced. It is recorded as applied only when
    that row went through the auto-apply route, the live version moved past the
    proposal base, and the full live config is the proposed config. An uncertain
    submission is looked up before any new id is created. Human rejection is
    not retried. A stale, lapsed, or expired proposal is rebuilt from the live
    config with a new id. The passed configs are not a substitute for a failed
    live config read.
    """
    del configs  # Live config must come from the simulator on this path.
    out: list[ProposalDraft] = []
    for workflow in workflows:
        incidents = [
            incident
            for incident in find_open_incidents_for_workflow(store, workflow.workflow_id)
            if incident.signal_type == "caller_id_health"
        ]
        if not incidents:
            continue
        if _workflow_has_active_caller_rotation(store, workflow.workflow_id):
            continue

        def _incident_sort(incident: Any) -> tuple[bool, float, str]:
            raw = incident.evidence.get("recent_value") if isinstance(incident.evidence, dict) else None
            try:
                value = float(raw) if raw is not None else 0.0
            except (TypeError, ValueError):
                value = 0.0
            return (raw is None, value, incident.incident_id)

        incidents.sort(key=_incident_sort)
        try:
            live = client.workflow_config(workflow.workflow_id)
        except SimulatorError as exc:
            for incident in incidents:
                _skip_caller_rotation(
                    store,
                    workflow,
                    incident.incident_id,
                    "live_config_unavailable",
                    {"code": exc.code, "endpoint": exc.endpoint},
                )
            continue
        for incident in incidents:
            rows = _caller_rotation_rows(store, incident.incident_id)
            if any(prop["status"] == "rejected" for prop in rows):
                _skip_caller_rotation(store, workflow, incident.incident_id, "human_rejected")
                continue
            settled = [prop for prop in rows if prop["status"] in {"applied", "auto_applied"}]
            if settled:
                for prop in settled:
                    _resolve_matching_caller_rotation(
                        store,
                        workflow.workflow_id,
                        str(prop["proposal_id"]),
                        resolution=(
                            f"Recovered caller-id rotation {prop['proposal_id']} "
                            f"already {prop['status']}"
                        ),
                    )
                break
            ok_attn, attn_reason = _approver_attention_ok(store, workflow)
            if not ok_attn:
                from controller.services.fairness import ACTION_GOVERNANCE, FairScheduler

                FairScheduler(store).record_deferral(
                    workflow_id=workflow.workflow_id,
                    tenant_id=workflow.tenant_id,
                    action_kind=ACTION_GOVERNANCE,
                    priority=8,
                    reason=attn_reason,
                )
                break
            uncertain = [prop for prop in rows if prop["status"] == "auto_apply_uncertain"]
            if uncertain:
                proven = [prop for prop in uncertain if _auto_apply_is_proven(live, prop)]
                if proven:
                    out.extend(
                        _record_recovered_auto_apply(store, workflow, prop, live) for prop in proven
                    )
                    break
                for prop in uncertain:
                    audit(
                        store,
                        kind="caller_id_auto_apply_uncertain",
                        summary=(
                            f"Caller-id auto-apply {prop['proposal_id']} is not confirmed "
                            "on the live config"
                        ),
                        details={
                            "base_version": prop["base_version"],
                            "live_version": live.version,
                            "route": prop["route"],
                        },
                        workflow_id=workflow.workflow_id,
                        tenant_id=workflow.tenant_id,
                        related_proposal_id=str(prop["proposal_id"]),
                        related_incident_id=prop["source_ref"],
                        idempotency_parts=[prop["proposal_id"], "auto_apply_unreconciled"],
                    )
                break
            if workflow_has_uncertain_auto_apply(store, workflow.workflow_id):
                break
            drafts = [prop for prop in rows if prop["status"] == "draft"]
            unconfirmed = [prop for prop in rows if prop["status"] == "submitted_unconfirmed"]
            if drafts or unconfirmed:
                outcome, recovered, lookup_code = _recover_stranded_caller_drafts(
                    client, store, workflow, live, drafts, unconfirmed
                )
                if outcome == "lookup_failed":
                    _skip_caller_rotation(
                        store,
                        workflow,
                        incident.incident_id,
                        "remote_approval_lookup_failed",
                        {"code": lookup_code},
                    )
                    break
                if outcome == "rejected":
                    _skip_caller_rotation(store, workflow, incident.incident_id, "human_rejected")
                    continue
                if outcome == "unconfirmed":
                    break
                if outcome == "resumed":
                    out.extend(recovered)
                    break
            built = build_caller_id_rotation_proposal(store, workflow, live, incident)
            if built.draft is None:
                _skip_caller_rotation(
                    store,
                    workflow,
                    incident.incident_id,
                    built.reason or "skipped",
                    built.details,
                )
                continue
            mark_incident_mitigating(store, incident.incident_id, built.draft.proposal_id)
            submitted = submit_proposal(client, store, workflow, live, built.draft)
            out.append(submitted)
            # One allocation change per cycle. A second incident waits until this one settles.
            break
    return out


def workflow_has_uncertain_auto_apply(store: Store, workflow_id: str) -> bool:
    """True while a config write for this workflow may already be live."""
    return any(
        prop["status"] == "auto_apply_uncertain" for prop in store.list_proposals(workflow_id)
    )


_EXPERIMENT_STATUS_AFTER_AUTO_APPLY = {
    "experiment_winner": "promoted_auto_apply",
    "experiment_explore": "exploration_submitted",
    "decay_rollback": "rolled_back",
}


def _record_recovered_uncertain_auto_apply(
    store: Store,
    workflow: Workflow | None,
    prop: Any,
    live: WorkflowConfig,
) -> ProposalDraft:
    """Record one proven auto-apply on its original proposal. Never sends a PUT."""
    draft = _draft_from_stored_proposal(prop)
    draft.status = "auto_applied"
    draft.route = "auto_apply"
    draft.error_code = None
    draft.error_detail = None
    draft.submitted_at = draft.submitted_at or _now()
    draft.updated_at = _now()
    persist_proposal(store, draft)
    if workflow is not None:
        _snapshot_recovered_caller_apply(
            store, workflow, draft.proposal_id, int(live.version), live.config
        )
    else:
        save_snapshot(
            store,
            workflow_id=draft.workflow_id,
            version=int(live.version),
            config=live.config,
            source="post_apply",
            related_proposal_id=draft.proposal_id,
        )
    source = str(prop["source"] or "")
    source_ref = prop["source_ref"]
    promotion = _EXPERIMENT_STATUS_AFTER_AUTO_APPLY.get(source)
    if promotion and source_ref:
        store.update_experiment_promotion(str(source_ref), promotion)
    if source == CALLER_ID_ROTATION_SOURCE:
        _resolve_matching_caller_rotation(
            store,
            draft.workflow_id,
            draft.proposal_id,
            resolution=f"Recovered auto-applied rotation {draft.proposal_id} → v{live.version}",
        )
    audit(
        store,
        kind="auto_apply_recovered",
        summary=f"Recovered auto-applied {source} proposal {draft.proposal_id} → v{live.version}",
        details={
            "source": source,
            "proposal_id": draft.proposal_id,
            "base_version": prop["base_version"],
            "version": int(live.version),
        },
        workflow_id=draft.workflow_id,
        tenant_id=workflow.tenant_id if workflow is not None else None,
        related_proposal_id=draft.proposal_id,
        related_experiment_id=(
            str(source_ref) if promotion and source_ref else None
        ),
        related_incident_id=(
            str(source_ref) if source == CALLER_ID_ROTATION_SOURCE and source_ref else None
        ),
        idempotency_parts=[draft.proposal_id, "auto_apply_recovered", int(live.version)],
    )
    return draft


def reconcile_uncertain_auto_applies(
    client: SimulatorClient,
    store: Store,
    workflows: list[Workflow],
) -> list[ProposalDraft]:
    """Prove or hold every auto-apply whose response was lost.

    A proven row stays the original proposal and becomes auto_applied. An
    unproven row, or a workflow whose live config cannot be read, stays
    auto_apply_uncertain. This never sends another PUT and never creates a
    replacement proposal.
    """
    by_wf = {workflow.workflow_id: workflow for workflow in workflows}
    grouped: dict[str, list[Any]] = {}
    for prop in store.list_proposals(status="auto_apply_uncertain"):
        grouped.setdefault(str(prop["workflow_id"]), []).append(prop)
    recovered: list[ProposalDraft] = []
    for workflow_id, props in grouped.items():
        workflow = by_wf.get(workflow_id)
        try:
            live = client.workflow_config(workflow_id)
        except SimulatorError as exc:
            for prop in props:
                audit(
                    store,
                    kind="auto_apply_uncertain_unreconciled",
                    summary=(
                        f"Auto-apply {prop['proposal_id']} stays uncertain; "
                        "live config could not be read"
                    ),
                    details={
                        "source": prop["source"],
                        "proposal_id": prop["proposal_id"],
                        "reason": "live_config_unavailable",
                        "code": exc.code,
                    },
                    workflow_id=workflow_id,
                    tenant_id=workflow.tenant_id if workflow is not None else None,
                    related_proposal_id=str(prop["proposal_id"]),
                    idempotency_parts=[prop["proposal_id"], "live_config_unavailable"],
                )
            continue
        for prop in props:
            if _auto_apply_is_proven(live, prop):
                recovered.append(
                    _record_recovered_uncertain_auto_apply(store, workflow, prop, live)
                )
                continue
            audit(
                store,
                kind="auto_apply_uncertain_unreconciled",
                summary=f"Auto-apply {prop['proposal_id']} is not confirmed on the live config",
                details={
                    "source": prop["source"],
                    "proposal_id": prop["proposal_id"],
                    "reason": "not_proven",
                    "base_version": prop["base_version"],
                    "live_version": live.version,
                    "route": prop["route"],
                },
                workflow_id=workflow_id,
                tenant_id=workflow.tenant_id if workflow is not None else None,
                related_proposal_id=str(prop["proposal_id"]),
                idempotency_parts=[prop["proposal_id"], "not_proven"],
            )
    return recovered


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

    recovered_applies = reconcile_uncertain_auto_applies(client, store, workflows)
    report.auto_applied += len(recovered_applies)
    if recovered_applies:
        report.notes.append(f"auto_apply_recovered={len(recovered_applies)}")

    explorations = propose_experiment_exploration(client, store, workflows, configs, experiments)
    promotions = promote_experiment_winners(client, store, workflows, configs, experiments)
    built = explorations + promotions
    report.proposals_built = len(built)
    report.proposals_submitted = sum(
        1 for p in built if p.status in {"submitted", "approved", "auto_applied", "applied"}
    )
    report.auto_applied = sum(1 for p in built if p.status == "auto_applied")
    report.incidents_linked = sum(1 for p in promotions if p.approval_id or p.status == "auto_applied")
    if explorations:
        report.notes.append(f"exploration_proposals={len(explorations)}")

    rollbacks = propose_decay_rollbacks(client, store, workflows, configs, decay_hits or [])
    report.rollbacks_proposed = len(rollbacks)

    rotations = propose_caller_id_rotations(client, store, workflows, configs)
    report.proposals_built += len(rotations)
    report.proposals_submitted += sum(
        1 for prop in rotations if prop.status in {"submitted", "approved", "auto_applied", "applied"}
    )
    report.auto_applied += sum(1 for prop in rotations if prop.status == "auto_applied")
    if rotations:
        report.notes.append(f"caller_id_rotations={len(rotations)}")

    # Second reconcile/apply pass for anything that became approved mid-cycle (rare without advance).
    reconcile_approvals(client, store)
    report.applied += len(apply_approved(client, store))
    return report
