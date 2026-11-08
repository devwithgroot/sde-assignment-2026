"""Deterministic fair scheduler across tenants/workflows (Module 15 / Phase 8).

Policy (transparent):
- Each workflow has weight (default 1.0). Higher weight = more frequent service.
- Urgency boost: open high-severity incidents / guardrail signals raise priority.
- Selection is weighted round-robin by (wait_ticks + 1) * weight * urgency_factor,
  then workflow_id for ties — fully deterministic.
- Serving one workflow resets its wait; others' wait_ticks increase.
- Deferred actions are recorded with reasons when budgets/caps block work.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from controller.api.models import Workflow
from controller.persistence.repositories import Store
from controller.services.audit import audit

ACTION_EXPERIMENT = "experiment"
ACTION_PROMPT_INCIDENT = "prompt_incident"
ACTION_GOVERNANCE = "governance"
ACTION_APPROVAL_APPLY = "approval_apply"

PRIORITY_ROUTINE = 10
PRIORITY_OVERDUE_APPROVAL = 40
PRIORITY_GUARDRAIL = 70
PRIORITY_HALLUCINATION = 90


@dataclass(frozen=True)
class WorkItem:
    workflow_id: str
    tenant_id: str
    action_kind: str
    priority: int
    weight: float = 1.0
    reason: str = ""


@dataclass
class ScheduleDecision:
    selected: WorkItem | None
    deferred: list[dict[str, Any]] = field(default_factory=list)
    tick: int = 0
    explanation: str = ""


@dataclass
class FairScheduler:
    """In-memory + persisted weighted round-robin state."""

    store: Store
    tick: int = 0
    default_weight: float = 1.0
    max_wait_before_starvation_alert: int = 20

    def ensure_workflows(self, workflows: Iterable[Workflow], *, weights: dict[str, float] | None = None) -> None:
        weights = weights or {}
        now = _now()
        for wf in workflows:
            row = self.store.get_scheduler_state(wf.workflow_id)
            weight = float(weights.get(wf.workflow_id, weights.get(wf.tenant_id, self.default_weight)))
            if row is None:
                self.store.upsert_scheduler_state(
                    workflow_id=wf.workflow_id,
                    tenant_id=wf.tenant_id,
                    weight=weight,
                    last_served_tick=-1,
                    wait_ticks=0,
                    actions_taken=0,
                    last_action_kind=None,
                    updated_at=now,
                )
            else:
                self.store.upsert_scheduler_state(
                    workflow_id=wf.workflow_id,
                    tenant_id=wf.tenant_id,
                    weight=weight,
                    last_served_tick=row["last_served_tick"],
                    wait_ticks=row["wait_ticks"],
                    actions_taken=row["actions_taken"],
                    last_action_kind=row["last_action_kind"],
                    updated_at=now,
                )

    def urgency_for(self, store: Store, workflow_id: str) -> tuple[float, int, str]:
        """Return (urgency_factor, priority, label)."""
        incidents = store.list_incidents(workflow_id=workflow_id)
        open_inc = [i for i in incidents if i["status"] in {"detected", "investigating", "mitigating"}]
        halluc = [i for i in open_inc if i["signal_type"] == "hallucination_spike"]
        high = [i for i in open_inc if i["severity"] in {"high", "critical"}]
        approvals = store.list_controller_approvals(workflow_id=workflow_id, status="approved")
        if halluc:
            return 3.0, PRIORITY_HALLUCINATION, "hallucination_incident"
        if high:
            return 2.5, PRIORITY_GUARDRAIL, "high_severity_incident"
        if approvals:
            return 2.0, PRIORITY_OVERDUE_APPROVAL, "approved_awaiting_apply"
        if open_inc:
            return 1.5, PRIORITY_GUARDRAIL, "open_incident"
        return 1.0, PRIORITY_ROUTINE, "routine"

    def build_candidates(
        self,
        workflows: list[Workflow],
        *,
        action_kind: str,
    ) -> list[WorkItem]:
        items: list[WorkItem] = []
        for wf in workflows:
            factor, priority, label = self.urgency_for(self.store, wf.workflow_id)
            row = self.store.get_scheduler_state(wf.workflow_id)
            weight = float(row["weight"]) if row else self.default_weight
            items.append(
                WorkItem(
                    workflow_id=wf.workflow_id,
                    tenant_id=wf.tenant_id,
                    action_kind=action_kind,
                    priority=priority,
                    weight=weight * factor,
                    reason=label,
                )
            )
        return items

    def select_next(self, candidates: list[WorkItem]) -> ScheduleDecision:
        """Pick one candidate fairly; bump wait on others; persist decision."""
        self.tick += 1
        if not candidates:
            return ScheduleDecision(selected=None, tick=self.tick, explanation="no candidates")

        scored: list[tuple[float, int, str, WorkItem]] = []
        for item in candidates:
            row = self.store.get_scheduler_state(item.workflow_id)
            wait = int(row["wait_ticks"]) if row else 0
            # Higher score = more overdue * weight.
            score = (wait + 1) * item.weight
            scored.append((score, item.priority, item.workflow_id, item))
        # Deterministic: score desc, priority desc, workflow_id asc.
        scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
        winner = scored[0][3]

        now = _now()
        deferred: list[dict[str, Any]] = []
        for score, priority, _wid, item in scored[1:]:
            row = self.store.get_scheduler_state(item.workflow_id)
            wait = int(row["wait_ticks"]) + 1 if row else 1
            self.store.upsert_scheduler_state(
                workflow_id=item.workflow_id,
                tenant_id=item.tenant_id,
                weight=float(row["weight"]) if row else item.weight,
                last_served_tick=int(row["last_served_tick"]) if row else -1,
                wait_ticks=wait,
                actions_taken=int(row["actions_taken"]) if row else 0,
                last_action_kind=row["last_action_kind"] if row else None,
                updated_at=now,
            )
            if wait >= self.max_wait_before_starvation_alert:
                deferred.append(
                    {
                        "workflow_id": item.workflow_id,
                        "reason": "waiting_while_other_served",
                        "wait_ticks": wait,
                        "priority": priority,
                        "score": score,
                    }
                )

        wrow = self.store.get_scheduler_state(winner.workflow_id)
        self.store.upsert_scheduler_state(
            workflow_id=winner.workflow_id,
            tenant_id=winner.tenant_id,
            weight=float(wrow["weight"]) if wrow else winner.weight,
            last_served_tick=self.tick,
            wait_ticks=0,
            actions_taken=(int(wrow["actions_taken"]) if wrow else 0) + 1,
            last_action_kind=winner.action_kind,
            updated_at=now,
        )
        explanation = (
            f"selected {winner.workflow_id} for {winner.action_kind} "
            f"(priority={winner.priority}, reason={winner.reason}, tick={self.tick})"
        )
        audit(
            self.store,
            kind="scheduler_selected",
            summary=explanation,
            details={
                "workflow_id": winner.workflow_id,
                "tenant_id": winner.tenant_id,
                "action_kind": winner.action_kind,
                "priority": winner.priority,
                "tick": self.tick,
                "deferred_count": len(deferred),
            },
            workflow_id=winner.workflow_id,
            tenant_id=winner.tenant_id,
            idempotency_parts=[self.tick, winner.workflow_id, winner.action_kind],
        )
        return ScheduleDecision(selected=winner, deferred=deferred, tick=self.tick, explanation=explanation)

    def record_deferral(
        self,
        *,
        workflow_id: str,
        tenant_id: str | None,
        action_kind: str,
        priority: int,
        reason: str,
    ) -> str:
        defer_id = f"def_{hashlib.sha256(f'{workflow_id}|{action_kind}|{reason}|{self.tick}'.encode()).hexdigest()[:16]}"
        now = _now()
        self.store.upsert_deferred_action(
            defer_id=defer_id,
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            action_kind=action_kind,
            priority=priority,
            reason=reason,
            status="deferred",
            created_at=now,
            updated_at=now,
            resolved_at=None,
        )
        audit(
            self.store,
            kind="action_deferred",
            summary=f"Deferred {action_kind} for {workflow_id}: {reason}",
            details={"priority": priority, "defer_id": defer_id},
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            idempotency_parts=[defer_id],
        )
        return defer_id

    def order_workflows(self, workflows: list[Workflow], *, action_kind: str) -> list[Workflow]:
        """Return workflows in fair service order for this tick (winner first, then by wait)."""
        self.ensure_workflows(workflows)
        by_id = {w.workflow_id: w for w in workflows}
        candidates = self.build_candidates(workflows, action_kind=action_kind)
        decision = self.select_next(candidates)
        if decision.selected is None:
            return list(workflows)
        ordered = [by_id[decision.selected.workflow_id]]
        rest = [w for w in workflows if w.workflow_id != decision.selected.workflow_id]
        # Stable secondary order by wait_ticks desc then id.
        rest.sort(
            key=lambda w: (
                -(int((self.store.get_scheduler_state(w.workflow_id) or {"wait_ticks": 0})["wait_ticks"])),
                w.workflow_id,
            )
        )
        ordered.extend(rest)
        return ordered


def simulate_horizon(
    store: Store,
    workflows: list[Workflow],
    *,
    ticks: int,
    action_kind: str = ACTION_EXPERIMENT,
) -> dict[str, int]:
    """Pure scheduling simulation for tests — counts selections per workflow."""
    sched = FairScheduler(store=store)
    sched.ensure_workflows(workflows)
    counts = {w.workflow_id: 0 for w in workflows}
    for _ in range(ticks):
        decision = sched.select_next(sched.build_candidates(workflows, action_kind=action_kind))
        if decision.selected:
            counts[decision.selected.workflow_id] += 1
    return counts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
