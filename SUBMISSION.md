# Outcome-First Agent Loop — Submission

**Name:** Sandeep · **Date:** 9 Nov 2026 · **Hours actually spent (be honest):** Not formally tracked · **Language / storage:** Python 3.11 / SQLite (WAL)

> Copied from `SUBMISSION_TEMPLATE.md`. Headings retained. Numbers below are taken from locked grading reports and retained logs unless labelled as code-level verification, estimate, or incomplete.

---

## 0. TL;DR

1. The controller is an HTTP-only closed loop over the simulator: ingest → join/cohorts → KPIs → detect → experiment → governed propose/apply → audit, plus a recovery CLI and a read-only dashboard.
2. On `public_01`, ALL conclusion **+2.2%**, pickup **+0.3%**, engagement **+1.6%**; 4 applies (2 beneficial, 2 harmful); all 4 approvals approved; 0 credits.
3. On `public_02`, ALL conclusion **+1.2%**, pickup **-0.6%**, engagement **+1.7%**; 8 applies (6 beneficial, 2 harmful); one prompt fix with MTTR 1455; DID share stayed 0.49–0.517 (never rotated); W3 residual 0.344 after rollback.
4. Deliberately cut call-time optimisation (C4). Left caller-ID mitigation, decay quality, intentional kill-demo, and load performance Partial.
5. SRE target of &lt;10 wall-minutes per simulated day on `public_03_load` was **not met**; a later attempt failed on disk full.

## 1. Assumptions

1. The simulator HTTP API (`docs/SIMULATOR.md`) is the only world truth. The controller does not import `sim/`, parse scenario YAML, or read `runs/` as runtime inputs.
2. Simulated time advances only when the continuous loop POSTs `/sim/advance`. The recovery CLI never advances time.
3. Advance size must be a positive multiple of `tick_minutes` (15). The graded public_01 run used **60**-minute advances; the graded public_02 run used **120**-minute advances. `run.sh` / `Settings` default remains **60** minutes unless overridden by `--advance-minutes` or `CONTROLLER_ADVANCE_MINUTES`.
4. Live configs often start at 100% control, so challenger arms cannot gather samples until an exploration proposal is approved/applied through governance.
5. Experiment constants (from source): minimum **100** samples per arm, α = **0.05**, practical minimum effect = **0.02** (absolute), two-proportion z-test, per-comparison Holm-adjusted α thresholds `α/(m-rank)`.
6. Pickup and engagement are separate KPIs and are never blended into one success score. Conclusion rate is reported as the business outcome above both.
7. Detection uses population thresholds, not single calls. When enough calendar days exist, “recent” is the last **two** calendar days (`split_baseline_recent`).
8. Signed workflow `governance.auto_apply` overrides stakeholder recollections. Example: Sales’ claim that Northwind KYC allows auto-apply is false against live policy.
9. Prompt validation pass_rate is a safety gate only; it is not evidence of engagement lift.
10. A persisted arm verdict of `guardrail_violation` excludes that variant from later exploration for the same experiment id. A statistical `loser` is not treated as harmful.
11. SQLite WAL is acceptable inside the assignment envelope. At 10× production dial volume, partitioning is required before introducing new managed infrastructure.
12. Hours spent were not formally tracked; no estimate is inferred from git timestamps.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion at load scale | Partial | Idempotent chunk ingest and restartable checkpoints work; Attempt A on public_03 progressed but missed the &lt;10 wall-min/day target; Attempt B failed on disk full | Incremental metrics, rolling windows, disk/WAL guards, tenant-parallel workers |
| C2 | Shared cohorts | Partial | One `cohort_assignments` table is shared by metrics, experiments and dashboard; graded runs used only `default` | Feature-based cohorts actually used by detection and experiments |
| C3 | History bootstrap | Built | History chunks are ingested before D0 advances | — |
| C4 | Call-time optimisation | Cut | Lower ROI than closing the learning + governance loop inside the budget | Per-hour schedule experiments under governance |
| C5 | Caller-ID health | Partial | Detector + rotation builder + governance path exist; graded public_02 contains **zero** `caller_id` approval dims and flagged DID share stayed in **0.49–0.517** (days 2–9) | Recent-window DID comparison, multi-step rotate, post-apply share verification |
| C6 | Welcome/voice experiments | Partial | Welcome explore → stats → promote works end-to-end; voice is coded but unused in graded runs; dimensions are one-at-a-time with no interaction model | Voice scheduling; interaction-aware analysis if required |
| C7 | Winner / decay / rollback | Partial | Winners promote and decay rollback applies; public_02 restored the pre-promotion **explore** allocation, leaving W3 at 0.344 on day 9 | Rollback to a safe residual; continue pressure after `rolled_back` |
| C8 | Hallucination incident loop | Partial | Detect → LLM → validate → approve → apply worked once (ks_cancel prompt, MTTR 1455); credits 9 / validations 3 / LLM 1 | Faster apply-window timing; verify/close remaining open incidents |
| C9 | Governance client | Built | Persist-then-submit, reconcile, expire/lapse/stale paths, uncertain auto-apply recovery, apply-by-id | Fatigue-aware batching polish |
| C10 | Impact / dry-run | Built | Proposals carry impact, affected leads, diff and dry-run with labelled limitations | True historical cohort replay |
| C11 | Supervisor view | Built | Read-only FastAPI dashboard over the controller SQLite | Auth and multi-tenant packaging |
| C12 | Audit store | Built | `audit_decisions` answers why / who / what next from the controller DB | — |
| C13 | Crash recovery | Partial | Unexpected application-exception recovery demonstrated; intentional OS kill script exists but has **not** been executed | Run and attach intentional-kill demo output |
| C14 | Loop observability | Partial | Counters, alerts and supervisor summary exist | Event→decision lag; stable incident fingerprints across sliding windows |
| C15 | Fair credits/attention | Partial | WFQ, deferrals and emergency reserve exist; experiment credit pool was unused in graded runs | Multi-tenant load proof; spend experiment budget when appropriate |

