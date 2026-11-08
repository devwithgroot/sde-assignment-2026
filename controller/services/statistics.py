"""Deterministic frequentist comparisons for experiment arms.

Uses a two-proportion z-test with Holm-Bonferroni correction across challengers.
An LLM never calls into this module to pick a winner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Proportion:
    successes: int
    trials: int

    @property
    def rate(self) -> float | None:
        if self.trials <= 0:
            return None
        return self.successes / self.trials


@dataclass(frozen=True)
class ComparisonResult:
    effect_size: float | None
    p_value: float | None
    ci_low: float | None
    ci_high: float | None
    control_rate: float | None
    treatment_rate: float | None
    underpowered: bool
    reason: str


def _norm_sf(z: float) -> float:
    """One-sided survival function for standard normal via erfc."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def two_proportion_ztest(
    control: Proportion,
    treatment: Proportion,
    *,
    min_n: int,
    min_effect: float = 0.0,
) -> ComparisonResult:
    """Two-sided z-test of treatment - control. Underpowered if either arm < min_n."""
    if control.trials < min_n or treatment.trials < min_n:
        return ComparisonResult(
            effect_size=None,
            p_value=None,
            ci_low=None,
            ci_high=None,
            control_rate=control.rate,
            treatment_rate=treatment.rate,
            underpowered=True,
            reason=f"need >= {min_n} samples per arm (control={control.trials}, treatment={treatment.trials})",
        )
    p1 = control.successes / control.trials
    p2 = treatment.successes / treatment.trials
    effect = p2 - p1
    pooled = (control.successes + treatment.successes) / (control.trials + treatment.trials)
    se_null = math.sqrt(pooled * (1 - pooled) * (1 / control.trials + 1 / treatment.trials))
    if se_null <= 0:
        return ComparisonResult(
            effect_size=effect,
            p_value=1.0,
            ci_low=effect,
            ci_high=effect,
            control_rate=p1,
            treatment_rate=p2,
            underpowered=False,
            reason="zero variance under null",
        )
    z = effect / se_null
    # two-sided
    p_value = 2 * _norm_sf(abs(z))
    se_effect = math.sqrt(p1 * (1 - p1) / control.trials + p2 * (1 - p2) / treatment.trials)
    zcrit = 1.96
    return ComparisonResult(
        effect_size=effect,
        p_value=p_value,
        ci_low=effect - zcrit * se_effect,
        ci_high=effect + zcrit * se_effect,
        control_rate=p1,
        treatment_rate=p2,
        underpowered=False,
        reason="ok" if abs(effect) >= min_effect or p_value > 0.05 else "effect below practical minimum",
    )


def holm_adjust(p_values: list[float], alpha: float = 0.05) -> list[float]:
    """Return per-comparison adjusted alpha thresholds (Holm step-down).

    For m tests sorted by ascending p, the threshold for rank i (0-based) is alpha / (m - i).
    Returned list aligns with the original order: adjusted_alpha[i] for p_values[i].
    """
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    for rank, idx in enumerate(order):
        adjusted[idx] = alpha / (m - rank)
    return adjusted


def decide_challenger(
    *,
    comparison: ComparisonResult,
    adjusted_alpha: float,
    min_effect: float,
    guardrail_control: float | None,
    guardrail_treatment: float | None,
    guardrail_max_drop: float,
) -> tuple[str, str]:
    """Return (verdict, reason). verdict in winner|loser|inconclusive|guardrail_violation|underpowered."""
    if comparison.underpowered:
        return "underpowered", comparison.reason
    if guardrail_control is not None and guardrail_treatment is not None:
        drop = guardrail_control - guardrail_treatment
        if drop > guardrail_max_drop + 1e-12:
            return (
                "guardrail_violation",
                f"guardrail drop {drop:.4f} exceeds max {guardrail_max_drop:.4f}",
            )
    if comparison.p_value is None or comparison.effect_size is None:
        return "inconclusive", "missing test statistics"
    if comparison.p_value <= adjusted_alpha and comparison.effect_size >= min_effect:
        return (
            "winner",
            f"lift {comparison.effect_size:.4f}, p={comparison.p_value:.4g} <= alpha' {adjusted_alpha:.4g}",
        )
    if comparison.p_value <= adjusted_alpha and comparison.effect_size <= -min_effect:
        return (
            "loser",
            f"drop {comparison.effect_size:.4f}, p={comparison.p_value:.4g} <= alpha' {adjusted_alpha:.4g}",
        )
    return (
        "inconclusive",
        f"no significant practical lift (effect={comparison.effect_size:.4f}, p={comparison.p_value:.4g}, alpha'={adjusted_alpha:.4g})",
    )
