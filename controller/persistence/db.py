"""SQLite connection and schema.

One transaction per chunk: events, issue rows, the chunk record, and the
checkpoint commit together or not at all. Phase 3 adds normalized join tables
and a projection cursor over the events table.
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

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    tenant_id TEXT,
    leads INTEGER,
    config_version_at_creation INTEGER,
    ts TEXT,
    event_id INTEGER NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    lang TEXT,
    city_tier TEXT,
    age_band TEXT,
    value_band TEXT,
    features_json TEXT NOT NULL,
    ts TEXT,
    event_id INTEGER NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS calls (
    event_id INTEGER PRIMARY KEY,
    lead_id TEXT,
    person_id TEXT,
    campaign_id TEXT,
    workflow_id TEXT,
    tenant_id TEXT,
    attempt_no INTEGER,
    campaign_day_index INTEGER,
    config_version INTEGER,
    cohort_id TEXT,
    welcome_message TEXT,
    voice TEXT,
    caller_id TEXT,
    prompt_version TEXT,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    concluded INTEGER NOT NULL DEFAULT 0,
    disposition TEXT,
    duration_s INTEGER,
    turns INTEGER,
    halluc_flag INTEGER,
    flow_adherence REAL,
    ts TEXT,
    lead_matched INTEGER NOT NULL DEFAULT 0,
    campaign_matched INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS config_changes (
    event_id INTEGER PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    version INTEGER,
    actor TEXT,
    changed_dims_json TEXT,
    proposal_id TEXT,
    approval_id TEXT,
    ts TEXT
);

CREATE TABLE IF NOT EXISTS approval_decisions (
    event_id INTEGER PRIMARY KEY,
    approval_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    status TEXT,
    reasons_json TEXT,
    ts TEXT
);

CREATE TABLE IF NOT EXISTS cohort_assignments (
    lead_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    cohort_id TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (lead_id, config_version)
);

CREATE TABLE IF NOT EXISTS projection_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_event_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_workflow ON events(workflow_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_chunk ON events(chunk);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_person ON events(person_id);
CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id);

CREATE INDEX IF NOT EXISTS idx_leads_person ON leads(person_id);
CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(campaign_id);
CREATE INDEX IF NOT EXISTS idx_leads_workflow ON leads(workflow_id);
CREATE INDEX IF NOT EXISTS idx_calls_lead ON calls(lead_id);
CREATE INDEX IF NOT EXISTS idx_calls_person ON calls(person_id);
CREATE INDEX IF NOT EXISTS idx_calls_campaign ON calls(campaign_id);
CREATE INDEX IF NOT EXISTS idx_calls_workflow ON calls(workflow_id);
CREATE INDEX IF NOT EXISTS idx_calls_cohort ON calls(cohort_id);
CREATE INDEX IF NOT EXISTS idx_calls_unmatched ON calls(lead_matched);
CREATE INDEX IF NOT EXISTS idx_cohort_assignments_workflow ON cohort_assignments(workflow_id, config_version);
"""

SCHEMA_VERSION = 2


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
        return
    current = int(row["version"])
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"unsupported schema version {current}")
    if current < SCHEMA_VERSION:
        # CREATE IF NOT EXISTS above already added Phase 3 tables to older files.
        conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
