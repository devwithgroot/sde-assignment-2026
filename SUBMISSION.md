# Outcome-First Agent Loop — Submission

**Name:** Sandeep · **Date:** 8 Nov 2026 · **Hours actually spent (be honest):** ~18 across Phases 0–9 · **Language / storage:** Python 3.11 / SQLite (WAL)

> Copied from `SUBMISSION_TEMPLATE.md`. Headings retained. Metrics below are from live runs unless marked mocked.

---

## 0. TL;DR

HTTP-only controller: ingest → join/cohorts → KPIs → explore/experiment (Holm) → governed propose/apply → prompt incident loop → fair scheduling, with crash recovery and a read-only dashboard.  
`public_01`: ALL conclusion +1.6%, engagement +1.3%; 4 applies (3 beneficial, 1 harmful).  
`public_02`: ALL conclusion −1.7%; 4 applies (3 beneficial, 1 harmful); prompt fixes validated but **lapsed** before apply; DID flag not rotated away.  
Cut deep call-time optimisation (C4) and production-grade caller-ID rotation (C5); load path is honest but not 10‑min/day yet.

## 1. Assumptions

1. Simulator HTTP contract (`docs/SIMULATOR.md` + live `/openapi.json`) is the only world truth; scenario YAML / `runs/` paths are never controller inputs.
2. Advance must be a positive multiple of `tick_minutes` (15); default controller tick is 60, grading runs used 240–360 for wall-clock.
3. Exploration must be proposed through governance before challenger arms can gather live samples (defaults are often 100% control).
4. `min_sample_per_arm=100`, two-proportion z-tests, Holm correction, practical lift floor ~2 pts — chosen for false-winner control over summit speed.
5. Auto-apply only when policy allows **and** local impact/shift gates pass; otherwise `/approvals`.
6. Prompt validation pass_rate is safety-only; never treated as engagement proof.
7. SQLite WAL on local disk is acceptable for the assignment envelope; production 10× needs sharding before a new managed DB.
8. Windows graders may need `PYTHONUTF8=1` because `sim grade` writes Unicode Δ characters.
9. `--once` / recovery CLI never advance time; only the continuous `run.sh` loop advances.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion at load scale | Partial | Idempotent chunk ingest works; public_03 history (~5.5M events) ingests but full-day wall target unmet | Streaming project batches, smaller metric windows, parallel per-tenant workers |
| C2 | Shared cohorts | Built | One `cohort_assignments` object shared by metrics/experiments/dashboard | Richer feature cohorts beyond default |
| C3 | History bootstrap | Built | History chunks before D0 | — |
| C4 | Call-time optimisation | Cut | Lower ROI vs learning+governance in budget | Per-hour schedule experiments under governance |
| C5 | Caller-ID health | Partial | Pickup/DID signals exist; public_02 DID share stayed ~0.5 | Safe explore/rotate proposals on `caller_id` with guardrails |
| C6 | Welcome/voice experiments | Built | Balanced explore → stats → promote | Voice-second dimension scheduling polish |
| C7 | Decay + rollback | Partial | Decay detector + rollback proposals exist; little decay action in public runs | Tighter post-promote windows + cooldown |
| C8 | Hallucination incident loop | Partial | Detect → LLM → validate → propose worked on public_02; applies **lapsed** (MTTR null) | Apply-window aware submit timing + faster reconcile |
| C9 | Governance client | Built | Persist-then-submit, reconcile, apply-by-id, stale/lapse/reject | Smarter batching under fatigue |
| C10 | Impact / dry-run | Built | Formula impact, affected leads, labeled limitations | Better historical replay windows |
| C11 | Supervisor view | Built | Read-only dashboard over SQLite | Auth + multi-DB in prod |
| C12 | Audit store | Built | `audit_decisions` answers why/who/what next | — |
| C13 | Crash recovery | Built | Live kill/restart on public_01 + unit tests | — |
| C14 | Loop observability | Built | Counters, alerts, supervisor summary | True event→decision lag histogram |
| C15 | Fair credits/attention | Built | WFQ scheduler, emergency reserve, soft open-proposal cap | Live multi-tenant load proof |

Principle: ship a correct closed loop (ingest → evidence → governance → audit → recovery) before summit cosmetics or call-time optimisation.

## 3. Stakeholders

### 01 — Sales (Nikhil)
- **Accepted:** Show measurable lift; keep Kestrel on policy auto-apply where allowed.
- **Pushed back:** No blended KPI; no auto-apply on Northwind KYC; no silent “persuasive” prompt rollout; no bypassing approvals for speed.
- **Wrong claim:** “Northwind is fine with KYC auto-apply” — live policy is `auto_apply.enabled=false` for `nw_kyc`.
- **Reply (≤120 words):** We’re demoing real lift on separate pickup/engagement numbers, not a blended score. Kestrel stays on the auto-apply dims already in policy; Northwind KYC still needs human approval per their contract (your QBR note doesn’t match the signed policy). Prompt changes only ship with validation evidence and an approval. Speed comes from exploration + same-day apply windows, not from skipping governance.

