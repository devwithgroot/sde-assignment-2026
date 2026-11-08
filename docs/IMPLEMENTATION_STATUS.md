# Implementation status

Phase 0 is done. Phase 1 is done: an observe-only controller reads the live simulator and exits. It does not advance time. Phase 2 has not started.

Checked on 8 Nov 2026 with `.venv` Python 3.11.9. Simulator: `public_02_incidents`, `http://127.0.0.1:8700`, run directory `runs/phase0_02` (gitignored, disposable). World generation reported ready in 3.8 seconds. `python -m pytest -q tests` from `.venv`: 6 passed in 3.82s. Those tests belong to the simulator, not to a controller.

## Repository inventory

Present:

- `sim/` — the world. Not modified.
- `docs/SIMULATOR.md` — HTTP contract.
- `docs/IMPLEMENTATION_BLUEPRINT.md` — build plan. Phase 0 updated the verified-facts section from the live server.
- `docs/ASSIGNMENT_EXPLAINED.md` — plain-language notes. Not a deliverable.
- `scenarios/public_01_dev.yaml`, `public_02_incidents.yaml`, `public_03_load.yaml` — launch files only. The controller must not read them.
- `stakeholders/01` through `06` — Sales, Northwind compliance, platform/SRE, Kestrel supervisor, product, CTO.
- `examples/naive_controller.py` — bad HTTP example. `run.sh` no longer calls it.
- `controller/` — Phase 1 observe-only client. GET health, state, workflows, configs, and chunk metadata.
- `tests/unit/` — client, model, and observe tests.
- `tests/test_sim.py`, `Makefile`, `requirements.txt`, `SUBMISSION_TEMPLATE.md`, `README.md`.
- `.venv/` — project environment. Gitignored.

Absent:

- `SUBMISSION.md`
- `reports/`
- `scripts/crash_recovery.sh`

`run.sh <SIM_URL>` starts `python -m controller.main`. Phase 1 exits after one read. It does not run until the scenario finishes. That loop is a later phase.

## Proposed tree

Not created in Phase 0. Phase 1, when named, adds the first slice only: `controller/` settings, CLI, HTTP client, observe-only loop, and a `run.sh` that starts it.

```text
controller/
  __init__.py
  main.py
  settings.py
  api/client.py
  api/models.py
tests/unit/
docs/IMPLEMENTATION_STATUS.md
run.sh
```

Later phases add the rest of the tree in the blueprint. SQLite stays behind repository functions. No new service, and no dependency beyond the current `requirements.txt`, unless a later phase justifies one.

## Verified live, this run

