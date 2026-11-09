# Implementation status

**As of 9 Nov 2026** on `feat/outcome-first-agent-loop`.

This file is the **top-level status summary**. It must stay consistent with `SUBMISSION.md` and the locked reports under `reports/`. It does **not** claim that every blueprint acceptance requirement is complete.

## Verdict

The controller implements a closed HTTP-only outcome loop (ingest → join/cohorts → KPIs → detect → experiment → governed propose/apply → audit), with a recovery CLI and a read-only dashboard. **public_01** and **public_02** are graded. Several capabilities remain **Partial** or **Cut**. An intentional OS-level kill/restart has **not** been executed. Full-day `public_03_load` was **not** completed.

Authoritative write-up: [`SUBMISSION.md`](../SUBMISSION.md).

## Evidence links

| Artifact | Role |
|---|---|
| [`SUBMISSION.md`](../SUBMISSION.md) | Graded outcomes, C1–C15, stakeholder replies, known weaknesses |
| [`reports/public_01.md`](../reports/public_01.md) | Locked public_01 grade |
| [`reports/public_02.md`](../reports/public_02.md) | Locked public_02 grade |
| [`reports/public_03_load.md`](../reports/public_03_load.md) | Attempt A partial (1080/7200); Attempt B disk-full |
| [`reports/crash_recovery.md`](../reports/crash_recovery.md) | Exception-path recovery; OS kill **not executed** |
| [`docs/IMPLEMENTATION_BLUEPRINT.md`](IMPLEMENTATION_BLUEPRINT.md) | Planned design + **as-built reconciliation (9 Nov 2026)** |
| [`docs/ENGINEERING_REVIEW.md`](ENGINEERING_REVIEW.md) | 8 Nov review; superseded findings labelled |

## C1–C15 (aligned with SUBMISSION.md)

| # | Status | One-line gap |
|---|---|---|
| C1 | Partial | public_03 Attempt A stopped at **1080/7200**; &lt;10 wall-min/day unmet |
| C2 | Partial | Shared table exists; graded runs used only `default` |
| C3 | Built | History before D0 ingested |
| C4 | Cut | Call-time optimisation deliberately out of scope |
| C5 | Partial | Rotation coded; graded public_02 had **zero** `caller_id` proposals; DID share 0.49–0.517 |
| C6 | Partial | Welcome explore/promote works; voice unused; Holm-style thresholds ≠ classical Holm FWER |
| C7 | Partial | Rollback applied but restored explore allocation (W3 residual **0.344**) |
| C8 | Partial | One prompt MTTR **1455**; credits 9 / val 3 / LLM 1; other incidents incomplete |
| C9 | Built | Lifecycle / expire / stale / uncertain auto-apply recovery |
| C10 | Built | Impact / affected / dry-run present; explore guardrail `not_assessed` |
| C11 | Built | Read-only dashboard (not started by `run.sh`) |
| C12 | Built | `audit_decisions` queryable |
| C13 | Partial | Exception-path demonstrated; intentional OS kill **not executed** |
| C14 | Partial | Counters/alerts exist; event→decision lag **not instrumented** |
| C15 | Partial | WFQ/credits exist; experiment credit pool unused in graded runs |

Do **not** overstate C7, C8, C13, C14, or C15.

## Measured public outcomes (locked reports)

- **public_01:** ALL conclusion **+2.2%**, pickup **+0.3%**, engagement **+1.6%**; 4 applies (2B/2H); 4 approvals all approved; 0 credits. Advance claimed **60** min.
- **public_02:** ALL conclusion **+1.2%**, pickup **-0.6%**, engagement **+1.7%**; 8 applies (6B/2H); 9 approval requests (8 approved, 1 expired, **0 lapsed**); credits **9**, validations **3**, LLM **1**; DID share **0.49–0.517** days 2–9; ks_cancel prompt MTTR **1455**; W3 day-9 share **0.344**. Advance claimed **120** min. **No** `caller_id` proposals.
- **Harmful exploration** observed on both public_01 and public_02 (2 harmful applies each).
- **public_03:** Attempt A measured to **1080/7200** simulated minutes; lag not instrumented; SRE target unmet. Attempt B failed on disk full.

## Crash recovery (honest)

- **Demonstrated:** unexpected application exception mid-public_01 → `python -m controller.recovery` → resume to `finished=true` (`reports/crash_recovery.md` Evidence A).
- **Not demonstrated:** intentional OS-level kill/restart (`scripts/crash_recovery_demo.ps1` exists, **not executed**). Do not treat C13 as Built.

## How to run

```powershell
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01
# other terminal:
bash run.sh http://127.0.0.1:8700
# or:
.\.venv\Scripts\python.exe -m controller.main --url http://127.0.0.1:8700 --db data/controller.sqlite
```

Controller advances until the scenario finishes. Use `--once` for a single non-advancing pass. Dashboard remains read-only and optional.

## Constraints still held

- No `sim/` edits; controller never reads `runs/` or scenario hidden truth as runtime input.
- Governance boundary absolute; `--no-governance` observes only.
- Pickup and engagement stay separate KPIs.

## Historical phase documents

Phase write-ups below are **historical records** of incremental work. They are not current acceptance claims. Prefer this file and `SUBMISSION.md` for present status.

- `docs/IMPLEMENTATION_STATUS_PHASE1.md` … `PHASE8.md`
- `docs/IMPLEMENTATION_STATUS_PHASE9.md`
- `docs/DASHBOARD.md` — read-only monitor