### 02 — Northwind Compliance (Meera)
- **Accepted:** Customer-facing dims need approval; scripts need validation evidence; audit within 24h; prefer slow/right; respect approver hours.
- **Pushed back:** Nothing material — this memo aligns with constraints.
- **Wrong claim:** None vs live policy.
- **Reply:** Agreed. Welcome/voice/prompt changes go through `/approvals` with diff, dry-run, and (for prompts) `validation_run_id`. Schedule/caller-ID auto-apply stays inside the signed latitude and will appear in the weekly summary. Every applied change is queryable from our audit store by workflow and time.

### 03 — Platform/SRE (Kabir)
- **Accepted:** Kill/restart demo; idempotency; actionable alerts; prefer no new infra; publish load numbers; expose loop health.
- **Pushed back:** Cannot honestly claim public_03 “one simulated day &lt; 10 wall minutes” on this build yet.
- **Wrong claim:** None.
- **Reply:** Crash recovery is demonstrated (kill mid-public_01, recover, finish) with idempotent ingest/apply. Storage stays SQLite WAL—no new service. Dashboard shows credits, pending approvals, and alerts. public_03 measurements are in `reports/public_03_load.md`; the first break at 10× is full-table experiment recompute on a single SQLite writer, which we’d shard by tenant before adding Postgres.

### 04 — Kestrel Ops (Anita)
- **Accepted:** Don’t silently overwrite supervisor edits; limit request flood; one consolidated screen; avoid lifeless compliance scripts.
- **Pushed back:** Hard “never before 11:00” isn’t in simulator policy; we honour `approver_hours` + fatigue cap instead.
- **Wrong claim:** None vs API.
- **Reply:** Supervisor edits rebase our experiments; we don’t clobber them. Requests are capped by `max_requests_per_24h` and the dashboard is the single screen for tests/wins/asks. Prompt replacements require validation so we don’t ship a “read the script” regression for engagement.

### 05 — Product (Tara)
- **Accepted:** Separate KPIs; one cohort object; expired≠resubmit stale; proposals carry impact/diff/affected/dry-run; only observed arms count.
- **Pushed back:** Per-person call-time is wishlist/cut (C4).
- **Wrong claim:** None.
- **Reply:** Pickup and engagement stay separate. Cohorts are one shared table. We picked min_n=100 + Holm to buy false-winner control at the cost of speed. Expired proposals get a new id/base, never a stale retry. Call-time personalisation is explicitly cut for this delivery.

### 06 — CTO (Aryamann)
- **Accepted:** Explainability &gt; opaque lift; spend credits where they change decisions; choose and say no; write for the on-call.
- **Pushed back:** “Bit of everything” — we cut C4 and left C5 partial on purpose.
- **Wrong claim:** Trust contract over memos (including Sales’ KYC auto-apply).
- **Reply:** We prioritised a governable learning loop with audit and recovery over call-time and deep DID rotation. Sales’ KYC auto-apply claim is false against policy. Credits go to hallucination suggest/validate only when incidents fire. First production weeks are sequenced in §14.

## 4. Architecture

```text
Simulator :8700 (HTTP only)
    ^ GET health/state/workflows/config/chunks
    ^ POST advance | approvals | apply | llm | validation | auto-apply
    |
controller.main loop
    -> SimulatorClient
    -> SQLite Store (events, joins, cohorts, KPIs, experiments,
         incidents, proposals, approvals, audit, fairness, credits)
    -> services: ingest -> project/join -> metrics -> detect
         -> experiments -> prompt_incident -> governance
         -> fairness/credits/observability
    |
dashboard (read-only GET over same SQLite + optional sim URL)
```

**Restart:** reopen DB → idempotent ingest → reconcile approvals → apply approved → resume loop (recovery CLI skips advance).

## 5. The learning loop

- **Cohorts:** shared `cohort_assignments` (default cohort today); avoid fragmentation by not splitting below evidence needs.
- **Experiments:** plan balanced welcome/voice allocations with control + exploration floors; submit **explore** proposals when underpowered; one primary dimension at a time.
- **Winner:** two-proportion tests, Holm across challengers, min effect, guardrail non-inferiority; statuses `winner|no_winner|inconclusive|underpowered|guardrail_violation`.
- **Decay/rollback:** promoted winners monitored; rollback proposals reuse governance (limited live fire in public runs).
- **Call time / caller ID:** history retained; active call-time optimisation cut; DID degradation visible on public_02 but not yet safely rotated (C5 partial).
- **Guardrail:** harmful primary lift blocked when guardrail drop exceeds workflow max.

## 6. Governance

