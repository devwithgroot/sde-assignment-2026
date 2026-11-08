"""Event, chunk, and checkpoint repository."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from controller.api.models import ChunkMeta
from controller.persistence.db import connect, init_schema
from controller.services.parsing import ParseIssue, ParsedEvent, parse_chunk


class IngestIntegrityError(Exception):
    def __init__(self, chunk: int, detail: str):
        self.chunk = chunk
        self.detail = detail
        super().__init__(f"chunk {chunk}: {detail}")


@dataclass(frozen=True)
class Checkpoint:
    kind: str
    last_chunk: int
    last_event_id: int


@dataclass(frozen=True)
class ChunkCommit:
    chunk: int
    inserted: int
    duplicates: int
    issues: int
    already_committed: bool = False


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn = connect(self.path)
        init_schema(self.conn)

    def close(self) -> None:
        self.conn.close()

    def transaction(self):
        return _Transaction(self.conn)

    def event_count(self, kind: str | None = None) -> int:
        if kind is None:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind = ?", (kind,)).fetchone()
        return int(row["n"])

    def distinct_event_ids(self) -> int:
        row = self.conn.execute("SELECT COUNT(DISTINCT event_id) AS n FROM events").fetchone()
        return int(row["n"])

    def issue_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM ingest_issues").fetchone()
        return int(row["n"])

    def checkpoint(self, kind: str) -> Checkpoint | None:
        row = self.conn.execute(
            "SELECT kind, last_chunk, last_event_id FROM checkpoints WHERE kind = ?", (kind,)
        ).fetchone()
        if row is None:
            return None
        return Checkpoint(kind=row["kind"], last_chunk=row["last_chunk"], last_event_id=row["last_event_id"])

    def is_committed(self, chunk: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM chunks WHERE chunk = ?", (chunk,)).fetchone()
        return row is not None

    def commit_chunk(self, meta: ChunkMeta, body: str) -> ChunkCommit:
        if self.is_committed(meta.chunk):
            return ChunkCommit(chunk=meta.chunk, inserted=0, duplicates=0, issues=0, already_committed=True)
        parsed = parse_chunk(body)
        with self.transaction():
            if self.is_committed(meta.chunk):
                return ChunkCommit(chunk=meta.chunk, inserted=0, duplicates=0, issues=0, already_committed=True)
            inserted, duplicates = self._insert_events(meta, parsed.events)
            self._insert_issues(meta.chunk, parsed.issues)
            if parsed.non_empty_lines != meta.events:
                raise IngestIntegrityError(
                    meta.chunk,
                    f"body has {parsed.non_empty_lines} non-empty lines, chunk metadata says {meta.events}",
                )
            last_event_id = max((event.event_id for event in parsed.events), default=0)
            now = _now()
            self.conn.execute(
                """INSERT INTO chunks (
                       chunk, kind, declared_events, first_event_id, last_event_id,
                       inserted, duplicates, issues, committed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    meta.chunk,
                    meta.kind,
                    meta.events,
                    meta.first_event_id,
                    meta.last_event_id,
                    inserted,
                    duplicates,
                    len(parsed.issues),
                    now,
                ),
            )
            self._upsert_checkpoint(meta.kind, meta.chunk, last_event_id, now)
        return ChunkCommit(chunk=meta.chunk, inserted=inserted, duplicates=duplicates, issues=len(parsed.issues))

    def _insert_events(self, meta: ChunkMeta, events: list[ParsedEvent]) -> tuple[int, int]:
        sql = """INSERT OR IGNORE INTO events (
                    event_id, chunk, kind, type, ts, workflow_id, tenant_id,
                    campaign_id, lead_id, person_id, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        before = int(self.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"])
        batch: list[tuple] = []
        for event in events:
            batch.append(
                (
                    event.event_id,
                    meta.chunk,
                    meta.kind,
                    event.type,
                    event.ts,
                    event.workflow_id,
                    event.tenant_id,
                    event.campaign_id,
                    event.lead_id,
                    event.person_id,
                    event.raw_json,
                )
            )
            if len(batch) >= 2000:
                self.conn.executemany(sql, batch)
                batch.clear()
        if batch:
            self.conn.executemany(sql, batch)
        after = int(self.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"])
        inserted = after - before
        return inserted, len(events) - inserted

    def _insert_issues(self, chunk: int, issues: list[ParseIssue]) -> None:
        if not issues:
            return
        self.conn.executemany(
            """INSERT OR IGNORE INTO ingest_issues (chunk, line_no, reason, raw_line)
               VALUES (?, ?, ?, ?)""",
            [(chunk, issue.line_no, issue.reason, issue.raw_line) for issue in issues],
        )

    def _upsert_checkpoint(self, kind: str, chunk: int, last_event_id: int, now: str) -> None:
        current = self.checkpoint(kind)
        if current is not None and current.last_chunk > chunk:
            return
        event_id = last_event_id
        if current is not None and current.last_event_id > event_id:
            event_id = current.last_event_id
        self.conn.execute(
            """INSERT INTO checkpoints (kind, last_chunk, last_event_id, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(kind) DO UPDATE SET
                 last_chunk = excluded.last_chunk,
                 last_event_id = excluded.last_event_id,
                 updated_at = excluded.updated_at""",
            (kind, chunk, event_id, now),
        )

    def projection_cursor(self) -> int:
        row = self.conn.execute("SELECT last_event_id FROM projection_state WHERE id = 1").fetchone()
        return int(row["last_event_id"]) if row is not None else 0

    def set_projection_cursor(self, last_event_id: int) -> None:
        self.conn.execute(
            """INSERT INTO projection_state (id, last_event_id, updated_at)
               VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 last_event_id = excluded.last_event_id,
                 updated_at = excluded.updated_at""",
            (last_event_id, _now()),
        )

    def iter_events_after(self, after_event_id: int):
        cursor = self.conn.execute(
            """SELECT event_id, type, raw_json FROM events
               WHERE event_id > ?
               ORDER BY event_id ASC""",
            (after_event_id,),
        )
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                yield dict(row)

    def lead_exists(self, lead_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        return row is not None

    def campaign_exists(self, campaign_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM campaigns WHERE campaign_id = ?", (campaign_id,)).fetchone()
        return row is not None

    def repair_calls_for_lead(self, lead_id: str, campaign_id: str) -> int:
        before = self.conn.total_changes
        self.conn.execute(
            """UPDATE calls
               SET lead_matched = 1,
                   campaign_matched = CASE
                       WHEN campaign_id = ? THEN 1
                       WHEN EXISTS (SELECT 1 FROM campaigns c WHERE c.campaign_id = calls.campaign_id) THEN 1
                       ELSE campaign_matched
                   END
               WHERE lead_id = ? AND lead_matched = 0""",
            (campaign_id, lead_id),
        )
        return max(self.conn.total_changes - before, 0)

    def upsert_cohort_assignment(
        self,
        lead_id: str,
        workflow_id: str,
        config_version: int,
        cohort_id: str,
        source: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO cohort_assignments (lead_id, workflow_id, config_version, cohort_id, source)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(lead_id, config_version) DO UPDATE SET
                 cohort_id = excluded.cohort_id,
                 source = excluded.source,
                 workflow_id = excluded.workflow_id""",
            (lead_id, workflow_id, config_version, cohort_id, source),
        )

    def table_count(self, table: str) -> int:
        allowed = {
            "campaigns",
            "leads",
            "calls",
            "config_changes",
            "approval_decisions",
            "cohort_assignments",
            "events",
        }
        if table not in allowed:
            raise ValueError(table)
        row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])

    def unmatched_call_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM calls WHERE lead_matched = 0").fetchone()
        return int(row["n"])

    def returning_person_count(self) -> int:
        row = self.conn.execute(
            """SELECT COUNT(*) AS n FROM (
                   SELECT person_id FROM leads GROUP BY person_id HAVING COUNT(DISTINCT campaign_id) > 1
               )"""
        ).fetchone()
        return int(row["n"])

    def concluded_lead_count(self) -> int:
        row = self.conn.execute(
            """SELECT COUNT(DISTINCT lead_id) AS n FROM calls
               WHERE concluded = 1 AND lead_id IS NOT NULL"""
        ).fetchone()
        return int(row["n"])

    def _call_filters(
        self,
        workflow_id: str,
        *,
        cohort_id: str | None = None,
        caller_id: str | None = None,
        start_ts: str | None = None,
        end_ts: str | None = None,
    ) -> tuple[str, list]:
        clauses = ["workflow_id = ?"]
        params: list = [workflow_id]
        if cohort_id is not None:
            clauses.append("cohort_id = ?")
            params.append(cohort_id)
        if caller_id is not None:
            clauses.append("caller_id = ?")
            params.append(caller_id)
        if start_ts is not None:
            clauses.append("ts >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("ts <= ?")
            params.append(end_ts)
        return " AND ".join(clauses), params

    def call_totals(
        self,
        workflow_id: str,
        *,
        cohort_id: str | None = None,
        caller_id: str | None = None,
        start_ts: str | None = None,
        end_ts: str | None = None,
    ) -> tuple[int, int, int, int, int]:
        where, params = self._call_filters(
            workflow_id, cohort_id=cohort_id, caller_id=caller_id, start_ts=start_ts, end_ts=end_ts
        )
        row = self.conn.execute(
            f"""SELECT
                    COUNT(*) AS dials,
                    COALESCE(SUM(connected), 0) AS connected,
                    COALESCE(SUM(engaged), 0) AS engaged,
                    COALESCE(SUM(CASE WHEN halluc_flag = 1 THEN 1 ELSE 0 END), 0) AS halluc_flags,
                    COALESCE(SUM(CASE WHEN halluc_flag IS NOT NULL THEN 1 ELSE 0 END), 0) AS connected_with_qa
                FROM calls WHERE {where}""",
            params,
        ).fetchone()
        return (
            int(row["dials"]),
            int(row["connected"]),
            int(row["engaged"]),
            int(row["halluc_flags"]),
            int(row["connected_with_qa"]),
        )

    def uploaded_leads(self, workflow_id: str) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM leads WHERE workflow_id = ?", (workflow_id,)).fetchone()
        return int(row["n"])

    def leads_in_cohort(self, workflow_id: str, cohort_id: str) -> int:
        row = self.conn.execute(
            """SELECT COUNT(DISTINCT lead_id) AS n FROM cohort_assignments
               WHERE workflow_id = ? AND cohort_id = ?""",
            (workflow_id, cohort_id),
        ).fetchone()
        return int(row["n"])

    def concluded_leads_for_workflow(
        self,
        workflow_id: str,
        *,
        cohort_id: str | None = None,
        caller_id: str | None = None,
        start_ts: str | None = None,
        end_ts: str | None = None,
    ) -> int:
        where, params = self._call_filters(
            workflow_id, cohort_id=cohort_id, caller_id=caller_id, start_ts=start_ts, end_ts=end_ts
        )
        row = self.conn.execute(
            f"""SELECT COUNT(DISTINCT lead_id) AS n FROM calls
                WHERE concluded = 1 AND lead_id IS NOT NULL AND {where}""",
            params,
        ).fetchone()
        return int(row["n"])

    def call_days(self, workflow_id: str) -> list[str]:
        rows = self.conn.execute(
            """SELECT DISTINCT substr(ts, 1, 10) AS day FROM calls
               WHERE workflow_id = ? AND ts IS NOT NULL
               ORDER BY day""",
            (workflow_id,),
        ).fetchall()
        return [row["day"] for row in rows if row["day"]]

    def caller_ids_for_workflow(self, workflow_id: str) -> list[str]:
        rows = self.conn.execute(
            """SELECT DISTINCT caller_id FROM calls
               WHERE workflow_id = ? AND caller_id IS NOT NULL
               ORDER BY caller_id""",
            (workflow_id,),
        ).fetchall()
        return [row["caller_id"] for row in rows]

    def cohort_ids_for_workflow(self, workflow_id: str) -> list[str]:
        rows = self.conn.execute(
            """SELECT DISTINCT cohort_id FROM calls
               WHERE workflow_id = ? AND cohort_id IS NOT NULL
               ORDER BY cohort_id""",
            (workflow_id,),
        ).fetchall()
        return [row["cohort_id"] for row in rows]

    def upsert_metric_snapshot(self, slice_) -> None:
        # SQLite UNIQUE treats each NULL as distinct; store empty sentinels for key columns.
        cohort_key = slice_.cohort_id if slice_.cohort_id is not None else ""
        caller_key = slice_.caller_id if slice_.caller_id is not None else ""
        version_key = slice_.config_version if slice_.config_version is not None else -1
        self.conn.execute(
            """INSERT INTO metric_snapshots (
                   computed_at, window_label, window_start_ts, window_end_ts,
                   workflow_id, cohort_id, caller_id, config_version,
                   dials, connected, engaged, uploaded_leads, concluded_leads,
                   pickup, engagement, conclusion_rate, halluc_flags, connected_with_qa, halluc_rate
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(window_label, workflow_id, cohort_id, caller_id, config_version) DO UPDATE SET
                 computed_at = excluded.computed_at,
                 window_start_ts = excluded.window_start_ts,
                 window_end_ts = excluded.window_end_ts,
                 dials = excluded.dials,
                 connected = excluded.connected,
                 engaged = excluded.engaged,
                 uploaded_leads = excluded.uploaded_leads,
                 concluded_leads = excluded.concluded_leads,
                 pickup = excluded.pickup,
                 engagement = excluded.engagement,
                 conclusion_rate = excluded.conclusion_rate,
                 halluc_flags = excluded.halluc_flags,
                 connected_with_qa = excluded.connected_with_qa,
                 halluc_rate = excluded.halluc_rate""",
            (
                _now(),
                slice_.window_label,
                slice_.window_start_ts,
                slice_.window_end_ts,
                slice_.workflow_id,
                cohort_key,
                caller_key,
                version_key,
                slice_.dials,
                slice_.connected,
                slice_.engaged,
                slice_.uploaded_leads,
                slice_.concluded_leads,
                slice_.pickup,
                slice_.engagement,
                slice_.conclusion_rate,
                slice_.halluc_flags,
                slice_.connected_with_qa,
                slice_.halluc_rate,
            ),
        )

    def insert_signal(self, signal) -> bool:
        before = self.conn.total_changes
        self.conn.execute(
            """INSERT INTO detection_signals (
                   fingerprint, workflow_id, signal_type, kpi, dimension, cohort_id, caller_id,
                   severity, baseline_value, recent_value, baseline_n, recent_n, threshold,
                   message, evidence_json, sim_ts, created_at, suppressed
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(fingerprint) DO NOTHING""",
            (
                signal.fingerprint,
                signal.workflow_id,
                signal.signal_type,
                signal.kpi,
                signal.dimension,
                signal.cohort_id,
                signal.caller_id,
                signal.severity,
                signal.baseline_value,
                signal.recent_value,
                signal.baseline_n,
                signal.recent_n,
                signal.threshold,
                signal.message,
                json.dumps(signal.evidence, sort_keys=True),
                signal.sim_ts,
                _now(),
            ),
        )
        return self.conn.total_changes > before

    def list_signals(self, workflow_id: str | None = None) -> list[sqlite3.Row]:
        if workflow_id is None:
            return list(self.conn.execute("SELECT * FROM detection_signals ORDER BY id").fetchall())
        return list(
            self.conn.execute(
                "SELECT * FROM detection_signals WHERE workflow_id = ? ORDER BY id",
                (workflow_id,),
            ).fetchall()
        )

    def list_metric_snapshots(self, workflow_id: str, window_label: str | None = None) -> list[sqlite3.Row]:
        if window_label is None:
            return list(
                self.conn.execute(
                    "SELECT * FROM metric_snapshots WHERE workflow_id = ? ORDER BY id",
                    (workflow_id,),
                ).fetchall()
            )
        return list(
            self.conn.execute(
                """SELECT * FROM metric_snapshots
                   WHERE workflow_id = ? AND window_label = ?
                   ORDER BY id""",
                (workflow_id, window_label),
            ).fetchall()
        )

    def variant_call_totals(
        self,
        workflow_id: str,
        dimension: str,
        variant: str,
        *,
        cohort_id: str | None = None,
    ) -> tuple[int, int, int]:
        """Return (dials, connected, engaged) for one call-variant cell."""
        if dimension not in {"welcome_message", "voice", "caller_id"}:
            raise ValueError(f"unsupported dimension: {dimension}")
        clauses = ["workflow_id = ?", f"{dimension} = ?"]
        params: list = [workflow_id, variant]
        if cohort_id is not None:
            clauses.append("cohort_id = ?")
            params.append(cohort_id)
        where = " AND ".join(clauses)
        row = self.conn.execute(
            f"""SELECT
                    COUNT(*) AS dials,
                    COALESCE(SUM(connected), 0) AS connected,
                    COALESCE(SUM(engaged), 0) AS engaged
                FROM calls WHERE {where}""",
            params,
        ).fetchone()
        return int(row["dials"]), int(row["connected"]), int(row["engaged"])

    def observed_variants(
        self,
        workflow_id: str,
        dimension: str,
        *,
        cohort_id: str | None = None,
    ) -> list[tuple[str, int, int, int]]:
        """Variants seen in calls with (variant, dials, connected, engaged), dials desc."""
        if dimension not in {"welcome_message", "voice", "caller_id"}:
            raise ValueError(f"unsupported dimension: {dimension}")
        clauses = [f"workflow_id = ?", f"{dimension} IS NOT NULL"]
        params: list = [workflow_id]
        if cohort_id is not None:
            clauses.append("cohort_id = ?")
            params.append(cohort_id)
        where = " AND ".join(clauses)
        rows = self.conn.execute(
            f"""SELECT {dimension} AS variant,
                       COUNT(*) AS dials,
                       COALESCE(SUM(connected), 0) AS connected,
                       COALESCE(SUM(engaged), 0) AS engaged
                FROM calls WHERE {where}
                GROUP BY {dimension}
                ORDER BY dials DESC, variant ASC""",
            params,
        ).fetchall()
        return [(row["variant"], int(row["dials"]), int(row["connected"]), int(row["engaged"])) for row in rows]

    def upsert_experiment(self, experiment) -> None:
        self.conn.execute(
            """INSERT INTO experiments (
                   experiment_id, workflow_id, tenant_id, cohort_id, dimension,
                   primary_kpi, guardrail_kpi, guardrail_max_drop, status, control_variant,
                   planned_allocation_json, min_sample_per_arm, alpha, min_effect,
                   created_at, updated_at, decision, decision_reason, winner_variant,
                   promotion_status, evidence_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(experiment_id) DO UPDATE SET
                 tenant_id = excluded.tenant_id,
                 primary_kpi = excluded.primary_kpi,
                 guardrail_kpi = excluded.guardrail_kpi,
                 guardrail_max_drop = excluded.guardrail_max_drop,
                 status = excluded.status,
                 control_variant = excluded.control_variant,
                 planned_allocation_json = excluded.planned_allocation_json,
                 min_sample_per_arm = excluded.min_sample_per_arm,
                 alpha = excluded.alpha,
                 min_effect = excluded.min_effect,
                 updated_at = excluded.updated_at,
                 decision = excluded.decision,
                 decision_reason = excluded.decision_reason,
                 winner_variant = excluded.winner_variant,
                 promotion_status = excluded.promotion_status,
                 evidence_json = excluded.evidence_json""",
            (
                experiment.experiment_id,
                experiment.workflow_id,
                experiment.tenant_id,
                experiment.cohort_id,
                experiment.dimension,
                experiment.primary_kpi,
                experiment.guardrail_kpi,
                experiment.guardrail_max_drop,
                experiment.status,
                experiment.control_variant,
                json.dumps(experiment.planned_allocation, sort_keys=True),
                experiment.min_sample_per_arm,
                experiment.alpha,
                experiment.min_effect,
                experiment.created_at,
                experiment.updated_at,
                experiment.decision,
                experiment.decision_reason,
                experiment.winner_variant,
                experiment.promotion_status,
                json.dumps(experiment.evidence, sort_keys=True),
            ),
        )

    def upsert_experiment_arm(self, arm) -> None:
        self.conn.execute(
            """INSERT INTO experiment_arms (
                   experiment_id, variant, is_control, planned_weight,
                   dials, connected, engaged, primary_rate, guardrail_rate,
                   sample_n, sample_ok, verdict, effect_size, p_value,
                   adjusted_alpha, ci_low, ci_high, reason
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(experiment_id, variant) DO UPDATE SET
                 is_control = excluded.is_control,
                 planned_weight = excluded.planned_weight,
                 dials = excluded.dials,
                 connected = excluded.connected,
                 engaged = excluded.engaged,
                 primary_rate = excluded.primary_rate,
                 guardrail_rate = excluded.guardrail_rate,
                 sample_n = excluded.sample_n,
                 sample_ok = excluded.sample_ok,
                 verdict = excluded.verdict,
                 effect_size = excluded.effect_size,
                 p_value = excluded.p_value,
                 adjusted_alpha = excluded.adjusted_alpha,
                 ci_low = excluded.ci_low,
                 ci_high = excluded.ci_high,
                 reason = excluded.reason""",
            (
                arm.experiment_id,
                arm.variant,
                1 if arm.is_control else 0,
                arm.planned_weight,
                arm.dials,
                arm.connected,
                arm.engaged,
                arm.primary_rate,
                arm.guardrail_rate,
                arm.sample_n,
                1 if arm.sample_ok else 0,
                arm.verdict,
                arm.effect_size,
                arm.p_value,
                arm.adjusted_alpha,
                arm.ci_low,
                arm.ci_high,
                arm.reason,
            ),
        )

    def list_experiments(self, workflow_id: str | None = None) -> list[sqlite3.Row]:
        if workflow_id is None:
            return list(self.conn.execute("SELECT * FROM experiments ORDER BY experiment_id").fetchall())
        return list(
            self.conn.execute(
                "SELECT * FROM experiments WHERE workflow_id = ? ORDER BY experiment_id",
                (workflow_id,),
            ).fetchall()
        )

    def get_experiment(self, experiment_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()

    def list_experiment_arms(self, experiment_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM experiment_arms
                   WHERE experiment_id = ?
                   ORDER BY is_control DESC, variant ASC""",
                (experiment_id,),
            ).fetchall()
        )

    def active_experiment_dimension(self, workflow_id: str, cohort_id: str) -> str | None:
        """Return dimension of a non-terminal experiment for this workflow/cohort, if any."""
        row = self.conn.execute(
            """SELECT dimension FROM experiments
               WHERE workflow_id = ? AND cohort_id = ?
                 AND status IN ('planned', 'running')
               ORDER BY experiment_id LIMIT 1""",
            (workflow_id, cohort_id),
        ).fetchone()
        return row["dimension"] if row else None


class _Transaction:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
