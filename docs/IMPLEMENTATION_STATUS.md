# Implementation status

Phases 0–9 are complete for the assignment floor. Phase 9 added the continuous advance loop, exploration proposals, public_01/02 grading, public_03 load measurements (partial), crash-recovery evidence, and `SUBMISSION.md`.

Per-phase details:

- `docs/IMPLEMENTATION_STATUS_PHASE1.md` … `PHASE8.md`
- `docs/IMPLEMENTATION_STATUS_PHASE9.md` — public scenarios, load, submission
- `docs/DASHBOARD.md` — read-only monitor
- `SUBMISSION.md` — deliverable write-up
- `reports/public_01.md`, `reports/public_02.md`, `reports/public_03_load.md`, `reports/crash_recovery.md`

Checked on 8 Nov 2026 with `.venv` Python 3.11.9.

## How to run

```powershell
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01
# other terminal:
bash run.sh http://127.0.0.1:8700
# or:
.\.venv\Scripts\python.exe -m controller.main --url http://127.0.0.1:8700 --db data/controller.sqlite
```

Controller advances until the scenario finishes. Use `--once` for a single non-advancing pass. Dashboard remains read-only.

## Constraints still held

- No `sim/` edits; controller never reads `runs/` or scenario hidden truth as input.
- Governance boundary absolute; `--no-governance` observes only.
- Pickup and engagement stay separate KPIs.

## Known gaps (also in SUBMISSION.md)

- public_03 not finished end-to-end; &lt;10 wall-min/day unmet.
- Caller-ID rotation partial; some prompt approvals lapsed on public_02.
- One harmful exploration apply on each of public_01/02.
