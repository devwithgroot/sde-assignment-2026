"""Event, chunk, and checkpoint repository."""

from __future__ import annotations

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
