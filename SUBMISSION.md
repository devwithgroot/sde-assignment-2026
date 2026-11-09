# Outcome-First Agent Loop — Submission

**Name:** Sandeep · **Date:** 9 Nov 2026 · **Hours actually spent (be honest):** ~18 across Phases 0–9 · **Language / storage:** Python 3.11 / SQLite (WAL)

> Copied from `SUBMISSION_TEMPLATE.md`. Headings retained. Metrics below are from live runs unless marked mocked.

---

## 0. TL;DR

HTTP-only controller: ingest → join/cohorts → KPIs → explore/experiment (Holm) → governed propose/apply → prompt incident loop → fair scheduling, with restart reconciliation and a read-only dashboard.
`public_01`: ALL conclusion **+2.2%**, pickup **+0.3%**, engagement **+1.6%**; 4 applies (2 beneficial, 2 harmful); clean governance.
`public_02`: ALL conclusion **+1.2%**, pickup **−0.6%**, engagement **+1.7%**; 8 applies (6 beneficial, 2 harmful); ks_cancel prompt MTTR 1455 min; DID share never rotated off ~0.5.
Cut deep call-time optimisation (C4); left caller-ID mitigation and load performance partial; SRE 10 wall-min/day **not met**.

## 1. Assumptions

1. Simulator HTTP contract (`docs/SIMULATOR.md` + live `/openapi.json`) is the only world truth; scenario YAML / `runs/` paths are never controller inputs.
2. Advance must be a positive multiple of `tick_minutes` (15); default controller tick is 60, grading runs used 240–360 for wall-clock.
3. Exploration must be proposed through governance before challenger arms can gather live samples (defaults are often 100% control).
4. `min_sample_per_arm=100`, α=0.05, two-proportion z-tests with Holm correction, practical lift floor **2 percentage points** — chosen for false-winner control over summit speed.
5. Detection uses the last **two calendar days** as the recent window when enough distinct call days exist (`split_baseline_recent`).
6. Auto-apply only when live workflow policy allows **and** local impact/shift gates pass; otherwise `/approvals`. Memo claims do not override signed policy.
7. Prompt validation pass_rate is safety-only; never treated as engagement proof.
8. Variants previously marked `guardrail_violation` are excluded from later exploration for that experiment id; statistical losers are not treated as harmful.
9. SQLite WAL on local disk is acceptable for the assignment envelope; production 10× needs sharding before a new managed DB.
10. Windows graders may need `PYTHONUTF8=1` because `sim grade` writes Unicode Δ characters.
11. `--once` / recovery CLI never advance time; only the continuous `run.sh` loop advances.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion at load scale | Partial | Idempotent chunk ingest works; measured partial public_03 progressed, but SRE 10 wall-min/day unmet; a later attempt hit SQLite disk-full | Streaming project batches, smaller metric windows, free-disk checks, parallel per-tenant workers |
| C2 | Shared cohorts | Built | One `cohort_assignments` object shared by metrics/experiments/dashboard | Richer feature cohorts beyond default |
| C3 | History bootstrap | Built | History chunks before D0 | — |
| C4 | Call-time optimisation | Cut | Lower ROI vs learning+governance in budget | Per-hour schedule experiments under governance |
| C5 | Caller-ID health | Partial | Detection + rotation proposal path exist; public_02 `ks_cancel` D2 share stayed ~0.49–0.52 through D9 — rotation did **not** succeed | Safer rotate apply timing, residual-share caps, post-apply verification that share actually falls |
| C6 | Welcome/voice experiments | Built | Balanced explore → stats → promote; guardrail violators excluded from re-explore | Voice-second dimension scheduling polish |
| C7 | Decay + rollback | Partial | Decay detector + rollback proposals exist; public_02 still shows `nw_renewal` W3 at **0.344** share on day 9 | Tighter post-promote windows, durable promotion→decay handoff, cooldown |
| C8 | Hallucination incident loop | Partial | public_02 ks_cancel supervisor edit fixed via approval (MTTR **1455** sim-min); credits/validation used; end state still had open incidents | Faster apply-window scheduling; close/verify all open incidents, not only the one that applied |
| C9 | Governance client | Built | Persist-then-submit, reconcile (incl. uncertain auto-apply), apply-by-id, stale/lapse/reject | Smarter batching under fatigue |
| C10 | Impact / dry-run | Built | Formula impact, affected leads, labeled limitations (explore reports guardrail as not assessed) | Better historical replay windows |
| C11 | Supervisor view | Built | Read-only dashboard over SQLite | Auth + multi-DB in prod |
| C12 | Audit store | Built | `audit_decisions` answers why/who/what next | — |
| C13 | Crash recovery | Partial | Unexpected process exit mid-public_01 recovered and finished (`reports/crash_recovery.md`); deliberate OS-level kill/restart **not yet demonstrated** | Scripted kill −9 / SIGKILL mid-cycle + same resume proof |
| C14 | Loop observability | Built | Counters, alerts, supervisor summary | True event→decision lag histogram |
| C15 | Fair credits/attention | Built | WFQ scheduler, emergency reserve, soft open-proposal cap | Live multi-tenant load proof |

