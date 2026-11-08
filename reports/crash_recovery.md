# Crash recovery evidence

## Method (live, public_01)

1. Started `python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01`.
2. Started controller with `data/ctrl_public01.sqlite`, `--advance-minutes 240`.
3. Controller completed cycle 1 (submitted 3 exploration proposals), advanced to `sim_minute=240`, then crashed on cycle 2 with `AttributeError` on `sqlite3.Row.get` while checking open proposals.
4. Simulator was **not** restarted (still at minute 240 with pending approvals).
5. Ran recovery (no time advance):

```powershell
.\.venv\Scripts\python.exe -m controller.recovery --url http://127.0.0.1:8700 --db data/ctrl_public01.sqlite
```

6. Resumed the same controller + same DB until finished:

```powershell
.\.venv\Scripts\python.exe -m controller.main --url http://127.0.0.1:8700 --db data/ctrl_public01.sqlite --advance-minutes 240
```

## Results

Recovery JSON (abbreviated from `logs/public01_crash_recovery.log`):

- `approvals_reconciled`: 3
- `applied`: 0 (approvals still pending at minute 240; apply window not yet reached)
- `ingest`: history/events committed 0 (idempotent; cursor already at event 187400)
- `duplicate_proposals`: 0
- alert `recovery_ran` recorded

Resume run (`logs/public01_controller_resume.log`):

- Reconciled the 3 pending approvals without re-POSTing duplicate exploration proposals.
- Applied 3 exploration changes once approved (`cycle=3 … applied=3`).
- Later submitted/applied winner promotion for `nw_renewal` (W3).
- Scenario reached `finished=true` at minute 11520 (`done cycles=48 wall_seconds=174.3`).

## What was lost / not lost

| State | Outcome |
|---|---|
| Event cursor / SQLite facts | Preserved (idempotent re-ingest) |
| Pending approvals | Reconciled from simulator; not lost |
| Duplicate proposal POST | Avoided |
| In-flight cycle mid-governance | Cycle restarted cleanly after recovery |
| Simulated time | Unchanged by recovery CLI (as designed) |

## Mocked unit coverage (also run)

`tests/unit/test_crash_recovery.py` covers ingest replay idempotency and single apply after restart without a live world.
