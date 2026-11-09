# Implementation status — Phase 9

Phase 9 is done for the mandatory floor: continuous controller loop, public_01/02 graded end-to-end, public_03 load measurements (partial but measured), crash-recovery evidence, `SUBMISSION.md`, and verification. No further phase started.

Checked on 8 Nov 2026 with `.venv` Python 3.11.9.

## What was implemented

### Continuous loop
- `SimulatorClient.advance` (`POST /sim/advance`); 409 `finished` treated as terminal.
- `controller.main` defaults to run-until-finished; `--once` for single pass; `--advance-minutes` / `CONTROLLER_ADVANCE_MINUTES` (multiple of 15, default 60).
- `run.sh` starts the continuous controller (sets `PYTHONUTF8` / `PYTHONUNBUFFERED`).

### Exploration proposals (required for learning)
- `build_explore_proposal` + `propose_experiment_exploration` submit planned balanced allocations when experiments are underpowered (governance path). Without this, public scenarios stayed 100% control and never learned.

### Public runs (live)
| Scenario | Commands (abbrev.) | Result |
|---|---|---|
| public_01 | baseline `runs/base01`; serve `runs/mine01`; controller `data/ctrl_public01.sqlite` advance 240; `sim grade` → `reports/public_01.md` | Finished. ALL conclusion **+1.6%**, engagement **+1.3%**. 4 applies (3 beneficial, 1 harmful). |
| public_02 | baseline `runs/base02`; serve `runs/mine02`; controller `data/ctrl_public02.sqlite` advance 240; grade → `reports/public_02.md` | Finished. ALL conclusion **−1.7%**. 4 applies (3 beneficial, 1 harmful). Prompt proposals validated then **lapsed**. Credits 54 / validations 18 / LLM 6. |
| public_03 | serve `runs/mine03`; controller `data/ctrl_public03.sqlite` advance 360 | **Partial.** Stopped at sim_minute **1080/7200**. Measured in `reports/public_03_load.md`. |

### Crash recovery (live)
- Controller crashed mid-public_01 after cycle 1 (`sqlite3.Row.get` bug — fixed).
- `controller.recovery` reconciled 3 pending approvals without advancing time.
- Same DB resumed to finished. Evidence: `reports/crash_recovery.md`, `logs/public01_crash_recovery.log`.

### Submission
- `SUBMISSION.md` completed (headings 0–14 + stakeholder replies).
- Reports: `reports/public_01.md`, `reports/public_02.md`, `reports/public_03_load.md`, `reports/crash_recovery.md`.

## Exact reproducible commands

```powershell
$env:PYTHONUTF8='1'
# public_01
.\.venv\Scripts\python.exe -m sim baseline --scenario scenarios/public_01_dev.yaml --run-dir runs/base01
.\.venv\Scripts\python.exe -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01
# other terminal:
.\.venv\Scripts\python.exe -m controller.main --url http://127.0.0.1:8700 --db data/ctrl_public01.sqlite --advance-minutes 240
# or: bash run.sh http://127.0.0.1:8700
.\.venv\Scripts\python.exe -m sim grade --run runs/mine01 --baseline runs/base01 --out reports/public_01.md

# public_02 (same pattern with public_02_incidents / base02 / mine02 / ctrl_public02)

# recovery
.\.venv\Scripts\python.exe -m controller.recovery --url http://127.0.0.1:8700 --db data/ctrl_public01.sqlite

# tests
.\.venv\Scripts\python.exe -m pytest -q tests
git diff --check
```

## Tests

```text
.\.venv\Scripts\python.exe -m pytest -q tests
96 passed, 1 warning in 360.96s
git diff --check
(clean; only CRLF normalization notice on test_governance.py)
```

Added/updated: client advance unit test; explore proposal unit test; `--once` preserves observe path.

## Limitations / missing evidence

- public_03 **not** finished to `end_minute`; SRE “&lt;10 wall-min/sim-day” **not met** (~107 min/day extrapolated).
- Event→decision lag histogram not instrumented.
- public_02 DID rotation not applied; prompt MTTR null due to lapsed approvals.
- Grade on Windows requires `PYTHONUTF8=1` (sim writes Unicode Δ).

## Suggested commit message

```text
test: validate public scenarios and document results

Add continuous advance loop and exploration proposals; grade public_01/02;
measure public_03 load; complete SUBMISSION.md and crash-recovery evidence.
```
