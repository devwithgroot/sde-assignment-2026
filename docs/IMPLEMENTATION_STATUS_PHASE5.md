# Implementation status — Phase 5

Phase 5 is done. The controller plans and evaluates observational welcome_message / voice experiments with balanced traffic splits, minimum samples, deterministic two-proportion tests, Holm multiple-comparison protection, and guardrail checks. It still does not advance the clock, submit approvals, apply config, or promote winners (Phase 6).

Checked on 8 Nov 2026 with `.venv` Python 3.11.9. Full suite: `68 passed, 1 skipped` (live ingest skipped when simulator is down).

## Scope completed

- Experiment planner (welcome_message / voice only, one dimension per workflow+cohort).
- Candidate variants only from workflow `options` (unsupported values rejected).
- Balanced planned allocations with control floor and exploration floor.
- Shared cohort IDs from the Phase 3 cohort engine (`calls.cohort_id` / `cohort_assignments`).
- Minimum sample per arm = `MIN_COHORT_SAMPLE` (100) by default.
- Deterministic frequentist winner selection: two-proportion z-test + Holm-adjusted alphas.
- Verdicts: `winner`, `loser`, `inconclusive`, `guardrail_violation`, `underpowered`.
- Experiment lifecycle statuses: `planned` → `running` → `winner` | `inconclusive` | `guardrail_violation`.
- Historical dry-run / impact estimate stored in evidence (explicitly non-causal).
- `promotion_status = ready_to_propose_observe_only` — no apply.
- Dashboard extended to show real experiment rows (status, arms, samples, stats, reasons).
- Insufficient data shown as warnings / `underpowered`, never fabricated winners.

Explicitly not built (Phase 6+): approvals, apply, promotion, decay/rollback, proposal store, caller-ID exploration experiments, schedule-hour experiments.

## Files

Created:

- `controller/services/statistics.py`
- `controller/services/experiments.py`
- `tests/unit/test_statistics.py`
- `tests/unit/test_experiments.py`
- `docs/IMPLEMENTATION_STATUS_PHASE5.md`

Modified:

- `controller/persistence/db.py` — schema v4: `experiments`, `experiment_arms`
- `controller/persistence/repositories.py` — variant totals, experiment upsert/list
- `controller/main.py` — evaluate experiments after detection
- `dashboard/queries.py`, `dashboard/render.py`, `dashboard/app.py` — experiment panels
- `docs/DASHBOARD.md`
- `tests/unit/test_dashboard.py`
- `tests/integration/test_live_ingest.py` — skip on `SimulatorError` when sim is down

## Decision rules

| Rule | Value |
|---|---|
| Dimensions | `welcome_message`, `voice` (one active at a time per workflow+cohort) |
| Primary KPI | from `workflow.objective.primary` (`engagement_rate` → engagement, `pickup_rate` → pickup) |
| Guardrail | from `objective.guardrail` (e.g. `pickup_rate_max_drop`) |
| Min sample / arm | 100 (engagement uses connected; pickup uses dials) |
| Alpha | 0.05 before Holm |
| Min practical effect | 0.02 absolute rate lift |
| Control | heaviest current allocation, else most dials, else first option |
| Promotion | not applied; marked observe-only |

## Dashboard

See `docs/DASHBOARD.md`. The experiments section now shows:

- Experiment status, decision, control, winner, promotion status, min sample, reason
- Per-arm sample sizes, `sample_ok`, primary/guardrail rates, verdict, effect, p, Holm α′, reason
- Clear copy when tables exist but rows are empty, or when decision is underpowered

Still unavailable (labeled, not faked): proposals, controller approvals, full audit decision store.

## Tests

```text
.\.venv\Scripts\python.exe -m pytest -q tests
68 passed, 1 skipped
```

Coverage includes known winner, no difference, underpowered, guardrail harm, multiple-comparison Holm thresholds, one-dimension conflict, options validation, balanced allocations, repeatable decisions, and dashboard experiment data.

## Remains (not Phase 5)

- Phase 6: governance proposals, approvals, apply, promotion, supervisor reconciliation, decay/rollback
- Phase 7: prompt incident / LLM / validation loop
- Phase 8: fairness scheduler, observability, crash recovery
- Phase 9: public scenario grading and submission pack
