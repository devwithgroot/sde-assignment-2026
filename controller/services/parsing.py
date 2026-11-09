"""NDJSON parsing for simulator chunks.

A bad line becomes a recorded issue. It is not inserted as an event and it is
not dropped without a reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

KNOWN_TYPES = frozenset({"campaign_created", "lead", "call", "config_changed", "approval_decided"})
_RAW_LIMIT = 2000


@dataclass(frozen=True)
class ParsedEvent:
    event_id: int
    type: str
    ts: str | None
    workflow_id: str | None
    tenant_id: str | None
    campaign_id: str | None
    lead_id: str | None
    person_id: str | None
    raw_json: str
    unexpected_type: bool


@dataclass(frozen=True)
class ParseIssue:
    line_no: int
    reason: str
    raw_line: str


@dataclass
class ParsedChunk:
    events: list[ParsedEvent] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    non_empty_lines: int = 0


def parse_chunk(body: str) -> ParsedChunk:
    parsed = ParsedChunk()
    for line_no, raw in enumerate(body.splitlines(), start=1):
        if not raw.strip():
            continue
        parsed.non_empty_lines += 1
        item = parse_line(line_no, raw)
        if isinstance(item, ParseIssue):
            parsed.issues.append(item)
        else:
            parsed.events.append(item)
    return parsed


def parse_line(line_no: int, raw: str) -> ParsedEvent | ParseIssue:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return ParseIssue(line_no, f"malformed_json: {exc.msg}", raw[:_RAW_LIMIT])
    if not isinstance(obj, dict):
        return ParseIssue(line_no, "not_an_object", raw[:_RAW_LIMIT])
    event_id = obj.get("event_id")
    event_type = obj.get("type")
    if type(event_id) is not int:
        return ParseIssue(line_no, "missing_or_bad_event_id", raw[:_RAW_LIMIT])
    if not isinstance(event_type, str) or not event_type:
        return ParseIssue(line_no, "missing_or_bad_type", raw[:_RAW_LIMIT])
    return ParsedEvent(
        event_id=event_id,
        type=event_type,
        ts=_text(obj, "ts"),
        workflow_id=_text(obj, "workflow_id"),
        tenant_id=_text(obj, "tenant_id"),
        campaign_id=_text(obj, "campaign_id"),
        lead_id=_text(obj, "lead_id"),
        person_id=_text(obj, "person_id"),
        raw_json=raw,
        unexpected_type=event_type not in KNOWN_TYPES,
    )


def _text(obj: dict, key: str) -> str | None:
    value = obj.get(key)
    return value if isinstance(value, str) else None
