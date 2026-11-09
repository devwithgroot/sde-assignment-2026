# Controller monitoring dashboard

Read-only local UI over the controller SQLite database. It shows what the controller has ingested, joined, measured, flagged, experimentally evaluated, and (from Phase 6) tracked as incidents, proposals, approvals, and audit decisions. It does **not** advance the simulator, submit approvals, apply config, resolve incidents, or run controller actions.

## Start

From the repo root, with `.venv` active or using the venv Python:

```powershell
.\.venv\Scripts\python.exe -m dashboard --db data/phase3_verify.sqlite --sim-url http://127.0.0.1:8700
```

Then open [http://127.0.0.1:8800/](http://127.0.0.1:8800/).

Useful flags:

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `data/controller.sqlite` | Controller SQLite path |
| `--sim-url` | unset | Optional. Only `GET /health` and `GET /sim/state` |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8800` | Bind port |

JSON snapshot: [http://127.0.0.1:8800/api/snapshot](http://127.0.0.1:8800/api/snapshot).

## What you will see

- Simulator clock (if `--sim-url` is set)
- Event / campaign / lead / call counts and ingest checkpoints
- Workflow pickup, engagement, conclusion rates (`n/a` = no denominator)
- Caller ID pickup slices
- Open detection signals and reasons
- Cohort assignment counts
- Config-change and approval-decision **events** from the simulator (if ingested)
- Ingest parse issues
- **Experiments** — status, arms, samples, stats, promotion status
- **Incidents** — severity, status, linked experiment/proposal, resolution
- **Proposals** — source, route, dims, impact, affected leads, dry-run limitation note, status
- **Controller approvals** — pending/approved/rejected/expired/lapsed/applied, expiry, apply_by, reasons
- **Audit decisions** — controller action history
- **LLM suggestion attempts** — status, selected prompt, validation id, linked proposal (suggestions are untrusted)
- **Validation runs** — pass_rate, threshold meet/fail, errors
- **Fairness & observability** — scheduler waits/actions, deferred reasons, credit reserves, alerts, counters
- Explicit **not implemented yet** for Phase 9 grading/submission UI

## Insufficient / unavailable data

- Missing DB → error, not a fake healthy page.
- Empty incident/proposal/approval/audit tables → clear “none yet” / warning copy, not fabricated success.
- Pending proposals or open incidents → warnings in the snapshot.
- KPI `n/a` still means no denominator, not zero.

## Safety

- Dashboard code paths only read SQLite.
- Simulator access, when enabled, is GET-only.
- No `/approvals`, `/apply`, `/config` PUT, or `/sim/advance` from this process.
- The dashboard never submits proposals, applies approvals, or closes incidents.
