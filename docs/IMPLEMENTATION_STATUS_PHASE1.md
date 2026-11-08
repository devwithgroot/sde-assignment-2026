# Implementation status — Phase 1

Phase 1 is done. The controller reads the running simulator once and exits. It does not advance time, submit approvals, or change config. Phase 2 has not started.

Checked on 8 Nov 2026 with `.venv` Python 3.11.9 against the simulator already listening at `http://127.0.0.1:8700` (`public_02_incidents`, minute 0). `sim/` was not modified. Scenario YAML was not read.

Phase 0's API verification stays in `docs/IMPLEMENTATION_STATUS.md`.

## What was built

| File | Role |
|---|---|
| `controller/settings.py` | `SIM_URL` from `--url` or the environment. Timeout, retry count, and log level from `CONTROLLER_TIMEOUT_SECONDS` (30), `CONTROLLER_MAX_RETRIES` (2), and `CONTROLLER_LOG_LEVEL` (`INFO`). |
| `controller/api/models.py` | Pydantic models for health, sim state, workflow, current config, and chunk metadata. Unknown JSON fields are kept. |
| `controller/api/client.py` | GET-only httpx client. Timeouts. Retries on transport errors and HTTP 5xx, including `Retry-After`. No retry on 4xx. Errors raise `SimulatorError` with status, code, detail, and endpoint. |
| `controller/main.py` | One observe pass, JSON logs on stderr, a short summary on stdout, then exit. |
| `run.sh` | `./run.sh <SIM_URL>` starts that command. Uses `.venv` on Windows or Linux when it is present. |
| `tests/unit/test_client.py` | Retry, `Retry-After`, 4xx, bad JSON, connection failure, and the absence of write methods. |
| `tests/unit/test_models.py` | Payload shapes from the live server. |
| `tests/unit/test_observe.py` | The observe pass issues only GETs. Missing `--url` exits 2. |

No package was added. Pydantic and httpx were already installed.

The client has no method for `POST /sim/advance`, `POST /approvals`, `POST /workflows/{id}/apply`, or `PUT /workflows/{id}/config`.

## What this phase does not do

- It does not advance simulated time. `run.sh` does not run until the scenario finishes. That loop is a later phase. This phase was explicitly told not to advance.
- It does not download chunk bodies. It reads the chunk list (metadata) only, and it does not open the `path` field.
- It does not persist anything. There is no database yet.
- It does not choose cohorts, detect drift, or propose a config.

## Commands

```text
.\.venv\Scripts\python.exe -m pytest -q tests
18 passed in 3.67s
```

That is 6 existing simulator tests plus 12 new unit tests.

After the httpx access log was turned down, the unit tests were run again:

```text
.\.venv\Scripts\python.exe -m pytest -q tests\unit
12 passed in 0.24s
```

Live run, against the simulator that was already up:

```text
.\.venv\Scripts\python.exe -m controller.main --url http://127.0.0.1:8700
```

Stdout:

```text
observe scenario=public_02_incidents minute=0 finished=false workflows=3 history_chunks=1 event_chunks=0
  nw_renewal version=1 primary=engagement_rate mid_flight=allowed
  nw_kyc version=1 primary=pickup_rate mid_flight=next_campaign_only
  ks_cancel version=1 primary=engagement_rate mid_flight=allowed
```

Stderr is one JSON object per line. The same run included:

```text
{"event": "health", "ok": true}
{"chunks": 1, "credits_remaining": 200.0, "event": "state", "finished": false, "last_event_id": 187274, "scenario": "public_02_incidents", "sim_minute": 0, "tick_minutes": 15, "ts": "2027-01-04T00:00:00+05:30"}
{"event": "config", "actor": "business_default", "cohorts": 0, "prompt_version": "P1", "version": 1, "workflow_id": "nw_renewal"}
{"count": 1, "event": "chunks", "events": 187274, "kind": "history"}
{"count": 0, "event": "chunks", "events": 0, "kind": "events"}
```

`nw_kyc` logged `auto_apply: false` and `mid_flight: next_campaign_only`. `ks_cancel` logged auto-apply dims `schedule`, `caller_id`, `welcome_message`, `voice`. The clock in the response was still minute 0, so this process did not advance the simulator.

## Verified by this controller

| Check | Result |
|---|---|
| `GET /health` | `ok: true` |
| `GET /sim/state` | `public_02_incidents`, minute 0, not finished, tick 15, 200 credits, 1 chunk, last event id 187274 |
| `GET /workflows` | `nw_renewal`, `nw_kyc`, `ks_cancel` parse into `Workflow` |
| `GET /workflows/{id}/config` | version 1, actor `business_default`, prompt `P1`, no cohorts |
| `GET /events/chunks?kind=history` | 1 chunk, 187274 events |
| `GET /events/chunks?kind=events` | empty list |
| Writes | No POST or PUT from this process |

## Assumptions

- A missing URL exits 2. A simulator error exits 1. A clean read exits 0.
- `CONTROLLER_MAX_RETRIES=2` means two retries after the first attempt, so three tries.
- Backoff is `min(2^attempt, 8)` seconds plus a little jitter, unless `Retry-After` is present. `Retry-After` is capped at 30 seconds. Unit tests replace sleep with a no-op.
- Chunk `path` is logged nowhere and never opened.
- The assignment text says `./run.sh` runs until the scenario finishes. Phase 1 does not. The script comment says so. A later phase has to add the clock loop before that contract is met.
- `bash run.sh` was not executed on this Windows shell. The same command was run as `python -m controller.main --url ...`, which is what `run.sh` execs.

## Phase 1 gate

- Observe-only controller connects to the running simulator and prints the three workflows.
- Unit tests cover retries, 4xx, bad JSON, transport failure, model parsing, and GET-only behavior.
- No config was changed and the simulated minute stayed 0.
- `sim/` was not edited.
- Phase 2 was not started.

Suggested commit message, not created:

```text
feat: add observe-only controller client
```
