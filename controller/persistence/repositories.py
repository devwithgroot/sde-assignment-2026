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
            "experiments",
            "experiment_arms",
            "incidents",
            "proposals",
            "controller_approvals",
            "audit_decisions",
            "config_snapshots",
            "llm_suggestion_attempts",
            "validation_run_records",
            "scheduler_state",
            "deferred_actions",
            "credit_budget_state",
            "observability_counters",
            "operational_alerts",
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
                 promotion_status = CASE
                   WHEN experiments.promotion_status IN (
                          'promoted', 'promoted_auto_apply', 'proposal_submitted',
                          'decay_detected', 'rolled_back', 'exploration_submitted'
                        )
                    AND excluded.promotion_status IN (
                          'observe_only_not_promoted', 'ready_to_propose_observe_only'
                        )
                   THEN experiments.promotion_status
                   ELSE excluded.promotion_status
                 END,
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

    def update_experiment_promotion(self, experiment_id: str, promotion_status: str, *, decision_reason: str | None = None) -> None:
        if decision_reason is None:
            self.conn.execute(
                """UPDATE experiments SET promotion_status = ?, updated_at = ?
                   WHERE experiment_id = ?""",
                (promotion_status, _now(), experiment_id),
            )
        else:
            self.conn.execute(
                """UPDATE experiments SET promotion_status = ?, decision_reason = ?, updated_at = ?
                   WHERE experiment_id = ?""",
                (promotion_status, decision_reason, _now(), experiment_id),
            )

    def upsert_incident(self, incident) -> None:
        self.conn.execute(
            """INSERT INTO incidents (
                   incident_id, workflow_id, tenant_id, signal_fingerprint, signal_type, severity,
                   status, title, message, evidence_json, related_experiment_id, related_proposal_id,
                   resolution, created_at, updated_at, closed_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(incident_id) DO UPDATE SET
                 status = excluded.status,
                 severity = excluded.severity,
                 title = excluded.title,
                 message = excluded.message,
                 evidence_json = excluded.evidence_json,
                 related_experiment_id = excluded.related_experiment_id,
                 related_proposal_id = excluded.related_proposal_id,
                 resolution = excluded.resolution,
                 updated_at = excluded.updated_at,
                 closed_at = excluded.closed_at""",
            (
                incident.incident_id,
                incident.workflow_id,
                incident.tenant_id,
                incident.signal_fingerprint,
                incident.signal_type,
                incident.severity,
                incident.status,
                incident.title,
                incident.message,
                json.dumps(incident.evidence, sort_keys=True),
                incident.related_experiment_id,
                incident.related_proposal_id,
                incident.resolution,
                incident.created_at,
                incident.updated_at,
                incident.closed_at,
            ),
        )

    def get_incident_by_fingerprint(self, fingerprint: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM incidents WHERE signal_fingerprint = ?", (fingerprint,)
        ).fetchone()

    def get_incident(self, incident_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()

    def list_incidents(self, workflow_id: str | None = None, status: str | None = None) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list = []
        if workflow_id is not None:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return list(self.conn.execute(f"SELECT * FROM incidents{where} ORDER BY updated_at DESC", params).fetchall())

    def upsert_proposal(self, proposal) -> None:
        self.conn.execute(
            """INSERT INTO proposals (
                   proposal_id, workflow_id, base_version, status, source, source_ref,
                   changed_dims_json, proposed_config_json, change_summary, diff_json, dry_run_json,
                   impact_score, affected_leads, rationale, evidence_json, approval_id, route,
                   error_code, error_detail, created_at, updated_at, submitted_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(proposal_id) DO UPDATE SET
                 status = excluded.status,
                 base_version = excluded.base_version,
                 changed_dims_json = excluded.changed_dims_json,
                 proposed_config_json = excluded.proposed_config_json,
                 change_summary = excluded.change_summary,
                 diff_json = excluded.diff_json,
                 dry_run_json = excluded.dry_run_json,
                 impact_score = excluded.impact_score,
                 affected_leads = excluded.affected_leads,
                 rationale = excluded.rationale,
                 evidence_json = excluded.evidence_json,
                 approval_id = excluded.approval_id,
                 route = excluded.route,
                 error_code = excluded.error_code,
                 error_detail = excluded.error_detail,
                 updated_at = excluded.updated_at,
                 submitted_at = excluded.submitted_at""",
            (
                proposal.proposal_id,
                proposal.workflow_id,
                proposal.base_version,
                proposal.status,
                proposal.source,
                proposal.source_ref,
                json.dumps(proposal.changed_dims, sort_keys=True),
                json.dumps(proposal.proposed_config, sort_keys=True),
                proposal.change_summary,
                json.dumps(proposal.diff, sort_keys=True),
                json.dumps(proposal.dry_run, sort_keys=True),
                proposal.impact_score,
                proposal.affected_leads,
                proposal.rationale,
                json.dumps(proposal.evidence, sort_keys=True) if proposal.evidence is not None else None,
                proposal.approval_id,
                proposal.route,
                proposal.error_code,
                proposal.error_detail,
                proposal.created_at,
                proposal.updated_at,
                proposal.submitted_at,
            ),
        )

    def get_proposal(self, proposal_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()

    def list_proposals(self, workflow_id: str | None = None, status: str | None = None) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list = []
        if workflow_id is not None:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return list(self.conn.execute(f"SELECT * FROM proposals{where} ORDER BY updated_at DESC", params).fetchall())

    def upsert_controller_approval(self, approval) -> None:
        self.conn.execute(
            """INSERT INTO controller_approvals (
                   approval_id, proposal_id, workflow_id, status, base_version, changed_dims_json,
                   created_at, updated_at, expires_at, apply_by, decided_at, reasons_json,
                   applied_version, last_reconciled_at, raw_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(approval_id) DO UPDATE SET
                 status = excluded.status,
                 base_version = excluded.base_version,
                 changed_dims_json = excluded.changed_dims_json,
                 updated_at = excluded.updated_at,
                 expires_at = excluded.expires_at,
                 apply_by = excluded.apply_by,
                 decided_at = excluded.decided_at,
                 reasons_json = excluded.reasons_json,
                 applied_version = excluded.applied_version,
                 last_reconciled_at = excluded.last_reconciled_at,
                 raw_json = excluded.raw_json""",
            (
                approval.approval_id,
                approval.proposal_id,
                approval.workflow_id,
                approval.status,
                approval.base_version,
                json.dumps(approval.changed_dims, sort_keys=True) if approval.changed_dims is not None else None,
                approval.created_at,
                approval.updated_at,
                approval.expires_at,
                approval.apply_by,
                approval.decided_at,
                json.dumps(approval.reasons, sort_keys=True) if approval.reasons is not None else None,
                approval.applied_version,
                approval.last_reconciled_at,
                json.dumps(approval.raw, sort_keys=True) if approval.raw is not None else None,
            ),
        )

    def get_controller_approval(self, approval_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM controller_approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()

    def list_controller_approvals(
        self, workflow_id: str | None = None, status: str | None = None
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list = []
        if workflow_id is not None:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return list(
            self.conn.execute(f"SELECT * FROM controller_approvals{where} ORDER BY updated_at DESC", params).fetchall()
        )

    def insert_audit(self, record) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO audit_decisions (
                   decision_id, kind, workflow_id, tenant_id, cohort_id,
                   related_incident_id, related_proposal_id, related_approval_id, related_experiment_id,
                   sim_ts, wall_ts, summary, details_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.decision_id,
                record.kind,
                record.workflow_id,
                record.tenant_id,
                record.cohort_id,
                record.related_incident_id,
                record.related_proposal_id,
                record.related_approval_id,
                record.related_experiment_id,
                record.sim_ts,
                record.wall_ts,
                record.summary,
                json.dumps(record.details, sort_keys=True),
            ),
        )

    def list_audit(self, workflow_id: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        if workflow_id is None:
            return list(
                self.conn.execute(
                    "SELECT * FROM audit_decisions ORDER BY wall_ts DESC LIMIT ?", (limit,)
                ).fetchall()
            )
        return list(
            self.conn.execute(
                """SELECT * FROM audit_decisions WHERE workflow_id = ?
                   ORDER BY wall_ts DESC LIMIT ?""",
                (workflow_id, limit),
            ).fetchall()
        )

    def upsert_config_snapshot(self, snapshot) -> None:
        self.conn.execute(
            """INSERT INTO config_snapshots (
                   snapshot_id, workflow_id, version, config_json, source, related_proposal_id, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(workflow_id, version, source) DO UPDATE SET
                 config_json = excluded.config_json,
                 related_proposal_id = excluded.related_proposal_id,
                 created_at = excluded.created_at""",
            (
                snapshot.snapshot_id,
                snapshot.workflow_id,
                snapshot.version,
                json.dumps(snapshot.config, sort_keys=True),
                snapshot.source,
                snapshot.related_proposal_id,
                snapshot.created_at,
            ),
        )

    def latest_config_snapshot(self, workflow_id: str, source: str = "rollback_candidate") -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM config_snapshots
               WHERE workflow_id = ? AND source = ?
               ORDER BY version DESC LIMIT 1""",
            (workflow_id, source),
        ).fetchone()

    def snapshot_for_proposal(self, proposal_id: str, source: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM config_snapshots
               WHERE related_proposal_id = ? AND source = ?
               ORDER BY version DESC LIMIT 1""",
            (proposal_id, source),
        ).fetchone()

    def pre_promotion_rollback_snapshot(
        self, workflow_id: str, experiment_id: str | None = None
    ) -> sqlite3.Row | None:
        """Config that existed before the monitored winner promotion.

        Prefer the ``pre_apply`` / ``rollback_candidate`` snapshot tied to the
        experiment's winner proposal. Fall back to the workflow's latest
        ``rollback_candidate`` (written only for experiment_winner submits).
        """
        if experiment_id:
            props = self.recent_proposals_for_workflow(workflow_id, limit=100)
            for prop in props:
                if prop["source"] != "experiment_winner" or prop["source_ref"] != experiment_id:
                    continue
                if prop["status"] not in {
                    "submitted",
                    "approved",
                    "applied",
                    "auto_applied",
                }:
                    continue
                for source in ("pre_apply", "rollback_candidate"):
                    snap = self.snapshot_for_proposal(prop["proposal_id"], source)
                    if snap is not None:
                        return snap
        return self.latest_config_snapshot(workflow_id, source="rollback_candidate")

    def recent_approval_decision_events(self, workflow_id: str | None = None) -> list[sqlite3.Row]:
        if workflow_id is None:
            return list(self.conn.execute("SELECT * FROM approval_decisions ORDER BY event_id DESC").fetchall())
        return list(
            self.conn.execute(
                "SELECT * FROM approval_decisions WHERE workflow_id = ? ORDER BY event_id DESC",
                (workflow_id,),
            ).fetchall()
        )

    def recent_config_change_events(self, workflow_id: str | None = None) -> list[sqlite3.Row]:
        if workflow_id is None:
            return list(self.conn.execute("SELECT * FROM config_changes ORDER BY event_id DESC").fetchall())
        return list(
            self.conn.execute(
                "SELECT * FROM config_changes WHERE workflow_id = ? ORDER BY event_id DESC",
                (workflow_id,),
            ).fetchall()
        )

    def upsert_llm_attempt(self, attempt) -> None:
        self.conn.execute(
            """INSERT INTO llm_suggestion_attempts (
                   attempt_id, incident_id, workflow_id, status, problem, request_json, response_json,
                   error_code, error_detail, credits_before, credits_after, selected_prompt_version,
                   selected_validation_run_id, related_proposal_id, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(attempt_id) DO UPDATE SET
                 status = excluded.status,
                 request_json = excluded.request_json,
                 response_json = excluded.response_json,
                 error_code = excluded.error_code,
                 error_detail = excluded.error_detail,
                 credits_before = excluded.credits_before,
                 credits_after = excluded.credits_after,
                 selected_prompt_version = excluded.selected_prompt_version,
                 selected_validation_run_id = excluded.selected_validation_run_id,
                 related_proposal_id = excluded.related_proposal_id,
                 updated_at = excluded.updated_at""",
            (
                attempt.attempt_id,
                attempt.incident_id,
                attempt.workflow_id,
                attempt.status,
                attempt.problem,
                json.dumps(attempt.request, sort_keys=True) if attempt.request is not None else None,
                json.dumps(attempt.response, sort_keys=True) if attempt.response is not None else None,
                attempt.error_code,
                attempt.error_detail,
                attempt.credits_before,
                attempt.credits_after,
                attempt.selected_prompt_version,
                attempt.selected_validation_run_id,
                attempt.related_proposal_id,
                attempt.created_at,
                attempt.updated_at,
            ),
        )

    def get_llm_attempt(self, attempt_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM llm_suggestion_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()

    def list_llm_attempts(self, incident_id: str | None = None) -> list[sqlite3.Row]:
        if incident_id is None:
            return list(
                self.conn.execute("SELECT * FROM llm_suggestion_attempts ORDER BY updated_at DESC").fetchall()
            )
        return list(
            self.conn.execute(
                """SELECT * FROM llm_suggestion_attempts WHERE incident_id = ?
                   ORDER BY updated_at DESC""",
                (incident_id,),
            ).fetchall()
        )

    def upsert_validation_record(self, record) -> None:
        self.conn.execute(
            """INSERT INTO validation_run_records (
                   record_id, attempt_id, incident_id, workflow_id, prompt_version, n,
                   validation_run_id, pass_rate, failures, status, credits_after,
                   error_code, error_detail, meets_threshold, raw_json, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(record_id) DO UPDATE SET
                 validation_run_id = excluded.validation_run_id,
                 pass_rate = excluded.pass_rate,
                 failures = excluded.failures,
                 status = excluded.status,
                 credits_after = excluded.credits_after,
                 error_code = excluded.error_code,
                 error_detail = excluded.error_detail,
                 meets_threshold = excluded.meets_threshold,
                 raw_json = excluded.raw_json,
                 updated_at = excluded.updated_at""",
            (
                record.record_id,
                record.attempt_id,
                record.incident_id,
                record.workflow_id,
                record.prompt_version,
                record.n,
                record.validation_run_id,
                record.pass_rate,
                record.failures,
                record.status,
                record.credits_after,
                record.error_code,
                record.error_detail,
                1 if record.meets_threshold else 0,
                json.dumps(record.raw, sort_keys=True) if record.raw is not None else None,
                record.created_at,
                record.updated_at,
            ),
        )

    def list_validation_records(
        self, *, attempt_id: str | None = None, incident_id: str | None = None
    ) -> list[sqlite3.Row]:
        if attempt_id is not None:
            return list(
                self.conn.execute(
                    """SELECT * FROM validation_run_records WHERE attempt_id = ?
                       ORDER BY created_at""",
                    (attempt_id,),
                ).fetchall()
            )
        if incident_id is not None:
            return list(
                self.conn.execute(
                    """SELECT * FROM validation_run_records WHERE incident_id = ?
                       ORDER BY created_at""",
                    (incident_id,),
                ).fetchall()
            )
        return list(self.conn.execute("SELECT * FROM validation_run_records ORDER BY created_at DESC").fetchall())

    def upsert_scheduler_state(
        self,
        *,
        workflow_id: str,
        tenant_id: str,
        weight: float,
        last_served_tick: int,
        wait_ticks: int,
        actions_taken: int,
        last_action_kind: str | None,
        updated_at: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO scheduler_state (
                   workflow_id, tenant_id, weight, last_served_tick, wait_ticks,
                   actions_taken, last_action_kind, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(workflow_id) DO UPDATE SET
                 tenant_id = excluded.tenant_id,
                 weight = excluded.weight,
                 last_served_tick = excluded.last_served_tick,
                 wait_ticks = excluded.wait_ticks,
                 actions_taken = excluded.actions_taken,
                 last_action_kind = excluded.last_action_kind,
                 updated_at = excluded.updated_at""",
            (
                workflow_id,
                tenant_id,
                weight,
                last_served_tick,
                wait_ticks,
                actions_taken,
                last_action_kind,
                updated_at,
            ),
        )

    def get_scheduler_state(self, workflow_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scheduler_state WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()

    def list_scheduler_state(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM scheduler_state ORDER BY workflow_id").fetchall())

    def upsert_deferred_action(
        self,
        *,
        defer_id: str,
        workflow_id: str,
        tenant_id: str | None,
        action_kind: str,
        priority: int,
        reason: str,
        status: str,
        created_at: str,
        updated_at: str,
        resolved_at: str | None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO deferred_actions (
                   defer_id, workflow_id, tenant_id, action_kind, priority, reason,
                   status, created_at, updated_at, resolved_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(defer_id) DO UPDATE SET
                 status = excluded.status,
                 reason = excluded.reason,
                 updated_at = excluded.updated_at,
                 resolved_at = excluded.resolved_at""",
            (
                defer_id,
                workflow_id,
                tenant_id,
                action_kind,
                priority,
                reason,
                status,
                created_at,
                updated_at,
                resolved_at,
            ),
        )

    def list_deferred_actions(self, status: str | None = None) -> list[sqlite3.Row]:
        if status is None:
            return list(self.conn.execute("SELECT * FROM deferred_actions ORDER BY created_at DESC").fetchall())
        return list(
            self.conn.execute(
                "SELECT * FROM deferred_actions WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
        )

    def get_credit_budget(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM credit_budget_state WHERE id = 1").fetchone()

    def upsert_credit_budget(
        self,
        *,
        emergency_reserve: float,
        experiment_budget_cap: float,
        experiment_spent: float,
        emergency_spent: float,
        last_sim_credits: float | None,
        updated_at: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO credit_budget_state (
                   id, emergency_reserve, experiment_budget_cap, experiment_spent,
                   emergency_spent, last_sim_credits, updated_at
               ) VALUES (1, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 emergency_reserve = excluded.emergency_reserve,
                 experiment_budget_cap = excluded.experiment_budget_cap,
                 experiment_spent = excluded.experiment_spent,
                 emergency_spent = excluded.emergency_spent,
                 last_sim_credits = excluded.last_sim_credits,
                 updated_at = excluded.updated_at""",
            (
                emergency_reserve,
                experiment_budget_cap,
                experiment_spent,
                emergency_spent,
                last_sim_credits,
                updated_at,
            ),
        )

    def bump_observability_counter(self, name: str, delta: float = 1.0) -> None:
        now = _now()
        self.conn.execute(
            """INSERT INTO observability_counters (name, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 value = value + excluded.value,
                 updated_at = excluded.updated_at""",
            (name, delta, now),
        )

    def list_observability_counters(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM observability_counters ORDER BY name").fetchall())

    def insert_operational_alert(
        self,
        *,
        alert_id: str,
        kind: str,
        severity: str,
        workflow_id: str | None,
        message: str,
        details: dict,
        created_at: str,
    ) -> bool:
        before = self.conn.total_changes
        self.conn.execute(
            """INSERT INTO operational_alerts (
                   alert_id, kind, severity, workflow_id, message, details_json, created_at, suppressed
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(alert_id) DO NOTHING""",
            (alert_id, kind, severity, workflow_id, message, json.dumps(details, sort_keys=True), created_at),
        )
        return self.conn.total_changes > before

    def list_operational_alerts(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM operational_alerts WHERE suppressed = 0
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        )

    def recent_proposals_for_workflow(self, workflow_id: str, *, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM proposals WHERE workflow_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (workflow_id, limit),
            ).fetchall()
        )


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