Principle: ship a correct closed loop (ingest → evidence → governance → audit → recovery) before summit cosmetics or call-time optimisation. Prefer honest Partial over claiming Built when public evidence is incomplete.

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
- **Accepted:** Survive unexpected exit; idempotency; actionable alerts; prefer no new infra; publish load numbers; expose loop health.
- **Pushed back:** Cannot honestly claim public_03 “one simulated day &lt; 10 wall minutes” on this build yet. Deliberate kill/restart demo is still outstanding.
- **Wrong claim:** None.
- **Reply:** We recovered after an unexpected controller exit mid-public_01 (reconcile + resume to finish) with idempotent ingest/apply; a deliberate OS kill demo is still to be filmed. Storage stays SQLite WAL—no new service. Dashboard shows credits, pending approvals, and alerts. public_03 measurements are in `reports/public_03_load.md` (partial; 10‑min target not met). First break at 10× is full-table experiment recompute on a single SQLite writer—shard by tenant before Postgres.

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
- **Pushed back:** “Bit of everything” — we cut C4 and left C5/C7/C13 partial on purpose where evidence is incomplete.
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

**Restart:** reopen DB → idempotent ingest → reconcile approvals / uncertain auto-applies → apply approved → resume loop (recovery CLI skips advance).

## 5. The learning loop

- **Cohorts:** shared `cohort_assignments` (default cohort today); minimum actionable sample **100**; avoid fragmentation by not splitting below evidence needs.
- **Experiments:** plan balanced welcome/voice allocations with control floor (0.30) and exploration floor (0.10); submit **explore** proposals when underpowered; one primary dimension at a time. Persisted `guardrail_violation` arms are excluded from later plans/proposals; `loser` is not.
- **Winner:** two-proportion z-tests, Holm across challengers, α=0.05, min effect **0.02**, guardrail non-inferiority; statuses `winner|no_winner|inconclusive|underpowered|guardrail_violation|no_safe_candidates`.
- **Decay/rollback:** promoted winners monitored; rollback proposals reuse governance. Live evidence incomplete: public_02 left `nw_renewal:welcome_message=W3` at **0.344** of in-population calls on day 9.
- **Call time / caller ID:** history retained; active call-time optimisation cut (C4). Caller-ID degradation is detected and a rotation path exists, but public_02 did not clear the flagged DID share (~0.49–0.52 through D9).
- **Detection window:** when enough call calendar days exist, baseline = earlier days and recent = last **two** calendar days.
- **Guardrail:** harmful primary lift blocked when guardrail drop exceeds workflow max; exploration dry-run does not claim `guardrail_ok` without assessment.

## 6. Governance

