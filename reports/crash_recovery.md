# Crash recovery evidence report

**Document purpose.** Document the controller's recovery behaviour after an unexpected failure, including reuse of the same SQLite database, reconciliation with the still-running simulator, and resumption to completion. The available evidence does not independently verify the absence of duplicate HTTP-level apply or proposal requests.
**Date of demonstrated incident:** 8 Nov 2026 (wall clock in recovery JSON / logs).
**Related code:** `controller/recovery.py`, `controller/services/crash_recovery.py`, `controller/services/governance.py`, `controller/services/ingestion.py`.
**Related tests:** `tests/unit/test_crash_recovery.py` (mocked ingest/apply idempotency); governance unit tests for uncertain auto-apply on the normal cycle path.

---

## 1. Purpose and recovery objective

The assignment requires crash safety: kill the controller at any point, restart it, and continue without:

- double-applying an approval,
- losing a pending approval,
- forgetting learned experiment state,
- advancing simulated time during recovery itself.

This report separates two different kinds of evidence:

| Evidence class | Status |
|---|---|
| **A. Unexpected application exception** mid-run → recovery CLI → resume to finish | **Demonstrated** with retained logs |
| **B. Intentional OS-level process kill** (Ctrl+C / `Stop-Process` / SIGKILL) → same-DB resume | **Planned procedure only** (`scripts/crash_recovery_demo.ps1`); **not executed** |

These must not be combined into one successful claim.

---

## 2. System components involved

| Component | Role in recovery |
|---|---|
| Simulator (`sim serve`) | Remains running; owns live config versions, approval objects, and simulated clock |
| Controller process (`controller.main`) | Normal loop; may die mid-cycle |
| Controller SQLite DB | Durable events, checkpoints, proposals, approvals, experiments, audit |
| Recovery CLI (`python -m controller.recovery`) | Reconciles without posting `/sim/advance` |
| `run_crash_recovery()` | Idempotent ingest → reconcile approvals → apply approved → resume prompt incidents → audit/alert |
| Normal governance cycle | Also reconciles uncertain auto-applies; recovery CLI currently does **not** |

---

## 3. Failure scenario actually observed (Evidence A)

### Scenario context

- Simulator: `python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01`
- Controller DB: `data/ctrl_public01.sqlite`
- Advance size: `--advance-minutes 240`
- Logs: `logs/public01_controller.log` (pre-crash), `logs/public01_crash_recovery.log`, `logs/public01_controller_resume.log`

### What failed

After the first cycle had ingested history, planned underpowered welcome experiments, and **submitted three exploration proposals**, the controller advanced simulated time to minute 240 and then crashed during the next governance pass while scanning open proposals:

```text
AttributeError: 'sqlite3.Row' object has no attribute 'get'
```

Stack (from `logs/public01_controller.log`): crash inside `propose_experiment_exploration` while filtering proposal rows.

This is an **unexpected application exception**, not an intentional OS kill.

### Provenance caveat

This demonstration used `runs/mine01` / `data/ctrl_public01.sqlite`. It is **not** the same process that produced the locked grading report `reports/public_01.md` (that grade came from a later clean run). The recovery behaviour remains valid live evidence of exception → reconcile → resume.

---

## 4. Chronological incident timeline (verified)

Event ordering below is reconstructed from retained controller logs. Simulated-time values come from controller state lines. The only wall-clock timestamp presented as such is the explicitly recorded recovery timestamp (`2026-11-08T09:30:28Z` in the recovery JSON / alert). No other wall-clock timestamps are invented.

| Phase | Evidence | What happened |
|---|---|---|
| Bootstrap | `public01_controller.log` | History ingest: 187400 events committed; project produced 46200 leads / 141158 calls |
| Cycle 1 planning | same | Three experiments underpowered; `exploration_submitted` for nw_renewal, nw_kyc, ks_cancel |
| Cycle 1 governance | same | `proposals=3 submitted=3 auto_applied=0 applied=0`; approvals pending = 3 |
| Advance | same | `/sim/advance` 240 minutes → `sim_minute=240`, `finished=false` |
| Crash | same | `AttributeError` on `sqlite3.Row.get` during next explore scan |
| Recovery CLI | `public01_crash_recovery.log`, wall `2026-11-08T09:30:28Z` | Recovery ran against same URL + DB; alert `recovery_ran` |
| Resume start | `public01_controller_resume.log` | Controller reopened same DB; observed `sim_minute=240` still |
| Resume apply | same, cycle summary | `cycle=3 … applied=3` — the resume cycle summary reported applied=3 for the three pending approvals |
| Later learning | same | `nw_renewal` reached `winner=W3` (log line at winner decision) |
| Finish | same | `done cycles=48 wall_seconds=174.3 … minute=11520 finished=true db=data/ctrl_public01.sqlite` |

Simulator process was **not** restarted between crash and finish.

---

## 5. State persisted before the failure

From the pre-crash cycle summary and recovery supervisor snapshot:

| State | Value at crash boundary |
|---|---|
| Events in controller DB | 187400 (cursor already at last history event) |
| Calls / leads | 141158 calls, 46200 leads |
| Experiments | 3 (welcome_message on each workflow), underpowered, exploration submitted |
| Proposals | 3 explore proposals persisted before crash |
| Controller approvals | 3 pending |
| Incidents | 0 |
| Simulated minute | 240 |
| Credits remaining (sim) | 200.0 |

Because proposals and approvals were written before the exception, recovery had durable rows to reconcile against the simulator.

---

## 6. What the recovery process does