**Selection principle.** Ship a correct closed loop — ingest, evidence, governance, audit, restart — before summit cosmetics. Prefer an honest Partial with a named gap over Built without public evidence. That is why C4 was cut and why C5/C7/C13 remain Partial despite substantial code.

## 3. Stakeholders

### 01 — Sales (Nikhil)
- **Accepted:** Show measurable lift; use Kestrel auto-apply only where signed policy already allows it. Appears as separate KPI reporting and policy-gated auto-apply in governance.
- **Pushed back / declined:** Blended summit score; silent “persuasive” prompt rollout; skipping approvals for speed; Northwind KYC auto-apply.
- **Wrong against policy:** “Northwind told me they’re fine with auto-apply on the KYC flow” — live `nw_kyc` has `auto_apply.enabled=false`.
- **Reply (≤120 words):** We’ll show separate pickup and engagement numbers, not a blended summit score. Kestrel stays on the dims its signed policy allows; Northwind KYC still needs human approval — that QBR note does not match the live contract. Prompt changes move only with validation evidence and an approval. Speed comes from same-day explore/apply windows, not from bypassing governance.

### 02 — Northwind Compliance (Meera)
- **Accepted:** Human approval for customer-facing changes; validation evidence on scripts; 24h auditability; prefer slow/right; respect approver hours. Appears as `/approvals` for welcome/voice/prompt, `validation_run_id` on prompt proposals, and `audit_decisions`.
- **Pushed back / declined:** Nothing material — the memo aligns with platform constraints.
- **Wrong against policy:** None.
- **Reply (≤120 words):** Agreed. Welcome, voice and prompt changes go through `/approvals` with diff, dry-run and (for prompts) `validation_run_id`. Operational latitude for schedule/caller-ID stays inside the signed policy and will appear in the weekly summary. Any applied change is queryable from our audit store by workflow and time within 24 hours.

### 03 — Platform/SRE (Kabir)
- **Accepted:** Survive stop/restart; idempotency; actionable alerts; prefer no new stateful infra; publish load numbers; expose loop health. Appears as recovery CLI, unique chunk/event/proposal keys, operational alerts, SQLite WAL, `reports/public_03_load.md`, and supervisor summary.
- **Pushed back / declined:** Claiming one public_03 simulated day in &lt;10 wall minutes; claiming an intentional kill demo that has not been executed.
- **Wrong against policy:** None.
- **Reply (≤120 words):** We recovered after an unexpected controller exception mid-public_01 (reconcile + resume to finish) without double-applying. A deliberate OS-level kill script is prepared but not yet run. Storage stays SQLite WAL. Load numbers are in `reports/public_03_load.md`: partial run, target not met; a later attempt died on disk full. At 10× the first break is full-table recompute on a single SQLite writer — shard by tenant before Postgres.

