# Implementation status — Phase 2

Phase 2 is done. The controller stores simulator events in SQLite and can ingest the same chunks again without duplicating rows. It still does not advance the clock, submit approvals, or change workflow config. Phase 3 has not started.

Checked on 8 Nov 2026 with `.venv` Python 3.11.9 against `http://127.0.0.1:8700` (`public_02_incidents`, minute 0). `sim/` was not modified. The controller did not read `runs/` or scenario files.

## Scope completed

- SQLite schema, connection, and WAL mode.
- One events table for history and live chunks, with the raw JSON line plus a few indexed columns.
- Chunk commit records, per-kind checkpoints, and an issues table for bad lines.
- Idempotent insert (`INSERT OR IGNORE` on `event_id`).
- A chunk, its issues, and its checkpoint commit in one transaction. A failure rolls that work back.
- History is ingested first, then live events. At minute 0 the live list is empty.
- Restart reads the checkpoint and does not download a chunk that is already committed.
- Phase 1 observe output is unchanged and still runs before ingestion.

Not built, on purpose: cohorts, lead/call joins, metrics, detection, experiments, approvals, config writes, and `/sim/advance`.

## Files

Created:

- `controller/persistence/db.py` — connection and schema
- `controller/persistence/repositories.py` — store, transaction, chunk commit
- `controller/services/parsing.py` — NDJSON parsing
- `controller/services/ingestion.py` — fetch metadata, download bodies, commit
- `tests/unit/test_storage.py`
- `tests/unit/test_ingest.py`
- `tests/integration/test_live_ingest.py`

Modified:

- `controller/api/client.py` — added `chunk_text`. Existing GET methods are unchanged. There is still no advance, approval, or config write method.
- `controller/settings.py` — optional `CONTROLLER_DB` (default `data/controller.sqlite`) and `CONTROLLER_CHUNK_TIMEOUT_SECONDS` (default 180). `--url` still works. Optional `--db` was added.
- `controller/main.py` — after the Phase 1 observe pass, ingests into the database.
- `.gitignore` — `data/` and `*.sqlite`.

`run.sh` was not edited. It still execs `python -m controller.main --url "$SIM_URL"`, which now also ingests.

## Schema

`events` — `event_id` primary key. Columns: `chunk`, `kind` (`history` or `events`), `type`, `ts`, `workflow_id`, `tenant_id`, `campaign_id`, `lead_id`, `person_id`, `raw_json`. Indexes on workflow, type, timestamp, chunk, lead, person, and campaign.

`chunks` — one row per committed chunk number. Declared event count, inserted count, duplicate count, issue count.

`checkpoints` — one row per kind: last committed chunk number and the highest event id seen for that kind.

`ingest_issues` — `(chunk, line_no)` primary key, reason, and a truncated copy of the bad line.

`schema_meta` — version `1`.

## Ingestion

1. Read the checkpoint for `history`. If none exists, ask for chunks with `after=-1`.
2. Download each uncommitted chunk with `GET /events/chunks/{n}`. The `path` field on the metadata is not opened.
3. Parse NDJSON line by line. A line must be a JSON object with an integer `event_id` and a string `type`.
4. If the number of non-empty lines does not match the chunk's `events` count, raise and roll the transaction back. The checkpoint does not move.
5. Otherwise insert events, record bad lines, write the chunk row, and move the checkpoint in that same transaction.
6. Repeat for `kind=events`.

A bad line is stored in `ingest_issues` and is not inserted as an event. The chunk still commits when the line count matches, so the bad line is not fetched forever and is not dropped without a record.

A second run uses `after=<last committed chunk>`. Already-committed chunk numbers are skipped even if the client returns them again.

## Verified on the running simulator

Database: `data/phase2_verify.sqlite` (gitignored).