State machine: `draft → submitted → approved|rejected|expired|lapsed|stale → applied` (+ `auto_applied` / `auto_apply_uncertain` recovery).
Batching via open-proposal soft cap + `max_requests_per_24h`. Auto-apply only for allowed dims within `max_shift` and local gates. Impact/affected/dry-run computed in `change_builder`. Supervisor mid-edit: refresh live config, rebase; never silent overwrite. Restart reconciles pending approvals and uncertain auto-applies before new proposals.

## 7. Incidents

Population thresholds open incidents; hallucination path spends credits on suggest-fixes + validation.

**public_02 timeline (from `reports/public_02.md` + controller end-state observation):**
- **DID flagged** `ks_cancel` at D2 10:00. Flagged DID share by day stayed ≈ **0.49–0.52** through D9 — do **not** claim caller-ID rotation succeeded.
- **Supervisor edit** `ks_cancel` at D3 11:30: `mttr_minutes=1455`, `fixed_by=controller_approved` (prompt apply on ks_cancel v6 is the matching beneficial prompt change).
- **Supervisor edit** `nw_renewal` at D5 15:00: grading report provides **no MTTR**.
- Credits used **9**; validation runs **3**; LLM fix calls **1**.
- **Controller observation (not a grade claim):** final supervisor summary reported **9 open incidents**, **0** pending proposals, **0** pending approvals — recorded as end-of-run controller state, not proof that every incident was resolved.

## 8. Scale

**Earlier measured partial run** (`reports/public_03_load.md`): history **5.54M** events; at deliberate stop **6.22M** events / **4.68M** calls; DB **~7.4 GB**; peak RSS **~188 MB**; **12** proposals applied; cycles **3** to sim_minute **1080/7200**. Throughput ~**1295 events/s** wall including bootstrap. Extrapolated **~107 wall-min / simulated day** — SRE **10 wall-min / simulated day target not met**. Full 5-day finish was not completed in that session.

**Later attempt (not a valid full-run measurement):** ingested **5,537,379** history events and projected ~**2.9M** calls, then failed with SQLite `database or disk is full`. At failure the main DB was ~**4.24 GB** and WAL ~**17.44 GB**, with only ~**0.02 GB** free on `C:`. That partial DB was deleted during disk cleanup. Do **not** treat this attempt as a completed load run or as replacement numbers for the earlier report.

**10× break first:** single-writer SQLite + full recompute of experiments/metrics each cycle, plus disk/WAL growth during projection. Partition by tenant/workflow, keep rolling windows, keep file-backed shards before introducing Postgres.

## 9. Reliability

`reports/crash_recovery.md` documents recovery after an **unexpected application exception** (`AttributeError` on `sqlite3.Row.get`) mid-public_01: recovery CLI reconciled approvals without advancing time; resume finished the scenario without duplicate proposal POSTs.
That is **not** the same as the requested deliberate OS-level kill/restart demonstration, which has **not yet been shown**.
Idempotent chunk inserts and approval / uncertain-auto-apply reconcile prevent double-apply. 4xx mapped to typed `SimulatorError`; 409 finished ends the loop cleanly; 5xx retried with backoff.

## 10. Audit & observability

Query used (public_01 DB):

```sql
SELECT decision_id, kind, workflow_id, summary, wall_ts
FROM audit_decisions
WHERE workflow_id = 'nw_renewal'
ORDER BY wall_ts;
```

Sample rows: `approval_submitted` → `approval_reconciled` → `applied` (“Applied approval apr_00004 → v3”) for the W3 winner promotion.
**Page:** apply failures near window end, credit emergency exhaustion, ingest integrity errors, unresolved `auto_apply_uncertain`.
**Don’t page:** informational `recovery_ran`, routine underpowered experiment notes.

## 11. Results

### public_01 (`reports/public_01.md`)

