"""Deterministic experiment statistics."""

from __future__ import annotations

from controller.services.statistics import (
    Proportion,
    decide_challenger,
    holm_adjust,
    two_proportion_ztest,
)


def test_underpowered_when_below_min_n():
    result = two_proportion_ztest(Proportion(10, 50), Proportion(12, 50), min_n=100)
    assert result.underpowered is True
    assert result.p_value is None


def test_known_winner_large_lift():
    # control 20%, treatment 40%, n=500 each — clear lift
    result = two_proportion_ztest(Proportion(100, 500), Proportion(200, 500), min_n=100)
    assert result.underpowered is False
    assert result.effect_size is not None and result.effect_size > 0.15
    assert result.p_value is not None and result.p_value < 0.001


def test_no_difference_inconclusive():
    result = two_proportion_ztest(Proportion(100, 500), Proportion(100, 500), min_n=100)
    assert result.underpowered is False
    assert result.effect_size is not None and abs(result.effect_size) < 1e-12
    assert result.p_value == 1.0


def test_holm_adjust_order():
    alphas = holm_adjust([0.04, 0.01, 0.03], alpha=0.05)
    # sorted p: 0.01, 0.03, 0.04 → thresholds 0.05/3, 0.05/2, 0.05/1
    assert alphas[1] == 0.05 / 3
    assert alphas[2] == 0.05 / 2
    assert alphas[0] == 0.05 / 1


def test_decide_challenger_guardrail_and_winner():
    good = two_proportion_ztest(Proportion(100, 500), Proportion(200, 500), min_n=100)
    verdict, _ = decide_challenger(
        comparison=good,
        adjusted_alpha=0.05,
        min_effect=0.02,
        guardrail_control=0.30,
        guardrail_treatment=0.29,
        guardrail_max_drop=0.02,
    )
    assert verdict == "winner"

    harm = two_proportion_ztest(Proportion(100, 500), Proportion(200, 500), min_n=100)
    verdict, reason = decide_challenger(
        comparison=harm,
        adjusted_alpha=0.05,
        min_effect=0.02,
        guardrail_control=0.30,
        guardrail_treatment=0.20,
        guardrail_max_drop=0.02,
    )
    assert verdict == "guardrail_violation"
    assert "guardrail" in reason


def test_decide_repeatable():
    a = two_proportion_ztest(Proportion(80, 400), Proportion(120, 400), min_n=100)
    b = two_proportion_ztest(Proportion(80, 400), Proportion(120, 400), min_n=100)
    assert a == b