State machine: `draft → submitted → approved|rejected|expired|lapsed|stale → applied` (+ `auto_applied`).  
Batching via open-proposal soft cap + `max_requests_per_24h`. Auto-apply only for allowed dims within `max_shift` and local gates. Impact/affected/dry-run computed in `change_builder`. Supervisor mid-edit: refresh live config, rebase; never silent overwrite.

## 7. Incidents

Population thresholds open incidents; hallucination path spends credits on suggest-fixes + validation.  
**public_02 timeline (from grade + audit):** DID flagged D2 10:00 (share stayed ~0.5 — gap). Supervisor bad prompt D3 11:30 — controller proposed validated prompt replacements (P3/P5/…) but approvals **lapsed** (6 lapsed) so `mttr_minutes=null`. Credits used 54; validation runs 18; LLM calls 6.

## 8. Scale

Live partial public_03 (`reports/public_03_load.md`): history **5.54M** events; at stop **6.22M** events / **4.68M** calls; DB **~7.4 GB**; peak RSS **~188 MB**; **12** proposals applied; cycles **3** completed to sim_minute **1080/7200**. Throughput ~**1295 events/s** wall including bootstrap. Extrapolated **~107 wall-min / simulated day** — SRE 10‑min target **not met**. Full 5-day finish not completed in-session (stopped deliberately; numbers are measured, not invented).  
**10× break first:** single-writer SQLite + full recompute of experiments/metrics each cycle. Partition by tenant/workflow, keep rolling windows, keep file-backed shards before introducing Postgres.

## 9. Reliability

Live crash recovery on public_01 documented in `reports/crash_recovery.md`. Idempotent chunk inserts and approval reconcile prevent double-apply. 4xx mapped to typed `SimulatorError`; 409 finished ends the loop cleanly; 5xx retried with backoff.

## 10. Audit & observability

Query used (public_01 DB):

```sql
SELECT decision_id, kind, workflow_id, summary, wall_ts
FROM audit_decisions
WHERE workflow_id = 'nw_renewal'
ORDER BY wall_ts;
```

Sample rows: `approval_submitted` → `approval_reconciled` → `applied` (“Applied approval apr_00004 → v3”) for the W3 winner promotion.  
**Page:** apply failures near window end, credit emergency exhaustion, ingest integrity errors.  
**Don’t page:** informational `recovery_ran`, routine underpowered experiment notes.

## 11. Results

### public_01 (`reports/public_01.md`)

| workflow | conclusion Δ | pickup Δ | engagement Δ |
|---|---|---|---|
| ks_cancel | +2.7% | +2.2% | -0.0% |
| nw_kyc | +0.9% | -2.2% | +3.3% |
| nw_renewal | +1.0% | -0.1% | +2.0% |
| ALL | +1.6% | +0.5% | +1.3% |

4 applied — beneficial 3 / harmful 1 (`ks_cancel` explore engageΔ=−0.023). Exploration enabled learning; renewal W3 promote helped.

### public_02 (`reports/public_02.md`)

| workflow | conclusion Δ | pickup Δ | engagement Δ |
|---|---|---|---|
| ks_cancel | -1.1% | -0.9% | +0.3% |
| nw_kyc | -4.4% | -1.4% | -1.7% |
| nw_renewal | -0.6% | +0.1% | -0.5% |
| ALL | -1.7% | -0.6% | -0.5% |

4 applied — beneficial 3 / harmful 1 (`nw_kyc` explore). Prompt incident proposals lapsed; DID not mitigated. Next: apply-window-aware prompt submit + caller-ID rotate.

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Blended summit KPI | Sales ask | Violates product/constraints; keep separate rates |
| Always auto-apply winners | Speed | Breaks Northwind KYC / governance absolute |
| Bayesian bandits | Faster adapt | Harder audit; used frequentist + Holm |
| Postgres from day 1 | SRE scale wish | New infra cost; SQLite shards first |
| Deep call-time personalisation | Summit headline | Cut for loop correctness |

## 13. Known weaknesses

- Exploration can be graded **harmful** before a winner (ks_cancel/public_01; nw_kyc/public_02).
- Prompt fixes on public_02 validated but lapsed — incident MTTR null.
- Caller-ID flag on `ks_cancel` not rotated.
- public_03 not yet under 10 wall-min/simulated-day.
- Event→decision lag histogram not instrumented.
- Decay rollback rarely fired on these public seeds.

## 14. If this went to production at Formi next month

1. Harden apply-window scheduling for prompt incidents; add DID rotate proposals.  
2. Ship tenant-sharded SQLite workers + rolling KPI windows for public_03 envelope.  
3. Add on-call runbook from audit queries + dashboard alerts only.  
4. Shadow-run one Northwind + one Kestrel workflow with human dual-control.  
5. Only then consider call-time experiments (C4) behind approvals.