### 04 — Kestrel Ops (Anita)
- **Accepted:** Do not silently overwrite supervisor edits; limit request flood; one consolidated screen; avoid lifeless compliance scripts. Appears as live-config rebase, `max_requests_per_24h`, dashboard, and validation-gated prompt changes.
- **Pushed back / declined:** Hard “never before 11:00”. That is a preference, not simulator policy. The controller honours `approver_hours` (Kestrel starts at 10:00) and the fatigue cap.
- **Wrong against policy:** None versus the API contract.
- **Reply (≤120 words):** Supervisor edits rebase our next proposals; we do not clobber them. Request volume is capped by policy, and the dashboard is the single screen for tests, wins and asks. Prompt replacements require validation so we do not ship a “read the script” engagement regression.

### 05 — Product (Tara)
- **Accepted:** Separate pickup and engagement; one shared cohort object; expired ≠ resubmit stale; proposals carry impact/diff/affected/dry-run; only observed arms count. Appears throughout metrics, cohorts, governance and change builder.
- **Pushed back / declined:** Per-person call-time (wishlist P0) — cut as C4.
- **Wrong against policy:** None.
- **Reply (≤120 words):** Pickup and engagement stay separate. Cohorts share one table (today populated as `default`). We chose n≥100 and Holm-adjusted thresholds to buy false-winner control at the cost of speed. Expired proposals get a new id/base with fresh evidence. Call-time personalisation is explicitly out of this delivery.

### 06 — CTO (Aryamann)
- **Accepted:** Explainability over opaque lift; spend credits where they change decisions; choose and say no; write for the on-call. Appears as audit-first design, credit use on hallucination only, and the C4 cut / Partial statuses.
- **Pushed back / declined:** “Bit of everything.” C4 is cut; C5/C7/C13 remain Partial where public evidence is incomplete.
- **Wrong against policy:** Trust the signed contract over memos, including Sales’ KYC auto-apply claim.
- **Reply (≤120 words):** We prioritised a governable learning loop with audit and restart reconciliation over call-time and deep DID rotation. Sales’ KYC auto-apply claim is false against policy. Credits went to hallucination suggest/validate when incidents fired. First production weeks are sequenced in §14.

## 4. Architecture

```text
Simulator :8700 (HTTP only)
  GET  /health /sim/state /workflows /workflow-config /chunks
  POST /sim/advance /approvals /apply /llm/* /validation /auto-apply
                 |
                 v
controller.main  (continuous loop)
  or  python -m controller.recovery  (no /sim/advance)
                 |
    SimulatorClient  -->  SQLite Store (WAL)
                          events, joins, cohorts, KPI snapshots,
                          signals, experiments, incidents,
                          proposals, approvals, config snapshots,
                          audit_decisions, fairness, credits,
                          observability counters/alerts
                 |
    services pipeline each cycle:
      ingest -> project/join -> metrics -> detect
      -> experiments -> prompt_incident -> governance
      -> fairness / credits / observability
                 |
dashboard (read-only GET over the same SQLite; optional sim URL)
```

**Components.** `controller/api` talks HTTP only. `controller/persistence` owns SQLite schema and idempotent writes. `controller/services` owns learning, incidents and governance. `dashboard/` is a separate read-only view.

**Where state lives.** All durable controller state is in the SQLite file configured by `--db` / `CONTROLLER_DB` (default `data/controller.sqlite`). The simulator remains the source of live config versions and approval decisions; the controller mirrors them locally.

**On restart.** Reopen the same DB → idempotent ingest from checkpoints → reconcile approvals (and, on the normal governance cycle, uncertain auto-applies) → apply already-approved items → resume planning. The recovery CLI does **not** POST `/sim/advance`. Uniqueness on chunk identity, `event_id`, `proposal_id` and approval rows prevents double ingest / double submit / double apply.

## 5. The learning loop

### Cohorts
Leads are projected into a single shared `cohort_assignments` table used by metrics, experiments and the dashboard. The minimum actionable sample is **100** (`MIN_COHORT_SAMPLE`), chosen so two-proportion tests have a usable denominator and tiny slices cannot trigger changes. Graded runs used only the `default` cohort — traffic is not fragmented into underpowered feature buckets, but also not personalised by language/age/etc.

