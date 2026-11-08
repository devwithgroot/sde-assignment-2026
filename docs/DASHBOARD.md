# Controller monitoring dashboard

Read-only local UI over the controller SQLite database. It shows what the controller has ingested, joined, measured, flagged, and (from Phase 5) evaluated as observational experiments. It does **not** advance the simulator, submit approvals, change workflow config, run experiments interactively, or bypass governance.

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
- Config-change and approval-decision **events** from the simulator (if any were ingested)
- Ingest parse issues
- **Experiments (Phase 5)** when the controller has evaluated them:
  - Experiment status and progress (`planned` / `running` / `winner` / `inconclusive` / `guardrail_violation`)
  - Workflow, cohort, and dimension under test
  - Control vs challenger variant arms
  - Sample sizes, `sample_ok`, and minimum-sample warnings
  - Baseline vs challenger primary / guardrail rates
  - Statistical evidence: effect size, p-value, Holm-adjusted α′, winner-selection reason
  - Winning, losing, inconclusive, and guardrail-violating arms
  - Promotion status (`ready_to_propose_observe_only` until Phase 6)
  - Experiment history rows already stored in SQLite
- Explicit **not implemented yet** panels for proposals, controller approval stores, and a full audit decision API (Phase 6+)

## Insufficient / unavailable data

- Missing DB → error, not a fake healthy page.
- Experiment tables missing → warning that Phase 5 has not been run on this DB.
- Tables present but no rows → “insufficient data — nothing to show.”
- `status=running` / `decision=underpowered` → not enough samples for a winner; no fabricated result.
- KPI `n/a` still means no denominator, not zero.

## What you will not see yet

Phase 6+ has not been built:

- Controller-authored proposals
- Approval submit / apply / promotion actions
- A full decision audit API beyond detection signals + experiment evidence JSON

Those sections stay labeled unavailable.

## Safety

- Dashboard code paths only read SQLite.
- Simulator access, when enabled, is GET-only.
- No `/approvals`, `/apply`, `/config` PUT, or `/sim/advance` from this process.
- The dashboard never starts experiments, edits allocations, or marks winners for apply.
