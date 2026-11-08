# public_03_load measurements

Measured live against `python -m sim serve --scenario scenarios/public_03_load.yaml --run-dir runs/mine03`.
Controller: `python -m controller.main --url http://127.0.0.1:8700 --db data/ctrl_public03.sqlite --advance-minutes 360`.

**Honesty note:** The full 5 simulated days was not completed in this session. The controller was stopped after progressing to `sim_minute=1080` / `end_minute=7200` because each cycle's metrics/experiment recompute over multi-million-row SQLite dominated wall time. Numbers below are **actual measurements from the live partial run**, not inferred from YAML.

## Methodology

- Advance tick: **360** simulated minutes per cycle.
- Wall clock: ~**80 minutes** observed from controller start to stop (history ingest + project + 3 completed cycles + in-progress 4th).
- Peak memory: max sampled working set ~**188 MB** during the run (process `WorkingSet64`).
- DB size: SQLite file size at stop.
- Event throughput: controller `events` rows / wall seconds (includes history bootstrap).
- Decision lag: not instrumented as event-ts → decision-ts; bottleneck is per-cycle full recompute (see below).
- Credits/decisions: from controller SQLite.

## Results

| Metric | Value |
|---|---|
| Sim minute at stop | 1080 / 7200 |
| Sim finished | False |
| Simulated days completed | 0.75 |
| Wall-clock seconds (approx) | 4800 |
| Wall-clock per simulated day (extrapolated) | ~6400 s (~106.7 min) |
| Peak working set (MB) | ~188 |
| Controller DB size (MB) | 7396.7 |
| Events in controller DB | 6215415 |
| Calls in controller DB | 4679295 |
| Leads in controller DB | 1536000 |
| Event ingest throughput (events/s wall, incl. bootstrap) | 1294.9 |
| Controller cycles completed | 3.0 |
| Proposals | 12 |
| Approvals tracked | 12 |
| Approvals applied (local) | 12 |
| Sim credits remaining | 400.0 |

### Credit budget (controller view)

```
{"id":1,"emergency_reserve":30.0,"experiment_budget_cap":50.0,"experiment_spent":0.0,"emergency_spent":0.0,"last_sim_credits":400.0,"updated_at":"2026-11-08T10:51:13.994342+00:00"}
```

### History bootstrap (from controller stderr)

- History chunks: 23
- History events inserted: **5,537,379**
- First project pass: processed **5,537,379** → campaigns=84, leads=1,344,000, calls=4,193,295

## Bottlenecks observed

1. **Full metrics + experiment recompute each cycle** over multi-million `calls` rows — dominant after bootstrap.
2. **History bootstrap** itself is large (5.5M events → multi-GB SQLite) before D0 advances.
3. **Stdout buffering** on Windows redirected logs hid cycle lines (stderr progress remained visible).

## SRE 10 wall-min / simulated-day target

**Not met.** Extrapolated ~106.7 wall-minutes per simulated day on this machine/build.

## 10× scaling plan

At 10× dials/workflows, the first break is single-writer SQLite + full-table experiment scans. Partition by tenant/workflow, maintain rolling KPI windows, and keep file-backed shards before introducing a new managed database (aligns with SRE preference to avoid new stateful infra).