Implementation: `controller/services/crash_recovery.py` → `run_crash_recovery()`.

When `mutate=true` (default for the CLI):

1. **Idempotent ingestion** — `ingest_available(client, store)` resumes from checkpoints. Duplicate chunks/events are counted, not re-inserted.
2. **Approval reconciliation** — `reconcile_approvals(client, store)` GETs simulator approval state into local rows.
3. **Apply approved** — `apply_approved(client, store)` POSTs `/apply` only for approvals already approved and still inside apply rules.
4. **Prompt-repair resumption** — `run_prompt_incident_cycle(..., mutate=True)` resumes proposed prompt attempts without inventing duplicate suggestions by design.
5. **Duplicate accounting** — counts proposal_id collisions (should be zero if primary keys held).
6. **Audit + alert** — writes `crash_recovery_completed` and emits `recovery_ran` (severity low).

Explicit non-goals of the recovery CLI:

- It does **not** call `/sim/advance`.
- It does **not** call `reconcile_uncertain_auto_applies` (that path runs inside `run_governance_cycle` on normal loops).

---

## 7. Exact recovery commands used

Recovery (no time advance):

```powershell
.\.venv\Scripts\python.exe -m controller.recovery --url http://127.0.0.1:8700 --db data/ctrl_public01.sqlite
```

Resume on the **same** database:

```powershell
.\.venv\Scripts\python.exe -m controller.main --url http://127.0.0.1:8700 --db data/ctrl_public01.sqlite --advance-minutes 240
```

---

## 8. Same-database reuse after recovery

Both recovery and resume pointed at `data/ctrl_public01.sqlite`. The resume log final line repeats `db=data/ctrl_public01.sqlite`. No DB file swap, restore-from-backup, or scenario re-seed was required for the controller side. The simulator continued from the same `runs/mine01` world at the post-crash minute.

---

## 9. What was verified after recovery

### Verified (Evidence A)

| Check | Result |
|---|---|
| Recovery CLI exit path produced JSON report | Yes (`logs/public01_crash_recovery.log`) |
| Approvals reconciled | **3** |
| Applies during recovery CLI itself | **0** (approvals still pending; apply window not yet reached) |
| Ingest committed during recovery | **0** history / **0** events (idempotent; already at event 187400) |
| Duplicate proposals reported | **0** |
| Simulated minute unchanged by recovery | Yes — resume still saw `sim_minute=240` before further advances |
| Resume cycle summary reported applied=3 for the three pending approvals | Yes — `applied=3` on resume cycle 3 |
| Scenario completed | Yes — `finished=true` at minute 11520; `done cycles=48 wall_seconds=174.3` |
| Simulator stayed up across controller crash | Yes |

### Not verified / limitations

| Check | Status |
|---|---|
| Intentional OS kill (Ctrl+C / Stop-Process / SIGKILL) | **Not executed** |
| Uncertain auto-apply reconciliation inside recovery CLI | **Not performed by this CLI** (only on normal governance cycle) |
| Bit-identical proposal bytes / HTTP-level duplicate POSTs vs pre-crash | Not independently verified; recovery reported `duplicate_proposals=0` only |
| Same evidence as locked `reports/public_01.md` grade run | **No** — different run directory / later clean grade |

---

## 10. Simulated time during recovery

Recovery CLI design and observed resume state agree: **simulated time did not advance during recovery**.

- Code path posts no `/sim/advance`.
- First resume state line still reports `sim_minute=240`, matching the pre-crash advance.
- Subsequent minute increases occur only after resume `controller.main` advances again.

---

## 11. Final outcome and remaining limitations

**Outcome of Evidence A.** The controller recovered from an unexpected mid-run application exception, reused the same SQLite database, reconciled three pending approvals, and drove the simulator to `finished=true`. Precisely: recovery reported `duplicate_proposals=0`; the resume cycle summary reported `applied=3` for the three pending approvals; HTTP-level duplicate POSTs were **not** independently verified.

**Remaining limitations.**

1. This is exception-path evidence, not an intentional process-kill demonstration.
2. Recovery CLI omits uncertain-auto-apply reconciliation.
3. Evidence provenance differs from the locked public_01 grading report.
4. Unit tests cover mocked restart idempotency but cannot replace a live OS-kill demo.

---

## 12. Planned intentional kill / restart procedure (Evidence B — not executed)

### Script

`scripts/crash_recovery_demo.ps1` is a Windows PowerShell procedure intended to demonstrate Evidence B.

### Intended steps

1. Refuse to start if something is already serving `:8700`.
2. Start a **fresh** public_01 simulator under a dedicated `--run-dir` (default `runs/mine01_crash_demo`).
3. Start the controller against a **fresh** DB (default `data/ctrl_public01_crash_demo.sqlite`).
4. Wait until `sim_minute > 0` (or timeout).
5. Kill **only** the controller with `Stop-Process`; leave the simulator running.
6. Run `python -m controller.recovery` against the **same** DB (no advance).
7. Resume `controller.main` on the same URL + DB until `finished=true` or timeout.
8. Emit labelled logs under `logs/crash_demo_*` and a summary file for manual review of duplicates / finish state.

### Status

**Not executed for this submission.** No successful output from this script is claimed. Until it is run and its labelled output is reviewed and pasted here, intentional-kill recovery remains an **incomplete** reliability demonstration.

### Success criteria (for a future run)

1. Simulator stays up across controller kill.
2. Recovery CLI exits 0 against the same DB.
3. Simulator reaches `finished=true` after resume.
4. No duplicate explore/winner POSTs for the same `proposal_id` (check sim log + DB).