### Experiment design
For `welcome_message` and `voice`, the planner builds a balanced allocation with a control floor of **0.30** and an exploration floor of **0.10** per challenger. Exploration proposals are submitted when arms are underpowered so live traffic can gather samples. After a winner, traffic concentrates on the winner while keeping a control residual. Only one primary dimension is active per workflow/cohort at a time (`welcome_message` preferred). Welcome × voice interactions are **not** modelled; graded runs never selected `voice`.

### Deciding a winner
Decision rule is frequentist, not Bayesian and not a bandit:
1. Require ≥100 samples per arm on the primary KPI.
2. Run a two-proportion z-test of each challenger versus control.
3. Compare each p-value to a Holm-adjusted α threshold `α/(m-rank)` with α=0.05.
4. Require practical lift ≥0.02 (absolute).
5. Reject as `guardrail_violation` if guardrail drop exceeds the workflow max, even if primary lift looks good.

Verdicts: `winner`, `loser`, `inconclusive`, `underpowered`, `guardrail_violation`, and terminal `no_safe_candidates` when too few safe variants remain. Limitation: this is **not** textbook Holm step-down. The implementation assigns each challenger a Holm-style α threshold `α/(m-rank)` but does not apply the classical stop-at-first-non-rejection procedure across the ordered list, so the standard Holm family-wise error-rate guarantee **cannot be claimed**. No empirical false-positive rate for this approximation was measured.

### Decay and rollback
After a winner is promoted, `detect_promoted_decay` compares recent vs baseline windows (primary drop threshold **0.05**). A rollback proposal restores the **pre-promotion** allocation snapshot frozen when the winner was submitted. In public_02 that snapshot was the explore split, so after a successful rollback `nw_renewal` W3 still held **0.344** of in-population calls on day 9. Detection also lags because baselines include long healthy history.

### Call time and caller ID
Call-time optimisation is **cut**. Caller-ID health detection compares each DID’s pickup to workflow pickup with threshold **0.08**, currently on an **all-time** window. A rotation proposal builder exists and is wired into governance, but the graded public_02 sim log contains no approval with `dims=["caller_id"]`. Per `reports/public_02.md`, the flagged DID share by day ranged **0.49–0.517** on days 2–9 — rotation was not demonstrated.

### Guardrail KPI
If a challenger improves the primary KPI but drops the guardrail beyond the workflow maximum, the arm is marked `guardrail_violation` and cannot win. That verdict is persisted and excluded from later exploration/planning for the same experiment id. Exploration dry-runs report `guardrail_ok` as **not assessed** rather than claiming safety. Customers can still be exposed to harmful explore variants before statistics finish — that is a known weakness visible in both public grades.

## 6. Governance

### Proposal lifecycle
```text
draft -> submitted -> approved | rejected | expired | lapsed | stale
                              -> applied
         \-> auto_applied
         \-> auto_apply_uncertain  (crash-safe; blocks further applies until reconciled)
```
- **Expired:** treated as a rejection of that attempt; a later attempt uses a new `proposal_id` and fresh base/evidence.
- **Lapsed:** approval timed out of the apply window; not silently re-applied.
- **Stale:** base version no longer matches live config; re-propose after refresh.
- **Uncertain auto-apply:** local write happened or may have happened before confirmation; workflow is blocked from new applies until reconciliation.

### Batching and timing
Proposals respect `approver_hours`, sampled approver latency, proposal TTL, apply windows and `max_requests_per_24h`. An open-proposal soft cap plus fairness deferrals reduce flood under attention pressure.

### Auto-apply vs approval
Auto-apply is used only when every changed dimension is in the workflow’s allowed set, total allocation shift ≤ `max_shift`, and local impact/shift gates pass. Otherwise the controller POSTs `/approvals`. It voluntarily refuses auto-apply when policy is off (e.g. Northwind KYC) or when the change is customer-facing beyond allowed dims (welcome/voice/prompt typically need approval unless policy says otherwise).

### Impact, affected leads, dry-run
`change_builder` attaches:
- **impact_score** — formula over historical arm rates / validation pass_rate (not a causal forecast);
- **affected_leads** — cohort or uploaded lead count;
- **dry_run** — labelled limitation text; exploration explicitly sets guardrail assessment to not assessed.

