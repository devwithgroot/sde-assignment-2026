# Implementation status — Phase 4

Phase 4 is done. The controller computes pickup, engagement, and business outcome separately, stores metric snapshots, and opens population-level detection signals. It still does not advance the clock, change config, run experiments, or ship a dashboard. Phase 5 has not started.

Checked on 8 Nov 2026 with `.venv` Python 3.11.9 against `http://127.0.0.1:8700` (`public_02_incidents`, minute 0), using the already projected DB `data/phase3_verify.sqlite` (schema upgraded to v3).

## Scope completed

- KPI engine: dials, connected, engaged, pickup, engagement, conclusion rate, hallucination flag rate.
- Missing denominators return `None` (printed as `n/a`), never a fake 0.0 rate when there is no data.
- Aggregations by workflow, cohort (`calls.cohort_id`), caller ID, and baseline/recent day windows.
- Metric snapshots persisted in SQLite.
- Detection signals (no config mutation):
  - pickup drift (baseline vs recent)
  - engagement drift (baseline vs recent)
  - caller ID health (pickup vs workflow overall)
  - hallucination spike (baseline vs recent, with absolute floor)
- Minimum sample `MIN_COHORT_SAMPLE` (100). Below that, no signal.
- Signal fingerprints prevent duplicate opens on restart.
- Read helpers: `list_metric_snapshots`, `list_signals`.

Explicitly not built: supervisor dashboard UI, experiments, approvals, config writes, `/sim/advance`.

## Files

Created:

- `controller/services/metrics.py`
- `controller/services/detector.py`
- `tests/unit/test_metrics.py`
- `tests/unit/test_detector.py`

Modified:

- `controller/persistence/db.py` — schema v3: `metric_snapshots`, `detection_signals`
- `controller/persistence/repositories.py` — totals, windows, snapshot/signal persistence
- `controller/main.py` — compute KPIs and print open signals after joins

## KPI definitions used

| KPI | Formula | No-data rule |
|---|---|---|
| Pickup | `connected / dials` | `None` if `dials == 0` |
| Engagement | `engaged / connected` | `None` if `connected == 0` |
| Business outcome | `distinct concluded leads / uploaded leads` | `None` if uploaded ≤ 0 |
| Hallucination rate | `halluc_flags / connected_with_qa` | `None` if no QA-bearing connects |

Uploaded leads for a workflow = rows in `leads`. Concluded leads = distinct `lead_id` with `concluded=1` (multiple attempts count once). Cohort business-outcome denominator = distinct leads in `cohort_assignments` for that cohort (documented estimate; live configs currently assign everyone to `default`).

Attribution always comes from the call event (`cohort_id`, `caller_id`, `config_version`).

## Detection rules

| Signal | Evidence | Thresholds |
|---|---|---|
| `pickup_drift` | baseline days vs last 2 recent days | both windows ≥ 100 dials; absolute drop ≥ 0.05 |
| `engagement_drift` | same windows | both ≥ 100 connected; drop ≥ 0.05 |
| `hallucination_spike` | same windows | both ≥ 100 QA connects; recent ≥ 0.03 and rise ≥ 0.02 |
| `caller_id_health` | caller pickup vs workflow pickup | both ≥ 100 dials; absolute drop ≥ 0.08 |

No signal is opened from one call. Re-running detection with the same fingerprint inserts nothing.

## Verified live

| Workflow | dials | pickup | engagement | conclusion |
|---|---|---|---|---|
| `nw_renewal` | 48783 | 0.2743 | 0.5545 | 0.4818 |
| `nw_kyc` | 27231 | 0.2621 | 0.5999 | 0.4369 |
| `ks_cancel` | 65018 | 0.2528 | 0.5042 | 0.3947 |

- Metric snapshots stored: 17 (workflow all/baseline/recent + per caller ID / cohort slices).
- Open signals on this history-only minute-0 world: **0**.
- Why zero is correct here: the spam-flagged caller ID in `public_02` fires at D2 10:00, which is after minute 0. Pre-D0 history does not contain that incident. Synthetic unit tests do open `caller_id_health`, `pickup_drift`, and `engagement_drift`.
- Simulator minute stayed 0. Config versions stayed 1.

## Tests

```text
.\.venv\Scripts\python.exe -m pytest -q tests\unit
45 passed
```

```text
.\.venv\Scripts\python.exe -m pytest -q tests
52 passed in 12.58s
```

Covered: hand-calculated KPIs, zero-denominator `None`, repeated attempts / distinct conclusions, small-sample silence, caller-ID gap, pickup drift, engagement-only drift, fingerprint dedupe.

## Commands

```text
.\.venv\Scripts\python.exe -m pytest -q tests\unit
.\.venv\Scripts\python.exe -m controller.main --url http://127.0.0.1:8700 --db data/phase3_verify.sqlite
.\.venv\Scripts\python.exe -m pytest -q tests
```

## Assumptions

- Baseline = all call days except the last two; recent = last two distinct `YYYY-MM-DD` values in call timestamps. If fewer than three days exist, drift signals are skipped; caller-ID health can still run on the full window.
- Snapshot unique keys store empty string / `-1` sentinels for null cohort/caller/version because SQLite treats each NULL as distinct in UNIQUE indexes.
- Detection is advisory only in this phase. It does not propose or apply config.

## Limitations

- No supervisor dashboard (explicitly deferred).
- No experiments or governance (Phase 5+).
- Live history at minute 0 produced no open signals; caller-ID degradation after D2 is not visible until the clock advances in a later phase.
- Wilson intervals are attached to KPI dicts for description only. They are not the winner test.

## Phase 4 gate

- KPIs and population detection implemented and tested.
- Denominator edge cases return no-data, not zero.
- Live run left the simulator unchanged.
- Phase 5 has not started.

Suggested commit message, not created:

```text
feat: add KPI metrics and population issue detection
```