| workflow | conclusion Δ | pickup Δ | engagement Δ |
|---|---|---|---|
| ks_cancel | +1.5% | +1.6% | −1.0% |
| nw_kyc | +3.5% | −0.2% | +3.9% |
| nw_renewal | +2.4% | −0.9% | +3.4% |
| ALL | +2.2% | +0.3% | +1.6% |

4 applied — beneficial **2** / harmful **2**.
Beneficial: `nw_kyc` v2 welcome (+0.039), `nw_renewal` v2 welcome (+0.066).
Harmful: `ks_cancel` v2 welcome (−0.023), `nw_renewal` v3 welcome (−0.036).
Governance: 4 approval requests, all approved; 0 expired / lapsed / conflicts; 0 credits, 0 validation runs, 0 LLM calls. Exploration enabled learning; one later renewal apply was still graded harmful.

### public_02 (`reports/public_02.md`)

| workflow | conclusion rate (Δ) | pickup (Δ) | engagement (Δ) |
|---|---|---|---|
| ks_cancel | 0.259 (+4.4%) | 0.181 (−1.7%) | 0.507 (+6.3%) |
| nw_kyc | 0.357 (−4.7%) | 0.238 (−1.7%) | 0.573 (−3.2%) |
| nw_renewal | 0.434 (+1.8%) | 0.265 (+1.1%) | 0.551 (+0.6%) |
| ALL | 0.338 (+1.2%) | 0.221 (−0.6%) | 0.539 (+1.7%) |

8 applied — beneficial **6** / harmful **2**:
- harmful: `nw_kyc` v2 (−0.040), `ks_cancel` v4 (−0.010)
- beneficial: `ks_cancel` v2 (+0.016), `nw_renewal` v2 (+0.059), `nw_renewal` v3 (+0.036), `ks_cancel` v3 (+0.010), `ks_cancel` v6 prompt (+0.102), `nw_renewal` v4 (+0.023)

Governance: 9 approval requests (8 approved, 1 expired); 8 applied via approval; 0 auto-applied; 0 conflicts; 0 lapsed. Credits **9** / validation **3** / LLM **1**.
Helped: ks_cancel conclusion/engagement and most renewal welcome applies; prompt fix landed. Hurt: nw_kyc explore and one ks_cancel welcome apply. Still open: DID share not mitigated; W3 decay share remaining; controller still showed open incidents at stop.

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Blended summit KPI | Sales ask | Violates product/constraints; keep separate rates |
| Always auto-apply winners | Speed | Breaks Northwind KYC / governance absolute |
| Bayesian bandits | Faster adapt | Harder audit; used frequentist + Holm |
| Postgres from day 1 | SRE scale wish | New infra cost; SQLite shards first |
| Deep call-time personalisation | Summit headline | Cut for loop correctness |

## 13. Known weaknesses

- Exploration can still be graded **harmful** before a winner (`ks_cancel`/`nw_renewal` on public_01; `nw_kyc`/`ks_cancel` on public_02).
- Caller-ID flag on `ks_cancel` public_02 never cleared (~0.5 share through D9) despite detection/rotation code.
- Decay rollback incomplete in live fire: `nw_renewal` W3 still **0.344** share on day 9.
- public_03 SRE 10 wall-min/simulated-day **not met**; later attempt failed on disk full (WAL explosion).
- Crash evidence is unexpected-exit recovery, not a deliberate OS kill demo yet.
- Event→decision lag histogram not instrumented.
- Controller end-of-public_02 summary still showed open incidents with an empty proposal/approval queue.

## 14. If this went to production at Formi next month

1. Film deliberate kill/restart; harden apply-window scheduling for prompt incidents; verify DID share actually falls after rotate.
2. Ship tenant-sharded SQLite workers + rolling KPI windows + disk/WAL guards for the public_03 envelope.
3. Close the decay→rollback gap so promoted winners cannot linger at ~⅓ traffic.
4. Add on-call runbook from audit queries + dashboard alerts only.
5. Shadow-run one Northwind + one Kestrel workflow with human dual-control.
6. Only then consider call-time experiments (C4) behind approvals.
