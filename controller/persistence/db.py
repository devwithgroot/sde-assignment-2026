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

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at TEXT NOT NULL,
    window_label TEXT NOT NULL,
    window_start_ts TEXT,
    window_end_ts TEXT,
    workflow_id TEXT NOT NULL,
    cohort_id TEXT,
    caller_id TEXT,
    config_version INTEGER,
    dials INTEGER NOT NULL,
    connected INTEGER NOT NULL,
    engaged INTEGER NOT NULL,
    uploaded_leads INTEGER,
    concluded_leads INTEGER,
    pickup REAL,
    engagement REAL,
    conclusion_rate REAL,
    halluc_flags INTEGER,
    connected_with_qa INTEGER,
    halluc_rate REAL,
    UNIQUE (window_label, workflow_id, cohort_id, caller_id, config_version)
);

CREATE TABLE IF NOT EXISTS detection_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    kpi TEXT NOT NULL,
    dimension TEXT,
    cohort_id TEXT,
    caller_id TEXT,
    severity TEXT NOT NULL,
    baseline_value REAL,
    recent_value REAL,
    baseline_n INTEGER,
    recent_n INTEGER,
    threshold REAL,
    message TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    sim_ts TEXT,
    created_at TEXT NOT NULL,
    suppressed INTEGER NOT NULL DEFAULT 0,
    UNIQUE (fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_metric_snapshots_workflow ON metric_snapshots(workflow_id, window_label);
CREATE INDEX IF NOT EXISTS idx_detection_signals_workflow ON detection_signals(workflow_id, signal_type);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    tenant_id TEXT,
    cohort_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    primary_kpi TEXT NOT NULL,
    guardrail_kpi TEXT NOT NULL,
    guardrail_max_drop REAL NOT NULL,
    status TEXT NOT NULL,
    control_variant TEXT NOT NULL,
    planned_allocation_json TEXT NOT NULL,
    min_sample_per_arm INTEGER NOT NULL,
    alpha REAL NOT NULL,
    min_effect REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decision TEXT,
    decision_reason TEXT,
    winner_variant TEXT,
    promotion_status TEXT,
    evidence_json TEXT
);

CREATE TABLE IF NOT EXISTS experiment_arms (
    experiment_id TEXT NOT NULL,
    variant TEXT NOT NULL,
    is_control INTEGER NOT NULL DEFAULT 0,
    planned_weight REAL NOT NULL,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    primary_rate REAL,
    guardrail_rate REAL,
    sample_n INTEGER NOT NULL DEFAULT 0,
    sample_ok INTEGER NOT NULL DEFAULT 0,
    verdict TEXT,
    effect_size REAL,
    p_value REAL,
    adjusted_alpha REAL,
    ci_low REAL,
    ci_high REAL,
    reason TEXT,
    PRIMARY KEY (experiment_id, variant),
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id)
);

CREATE INDEX IF NOT EXISTS idx_experiments_workflow ON experiments(workflow_id, status);
CREATE INDEX IF NOT EXISTS idx_experiment_arms_exp ON experiment_arms(experiment_id);
CREATE INDEX IF NOT EXISTS idx_calls_voice ON calls(voice);
CREATE INDEX IF NOT EXISTS idx_calls_welcome ON calls(welcome_message);
"""

SCHEMA_VERSION = 4


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