| Check | Result |
|---|---|
| State before | `public_02_incidents`, minute 0, not finished, 1 chunk, last event id 187274 |
| History chunks | chunk 0, 187274 events, event ids 1 through 187274 |
| Live chunks | empty list |
| First ingest | history inserted 187274, duplicates 0, issues 0. Live fetched 0 |
| Rows | 187274. Distinct `event_id` 187274 |
| Types | `call` 141032, `lead` 46200, `campaign_created` 42 |
| History checkpoint | chunk 0, event id 187274 |
| Events checkpoint | no row, because no live chunk has been committed |
| Second ingest, new connection, same file | history fetched 0, inserted 0. Live inserted 0. Row count still 187274 |
| CLI restart | `python -m controller.main --url http://127.0.0.1:8700 --db data/phase2_verify.sqlite` printed `inserted=0` for both kinds |
| State after | minute 0, scenario unchanged, last event id 187274 |
| Configs after | `nw_renewal`, `nw_kyc`, and `ks_cancel` still version 1, actor `business_default` |

The integration test `tests/integration/test_live_ingest.py` checks the same facts: counts match the chunk metadata, ids are unique, a reopened database inserts nothing, and workflow configs are unchanged.

## Verified with fixtures, not the live clock

The running simulator had no live chunks, and this phase is not allowed to call `/sim/advance`. Live ingestion was therefore also tested with a fake chunk of `kind=events`:

- A history chunk and a live chunk both insert, into separate checkpoints.
- Reopening the database and ingesting again fetches nothing new.
- An HTTP 500 on the chunk body leaves the checkpoint unset and inserts no rows.
- A second commit of the same chunk inserts nothing.
- A body whose line count disagrees with the metadata rolls back. Event count stays 0 and no checkpoint is written.
- A non-JSON line is one issue. The other lines in that chunk are stored.
- An empty chunk commits with 0 events.
- Two lines with the same `event_id` store one row and count one duplicate.

## Tests

```text
.\.venv\Scripts\python.exe -m pytest -q tests\unit tests\test_sim.py
31 passed in 3.87s
```

```text
.\.venv\Scripts\python.exe -m pytest -q tests\integration\test_live_ingest.py
1 passed in 6.68s
```

```text
.\.venv\Scripts\python.exe -m pytest -q tests
32 passed in 10.18s
```

No test failures.

## Commands

```text
.\.venv\Scripts\python.exe -m pytest -q tests\unit tests\test_sim.py
.\.venv\Scripts\python.exe -m pytest -q tests\integration\test_live_ingest.py
.\.venv\Scripts\python.exe -m controller.main --url http://127.0.0.1:8700 --db data/phase2_verify.sqlite
.\.venv\Scripts\python.exe -m pytest -q tests
```

`bash run.sh` was not run. `run.sh` execs the same module, and that module was run with `--url`.

## Assumptions

- A kind with no chunks does not get a checkpoint row. The next fetch uses `after=-1`. Verified for `events` on this server.
- Unknown event types are stored, not rejected. This history contained only `campaign_created`, `lead`, and `call`.
- `event_id` must be a JSON integer. Booleans are rejected. That rule was not hit by the live file.
- The default database for `run.sh` is `data/controller.sqlite`. The numbers above are from `data/phase2_verify.sqlite`.
- Chunk download uses a 180 second timeout because a history body is large. This run finished well inside that.

## Limitations

- Live events produced by the simulator were not ingested, because none exist at minute 0 and the clock was not advanced. The live code path was verified with a fixture and by fetching the empty live list from the server.
- Events are not split into lead or call tables. Later phases can read `raw_json` and the indexed columns.
- A truncated download that still has the declared number of lines would commit. A download with the wrong line count does not.

## Phase 2 gate

- Tests passed, including the live public_02 ingest.
- History ingestion matched the chunk metadata with no duplicate ids and no issues.
- Ingesting again after a restart inserted nothing.
- The simulator stayed at minute 0.
- Workflow configs stayed at version 1.
- Phase 1 observe output still runs.
- Phase 3 has not started.

Suggested commit message, not created:

```text
feat: persist simulator events with restart-safe ingestion
```
