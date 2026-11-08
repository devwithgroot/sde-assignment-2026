"""SQLite connection and schema.

One transaction per chunk: events, issue rows, the chunk record, and the
checkpoint commit together or not at all.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY,
    chunk INTEGER NOT NULL,
    kind TEXT NOT NULL,
    type TEXT NOT NULL,
    ts TEXT,
    workflow_id TEXT,
    tenant_id TEXT,
    campaign_id TEXT,
    lead_id TEXT,
    person_id TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    declared_events INTEGER NOT NULL,
    first_event_id INTEGER,
    last_event_id INTEGER,
    inserted INTEGER NOT NULL,
    duplicates INTEGER NOT NULL,
    issues INTEGER NOT NULL,
    committed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    kind TEXT PRIMARY KEY,
    last_chunk INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_issues (
    chunk INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    reason TEXT NOT NULL,
    raw_line TEXT NOT NULL,
    PRIMARY KEY (chunk, line_no)
);

CREATE INDEX IF NOT EXISTS idx_events_workflow ON events(workflow_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_chunk ON events(chunk);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_person ON events(person_id);
CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id);
"""

SCHEMA_VERSION = 1


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
    elif row["version"] != SCHEMA_VERSION:
        raise RuntimeError(f"unsupported schema version {row['version']}")