Honesty: these are decision aids, not proofs of future lift.

### Supervisor mid-edit
Before submit/apply the controller refreshes live workflow config. If a supervisor changed the base, the proposal is rebased or marked stale/conflict rather than silently overwriting the supervisor’s edit.

## 7. Incidents

### Detection thresholds (code-level)
| Signal | Threshold | Notes |
|---|---|---|
| Pickup drift | absolute drop ≥ 0.05 | recent vs baseline; n≥100 dials |
| Engagement drift | absolute drop ≥ 0.05 | recent vs baseline; n≥100 connected |
| Hallucination spike | rise ≥ 0.02 and absolute floor 0.03 | QA-flag based; noisy |
| Caller-ID health | DID pickup ≥ 0.08 below workflow | currently all-time window |

False-positive rate is not separately measured online; thresholds are conservative population gates, not calibrated FPR targets. Expected residual false positives come from window dilution and sliding fingerprints that can open multiple incidents for the same underlying drift.

### Fix selection, credits, verification
Hallucination path: open incident → spend credits on LLM suggest-fixes → run validation → build prompt proposal with `validation_run_id` → governance → post-apply verification helper against the hallucination floor. Credits on public_02: **9** used, **3** validation runs, **1** LLM fix call.

### public_02 timeline (authoritative grade + graded sim log)

| When | Event | Controller outcome |
|---|---|---|
| D2 10:00 | `did_flagged` on `ks_cancel` | Flagged DID share by day: 0.493, 0.513, 0.49, 0.498, 0.503, 0.511, 0.517, 0.509 (days 2–9; range **0.49–0.517**). Graded log has **no** `caller_id` approval. Mitigation **not demonstrated**. |
| D3 11:30 | Supervisor bad prompt on `ks_cancel` | Fixed by controller-approved prompt apply. Grade: `mttr_minutes=1455`, `fixed_by=controller_approved`. Matches ks_cancel v6 prompt engageΔ=+0.102. |
| D5 15:00 | Supervisor edit on `nw_renewal` | Grade provides **no MTTR**. Incomplete as an incident-closure claim. |
| Through D9 | Decaying `nw_renewal` W3 | Share ends at **0.344** after rollback to pre-promotion explore weights. |
| End of run | Controller supervisor summary | Observation only: **9** open incidents, **0** pending proposals, **0** pending approvals. This is **not** proof every incident was resolved. |

## 8. Scale

Measured on `public_03_load` as documented in `reports/public_03_load.md`. Two attempts must not be merged.

### Attempt A — measured partial run

| Metric | Value | Nature |
|---|---|---|
| History events | 5,537,379 | Measured |
| Events at stop | 6,215,415 (~6.22M) | Measured |
| Calls | 4,679,295 (~4.68M) | Measured |
| Peak working set | ~188 MB | Measured |
| DB size | 7396.7 MB (~7.4 GB) | Measured |
| Ingest throughput incl. bootstrap | 1294.9 events/s | Measured |
| Cycles completed | 3 | Measured |
| Sim minute | 1080 / 7200 | Measured |
| Wall-clock approx | ~4800 s (~80 min) | Measured/observed |
| Wall-clock per simulated day | ~106.7 min | **Extrapolated** |
| End-to-end decision lag | — | **Not instrumented** |
| Full 5 simulated days | Not completed | Three cycles completed; sim minute reached 1080/7200; reason for termination not independently verified from retained cycle logs beyond the measured stop point |
| SRE &lt;10 wall-min / sim-day | **Not met** | Against extrapolated ~106.7 |

### Attempt B — later failed attempt (not a completed benchmark)

Ingested 5,537,379 history events; projected ~2.9M calls; failed with `sqlite3.OperationalError: database or disk is full`. At failure: main DB ≈4.24 GB, WAL ≈17.44 GB, `C:` free ≈0.02 GB. Partial DB files were deleted in cleanup. No valid full-run throughput, peak RSS, or cycle counts exist for Attempt B.

### What breaks first at 10×
Single-writer SQLite plus full metrics/experiment recompute each cycle, amplified by WAL growth during projection. Partitioning plan: shard by tenant/workflow, keep rolling KPI windows, retain file-backed shards before introducing a new managed database.

## 9. Reliability

