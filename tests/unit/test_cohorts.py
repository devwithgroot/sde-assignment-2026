"""Shared cohort engine: first match, AND/OR, default, missing features."""

from controller.domain.cohorts import (
    DEFAULT_COHORT_ID,
    MAX_COHORTS,
    MIN_COHORT_SAMPLE,
    CohortDefinition,
    assign_cohort,
    cohort_is_actionable,
    parse_cohort_definitions,
)


def test_first_match_wins():
    cohorts = [
        CohortDefinition(id="senior_ta", where={"age_band": ["60+"], "lang": ["ta"]}),
        CohortDefinition(id="senior", where={"age_band": ["60+"]}),
    ]
    assignment = assign_cohort({"age_band": "60+", "lang": "ta"}, cohorts)
    assert assignment.cohort_id == "senior_ta"
    assert assignment.source == "config"


def test_and_across_keys_or_within_key():
    cohorts = [CohortDefinition(id="hi_or_en_young", where={"lang": ["hi", "en"], "age_band": ["18-30"]})]
    assert assign_cohort({"lang": "en", "age_band": "18-30"}, cohorts).cohort_id == "hi_or_en_young"
    assert assign_cohort({"lang": "ta", "age_band": "18-30"}, cohorts).cohort_id == DEFAULT_COHORT_ID
    assert assign_cohort({"lang": "en", "age_band": "46-60"}, cohorts).cohort_id == DEFAULT_COHORT_ID


def test_missing_feature_does_not_match():
    cohorts = [CohortDefinition(id="hi", where={"lang": ["hi"]})]
    assert assign_cohort({}, cohorts).cohort_id == DEFAULT_COHORT_ID
    assert assign_cohort({"city_tier": "1"}, cohorts).cohort_id == DEFAULT_COHORT_ID


def test_default_when_empty_cohorts():
    assert assign_cohort({"lang": "hi"}, []).cohort_id == DEFAULT_COHORT_ID


def test_parse_caps_at_max_and_ignores_unknown_features():
    raw = {
        "cohorts": [
            {"id": f"c{i}", "where": {"lang": ["hi"], "unknown": ["x"]}} for i in range(MAX_COHORTS + 5)
        ]
    }
    parsed = parse_cohort_definitions(raw)
    assert len(parsed) == MAX_COHORTS
    assert "unknown" not in parsed[0].where
    assert parsed[0].where == {"lang": ["hi"]}


def test_recomputed_vs_historical_id_are_separate_concepts():
    # Historical call cohort_id comes from the call event. Recomputation uses the engine.
    proposed = parse_cohort_definitions(
        {"cohorts": [{"id": "young", "where": {"age_band": ["18-30"]}}]}
    )
    historical_call_cohort = "default"
    recomputed = assign_cohort({"age_band": "18-30", "lang": "en"}, proposed)
    assert historical_call_cohort == "default"
    assert recomputed.cohort_id == "young"


def test_actionable_threshold():
    assert cohort_is_actionable(MIN_COHORT_SAMPLE) is True
    assert cohort_is_actionable(MIN_COHORT_SAMPLE - 1) is False
