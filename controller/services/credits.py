"""Credit budget: emergency prompt-incident reserve vs experiment/routine spend.

Policy:
- `emergency_reserve` credits are held back from routine/experiment spend.
- Prompt-incident LLM/validation may use the emergency pool.
- Experiment-related paid work (if any) may only use credits above the reserve,
  up to `experiment_budget_cap` cumulative spend tracked locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from controller.persistence.repositories import Store
from controller.services.audit import audit

DEFAULT_EMERGENCY_RESERVE = 30.0
DEFAULT_EXPERIMENT_CAP = 50.0


@dataclass
class CreditDecision:
    allowed: bool
    pool: str  # emergency | experiment | none
    reason: str
    sim_credits: float
    available_experiment: float
    available_emergency: float


class CreditBudget:
    def __init__(
        self,
        store: Store,
        *,
        emergency_reserve: float = DEFAULT_EMERGENCY_RESERVE,
        experiment_budget_cap: float = DEFAULT_EXPERIMENT_CAP,
    ):
        self.store = store
        self.emergency_reserve = emergency_reserve
        self.experiment_budget_cap = experiment_budget_cap
        self._ensure_row()

    def _ensure_row(self) -> None:
        row = self.store.get_credit_budget()
        if row is None:
            self.store.upsert_credit_budget(
                emergency_reserve=self.emergency_reserve,
                experiment_budget_cap=self.experiment_budget_cap,
                experiment_spent=0.0,
                emergency_spent=0.0,
                last_sim_credits=None,
                updated_at=_now(),
            )

    def refresh_from_sim(self, sim_credits: float) -> None:
        row = self.store.get_credit_budget()
        assert row is not None
        self.store.upsert_credit_budget(
            emergency_reserve=float(row["emergency_reserve"]),
            experiment_budget_cap=float(row["experiment_budget_cap"]),
            experiment_spent=float(row["experiment_spent"]),
            emergency_spent=float(row["emergency_spent"]),
            last_sim_credits=sim_credits,
            updated_at=_now(),
        )

    def snapshot(self) -> dict[str, float | None]:
        row = self.store.get_credit_budget()
        assert row is not None
        sim = row["last_sim_credits"]
        sim_f = float(sim) if sim is not None else 0.0
        reserve = float(row["emergency_reserve"])
        exp_spent = float(row["experiment_spent"])
        em_spent = float(row["emergency_spent"])
        cap = float(row["experiment_budget_cap"])
        return {
            "sim_credits": float(sim) if sim is not None else None,
            "emergency_reserve": reserve,
            "experiment_budget_cap": cap,
            "experiment_spent": exp_spent,
            "emergency_spent": em_spent,
            "available_emergency": max(0.0, min(sim_f, reserve) - 0.0),  # conceptual hold
            "available_experiment": max(0.0, sim_f - reserve),
            "experiment_remaining_cap": max(0.0, cap - exp_spent),
        }

    def can_spend(self, cost: float, *, purpose: str, sim_credits: float) -> CreditDecision:
        """purpose: emergency_prompt | experiment | routine."""
        self.refresh_from_sim(sim_credits)
        row = self.store.get_credit_budget()
        assert row is not None
        reserve = float(row["emergency_reserve"])
        exp_spent = float(row["experiment_spent"])
        cap = float(row["experiment_budget_cap"])
        available_experiment = max(0.0, sim_credits - reserve)
        available_emergency = max(0.0, sim_credits)  # emergency may consume into reserve

        if purpose == "emergency_prompt":
            if sim_credits < cost:
                return CreditDecision(
                    False, "none", f"insufficient_sim_credits need={cost} have={sim_credits}",
                    sim_credits, available_experiment, available_emergency,
                )
            return CreditDecision(
                True, "emergency", "emergency_prompt_allowed",
                sim_credits, available_experiment, available_emergency,
            )

        # experiment / routine cannot touch the reserve
        if available_experiment < cost:
            return CreditDecision(
                False, "none",
                f"reserve_protected need={cost} available_above_reserve={available_experiment} reserve={reserve}",
                sim_credits, available_experiment, available_emergency,
            )
        if purpose == "experiment" and exp_spent + cost > cap + 1e-9:
            return CreditDecision(
                False, "none",
                f"experiment_budget_exhausted spent={exp_spent} cap={cap} need={cost}",
                sim_credits, available_experiment, available_emergency,
            )
        return CreditDecision(
            True, "experiment", "experiment_spend_allowed",
            sim_credits, available_experiment, available_emergency,
        )

    def record_spend(self, cost: float, *, pool: str, sim_credits_after: float | None = None) -> None:
        row = self.store.get_credit_budget()
        assert row is not None
        exp = float(row["experiment_spent"])
        em = float(row["emergency_spent"])
        if pool == "emergency":
            em += cost
        else:
            exp += cost
        self.store.upsert_credit_budget(
            emergency_reserve=float(row["emergency_reserve"]),
            experiment_budget_cap=float(row["experiment_budget_cap"]),
            experiment_spent=exp,
            emergency_spent=em,
            last_sim_credits=sim_credits_after if sim_credits_after is not None else row["last_sim_credits"],
            updated_at=_now(),
        )
        audit(
            self.store,
            kind="credit_spend",
            summary=f"Recorded {pool} credit spend {cost}",
            details={"pool": pool, "cost": cost, "experiment_spent": exp, "emergency_spent": em},
            idempotency_parts=[pool, cost, exp, em, _now()[:16]],
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
