# Implementation status — Phase 3

Phase 3 is done. Raw events are projected into campaigns, leads, and calls. One shared cohort engine assigns leads from workflow config. The controller still does not advance the clock, submit approvals, or change config. Phase 4 has not started.

Checked on 8 Nov 2026 with `.venv` Python 3.11.9 against `http://127.0.0.1:8700` (`public_02_incidents`, minute 0). `sim/` was not modified. Scenario YAML and `runs/` were not read by the controller.

## Scope completed

- Schema v2: `campaigns`, `leads`, `calls`, `config_changes`, `approval_decisions`, `cohort_assignments`, `projection_state`.
- Event projection in `event_id` order after ingest.
- Call → lead by `lead_id`. Lead → campaign by `campaign_id`.
- Out-of-order calls stored with `lead_matched=0` and repaired when the lead arrives.
- Historical `cohort_id` kept on the call row (what was dialled).
- Recomputed assignments stored in `cohort_assignments` via `controller.domain.cohorts`.
- First-match cohort rules: AND across features, OR within a feature list, max 20 cohorts, else `default`.
- Restart-safe projection cursor. A second run projects 0 new rows.
- Concluded = `engaged` or a concluded disposition. Distinct concluded leads are counted once.

Not built, on purpose: KPI time series, detection, experiments, approvals, config writes, `/sim/advance`, supervisor UI.

## Files

Created:

- `controller/domain/cohorts.py` — the only cohort matching implementation
- `controller/services/joiner.py` — normalize and join
- `tests/unit/test_cohorts.py`
- `tests/unit/test_joiner.py`

Modified:

- `controller/persistence/db.py` — schema version 2 and join tables
- `controller/persistence/repositories.py` — projection cursor, join helpers, counts
- `controller/main.py` — project after ingest and print join summary

## Schema and join architecture

1. Phase 2 ingest writes the `events` table.
2. `project_events` reads `events` after `projection_state.last_event_id`.
3. Each event type writes its normalized table with `ON CONFLICT DO NOTHING`.
4. A call looks up lead and campaign. Missing lead → `lead_matched=0`.
5. When a lead is inserted, unmatched calls for that `lead_id` are repaired.
6. If a current workflow config is supplied, the lead is assigned a cohort with the shared engine and stored under `(lead_id, config_version)`.
7. The projection cursor advances only after a successful batch transaction.

## Shared cohort engine

`controller.domain.cohorts` is the single place for:

- `CohortDefinition` / `CohortAssignment`
- `parse_cohort_definitions`
- `assign_cohort` / `matches_where`
- `MIN_COHORT_SAMPLE` / `cohort_is_actionable` for later phases

Historical analysis must use `calls.cohort_id`. Evaluating a proposed config must call `assign_cohort` again. Detection and experiments must import this module, not copy the rules.

## Verified on the running simulator

Database: `data/phase3_verify.sqlite`.

| Check | Result |
|---|---|
| State before/after | minute 0, `public_02_incidents` |
| Configs after | all three workflows version 1 |
| Events projected | 187274 |
| Campaigns | 42 |
| Leads | 46200 |
| Calls | 141032 |
| Unmatched calls | 0 |
| Cohort assignments | 46200 (all `default`; live configs have empty `cohorts`) |
| Returning people | 9888 (`person_id` in more than one campaign) |
| Distinct concluded leads | 19990 |
| Projection cursor | 187274 |
| Restart | ingest inserted 0; project processed 0; counts unchanged |
| Non-default call cohorts | 0 on this history |

## Tests

```text
.\.venv\Scripts\python.exe -m pytest -q tests\unit
38 passed in 0.72s
```

```text
.\.venv\Scripts\python.exe -m pytest -q tests
45 passed in 10.34s
```

Unit coverage includes first-match / AND-OR / missing feature / default, call-before-lead repair, returning people, multiple attempts, historical vs recomputed cohort ids, config/approval projection, and restart-safe projection.

## Commands

```text
.\.venv\Scripts\python.exe -m pytest -q tests\unit
.\.venv\Scripts\python.exe -m controller.main --url http://127.0.0.1:8700 --db data/phase3_verify.sqlite
.\.venv\Scripts\python.exe -m controller.main --url http://127.0.0.1:8700 --db data/phase3_verify.sqlite
.\.venv\Scripts\python.exe -m pytest -q tests
```

## Assumptions

- Concluded means `engaged` is true or `disposition` is `concluded_interested` / `concluded_not_interested`. Distinct `lead_id` avoids double-counting multiple attempts.
- Empty `cohorts` in the live default config means every recomputed assignment is `default`. That matches the dialled `cohort_id` on this history.
- Schema upgrades from Phase 2 files use `CREATE IF NOT EXISTS` and bump `schema_meta` to 2. Existing Phase 2 event rows are projected on the next run.
- `MIN_COHORT_SAMPLE = 100` is recorded for later phases. Phase 3 does not refuse small cohorts; it only assigns them.

## Limitations

- No KPI aggregates yet. Those are Phase 4.
- Live configs on this server have no custom cohorts, so the shared engine's non-default path was verified in unit tests, not against the live history file.
- Config and approval event tables were verified with fixtures. This history chunk had none of those event types.
- Projection walks every new event once. At load scale that may need batching tuning later; this history finished in one run.

## Phase 3 gate

- Joins and shared cohort engine are implemented and tested.
- Live history projected with unmatched calls = 0 and restart processing 0.
- Simulator minute stayed 0. Config versions stayed 1.
- Phase 4 has not started.

Suggested commit message, not created:

```text
feat: normalize events and add shared cohort engine
```
