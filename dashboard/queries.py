"""Assemble a read-only snapshot from the controller SQLite store.

Phase 5 experiment tables are loaded when present. Missing Phase 6+ tables
are reported as unavailable, not as empty successes. Experiment panels never
fabricate winners when rows are absent or underpowered.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from controller.persistence.db import connect, init_schema


UNIMPLEMENTED = (
    ("proposals", "Governance proposals submitted by the controller (Phase 6+)"),
    ("controller_approvals", "Controller-side approval lifecycle store (Phase 6+)"),
    ("audit_decisions", "Full decision audit store beyond detection signals (Phase 6+/C12 expansion)"),
)


@dataclass
class DashboardSnapshot:
    db_path: str
    schema_version: int | None
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    projection_cursor: int | None = None
    workflow_kpis: list[dict[str, Any]] = field(default_factory=list)
    caller_kpis: list[dict[str, Any]] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    config_changes: list[dict[str, Any]] = field(default_factory=list)
    approval_decisions: list[dict[str, Any]] = field(default_factory=list)
    ingest_issues: list[dict[str, Any]] = field(default_factory=list)
    cohort_summary: list[dict[str, Any]] = field(default_factory=list)
    experiments: list[dict[str, Any]] = field(default_factory=list)
    experiment_arms: list[dict[str, Any]] = field(default_factory=list)
    experiments_available: bool = False
    unimplemented: list[dict[str, str]] = field(default_factory=list)
    simulator: dict[str, Any] | None = None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def load_snapshot(db_path: str | Path, *, simulator: dict[str, Any] | None = None) -> DashboardSnapshot:
    path = Path(db_path)
    snap = DashboardSnapshot(db_path=str(path), schema_version=None, ok=False, simulator=simulator)
    snap.unimplemented = [{"key": k, "detail": d} for k, d in UNIMPLEMENTED]

    if not path.exists():
        snap.errors.append(f"database not found: {path}")
        snap.warnings.append("Run the controller once to create and populate the SQLite file.")
        return snap

    conn: sqlite3.Connection | None = None
    try:
        try:
            conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.OperationalError:
            # Fallback if the platform rejects URI mode; still open without writes from this module.
            conn = connect(path)
            init_schema(conn)

        if _table_exists(conn, "schema_meta"):
            row = conn.execute("SELECT version FROM schema_meta").fetchone()
            snap.schema_version = int(row["version"]) if row else None

        for table in (
            "events",
            "chunks",
            "campaigns",
            "leads",
            "calls",
            "cohort_assignments",
            "metric_snapshots",
            "detection_signals",
            "config_changes",
            "approval_decisions",
            "ingest_issues",
            "experiments",
            "experiment_arms",
        ):
            snap.counts[table] = _count(conn, table)

        if _table_exists(conn, "checkpoints"):
            snap.checkpoints = [dict(r) for r in conn.execute("SELECT * FROM checkpoints ORDER BY kind")]
        if _table_exists(conn, "projection_state"):
            row = conn.execute("SELECT last_event_id FROM projection_state WHERE id = 1").fetchone()
            snap.projection_cursor = int(row["last_event_id"]) if row else None

        if _table_exists(conn, "calls") and snap.counts.get("calls", 0) > 0:
            unmatched = conn.execute("SELECT COUNT(*) AS n FROM calls WHERE lead_matched = 0").fetchone()
            snap.counts["unmatched_calls"] = int(unmatched["n"])
            returning = conn.execute(
                """SELECT COUNT(*) AS n FROM (
                       SELECT person_id FROM leads GROUP BY person_id HAVING COUNT(DISTINCT campaign_id) > 1
                   )"""
            ).fetchone()
            snap.counts["returning_people"] = int(returning["n"])
            concluded = conn.execute(
                "SELECT COUNT(DISTINCT lead_id) AS n FROM calls WHERE concluded = 1 AND lead_id IS NOT NULL"
            ).fetchone()
            snap.counts["concluded_leads"] = int(concluded["n"])

        if _table_exists(conn, "metric_snapshots"):
            snap.workflow_kpis = [
                dict(r)
                for r in conn.execute(
                    """SELECT workflow_id, window_label, dials, connected, engaged,
                              uploaded_leads, concluded_leads, pickup, engagement, conclusion_rate, halluc_rate
                       FROM metric_snapshots
                       WHERE window_label = 'all' AND IFNULL(cohort_id, '') = '' AND IFNULL(caller_id, '') = ''
                       ORDER BY workflow_id"""
                )
            ]
            snap.caller_kpis = [
                dict(r)
                for r in conn.execute(
                    """SELECT workflow_id, caller_id, dials, connected, pickup, engagement
                       FROM metric_snapshots
                       WHERE window_label = 'all' AND IFNULL(caller_id, '') != ''
                       ORDER BY workflow_id, caller_id"""
                )
            ]
        else:
            snap.warnings.append("metric_snapshots table missing — run Phase 4 metrics first.")

        if _table_exists(conn, "detection_signals"):
            snap.signals = [
                dict(r)
                for r in conn.execute(
                    """SELECT workflow_id, signal_type, kpi, caller_id, severity, message,
                              baseline_value, recent_value, baseline_n, recent_n, created_at
                       FROM detection_signals
                       WHERE suppressed = 0
                       ORDER BY id DESC
                       LIMIT 100"""
                )
            ]
        else:
            snap.warnings.append("detection_signals table missing — run Phase 4 detection first.")

        if _table_exists(conn, "config_changes"):
            snap.config_changes = [
                dict(r)
                for r in conn.execute(
                    """SELECT event_id, workflow_id, version, actor, changed_dims_json, ts
                       FROM config_changes ORDER BY event_id DESC LIMIT 50"""
                )
            ]

        if _table_exists(conn, "approval_decisions"):
            snap.approval_decisions = [
                dict(r)
                for r in conn.execute(
                    """SELECT event_id, approval_id, workflow_id, status, reasons_json, ts
                       FROM approval_decisions ORDER BY event_id DESC LIMIT 50"""
                )
            ]

        if _table_exists(conn, "ingest_issues"):
            snap.ingest_issues = [
                dict(r)
                for r in conn.execute(
                    """SELECT chunk, line_no, reason, raw_line
                       FROM ingest_issues ORDER BY chunk, line_no LIMIT 50"""
                )
            ]

        if _table_exists(conn, "cohort_assignments"):
            snap.cohort_summary = [
                dict(r)
                for r in conn.execute(
                    """SELECT workflow_id, cohort_id, COUNT(*) AS leads
                       FROM cohort_assignments
                       GROUP BY workflow_id, cohort_id
                       ORDER BY workflow_id, cohort_id"""
                )
            ]

        if _table_exists(conn, "experiments"):
            snap.experiments_available = True
            snap.experiments = []
            for row in conn.execute("SELECT * FROM experiments ORDER BY updated_at DESC, experiment_id"):
                item = dict(row)
                item["planned_allocation"] = _parse_json(item.get("planned_allocation_json"))
                item["evidence"] = _parse_json(item.get("evidence_json"))
                snap.experiments.append(item)
            if _table_exists(conn, "experiment_arms"):
                snap.experiment_arms = [
                    dict(r)
                    for r in conn.execute(
                        """SELECT * FROM experiment_arms
                           ORDER BY experiment_id, is_control DESC, variant"""
                    )
                ]
            if not snap.experiments:
                snap.warnings.append(
                    "Experiment tables exist but no experiments have been evaluated yet. "
                    "Run the controller observe loop after joins/KPIs."
                )
            else:
                underpowered = [
                    e for e in snap.experiments if e.get("decision") == "underpowered" or e.get("status") == "running"
                ]
                if underpowered:
                    snap.warnings.append(
                        f"{len(underpowered)} experiment(s) have insufficient samples for a winner decision."
                    )
        else:
            snap.experiments_available = False
            snap.warnings.append(
                "experiments table missing — run Phase 5 controller once to create schema and evaluate."
            )

        if snap.counts.get("events", 0) == 0:
            snap.warnings.append("No events ingested yet.")
        if snap.projection_cursor is None and snap.counts.get("events", 0) > 0:
            snap.warnings.append("Events exist but projection cursor is missing — joins may be incomplete.")

        snap.ok = True
    except Exception as exc:  # noqa: BLE001 — surface any DB problem to the UI
        snap.errors.append(str(exc))
        snap.ok = False
    finally:
        if conn is not None:
            conn.close()
    return snap
