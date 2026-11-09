"""Phase 7 hallucination incident loop: suggest-fixes → validation → governed proposal.

LLM output is untrusted. Winner/statistical decisions never consult the LLM.
Paid LLM/validation and proposal submission are skipped when mutate=False (--no-governance).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from controller.api.client import SimulatorClient, SimulatorError
from controller.api.models import SimState, Workflow, WorkflowConfig
from controller.persistence.repositories import Store
from controller.services.audit import audit
from controller.services.change_builder import build_prompt_proposal
from controller.services.credits import CreditBudget
from controller.services.fairness import ACTION_PROMPT_INCIDENT, FairScheduler
from controller.services.governance import (
    reconcile_uncertain_auto_applies,
    submit_proposal,
    workflow_has_uncertain_auto_apply,
)
from controller.services.incidents import (
    Incident,
    mark_incident_mitigating,
    resolve_incident,
    _row_to_incident,
)
from controller.services.observability import bump_counter

HALLUCINATION_SIGNAL = "hallucination_spike"
DEFAULT_VALIDATION_N = 200
SUGGEST_CREDIT_COST = 3
# Statuses that block a new suggestion attempt for the same incident.
BLOCKING_ATTEMPT_STATUSES = frozenset(
    {
        "pending",
        "suggested",
        "validating",
        "validated",
        "proposed",
        "submitted",
        "no_safe_candidate",
        "proposal_rejected",
    }
)
# Retry only after these terminal-but-retryable failures (e.g. credits returned later,
# or a related approval became stale/lapsed/expired without human rejection).
RETRYABLE_ATTEMPT_STATUSES = frozenset(
    {
        "credit_exhausted",
        "transport_error",
        "failed_retryable",
        "proposal_stale",
        "proposal_lapsed",
        "proposal_expired",
    }
)


def sync_attempt_with_proposal_status(store: Store, proposal_id: str, proposal_status: str) -> None:
    """When a prompt proposal ends, release or lock the related LLM attempt accordingly."""
    mapping = {
        "stale": "proposal_stale",
        "lapsed": "proposal_lapsed",
        "expired": "proposal_expired",
        "rejected": "proposal_rejected",
    }
    attempt_status = mapping.get(proposal_status)
    if attempt_status is None:
        return
    for row in store.list_llm_attempts():
        if row["related_proposal_id"] != proposal_id:
            continue
        if row["status"] in {"submitted", "proposed"}:
            attempt = LlmAttempt(
                attempt_id=row["attempt_id"],
                incident_id=row["incident_id"],
                workflow_id=row["workflow_id"],
                status=attempt_status,
                problem=row["problem"],
                created_at=row["created_at"],
                updated_at=_now(),
                request=json.loads(row["request_json"]) if row["request_json"] else None,
                response=json.loads(row["response_json"]) if row["response_json"] else None,
                error_code=f"proposal_{proposal_status}",
                error_detail=f"related proposal {proposal_id} became {proposal_status}",
                credits_before=row["credits_before"],
                credits_after=row["credits_after"],
                selected_prompt_version=row["selected_prompt_version"],
                selected_validation_run_id=row["selected_validation_run_id"],
                related_proposal_id=row["related_proposal_id"],
            )
            store.upsert_llm_attempt(attempt)


@dataclass
class LlmAttempt:
    attempt_id: str
    incident_id: str
    workflow_id: str
    status: str
    problem: str
    created_at: str
    updated_at: str
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    error_code: str | None = None
    error_detail: str | None = None
    credits_before: float | None = None
    credits_after: float | None = None
    selected_prompt_version: str | None = None
    selected_validation_run_id: str | None = None
    related_proposal_id: str | None = None


@dataclass
class ValidationRecord:
    record_id: str
    attempt_id: str
    incident_id: str
    workflow_id: str
    prompt_version: str
    n: int
    status: str
    created_at: str
    updated_at: str
    validation_run_id: str | None = None
    pass_rate: float | None = None
    failures: int | None = None
    credits_after: float | None = None
    error_code: str | None = None
    error_detail: str | None = None
    meets_threshold: bool = False
    raw: dict[str, Any] | None = None


@dataclass
class PromptIncidentReport:
    eligible: int = 0
    attempted: int = 0
    suggested: int = 0
    validated: int = 0
    proposed: int = 0
    submitted: int = 0
    skipped_no_mutate: int = 0
    skipped_duplicate: int = 0
    credit_blocked: int = 0
    failures: int = 0
    notes: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validation_credit_cost(n: int) -> int:
    return int(math.ceil(n / 100))


def attempt_id_for(incident_id: str, *, seq: int = 1) -> str:
    digest = hashlib.sha256(f"{incident_id}|suggest|{seq}".encode("utf-8")).hexdigest()[:20]
    return f"llm_{digest}"


def validation_record_id(attempt_id: str, prompt_version: str) -> str:
    digest = hashlib.sha256(f"{attempt_id}|{prompt_version}".encode("utf-8")).hexdigest()[:20]
    return f"valrec_{digest}"


def eligible_hallucination_incidents(store: Store) -> list[Incident]:
    rows = store.list_incidents()
    out: list[Incident] = []
    for row in rows:
        if row["signal_type"] != HALLUCINATION_SIGNAL:
            continue
        if row["status"] not in {"detected", "investigating", "mitigating"}:
            continue
        out.append(_row_to_incident(row))
    return out


def _latest_attempt(store: Store, incident_id: str):
    rows = store.list_llm_attempts(incident_id)
    return rows[0] if rows else None


def should_start_attempt(store: Store, incident: Incident) -> tuple[bool, str]:
    """Decide whether a new LLM suggestion call is justified."""
    if incident.related_proposal_id:
        prop = store.get_proposal(incident.related_proposal_id)
        if prop is not None and prop["status"] in {
            "draft",
            "submitted",
            "approved",
            "applied",
            "auto_applied",
        }:
            return False, "proposal_already_in_flight_or_applied"
    latest = _latest_attempt(store, incident.incident_id)
    if latest is None:
        return True, "no_prior_attempt"
    status = latest["status"]
    if status in BLOCKING_ATTEMPT_STATUSES:
        return False, f"prior_attempt_{status}"
    if status in RETRYABLE_ATTEMPT_STATUSES:
        return True, f"retry_after_{status}"
    return False, f"prior_attempt_{status}"


def _resume_proposed_attempts(
    client: SimulatorClient,
    store: Store,
    workflows: list[Workflow],
    report: PromptIncidentReport,
) -> None:
    """Crash recovery: finish governance submit for attempts stuck in proposed."""
    by_wf = {w.workflow_id: w for w in workflows}
    for row in store.list_llm_attempts():
        if row["status"] != "proposed" or not row["related_proposal_id"]:
            continue
        prop = store.get_proposal(row["related_proposal_id"])
        if prop is None:
            continue
        if prop["status"] not in {"draft", "failed", "blocked_auto_apply"}:
            continue
        if workflow_has_uncertain_auto_apply(store, row["workflow_id"]):
            continue
        workflow = by_wf.get(row["workflow_id"])
        if workflow is None:
            continue
        try:
            live = client.workflow_config(row["workflow_id"])
        except SimulatorError:
            continue
        from controller.services.change_builder import ProposalDraft
        from controller.services.governance import persist_proposal

        draft = ProposalDraft(
            proposal_id=prop["proposal_id"],
            workflow_id=prop["workflow_id"],
            base_version=prop["base_version"],
            status=prop["status"],
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
            approval_id=prop["approval_id"],
            route=prop["route"],
            error_code=prop["error_code"],
            error_detail=prop["error_detail"],
            created_at=prop["created_at"],
            updated_at=prop["updated_at"],
            submitted_at=prop["submitted_at"],
        )
        persist_proposal(store, draft)
        submitted = submit_proposal(client, store, workflow, live, draft)
        attempt = LlmAttempt(
            attempt_id=row["attempt_id"],
            incident_id=row["incident_id"],
            workflow_id=row["workflow_id"],
            status="submitted" if submitted.status in {"submitted", "approved", "auto_applied", "applied"} else "proposed",
            problem=row["problem"],
            request=json.loads(row["request_json"]) if row["request_json"] else None,
            response=json.loads(row["response_json"]) if row["response_json"] else None,
            error_code=submitted.error_code,
            error_detail=submitted.error_detail,
            credits_before=row["credits_before"],
            credits_after=row["credits_after"],
            selected_prompt_version=row["selected_prompt_version"],
            selected_validation_run_id=row["selected_validation_run_id"],
            related_proposal_id=submitted.proposal_id,
            created_at=row["created_at"],
            updated_at=_now(),
        )
        store.upsert_llm_attempt(attempt)
        if attempt.status == "submitted":
            report.submitted += 1
        report.notes.append(f"resumed proposal submit for {row['attempt_id']} → {submitted.status}")


def _parse_suggestions(payload: dict[str, Any], allowed: set[str]) -> list[dict[str, str]]:
    raw = payload.get("suggestions")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        pv = item.get("prompt_version")
        if not isinstance(pv, str) or not pv or pv not in allowed or pv in seen:
            continue
        seen.add(pv)
        summary = item.get("summary")
        out.append({"prompt_version": pv, "summary": str(summary) if summary is not None else ""})
    return out


def select_validated_candidate(
    records: list[ValidationRecord],
    *,
    min_pass_rate: float,
) -> ValidationRecord | None:
    """Deterministic selection: highest pass_rate among threshold passers, then prompt_version asc."""
    passing = [
        r
        for r in records
        if r.status == "completed"
        and r.pass_rate is not None
        and r.pass_rate >= min_pass_rate
        and r.validation_run_id
    ]
    if not passing:
        return None
    passing.sort(key=lambda r: (-(r.pass_rate or 0.0), r.prompt_version))
    return passing[0]


def run_prompt_incident_cycle(
    client: SimulatorClient,
    store: Store,
    workflows: list[Workflow],
    configs: list[WorkflowConfig],
    state: SimState,
    *,
    mutate: bool = True,
    validation_n: int = DEFAULT_VALIDATION_N,
    credit_budget: CreditBudget | None = None,
    scheduler: FairScheduler | None = None,
) -> PromptIncidentReport:
    report = PromptIncidentReport()
    by_wf = {w.workflow_id: w for w in workflows}
    eligible = eligible_hallucination_incidents(store)
    report.eligible = len(eligible)
    if not mutate:
        report.skipped_no_mutate = len(eligible)
        report.notes.append(
            "prompt remediation skipped: governance/mutate disabled "
            "(no LLM, validation, or proposal submit)"
        )
        return report

    budget = credit_budget or CreditBudget(store)
    budget.refresh_from_sim(float(state.credits_remaining))
    sched = scheduler or FairScheduler(store)
    if workflows:
        ordered = sched.order_workflows(workflows, action_kind=ACTION_PROMPT_INCIDENT)
        order_index = {w.workflow_id: i for i, w in enumerate(ordered)}
        eligible.sort(key=lambda inc: (order_index.get(inc.workflow_id, 10_000), inc.incident_id))

    reconcile_uncertain_auto_applies(client, store, workflows)
    _resume_proposed_attempts(client, store, workflows, report)

    if not eligible:
        report.notes.append("no open hallucination incidents")
        return report

    credits = float(state.credits_remaining)
    for incident in eligible:
        workflow = by_wf.get(incident.workflow_id)
        if workflow is None:
            report.notes.append(f"missing workflow object for {incident.workflow_id}")
            continue
        if workflow_has_uncertain_auto_apply(store, incident.workflow_id):
            report.notes.append(f"{incident.incident_id}: skip (auto_apply_uncertain)")
            continue
        ok, reason = should_start_attempt(store, incident)
        if not ok:
            report.skipped_duplicate += 1
            report.notes.append(f"{incident.incident_id}: skip ({reason})")
            continue

        prior = store.list_llm_attempts(incident.incident_id)
        seq = len(prior) + 1
        attempt_id = attempt_id_for(incident.incident_id, seq=seq)
        problem = (
            incident.message
            or f"population hallucination spike on {incident.workflow_id}"
        ).strip()
        if not problem:
            problem = f"hallucination incident {incident.incident_id}"

        spend = budget.can_spend(
            SUGGEST_CREDIT_COST, purpose="emergency_prompt", sim_credits=credits
        )
        if not spend.allowed or credits < SUGGEST_CREDIT_COST:
            report.credit_blocked += 1
            reason = spend.reason if not spend.allowed else f"need {SUGGEST_CREDIT_COST}, have {credits}"
            sched.record_deferral(
                workflow_id=incident.workflow_id,
                tenant_id=workflow.tenant_id,
                action_kind=ACTION_PROMPT_INCIDENT,
                priority=90,
                reason=reason,
            )
            attempt = LlmAttempt(
                attempt_id=attempt_id,
                incident_id=incident.incident_id,
                workflow_id=incident.workflow_id,
                status="credit_exhausted",
                problem=problem,
                credits_before=credits,
                error_code="insufficient_credits",
                error_detail=reason,
                created_at=_now(),
                updated_at=_now(),
            )
            store.upsert_llm_attempt(attempt)
            bump_counter(store, "prompt.credit_blocked")
            audit(
                store,
                kind="llm_suggest_blocked_credits",
                summary=f"Skipped suggest-fixes for {incident.incident_id}: {reason}",
                details={"credits": credits, "need": SUGGEST_CREDIT_COST, "decision": spend.reason},
                workflow_id=incident.workflow_id,
                related_incident_id=incident.incident_id,
                idempotency_parts=[attempt_id, "credit_block"],
            )
            continue

        # Persist pending before network call (crash recovery).
        attempt = LlmAttempt(
            attempt_id=attempt_id,
            incident_id=incident.incident_id,
            workflow_id=incident.workflow_id,
            status="pending",
            problem=problem,
            request={"workflow_id": incident.workflow_id, "problem": problem},
            credits_before=credits,
            created_at=_now(),
            updated_at=_now(),
        )
        store.upsert_llm_attempt(attempt)
        report.attempted += 1

        try:
            payload = client.suggest_fixes(workflow_id=incident.workflow_id, problem=problem)
        except SimulatorError as exc:
            status = "credit_exhausted" if exc.status == 402 or exc.code in {"credits_exhausted", "payment_required"} else (
                "transport_error" if exc.status == 0 else "failed_retryable"
            )
            attempt.status = status
            attempt.error_code = exc.code
            attempt.error_detail = str(exc.detail)
            attempt.updated_at = _now()
            store.upsert_llm_attempt(attempt)
            report.failures += 1
            audit(
                store,
                kind="llm_suggest_failed",
                summary=f"suggest-fixes failed for {incident.incident_id}: {exc.code}",
                details={"status": exc.status, "detail": exc.detail},
                workflow_id=incident.workflow_id,
                related_incident_id=incident.incident_id,
                idempotency_parts=[attempt_id, "suggest_fail", exc.code],
            )
            if status == "credit_exhausted":
                report.credit_blocked += 1
                credits = 0.0
            continue

        if "credits_remaining" in payload:
            try:
                credits = float(payload["credits_remaining"])
            except (TypeError, ValueError):
                credits = max(0.0, credits - SUGGEST_CREDIT_COST)
        else:
            credits = max(0.0, credits - SUGGEST_CREDIT_COST)
        budget.record_spend(SUGGEST_CREDIT_COST, pool="emergency", sim_credits_after=credits)
        bump_counter(store, "prompt.llm_suggest")

        allowed = set(workflow.options.prompt_version or [])
        suggestions = _parse_suggestions(payload, allowed)
        attempt.response = payload
        attempt.credits_after = credits
        attempt.status = "suggested" if suggestions else "no_safe_candidate"
        attempt.updated_at = _now()
        if not suggestions:
            attempt.error_code = "no_usable_suggestions"
            attempt.error_detail = "response had no allowed prompt_version candidates"
            store.upsert_llm_attempt(attempt)
            report.suggested += 1
            audit(
                store,
                kind="llm_suggest_empty",
                summary=f"No usable LLM suggestions for {incident.incident_id}",
                details={"raw_keys": list(payload.keys())},
                workflow_id=incident.workflow_id,
                related_incident_id=incident.incident_id,
                idempotency_parts=[attempt_id, "empty"],
            )
            continue

        store.upsert_llm_attempt(attempt)
        report.suggested += 1
        audit(
            store,
            kind="llm_suggest_received",
            summary=f"Received {len(suggestions)} untrusted prompt candidates for {incident.incident_id}",
            details={"candidates": [s["prompt_version"] for s in suggestions]},
            workflow_id=incident.workflow_id,
            related_incident_id=incident.incident_id,
            idempotency_parts=[attempt_id, "suggested"],
        )

        # Validate each candidate; LLM never picks the winner.
        attempt.status = "validating"
        attempt.updated_at = _now()
        store.upsert_llm_attempt(attempt)
        val_records: list[ValidationRecord] = []
        min_rate = float(workflow.governance.min_validation_pass_rate)
        cost = validation_credit_cost(validation_n)

        for cand in suggestions:
            # Resume-safe: skip if this candidate already completed for this attempt.
            existing_recs = store.list_validation_records(attempt_id=attempt_id)
            already = next(
                (
                    r
                    for r in existing_recs
                    if r["prompt_version"] == cand["prompt_version"] and r["status"] == "completed"
                ),
                None,
            )
            if already is not None:
                val_records.append(
                    ValidationRecord(
                        record_id=already["record_id"],
                        attempt_id=attempt_id,
                        incident_id=incident.incident_id,
                        workflow_id=incident.workflow_id,
                        prompt_version=already["prompt_version"],
                        n=already["n"],
                        status=already["status"],
                        validation_run_id=already["validation_run_id"],
                        pass_rate=already["pass_rate"],
                        failures=already["failures"],
                        credits_after=already["credits_after"],
                        meets_threshold=bool(already["meets_threshold"]),
                        created_at=already["created_at"],
                        updated_at=already["updated_at"],
                    )
                )
                continue

            if credits < cost:
                report.credit_blocked += 1
                rec = ValidationRecord(
                    record_id=validation_record_id(attempt_id, cand["prompt_version"]),
                    attempt_id=attempt_id,
                    incident_id=incident.incident_id,
                    workflow_id=incident.workflow_id,
                    prompt_version=cand["prompt_version"],
                    n=validation_n,
                    status="credit_exhausted",
                    error_code="insufficient_credits",
                    error_detail=f"need {cost}, have {credits}",
                    created_at=_now(),
                    updated_at=_now(),
                )
                store.upsert_validation_record(rec)
                val_records.append(rec)
                break

            rec = ValidationRecord(
                record_id=validation_record_id(attempt_id, cand["prompt_version"]),
                attempt_id=attempt_id,
                incident_id=incident.incident_id,
                workflow_id=incident.workflow_id,
                prompt_version=cand["prompt_version"],
                n=validation_n,
                status="pending",
                created_at=_now(),
                updated_at=_now(),
            )
            store.upsert_validation_record(rec)
            try:
                result = client.run_validation(
                    workflow_id=incident.workflow_id,
                    prompt_version=cand["prompt_version"],
                    n=validation_n,
                )
            except SimulatorError as exc:
                rec.status = "failed"
                rec.error_code = exc.code
                rec.error_detail = str(exc.detail)
                rec.updated_at = _now()
                store.upsert_validation_record(rec)
                val_records.append(rec)
                report.failures += 1
                if exc.status == 402:
                    credits = 0.0
                    report.credit_blocked += 1
                    break
                continue

            pass_rate = result.get("pass_rate")
            try:
                pass_rate_f = float(pass_rate) if pass_rate is not None else None
            except (TypeError, ValueError):
                pass_rate_f = None
            if "credits_remaining" in result:
                try:
                    credits = float(result["credits_remaining"])
                except (TypeError, ValueError):
                    credits = max(0.0, credits - cost)
            else:
                credits = max(0.0, credits - cost)

            rid = result.get("validation_run_id")
            if not isinstance(rid, str) or not rid or pass_rate_f is None:
                rec.status = "invalid_response"
                rec.error_code = "bad_validation_payload"
                rec.error_detail = "missing validation_run_id or pass_rate"
                rec.raw = result if isinstance(result, dict) else None
                rec.credits_after = credits
                rec.updated_at = _now()
                store.upsert_validation_record(rec)
                val_records.append(rec)
                continue

            rec.validation_run_id = rid
            rec.pass_rate = pass_rate_f
            try:
                rec.failures = int(result.get("failures")) if result.get("failures") is not None else None
            except (TypeError, ValueError):
                rec.failures = None
            rec.status = "completed"
            rec.credits_after = credits
            rec.meets_threshold = pass_rate_f >= min_rate
            rec.raw = result
            rec.updated_at = _now()
            store.upsert_validation_record(rec)
            val_records.append(rec)
            report.validated += 1
            budget.record_spend(cost, pool="emergency", sim_credits_after=credits)
            bump_counter(store, "prompt.validation")

        chosen = select_validated_candidate(val_records, min_pass_rate=min_rate)
        if chosen is None:
            attempt.status = "no_safe_candidate"
            attempt.error_code = "below_threshold_or_invalid"
            attempt.error_detail = f"no candidate met min_validation_pass_rate={min_rate}"
            attempt.updated_at = _now()
            store.upsert_llm_attempt(attempt)
            audit(
                store,
                kind="prompt_validation_no_winner",
                summary=f"No validated prompt for {incident.incident_id}",
                details={"min_pass_rate": min_rate, "candidates": len(val_records)},
                workflow_id=incident.workflow_id,
                related_incident_id=incident.incident_id,
                idempotency_parts=[attempt_id, "no_safe"],
            )
            continue

        attempt.status = "validated"
        attempt.selected_prompt_version = chosen.prompt_version
        attempt.selected_validation_run_id = chosen.validation_run_id
        attempt.updated_at = _now()
        store.upsert_llm_attempt(attempt)

        try:
            live = client.workflow_config(incident.workflow_id)
        except SimulatorError:
            live = next((c for c in configs if c.workflow_id == incident.workflow_id), None)
            if live is None:
                attempt.status = "failed_retryable"
                attempt.error_code = "config_fetch_failed"
                attempt.updated_at = _now()
                store.upsert_llm_attempt(attempt)
                report.failures += 1
                continue

        draft = build_prompt_proposal(
            store,
            workflow,
            live,
            prompt_version=chosen.prompt_version,
            validation_run_id=chosen.validation_run_id or "",
            pass_rate=float(chosen.pass_rate or 0.0),
            incident_id=incident.incident_id,
            attempt_id=attempt_id,
            problem=problem,
        )
        if draft is None:
            attempt.status = "no_safe_candidate"
            attempt.error_code = "proposal_build_failed"
            attempt.error_detail = "prompt proposal could not be built"
            attempt.updated_at = _now()
            store.upsert_llm_attempt(attempt)
            continue

        # Persist draft proposal before submit (idempotent proposal_id).
        from controller.services.governance import persist_proposal

        persist_proposal(store, draft)
        attempt.status = "proposed"
        attempt.related_proposal_id = draft.proposal_id
        attempt.updated_at = _now()
        store.upsert_llm_attempt(attempt)
        mark_incident_mitigating(store, incident.incident_id, draft.proposal_id)
        report.proposed += 1

        submitted = submit_proposal(client, store, workflow, live, draft)
        attempt.related_proposal_id = submitted.proposal_id
        if submitted.status in {"submitted", "approved", "auto_applied", "applied"}:
            attempt.status = "submitted"
            report.submitted += 1
        elif submitted.status in {"failed", "blocked_auto_apply"}:
            # Proposal exists; approval may have failed — keep proposed for restart reconcile.
            attempt.status = "proposed"
            attempt.error_code = submitted.error_code
            attempt.error_detail = submitted.error_detail
            report.failures += 1
        attempt.updated_at = _now()
        store.upsert_llm_attempt(attempt)
        audit(
            store,
            kind="prompt_proposal_routed",
            summary=f"Prompt proposal {submitted.proposal_id} status={submitted.status}",
            details={
                "prompt_version": chosen.prompt_version,
                "validation_run_id": chosen.validation_run_id,
                "pass_rate": chosen.pass_rate,
                "proposal_status": submitted.status,
            },
            workflow_id=incident.workflow_id,
            related_incident_id=incident.incident_id,
            related_proposal_id=submitted.proposal_id,
            idempotency_parts=[attempt_id, submitted.proposal_id, submitted.status],
        )

    _verify_applied_prompt_fixes(store)
    return report


def _verify_applied_prompt_fixes(store: Store) -> None:
    """Lightweight post-apply check using stored metric snapshots — no fabricated success."""
    for prop in store.list_proposals(status="applied"):
        if prop["source"] != "prompt_incident":
            continue
        incident_id = prop["source_ref"]
        if not incident_id:
            continue
        incident_row = store.get_incident(incident_id)
        if incident_row is None or incident_row["status"] in {"resolved", "closed"}:
            continue
        snaps = store.list_metric_snapshots(prop["workflow_id"], window_label="recent")
        if not snaps:
            snaps = store.list_metric_snapshots(prop["workflow_id"], window_label="all")
        if not snaps:
            continue
        # Prefer workflow-overall slice (empty cohort/caller sentinels).
        overall = None
        for s in snaps:
            if (s["cohort_id"] or "") == "" and (s["caller_id"] or "") == "":
                overall = s
                break
        if overall is None:
            overall = snaps[0]
        rate = overall["halluc_rate"]
        if rate is None:
            continue
        # Close only if recent hallucination is below absolute detector floor.
        from controller.services.detector import HALLUC_ABS_FLOOR

        if float(rate) < HALLUC_ABS_FLOOR:
            resolve_incident(
                store,
                incident_id,
                resolution=(
                    f"Post-apply verification: halluc_rate={float(rate):.4f} "
                    f"below floor {HALLUC_ABS_FLOOR} after proposal {prop['proposal_id']}"
                ),
            )
            audit(
                store,
                kind="prompt_fix_verified",
                summary=f"Verified prompt fix for incident {incident_id}",
                details={"halluc_rate": rate, "proposal_id": prop["proposal_id"]},
                workflow_id=prop["workflow_id"],
                related_incident_id=incident_id,
                related_proposal_id=prop["proposal_id"],
                idempotency_parts=[incident_id, prop["proposal_id"], "verified"],
            )

