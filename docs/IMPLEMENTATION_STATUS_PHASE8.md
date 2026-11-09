# Implementation status — Phase 8

Phase 8 is done: deterministic fair scheduling, emergency vs experiment credit reserves, approver-attention deferrals, observability counters/alerts/supervisor summary, crash-recovery CLI/script, and dashboard panels. Phase 9 has not started.

Checked on 8 Nov 2026 with `.venv` Python 3.11.9.

## Implementation

### Fair scheduling
- `controller/services/fairness.py` — weighted round-robin using `(wait+1)*weight*urgency`, ties broken by `workflow_id`.
- Urgency boosts hallucination / high-severity incidents and approved-awaiting-apply.
- Deferred actions persisted with reason + priority; starvation waits emit alerts.
- Main orders prompt-incident and governance work via the scheduler.

### Credit protection
- `controller/services/credits.py` — `emergency_reserve` (default 30) held back from experiment/routine spend; prompt LLM/validation uses the emergency pool; experiment spend capped (`experiment_budget_cap`, default 50).
- Prompt cycle checks budget before `/llm/suggest-fixes` and records emergency spend after LLM/validation.

### Approver attention
- Local soft cap using `max_requests_per_24h` on open draft/submitted/approved proposals before promoting experiment winners; excess work is deferred (not forced through).

### Observability
- `controller/services/observability.py` — counters, idempotent operational alerts, supervisor summary (actions/waits by tenant/workflow, credits, deferred, incidents/proposals/approvals, health).
- Structured JSON logs for alerts and summary builds.
- Dashboard **Fairness & observability** section (read-only).

### Crash recovery
- `controller/services/crash_recovery.py` + `python -m controller.recovery`
- Scripts: `scripts/crash_recovery.sh`, `scripts/crash_recovery.ps1`
- Re-ingest (idempotent), reconcile approvals, apply approved, resume proposed prompt attempts; **does not advance time**.
- Reconciliation will not downgrade locally `applied` approvals/proposals when the poll still says `approved`.

## Schema (v7)

`scheduler_state`, `deferred_actions`, `credit_budget_state`, `observability_counters`, `operational_alerts`

## Files changed / added

Created: `fairness.py`, `credits.py`, `observability.py`, `crash_recovery.py`, `controller/recovery.py`, `scripts/crash_recovery.sh`, `scripts/crash_recovery.ps1`, `tests/unit/test_fairness.py`, `tests/unit/test_crash_recovery.py`, this status doc.

Modified: `db.py` (v7), `repositories.py`, `prompt_incident.py`, `governance.py`, `main.py`, dashboard queries/render/app, `DASHBOARD.md`, `IMPLEMENTATION_STATUS.md`.

## Tests (mocked unless noted)

```text
.\.venv\Scripts\python.exe -m pytest -q tests
94 passed
git diff --check
(clean)
```

Includes:
- Round-robin / weighted fairness; no starvation over a horizon
- Incident priority without permanent starvation
- Emergency reserve blocking experiment spend; experiment cap exhaustion
- Deferral + supervisor summary
- Ingest replay idempotency after “crash”
- Pending approval apply once; second recovery does not re-apply
- Dashboard still GET-only / read-only

## Recovery commands

```powershell
# After an unexpected stop (does not advance sim time):
.\scripts\crash_recovery.ps1 -SimUrl http://127.0.0.1:8700 -DbPath data/controller.sqlite

# Or:
.\.venv\Scripts\python.exe -m controller.recovery --url http://127.0.0.1:8700 --db data/controller.sqlite

# Observe-only recovery (no approval apply / LLM):
.\.venv\Scripts\python.exe -m controller.recovery --url http://127.0.0.1:8700 --db data/controller.sqlite --no-governance
```

**Recovery evidence in this session:** unit tests above simulate kill/restart. Live kill-during-ingest against a running world was **not** executed here.

## Limitations / not verified live

- Continuous controller loop with `/sim/advance` still not built (single-shot `main` + recovery CLI).
- Live multi-tenant fairness under a long public_03 load not measured.
- Live crash kill + restart against `:8700` not run in this session (mocked only).
- Approver-hour deferral still primarily simulator-side; local cap is a soft open-proposal limit.
- Phase 9 public grading / `SUBMISSION.md` not started.

## Suggested commit message

```text
feat: add fairness observability and recovery

Add weighted fair scheduling, emergency credit reserves, deferred-action
audit, supervisor summary/alerts, dashboard fairness panels, and a
crash-recovery CLI that reconciles ingest and approvals without advancing time.
```