| Check | Result |
|---|---|
| `GET /health` | `200 {"ok": true}` |
| `GET /docs` | `200` HTML Swagger page |
| `GET /openapi.json` | Title `Outcome-loop simulator`. Paths match `sim/server.py`. Every body is an untyped object. Component schemas are only `HTTPValidationError` and `ValidationError`. |
| `GET /sim/state` | `scenario=public_02_incidents`, `tick_minutes=15`, `credits_remaining=200`, `end_minute=14400` (10 days), `ts=2027-01-04T00:00:00+05:30` at start |
| `POST /sim/advance` `0`, `10`, `14` | `400 {"error":"bad_minutes","detail":"minutes must be a positive multiple of 15"}` |
| `POST /sim/advance` `15`, then `30` | `200`. Clock moved `0 → 15 → 45`. Both returned `new_chunks: []` because the dial window had not opened. |
| `GET /workflows` | Three workflows. Public objects include cadence, window, capacity, `min_gap_minutes`, options, `prompt_catalog` (summaries only), objective, governance, `mid_flight`. `nw_kyc` auto-apply is `enabled: false`. |
| `GET /workflows/nw_renewal/config` | Keys `workflow_id`, `version`, `actor`, `applied_at`, `config`. Config keys `cohorts`, `allocations`, `prompt_version`, `schedule`. |
| `POST /approvals` | `200`. Keys: `approval_id`, `proposal_id`, `workflow_id`, `status`, `base_version`, `changed_dims`, `created_at`, `expires_at`. No `proposed_config`. |
| `POST /approvals` with almost no fields | `422 {"error":"missing_fields","detail":[...]}` listing every missing required name. |
| `POST /workflows/nw_renewal/apply` with only `approval_id` while pending | `409 {"error":"not_approved","detail":{"status":"pending"}}` |
| Same apply after `apply_by` | `410 {"error":"approval_lapsed","detail":"approved change was not applied within the apply window"}`. Following `GET` status was `lapsed`. `apply_by` was no longer in the body. `decided_at` and `reasons` remained. |
| Decided approval | Created `2027-01-04T00:45`. `decided_at` `2027-01-04T10:00`. `apply_by` `2027-01-04T12:00`. `reasons` `[]`. `changed_dims` `["voice"]`. |
| History chunks | One chunk. List fields: `chunk`, `events`, `first_event_id`, `kind`, `last_event_id`, `path`, `until_minute`, `until_ts`. `last_event_id` at startup was `187274`. Types in that chunk: `campaign_created`, `lead`, `call`. |
| Live events from 00:45 on D0 through 12:00 on D2 | One new chunk. Types: `campaign_created` 9, `lead` 9900, `call` 23300, `approval_decided` 1. No other type. No `config_changed`. |
| `ks_cancel` pickup by caller ID | 4 Jan `D1` 0.238 (2418 dials), `D2` 0.249 (2382). 5 Jan `D1` 0.245, `D2` 0.252. 6 Jan through noon `D1` 0.252 (587), `D2` 0.145 (613). The drop is visible only in the call rows. |

`path` on a chunk record points at a file under the simulator run directory. The controller fetches `GET /events/chunks/{n}` and does not open that path.

## Source-verified, not exercised live

- A successful apply returns `workflow_id`, `version`, and `changed_dims`, and the simulator writes the config it stored at proposal time. This run never applied in time, so that `200` body was not seen.
- `did_flagged` in the simulator source updates caller-ID health and writes no event. The live stream agrees: no such event type, and `D2` pickup on `ks_cancel` fell on 6 Jan. The controller must not hard-code `D2` or that date. Hidden scenarios will differ.
- Decision time is `now` plus a random draw from `approver_latency_minutes`, then snapped into `approver_hours`. Delay that falls before the window is not added again at opening. The 00:45 → 10:00 decision matches that rule. One sample does not show the in-hours case.

## Assumptions still open

- Successful-apply response fields, until a later phase applies inside `apply_by`.
- Whether `public_03` splits history into more than one chunk. This run of `public_02` used one.
- Exact false-positive rate of `qa.hallucination_flag`. The contract says the flag is noisy. No threshold was chosen in Phase 0.
- Pydantic is already installed because FastAPI needs it. Phase 1 uses it for response models. No new package was added.

## Stakeholder memos

Read. Not implemented. Policies observed on the live `GET /workflows` match the earlier reading: KYC auto-apply is off, renewal auto-apply is schedule and caller ID only (`max_shift` 0.5), Kestrel auto-apply includes welcome and voice at `max_shift` 0.2. Sales' claim that KYC auto-apply is allowed is false on this payload. Replies belong in `SUBMISSION.md`, which is a later phase.

## Phase 0 gate

- Live `/docs` and `/openapi.json` inspected.
- Advance limit, approval body, apply body, and caller-ID event behavior checked.
- Blueprint updated where the live server added facts the old text did not state.
- `sim/` not edited. Scenario YAML not used as a controller input.
- No controller package.

Suggested commit message, not created:

```text
docs: record phase 0 simulator verification
```

## Phase 1

The Phase 1 record is `docs/IMPLEMENTATION_STATUS_PHASE1.md`.

## Phase 2

The Phase 2 record is `docs/IMPLEMENTATION_STATUS_PHASE2.md`. Phase 3 has not started.