### Crash-recovery test (demonstrated)
Method and results are detailed in `reports/crash_recovery.md`. Summary:
- Failure mode observed: unexpected `AttributeError: 'sqlite3.Row' object has no attribute 'get'` after cycle 1 had submitted 3 explore proposals and advanced to `sim_minute=240`.
- Recovery command: `python -m controller.recovery --url http://127.0.0.1:8700 --db data/ctrl_public01.sqlite` (no time advance).
- Recovery result: `approvals_reconciled=3`, `applied=0`, ingest committed 0 (idempotent), `duplicate_proposals=0`.
- Resume on the **same** DB finished: `done cycles=48 wall_seconds=174.3 … finished=true`.

### Intentional process-kill test
`scripts/crash_recovery_demo.ps1` is a planned reproducible procedure. It has **not** been executed successfully in this submission. Do not treat the exception-path demo as an OS-kill proof.

### Idempotency keys
Chunk identity, `event_id`, `proposal_id`, and approval row identity. Re-ingest and re-submit paths are designed to no-op on duplicates.

### State lost / not lost
Preserved: event cursor, proposals, pending approvals, experiment rows. Not preserved across the exception itself: in-flight Python cycle locals. Simulated time was unchanged by recovery.

### Simulator 4xx / 5xx
4xx map to typed `SimulatorError`. `409` finished ends the loop cleanly. 5xx are retried with backoff. Recovery CLI does not currently call `reconcile_uncertain_auto_applies` (that runs inside `run_governance_cycle`).

## 10. Audit & observability

### Query: why did workflow X change, who approved, what happened next?

Against retained `data/ctrl_public01.sqlite` (controller DB from the exception-recovery public_01 run; illustrates the trail shape):

```sql
SELECT decision_id, kind, workflow_id, summary, wall_ts
FROM audit_decisions
WHERE workflow_id = 'nw_renewal'
  AND kind IN ('proposal_built','approval_submitted','approval_reconciled','applied')
ORDER BY wall_ts;
```

Observed output (abbreviated):

```text
proposal_built      | Built proposal prop_fb88447fd1e1256d669e (experiment_explore)
approval_submitted  | Submitted approval apr_00001 for proposal prop_fb88447fd1e1256d669e
approval_reconciled | Reconciled approval apr_00001 → approved
applied             | Applied approval apr_00001 → v2
proposal_built      | Built proposal prop_ebc4afee36888c7101c4 (experiment_winner)
approval_submitted  | Submitted approval apr_00004 for proposal prop_ebc4afee36888c7101c4
approval_reconciled | Reconciled approval apr_00004 → approved
applied             | Applied approval apr_00004 → v3
```

That answers: what changed (explore then winner), who/what approved it (`apr_00001` / `apr_00004` reconciled to approved), and what happened next (applied to config versions v2 then v3).

### Alerts
- **Page:** apply failures near window end; credit emergency exhaustion; ingest integrity errors; unresolved `auto_apply_uncertain`.
- **Do not page:** informational `recovery_ran`; routine underpowered experiment notes.

## 11. Results

### public_01 (`reports/public_01.md` — authoritative, locked)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ |
|---|---|---|---|---|---|---|
| ks_cancel | 0.338 | +1.5% | 0.242 | +1.6% | 0.486 | -1.0% |
| nw_kyc | 0.376 | +3.5% | 0.241 | -0.2% | 0.614 | +3.9% |
| nw_renewal | 0.430 | +2.4% | 0.261 | -0.9% | 0.564 | +3.4% |
| ALL | 0.377 | +2.2% | 0.249 | +0.3% | 0.538 | +1.6% |

**Applies (4 — beneficial 2 / harmful 2):**
- Beneficial: `nw_kyc` v2 welcome engageΔ=+0.039; `nw_renewal` v2 welcome engageΔ=+0.066.
- Harmful: `ks_cancel` v2 welcome engageΔ=-0.023; `nw_renewal` v3 welcome engageΔ=-0.036.

**Governance:** 4 approval requests; 4 approved; 0 expired; 0 lapsed; 0 apply conflicts; 0 auto-applied. Credits 0 / validation 0 / LLM 0. Incidents: none.

**Interpretation.** Exploration unlocked learning and produced net-positive ALL rates on all three reported KPIs. Two applies still hurt engagement — the cost of gathering samples before a winner. Pickup and engagement moved independently (e.g. nw_kyc pickup slightly down while engagement rose), which is exactly why they must stay separate.

