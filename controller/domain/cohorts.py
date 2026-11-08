"""Shared cohort definitions.

Detection, experiments, dry runs, and the supervisor view must import from here.
Do not copy matching rules into those modules later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

FEATURE_KEYS = ("lang", "city_tier", "age_band", "value_band")
DEFAULT_COHORT_ID = "default"
MAX_COHORTS = 20
# Later phases use this when deciding whether a cohort is large enough to act on.
MIN_COHORT_SAMPLE = 100


@dataclass(frozen=True)
class CohortDefinition:
    id: str
    where: dict[str, list[str]]


@dataclass(frozen=True)
class CohortAssignment:
    cohort_id: str
    matched_definition: CohortDefinition | None
    source: str  # "config" | "default"


def parse_cohort_definitions(config: Mapping[str, Any] | None) -> list[CohortDefinition]:
    """Read cohorts from a workflow config document. Caps at MAX_COHORTS."""
    if not config:
        return []
    raw = config.get("cohorts") or []
    if not isinstance(raw, list):
        return []
    out: list[CohortDefinition] = []
    for item in raw[:MAX_COHORTS]:
        if not isinstance(item, dict):
            continue
        cohort_id = item.get("id")
        where = item.get("where") or {}
        if not isinstance(cohort_id, str) or not cohort_id or not isinstance(where, dict):
            continue
        cleaned: dict[str, list[str]] = {}
        for key, values in where.items():
            if key not in FEATURE_KEYS:
                continue
            if not isinstance(values, list) or not values:
                continue
            cleaned[key] = [str(v) for v in values]
        out.append(CohortDefinition(id=cohort_id, where=cleaned))
    return out


def matches_where(features: Mapping[str, Any], where: Mapping[str, list[str]]) -> bool:
    """AND across feature keys. OR within one key's value list. Missing feature fails that key."""
    if not where:
        return True
    for key, allowed in where.items():
        value = features.get(key)
        if value is None:
            return False
        if str(value) not in {str(v) for v in allowed}:
            return False
    return True


def assign_cohort(features: Mapping[str, Any], cohorts: list[CohortDefinition]) -> CohortAssignment:
    """First matching cohort wins. Otherwise default."""
    for definition in cohorts[:MAX_COHORTS]:
        if matches_where(features, definition.where):
            return CohortAssignment(cohort_id=definition.id, matched_definition=definition, source="config")
    return CohortAssignment(cohort_id=DEFAULT_COHORT_ID, matched_definition=None, source="default")


def cohort_is_actionable(sample_size: int, *, minimum: int = MIN_COHORT_SAMPLE) -> bool:
    return sample_size >= minimum