### public_02 (`reports/public_02.md` — authoritative, locked)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ |
|---|---|---|---|---|---|---|
| ks_cancel | 0.259 | +4.4% | 0.181 | -1.7% | 0.507 | +6.3% |
| nw_kyc | 0.357 | -4.7% | 0.238 | -1.7% | 0.573 | -3.2% |
| nw_renewal | 0.434 | +1.8% | 0.265 | +1.1% | 0.551 | +0.6% |
| ALL | 0.338 | +1.2% | 0.221 | -0.6% | 0.539 | +1.7% |

**Applies (8 — beneficial 6 / harmful 2):**
- Harmful: `nw_kyc` v2 (-0.040); `ks_cancel` v4 (-0.010).
- Beneficial: `ks_cancel` v2 (+0.016); `nw_renewal` v2 (+0.059); `nw_renewal` v3 (+0.036); `ks_cancel` v3 (+0.010); `ks_cancel` v6 prompt (+0.102); `nw_renewal` v4 (+0.023).

**Governance:** 9 approval requests; 8 approved; 1 expired; 8 applied via approval; 0 auto-applied; 0 conflicts; 0 lapsed. Credits 9 / validation 3 / LLM 1.

**Interpretation.** Helped: ks_cancel conclusion/engagement and most renewal welcome moves; the prompt repair landed with measured MTTR. Hurt: nw_kyc explore and one ks_cancel welcome apply. Gaps that remain visible in the grade: DID share never mitigated; W3 still at 0.344 after rollback; nw_renewal supervisor-edit MTTR absent; controller end-state still showed open incidents.

**What I would change next.** Recent-window caller-ID detection; rollback to a safe residual instead of the explore split; apply-window-aware prompt timing; intentional kill demo evidence.

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Blended summit KPI | Sales ask for one number | Violates product/constraints; keep separate pickup and engagement |
| Always auto-apply winners | Speed | Breaks Northwind KYC and absolute governance; policy-gated auto-apply only |
| Bayesian bandits | Faster adaptation | Harder to audit for compliance; frequentist z-tests + adjusted α |
| Postgres from day 1 | SRE scale wish | New stateful infra cost; SQLite shards first |
| Deep call-time personalisation | Summit headline / Product wishlist | Cut (C4) to finish the closed learning loop |
| Classical full Holm stop-down | Stronger FWER claim | Not implemented; only per-comparison Holm-style α thresholds — standard Holm FWER guarantee cannot be claimed |

## 13. Known weaknesses

1. Explore applies can be graded harmful before a winner (public_01 and public_02 both show this).
2. Caller-ID detection uses all-time windows; graded public_02 never emitted a `caller_id` proposal while DID share stayed ~0.5.
3. Decay rollback restores pre-promotion (often explore) weights — W3 remained 0.344 after a “successful” rollback.
4. Multiple-comparison control uses Holm-style α thresholds without textbook Holm step-down, so the standard Holm FWER guarantee cannot be claimed.
5. Only the `default` cohort appears in live graded configs; voice never ran.
6. public_03 &lt;10 wall-min/sim-day unmet; later attempt failed on disk/WAL growth.
7. Intentional OS kill/restart has not been demonstrated.
8. Event→decision lag is not instrumented; sliding-window drift fingerprints can inflate open-incident counts.
9. Recovery CLI does not reconcile uncertain auto-applies (normal governance cycle does).
10. Final controller “9 open incidents” is an end-state observation, not evidence that incidents were cleared.

## 14. If this went to production at Formi next month

1. **Days 1–2:** Run and attach the intentional kill/restart demo; extend recovery CLI to reconcile uncertain auto-applies; freeze an on-call runbook from audit queries + pageable alerts only.
2. **Days 3–5:** Switch caller-ID detection to a recent window; require post-apply share decrease before closing the incident; change decay rollback to a safe residual and keep monitoring after `rolled_back`.
3. **Days 6–9:** Ship tenant-sharded SQLite workers, rolling KPI windows, and disk/WAL guards sufficient for the public_03 envelope; re-measure one simulated day wall-clock honestly.
4. **Days 10–12:** Dual-control shadow on one Northwind workflow and one Kestrel workflow with human review of every auto-apply candidate.
5. **Days 13–14:** Only if the above hold, reopen a narrowly scoped call-time experiment (C4) behind approvals — not before.
