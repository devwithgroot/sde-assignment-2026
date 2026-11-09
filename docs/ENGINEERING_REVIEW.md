# Engineering review

Reviewed 8 Nov 2026 against `README.md`, `docs/SIMULATOR.md`, and `SUBMISSION_TEMPLATE.md`. This review reads the repository as it is committed. It does not change controller code, tests, scenarios, or reports.

This session did not re-run `pytest`, `./run.sh`, or `sim grade`. Statements about tests cite the test names in the tree. Statements about live runs cite committed files under `reports/` and `logs/`. `logs/pytest_phase9.txt` records `96 passed, 1 warning`; that is a committed log, not a result from this review.

---

## 0. Supersession — revalidated 9 November 2026

> **Do not treat the body of this document (sections 1–N below) as current truth.** It is a frozen 8 Nov review. Several findings were fixed or contradicted by later commits, by the locked `reports/public_*.md` files, and by `SUBMISSION.md` (9 Nov). Prefer `SUBMISSION.md`, the locked grade reports, `docs/IMPLEMENTATION_STATUS.md`, and the as-built section of `docs/IMPLEMENTATION_BLUEPRINT.md`.

### Corrections vs current source and locked reports

| 8 Nov claim (below) | Current status (9 Nov) | Evidence |
|---|---|---|
| public_02: 10 requests, 4 applied, **6 lapsed**; MTTR **null**; **54** credits / **18** validations / **6** LLM | **Superseded.** Locked grade: **9** requests, **8** approved, **1** expired, **0** lapsed, **8** applied; ks_cancel supervisor_edit **mttr_minutes=1455**; credits **9** / validations **3** / LLM **1** | `reports/public_02.md` |
| public_02 ALL conclusion −1.7% (later summary) | **Superseded.** Locked ALL conclusion **+1.2%**, pickup **−0.6%**, engagement **+1.7%** | `reports/public_02.md` |
| Promotion status overwritten every cycle; decay cannot see winners; rollback never fired | **Superseded for code.** Sticky promotion statuses are preserved in `plan_experiment` / `_persist_experiment` (`STICKY_PROMOTION_STATUSES`). public_02 **did** apply a decay rollback; W3 share fell then residualled at **0.344** because rollback restored the pre-promotion **explore** allocation | `controller/services/experiments.py`; `reports/public_02.md` decaying-variants block; `SUBMISSION.md` |
| No `caller_id` builder / rotation path | **Superseded for code.** `build_caller_id_rotation_proposal` and `propose_caller_id_rotations` exist. **Still true as outcome:** graded public_02 has **zero** `caller_id` approval dims; DID share stayed **0.49–0.517** | `change_builder.py`, `governance.py`; `reports/public_02.md` |
| Explore `guardrail_ok: True` hard-coded | **Superseded.** Explore dry-run now reports `guardrail_ok: None` / `not_assessed`. **Still true:** harmful exploration applies occurred (2 on each public grade) | `change_builder.py`; grade reports |
| C13 crash/kill demonstrated as assignment floor | **Still Partial.** Exception-path recovery measured; intentional OS kill **not executed** | `reports/crash_recovery.md` |
| IMPLEMENTATION_STATUS says phases 0–9 complete for the floor | **Superseded.** Status doc rewritten 9 Nov; does not claim full acceptance | `docs/IMPLEMENTATION_STATUS.md` |
| Holm / multiple comparisons as textbook control | **Clarify.** Holm-**style** α thresholds are implemented; classical Holm step-down and its FWER guarantee are **not** claimed | `SUBMISSION.md` §1 / §13; blueprint as-built §G |

### Findings that remain directionally valid

- Caller-ID mitigation did not fire on graded public_02 (outcome gap; detector uses an all-time window).
- Harmful exploration applies occurred on both public grades.
- public_03 did not meet &lt;10 wall-min/day; Attempt A only reached **1080/7200**; event→decision lag not instrumented.
- Intentional OS-level kill/restart remains unexecuted.
- Explore impact score remains a coarse constant; true historical cohort replay is limited.
- Feature cohorts beyond `default` were unused in graded runs.
- Fairness reorders work; it does not hard-ration every cycle; experiment credit pool unused in graded runs.

### What to ignore in the historical body

Treat every concrete public_02 number that contradicts `reports/public_02.md` as **stale**. Treat “promotion overwrite prevents decay” as **stale relative to current `experiments.py`**. Treat “no caller_id builder” and “explore guardrail_ok True” as **stale relative to current builders**. Keep the assignment-requirement extraction (section 1) as still useful context.

---

## 1. Assignment requirements

Quoted or extracted from `README.md` and `docs/SIMULATOR.md`. These are the assignment's constraints, not a restatement of the implementation.

### Non-negotiable constraints (`README.md` §5)

1. **Governance boundary is absolute.** Every controller change goes through `/approvals` or a policy-permitted auto-apply. Supervisor edits are a fact of life and must not be overwritten silently.
2. **The business owns whether and when to call.** Cadence, dialling windows, and capacity are out of scope. The controller decides who gets called in a given hour and how the agent shows up.
3. **Pickup and engagement stay separate KPIs.** Optimise a workflow's `primary` KPI and respect its `guardrail`.
4. **Winner selection is statistical and deterministic.** An LLM may propose what to try. It never decides what won.
5. **Act on populations, not anecdotes.** Detection is threshold-based. No change is triggered by one call.
6. **Every decision is auditable** from the controller's own store. The simulator's logs do not count.
7. **Credits are finite.** LLM and validation calls cost credits. Running out mid-incident is a failure.
8. **Crash safety.** Kill the controller at any point, restart it, and it resumes. It must not double-apply, lose a pending approval, or forget what it learned.

### Simulator ground rules (`README.md` §4 and `docs/SIMULATOR.md`)

- The controller may only know what the HTTP API tells it. Reading `runs/`, importing `sim`, or parsing scenario files (including `base_pickup` or `did_base_health`) is disqualifying.
- `./run.sh <SIM_URL>` must start the controller and run until the scenario finishes, with no manual steps. Hidden scenarios run on a 4-vCPU / 8 GB Linux box.
- Simulated time moves only on `POST /sim/advance`. `minutes` must be a positive multiple of 15.
- Config changes happen only by supervisor edit, `POST /approvals` then `POST /workflows/{id}/apply`, or policy-permitted `PUT /workflows/{id}/config`.
- Prompt changes require `evidence.validation_run_id` for a validation run on the same workflow and prompt with `pass_rate ≥ min_validation_pass_rate`.
- Approved changes lapse if not applied within `apply_window_hours`. A stale `base_version` fails apply with `409 stale_base_version`; the controller must re-propose.
- Validation `pass_rate` is a noisy safety measure. It says nothing about engagement.
- History before D0 is available as `history` chunks. `event_id` is strictly increasing. Chunk numbers are global and increasing.

### Mandatory floor (`README.md` §6)

The controller runs `public_01` and `public_02` end to end with no manual steps, keeps an audit trail (C12, even a minimal one), never breaks the constraints above, and `SUBMISSION.md` is complete. Everything beyond that is a choice. Cuts must be written down.

### Deliverables (`README.md` §8)

1. Controller with `run.sh`.
2. `SUBMISSION.md` from the template.
3. `reports/`: `sim grade` output for public_01 and public_02, plus load-test numbers for public_03.
4. Crash-recovery evidence: a script or log showing a kill/restart mid-run and a correct finish.
5. Commit history that shows how thinking progressed.
6. Optional walkthrough video.

### Evaluation (`README.md` §9)

| Weight | What |
|---|---|
| 25% | Outcomes on hidden scenarios: lift on the primary KPI with the guardrail respected, harmful changes applied, incident time-to-fix, decay rollback, governance hygiene. No points for lift bought by breaking a constraint. |
| 25% | Design and reasoning: statistical soundness, cohort sizing, false positives, scale envelope. |
| 20% | Scope and stakeholder judgement. |
| 20% | Engineering: correctness, crash safety, idempotency, load throughput, tests where they matter. |
| 10% | Communication: a new engineer can run, debug, and extend from the documents. |

### Scale envelope (`README.md` §3)

About 6.5 lakh dials/day, peaks above 50k dials/hour, tens of tenants, hundreds of workflows. `scenarios/public_03_load.yaml` is the proof scenario (12 workflows, ~650k dials/day, ~850k events/day). Required numbers: ingest throughput, decision lag, memory, and where it breaks at 10×. Platform/SRE (`stakeholders/03_platform_sre.md`) asks for one simulated day of that scenario in under 10 wall-clock minutes on 4 vCPU / 8 GB.

### Capabilities C1–C15 (`README.md` §6)

| # | Capability, as written |
|---|---|
| C1 | Event ingestion at load-scenario scale: backlog-safe, idempotent, restartable |
| C2 | Lead/feature joins and **one shared cohort object** used by detection, experiments and the supervisor view |
| C3 | Bootstrapping from history before D0 |
| C4 | Call-time optimisation: per cohort and/or per person |
| C5 | Caller-ID health: detect degradation, rotate, explore safely |
| C6 | Variant experimentation on welcome message and voice: design, minimum sample sizes, multiple comparisons |
| C7 | Winner promotion, **decay detection and rollback** |
| C8 | Hallucination incident response: detect → LLM suggest → validate → propose with evidence → verify after apply |
| C9 | Governance client: proposal lifecycle, batching under approver fatigue, timing against approver hours, expiry/lapse, stale-base re-proposal, auto-apply within policy |
| C10 | Honest impact scores, affected-lead counts, and dry runs against historical cohorts |
| C11 | A consolidated supervisor view (API or minimal UI): one place, not fifty alerts |
| C12 | A queryable decision/audit store: *"why did X change"* |
| C13 | Crash recovery, demonstrated: kill mid-run, restart, show the run completes correctly |
| C14 | Observability of the loop itself: ingest lag, decisions/hour, credit burn, alerts an on-call engineer can act on |
| C15 | Fair allocation of credits and approver attention across tenants |

---

## 2. Requirements traceability

Status is judged from code plus tests plus committed run evidence. A phase status file saying "done" is not treated as proof.

| Requirement | Implementation location | Relevant tests | Evidence from actual runs | Status | Remaining gap |
|---|---|---|---|---|---|
| Constraint: HTTP-only, no `sim` import, no `runs/`, no hidden scenario fields | `controller/` imports only `controller.*` and `httpx`/`pydantic`. `SimulatorClient` in `controller/api/client.py` | `tests/unit/test_observe.py::test_observe_issues_only_gets`, `tests/unit/test_ingest.py::test_chunk_text_is_a_get` | Reports and logs show HTTP against `:8700`. No committed controller log shows a scenario-file read. | Verified in code and supported by test/run evidence | `scripts/measure_public03.ps1` mentions the scenario path in a report template. The controller process does not load it. |
| Constraint: governance boundary | `submit_proposal`, `apply_approved`, `auto_apply_permitted` in `controller/services/governance.py` and `controller/services/change_builder.py` | `tests/unit/test_governance.py` | Grade reports: public_01 `auto_applied: 0`, 4 approval applies; public_02 `auto_applied: 0`, 4 approval applies, 6 lapsed. | Verified in code and supported by test/run evidence | Supervisor-edit rebase is only "refresh live config". No live log shows a supervisor edit being preserved rather than overwritten. |
| Constraint: cadence/windows/capacity untouched | No writer for cadence, `dialling_window`, or `dials_per_hour`. Schedule edits are not built. | None specific | Applied dims in both grade reports are only `welcome_message`. | Verified in code and supported by test/run evidence | Call-time (C4) is cut, so the "who gets called in a given hour" half of the allowed control surface is unused. |
| Constraint: separate KPIs, primary + guardrail | `compute_workflow_kpis` in `controller/services/metrics.py`; `decide_challenger` in `controller/services/statistics.py` | `tests/unit/test_metrics.py`, `tests/unit/test_detector.py::test_separate_kpi_signals`, `tests/unit/test_statistics.py::test_decide_challenger_guardrail_and_winner`, `tests/unit/test_experiments.py::test_guardrail_violation` | Grade tables keep pickup, engagement, and conclusion separate. | Implemented but insufficiently demonstrated | Exploration proposals hard-code `guardrail_ok: True` in `build_explore_proposal`. Both public grades contain one harmful apply. |
| Constraint: LLM does not pick the winner | `select_validated_candidate` picks by `pass_rate` then `prompt_version`. Experiment winners come from `evaluate_experiment`. | `tests/unit/test_prompt_incident.py::test_select_validated_candidate_deterministic`, `tests/unit/test_experiments.py::test_known_winner_welcome` | public_02 used 6 LLM calls and 18 validations (grade). No prompt change was applied. | Verified in code and supported by test/run evidence | Prompt selection is safety-only, which matches the contract. It was not shown to change a live prompt. |
| Constraint: populations, not one call | `MIN_COHORT_SAMPLE = 100` in `controller/domain/cohorts.py`; detector thresholds in `controller/services/detector.py` | `tests/unit/test_detector.py::test_no_signal_under_small_sample`, `tests/unit/test_experiments.py::test_underpowered_experiment` | Public runs applied exploration before a winner existed. That is a population allocation, not a single-call trigger. | Verified in code and supported by test/run evidence | Detection windows are calendar-day splits, not a declared false-positive rate. |
| Constraint: own-store audit | `audit()` in `controller/services/audit.py`; table `audit_decisions` | Governance and prompt tests write audit rows indirectly. No test asserts the SUBMISSION SQL. | `SUBMISSION.md` §10 describes sample rows. The row text is not pasted from a command in the repo. | Implemented but insufficiently demonstrated | The "why did X change" answer is claimed, not shown as command output. |
| Constraint: credits | `CreditBudget.can_spend` in `controller/services/credits.py`; spend only on the prompt path | `tests/unit/test_fairness.py::test_emergency_reserve_blocks_experiment_spend`, `tests/unit/test_prompt_incident.py::test_credit_exhaustion_on_suggest` | public_01 credits used 0. public_02 credits used 54, validations 18, LLM 6, and the scenario still finished with credits left (grade). public_03 log shows `credits_remaining: 400` at minute 1080. | Implemented but insufficiently demonstrated | The experiment credit cap is never consulted by the live experiment path, because experiments do not call the LLM. "Running out mid-incident" is unit-tested, not shown on a live incident that then recovered. |
| Constraint: crash safety | `run_crash_recovery` in `controller/services/crash_recovery.py`; idempotent `commit_chunk` | `tests/unit/test_crash_recovery.py`, `tests/unit/test_storage.py`, `tests/unit/test_ingest.py` | `reports/crash_recovery.md` plus `logs/public01_crash_recovery.log` and `logs/public01_controller_resume.log`. Resume reached `finished=true` at minute 11520. | Implemented but insufficiently demonstrated | The kill was an `AttributeError` on `sqlite3.Row.get`, not an arbitrary kill. `duplicate_proposals` in the recovery JSON is `len(ids) - len(set(ids))` on a primary key, so it cannot be non-zero. |
| Floor: public_01 and public_02 end to end | `main()` loop in `controller/main.py`; `run.sh` | No test runs a full scenario. | Grade files exist and scenarios are `finished` in the sense that grade reports were produced. public_01 resume log: `done cycles=48 ... finished=true`. public_02 grade exists; its controller log is a config dump and does not contain a `done cycles` line. | Implemented but insufficiently demonstrated | Graded runs used `--advance-minutes 240`, not `run.sh`'s default of 60. public_02's controller log does not show a clean finish line. |
| Floor: `SUBMISSION.md` complete | `SUBMISSION.md` headings 0–14 | n/a | Document exists and keeps the template headings. | Implemented but insufficiently demonstrated | Several sections paraphrase results that the logs do not fully support. See §7. |
| C1 ingestion | `ingest_kind` in `controller/services/ingestion.py`; `Store.commit_chunk` | `tests/unit/test_ingest.py`, `tests/unit/test_storage.py`, `tests/integration/test_live_ingest.py` | public_03 stderr: history chunks 0–22, `inserted=5537379`, then live `last_event_id: 6215415` at minute 1080. | Partially implemented | Idempotent and restartable on the chunks that were fetched. Not backlog-safe at full public_03 scale: the run was stopped at 1080/7200. One GET lists all chunks (`sim/server.py` `list_chunks` has no page cap); a hidden server that pages would be missed. |
| C2 shared cohort | `assign_cohort` in `controller/domain/cohorts.py`; table `cohort_assignments`; experiments read `calls.cohort_id` via `cohort_ids_for_workflow` | `tests/unit/test_cohorts.py` including `test_recomputed_vs_historical_id_are_separate_concepts` | public_01/03 logs print `"cohorts": 0`. Assignments exist (public_01 resume: `assignments=72600`) but every lead is `default`. | Partially implemented | Two cohort identities: recomputed `cohort_assignments` versus the dial-time `calls.cohort_id`. Detection is workflow-level, not cohort-level. The shared-object requirement is not met once a config defines real cohorts. |
| C3 history bootstrap | `ingest_available` fetches `history` before `events` | `tests/unit/test_ingest.py::test_history_then_live_and_restart` | public_03 stderr: 5,537,379 history events inserted before the first advance. | Verified in code and supported by test/run evidence | Bootstrap cost dominates. It is correct and too slow for the SRE day budget. |
| C4 call-time | No `hour_weights` or `lead_preferred_hour` writer | None | None | Not implemented | Explicitly cut in `SUBMISSION.md`. Correct as a cut. Hidden scenarios can still reward it. |
| C5 caller ID | `_caller_id_signals` in `controller/services/detector.py`. `EXPERIMENT_DIMS` is only `welcome_message` and `voice`. | `tests/unit/test_detector.py::test_caller_id_health_on_known_gap` | public_02 grade: `did_flagged` on `ks_cancel` from D2, flagged DID share stays about 0.49–0.52 through D9. No `caller_id` dim in applied changes. | Partially implemented | Detect exists. Rotate and safe explore do not. |
| C6 welcome/voice experiments | `plan_experiment`, `evaluate_experiment`, `balanced_allocation` in `controller/services/experiments.py`; Holm in `controller/services/statistics.py` | `tests/unit/test_experiments.py`, `tests/unit/test_statistics.py` | Both public grades show welcome_message explores and later winner applies on `nw_renewal`. Voice is not in the applied-dim list. | Implemented but insufficiently demonstrated | One dimension at a time is coded. Voice never appears in the public apply list. Holm is not sequential (see §4). |
| C7 decay and rollback | `detect_promoted_decay` in `controller/services/decay.py`; `propose_decay_rollbacks`; `build_rollback_proposal` | No test calls `detect_promoted_decay` or `build_rollback_proposal`. | public_01 resume tail: `decay_hits=0` and all three experiments `promotion=observe_only_not_promoted`. Grade decaying-variant share for `nw_renewal:welcome_message=W3` stays about 0.54–0.56 for the rest of both public runs. | Partially implemented | Promotion state is overwritten every cycle, so decay cannot see a promoted winner. Rollback never fired in the recorded runs. |
| C8 hallucination loop | `run_prompt_incident_cycle` and `_verify_applied_prompt_fixes` in `controller/services/prompt_incident.py` | `tests/unit/test_prompt_incident.py` (mocked HTTP through propose). No test of `_verify_applied_prompt_fixes`. | public_02 grade: 6 LLM calls, 18 validations, 54 credits, `supervisor_edit` on `ks_cancel` with `mttr_minutes: null`, `lapsed: 6`. | Partially implemented | Detect → suggest → validate → propose happened. Apply and post-apply verification did not. |
| C9 governance client | `run_governance_cycle` | `tests/unit/test_governance.py` covers submit, apply, duplicate id, rejected/expired, stale mark, explore build | public_01: 4 requests, 4 approved, 0 lapsed. public_02: 10 requests, 10 approved, 6 lapsed, 0 expired, 0 conflicts. | Partially implemented | Stale is marked, not re-proposed. No approver-hour scheduler. Apply is only between 240-minute advances in the graded runs, which is longer than `apply_window_hours: 2`. |
| C10 impact, affected leads, dry run | `dry_run_impact`, `build_explore_proposal`, `build_winner_proposal`, `build_prompt_proposal` | `tests/unit/test_governance.py::test_build_winner_proposal`, `test_build_explore_proposal_when_underpowered` | Grade does not print impact scores. No committed audit dump shows a dry-run payload. | Implemented but insufficiently demonstrated | Explore impact is a constant 0.25. Explore dry-run sets `guardrail_ok: True` without a measurement. Affected-lead count uses `cohort_assignments`, which can disagree with dial-time cohorts. |
| C11 supervisor view | `dashboard/app.py` `create_app`, `dashboard/queries.py`, `dashboard/render.py` | `tests/unit/test_dashboard.py` | No dashboard log from the public runs. The dashboard is a separate process (`run_dashboard.sh`), not part of `run.sh`. | Implemented but insufficiently demonstrated | The view exists and is read-only. It was not shown against the public_01/02 databases in the committed evidence. |
| C12 audit store | `audit_decisions` plus `record_decision` | Indirect via governance/prompt/recovery tests | Recovery log includes an alert row. The public_01 "why W3 changed" query output is not in the repo. | Implemented but insufficiently demonstrated | Store is real. The review-day question is not answered with pasted output. |
| C13 crash recovery demonstrated | `controller/recovery.py`, `scripts/crash_recovery.ps1`, `scripts/crash_recovery.sh` | `tests/unit/test_crash_recovery.py::test_pending_approval_reconcile_no_duplicate_apply` | See crash section in §5. Resume finished public_01. | Partially implemented | One bug-induced crash, then a manual recovery CLI, then a resume. Not a kill-at-any-point demonstration. The helper script does not kill anything; it only runs `python -m controller.recovery`. |
| C14 loop observability | `build_supervisor_summary`, `bump_counter`, `emit_alert` in `controller/services/observability.py` | `tests/unit/test_fairness.py::test_deferral_and_supervisor_summary`, dashboard snapshot tests | Cycle lines in `logs/public01_controller_resume.log`. public_03 report says decision lag is not instrumented. | Partially implemented | Counters and a health summary exist. Ingest lag and event→decision lag are absent. `run.sh` does not expose them without reading stdout. |
| C15 fair credits and approver attention | `FairScheduler.order_workflows` in `controller/services/fairness.py`; per-workflow `_approver_attention_ok` | `tests/unit/test_fairness.py` | public_03 credit budget in the report shows `experiment_spent: 0`. Grade peak requests/24h stayed within policy caps. | Partially implemented | `order_workflows` still returns every workflow every cycle. Fairness reorders; it does not ration. The experiment credit cap is unused on the live path. |

---

## 3. Architecture review

### Runtime flow

`run.sh` execs `python -m controller.main --url "$SIM_URL"`. `Settings.from_env` defaults to `advance_minutes=60`, `db_path=data/controller.sqlite`, governance on, loop until finished.

`main()` opens `SimulatorClient` and `Store`, then calls `run_cycle`. If the scenario is not finished, it loops `client.advance(settings.advance_minutes)` and `run_cycle` until `finished`.

`run_cycle` (`controller/main.py`) does this, in order:

1. **Observe.** `observe()` GETs `/health`, `/sim/state`, `/workflows`, each `/workflows/{id}` config, and chunk metadata. It does not advance time.
2. **Ingest.** `ingest_available` → `ingest_kind` for `history`, then `events`. `SimulatorClient.chunks` GETs `/events/chunks?kind=&after=`. `SimulatorClient.chunk_text` GETs `/events/chunks/{n}` and ignores any filesystem `path` on the metadata. `Store.commit_chunk` parses NDJSON, `INSERT OR IGNORE`s into `events` by `event_id`, and writes `chunks` plus `checkpoints` in one transaction. A line-count mismatch raises `IngestIntegrityError` and rolls the chunk back.
3. **Project.** `project_events` walks `events` after `projection_state.last_event_id` and upserts `campaigns`, `leads`, `calls`, `config_changes`, `approval_decisions`. New leads get a row in `cohort_assignments` from `assign_cohort` using the **current** HTTP config, not `config_version_at_creation`. Calls store the event's own `cohort_id`.
4. **Metrics.** `compute_and_store_workflow_views` writes `metric_snapshots` for workflow, cohort, and caller-id slices. Pickup is connected/dials. Engagement is engaged/connected. Conclusion is distinct concluded leads / uploaded leads. They are not blended.
5. **Detect.** `detect_all` → `detect_workflow_issues`. Baseline is all but the last two calendar days (`split_baseline_recent`). Signals land in `detection_signals` only when both windows clear `MIN_COHORT_SAMPLE` (100) and the absolute threshold (`PICKUP_DROP`/`ENGAGEMENT_DROP` 0.05, `HALLUC_RISE` 0.02 with `HALLUC_ABS_FLOOR` 0.03, caller-id drop 0.08 versus workflow pickup).
6. **Experiments.** `run_all_experiments` plans one of `welcome_message` or `voice` per cohort id found on `calls`, evaluates with a two-proportion z-test and Holm thresholds, and upserts `experiments` / `experiment_arms`.
7. **Incidents.** `sync_incidents_from_signals` opens or refreshes `incidents`. Any open incident on the workflow is linked to that workflow's experiment.
8. **Decay.** `detect_promoted_decay` reads experiments whose `promotion_status` is `promoted` or `promoted_auto_apply`.
9. **Credits and order.** `CreditBudget.refresh_from_sim` stores `credit_budget_state`. `FairScheduler.order_workflows` reorders workflows and writes `scheduler_state`.
10. **Prompt incidents.** `run_prompt_incident_cycle` spends credits on `POST /llm/suggest-fixes` (cost treated as 3) and `POST /validation/runs`, then `build_prompt_proposal` and `submit_proposal`.
11. **Governance.** `run_governance_cycle` reconciles `controller_approvals`, applies approved ids, submits explore and winner proposals, then rollback proposals. Routes are `POST /approvals` or `PUT /workflows/{id}/config`, then `POST /workflows/{id}/apply` with only `approval_id`.
12. **Audit and summary.** `audit()` inserts `audit_decisions`. `build_supervisor_summary` reads counters, alerts, incidents, proposals, and the credit row.

The dashboard is not in this loop. `dashboard/app.py` `create_app` serves GET `/health`, `/api/snapshot`, and HTML from the same SQLite file. It may GET simulator health/state. It does not approve, apply, or advance.

### Tables that hold the loop

`events`, `chunks`, `checkpoints`, `ingest_issues`, `campaigns`, `leads`, `calls`, `config_changes`, `approval_decisions`, `cohort_assignments`, `projection_state`, `metric_snapshots`, `detection_signals`, `experiments`, `experiment_arms`, `incidents`, `proposals`, `controller_approvals`, `audit_decisions`, `config_snapshots`, `llm_suggestion_attempts`, `validation_run_records`, `scheduler_state`, `deferred_actions`, `credit_budget_state`, `observability_counters`, `operational_alerts`.

### Restart

Reopening the SQLite file restores checkpoints, the projection cursor, proposals, and approvals. The next `run_cycle` re-lists chunks after `checkpoints.last_chunk`, ignores committed chunk numbers, reconciles approvals, and applies anything still `approved`. `controller.recovery` does the ingest/reconcile/apply/prompt-resume steps and does not call `advance`. `main()` itself also reconciles on the next cycle, so the recovery CLI is not required for a normal restart.

### Coupling and design risks

- **One cycle recomputes the world.** Metrics, experiments, and detection scan `calls` for every workflow every cycle. public_03 stopped because of that, at 6.2 million events and a reported 7.4 GB database.
- **Promotion state is not durable across planning.** `plan_experiment` always constructs `promotion_status="observe_only_not_promoted"`. `Store.upsert_experiment` overwrites the column. `detect_promoted_decay` runs after that overwrite. A winner promoted on cycle N is invisible to decay on cycle N+1.
- **Two cohort keys.** Experiments and call KPIs filter `calls.cohort_id` (what the dialler used). Affected-lead counts and `cohort_assignments` use `assign_cohort` on the latest config. `tests/unit/test_cohorts.py` documents these as separate concepts. For `mid_flight: next_campaign_only`, the dialler locks cohorts per campaign and the joiner does not.
- **Fairness does not limit work.** `FairScheduler.select_next` picks one workflow, then `order_workflows` appends everyone else. `run_cycle` still runs prompt and governance for the full list.
- **Graded tick and `run.sh` tick differ.** Evidence runs advanced 240 minutes (public_01/02) or 360 (public_03). `run.sh` advances 60. `apply_window_hours` on the public workflows is 2. A 240-minute sleep can outlive an approval.
- **SQLite is a single writer** with full-row copies of every event in `events.raw_json` and again in `calls`. That is the 10× break the submission names, and the partial load run supports it.

---

## 4. Critical correctness checks

### HTTP-only simulator interaction

`controller/` does not import `sim` and does not read `runs/` or scenario YAML. `SimulatorClient.chunk_text` fetches `/events/chunks/{n}` and the docstring says it does not open the metadata `path`. GETs used for observation are covered by `test_observe_issues_only_gets`. Mutations are POST/PUT methods on the same client (`advance`, `request_approval`, `apply_approval`, `auto_apply`, `suggest_fixes`, `run_validation`).

This is holding in the code that runs under `run.sh`. Scripts under `scripts/` are operator tools. `measure_public03.ps1` writes a report that names the scenario path; it is not imported by the controller.

### Event ingestion idempotency and chunk recovery

`commit_chunk` is transactional. Duplicate `event_id`s are `INSERT OR IGNORE`. A second commit of the same chunk number returns `already_committed` without re-reading the body (`ingest_kind` passes `""` when `is_committed`). `test_api_failure_does_not_advance_checkpoint` and `test_replay_same_chunk_text_is_idempotent` cover that. `test_live_history_ingest_is_idempotent_and_does_not_advance` is the only integration test, and it skips if no simulator is up.

Checkpointing stores the highest committed chunk number, not a gap bitmap. The public simulator returns every chunk with `chunk > after` in one list (`sim/server.py` `list_chunks`). If a future server pages that list, `ingest_kind` will move the checkpoint to the highest chunk it saw and never go back for a hole. That is untested.

History is ingested before live events. public_03 stderr shows 23 history chunks, 5,537,379 inserts, 0 duplicates, 0 issues, then later live `last_event_id` 6,215,415. That supports idempotent history bootstrap. It does not show a crash in the middle of a chunk.

### Shared cohort consistency

`assign_cohort` is first-match AND/OR over `lang`, `city_tier`, `age_band`, `value_band`, else `default`, capped at 20. The joiner writes that result to `cohort_assignments` using the config object passed in from the current HTTP snapshot.

`Store.cohort_ids_for_workflow` and `variant_call_totals` filter `calls.cohort_id`, which is copied from the call event in `_upsert_call`. `compute_cohort_kpis` counts dials with that call cohort and counts uploaded leads with `leads_in_cohort` on `cohort_assignments`.

On the public runs the configs have zero cohort definitions (`"cohorts": 0` in the logs), so both paths collapse to `default` and the bug stays hidden. The unit test `test_recomputed_vs_historical_id_are_separate_concepts` asserts the split on purpose. Detection (`detect_workflow_issues`) does not take a cohort id at all, except that caller-id signals are workflow-wide. The supervisor snapshot can show `cohort_summary` from assignments. That is three consumers and two definitions.

### KPI separation and guardrail enforcement

`Rate` math keeps denominators separate. A zero denominator is `None`, tested in `test_rate_none_on_zero_denominator`. Experiment primary mapping accepts `engagement_rate` and `pickup_rate`. Conclusion is a third rate and is not what experiments optimise, which matches "optimise `primary`".

`decide_challenger` returns `guardrail_violation` when the guardrail drop exceeds `guardrail_max_drop`, before it can return `winner`. `test_guardrail_violation` covers an experiment that should not promote. `build_winner_proposal` carries the dry-run guardrail fields.

`build_explore_proposal` does not. Its dry run sets `"guardrail_ok": True` and `"impact_score": 0.25` with the limitation text saying it claims no causal lift. Those proposals are still submitted and can be applied. Both public grade reports mark one explore apply harmful (`ks_cancel` on public_01, `nw_kyc` on public_02). The guardrail is enforced on winner selection and not on the exploration apply that actually changes traffic.

### Experiment allocation, samples, tests, multiple comparisons, winner choice

`balanced_allocation` keeps a control floor of 0.30 and an exploration floor of 0.10 when there is room. `min_sample_per_arm` is 100. `two_proportion_ztest` is two-sided and returns `underpowered` below `min_n`. `DEFAULT_MIN_EFFECT` is 0.02. `choose_control` prefers the heaviest current weight, then the most-dialled allowed variant. Arms with no dials stay in the plan and block a decision until every arm reaches `min_n` (`evaluate_experiment` returns `underpowered` if any challenger is short). That matches "only what happened counts" in the sense that a missing arm cannot win. It also means a variant the approver never allows can keep the experiment underpowered forever, which then re-submits exploration.

`holm_adjust` assigns each test the threshold `alpha / (m - rank)` after sorting p-values. `evaluate_experiment` then calls `decide_challenger` independently. Classical Holm stops at the first non-rejection. This implementation can call a larger p-value a winner under its looser threshold even when a smaller p-value failed its stricter one. `test_holm_adjust_order` checks the threshold formula only.

When several challengers are winners, the sort is `(-effect, variant id)`. That part is deterministic and does not call an LLM. The promoted allocation is not 100% to the winner. `build_winner_proposal` sets the winner share to at least 0.55 and leaves the rest on the control. The grade's decaying-variant share sitting near 0.55 is what that allocation looks like if it is never rolled back.

`select_dimension` sticks to an existing active dimension, otherwise prefers the dimension that already has multi-arm traffic, with `welcome_message` before `voice` on ties. Voice is implemented and not evidenced in the public apply lists.

### Winner decay and rollback

`detect_promoted_decay` compares the last two calendar days with earlier days, requires 100 samples in both windows, and fires on a 0.05 primary drop or a 0.03 pickup guardrail drop. It then sets `promotion_status` to `decay_detected` and audits `decay_detected`. `propose_decay_rollbacks` builds a proposal from `latest_config_snapshot(..., source="rollback_candidate")`. `build_rollback_proposal` refuses a snapshot whose diff includes `prompt_version`.

There is no unit test for this path. In the live loop it cannot see promotions: `plan_experiment` resets `promotion_status` to `observe_only_not_promoted` and the upsert replaces the old value before `detect_promoted_decay` reads the table. The public_01 resume log ends with `decay_hits=0` and `promotion=observe_only_not_promoted` on all three experiments, after the grade shows a W3 promotion was applied. Both grade reports leave `nw_renewal:welcome_message=W3` on about 54–56% of in-population calls through the end of the scenario. That is a failed decay rollback, not an absence of decay in the scenario.

### Approval lifecycle, expiry, lapse, stale base, supervisor edits

`submit_proposal` persists the draft, audits `proposal_built`, then auto-applies or POSTs `/approvals`. A local row already in `submitted|approved|applied|auto_applied` is not sent again. `duplicate_proposal_id` is recovered by listing approvals. `test_duplicate_proposal_reconciliation` covers that.

`reconcile_approvals` GETs each non-terminal local approval and maps simulator status onto the proposal. `apply_approved` POSTs `{approval_id}` only for local `approved` rows. `approval_lapsed` and `not_approved` refresh the row. `stale_base_version` sets the proposal to `stale` and stops. Nothing in `promote_experiment_winners` or `run_prompt_incident_cycle` builds a fresh id from a `stale` or `lapsed` proposal unless the in-memory experiment is still a winner and the live version changed enough to change `proposal_id_for(...)`. `should_start_attempt` treats `lapsed` as not in-flight, so a prompt incident can try again, but `BLOCKING_ATTEMPT_STATUSES` may still block on the previous attempt status. The public_02 grade is the evidence that this did not recover: 6 lapses, `mttr_minutes: null`.

`test_stale_apply_marks_proposal` checks the mark. It does not check that a new proposal is submitted.

`_approver_attention_ok` counts local `draft|submitted|approved` rows against `max_requests_per_24h`. It does not know the simulator's rolling 24h clock, and it does not wait for `approver_hours`. Supervisor edits are visible as `config_changed` events and as a new `GET /workflows/{id}/config` version. Explore and winner builders refresh that GET before proposing. There is no test where a supervisor edit lands between submit and apply and the controller re-proposes instead of retrying the stale body. public_02 contains two `supervisor_edit` incidents. The grade does not show an apply conflict (`apply_conflicts: 0`), and the prompt incident was not fixed.

`auto_apply_permitted` requires policy `enabled`, every changed dim in `allowed_dims`, total-variation shift ≤ `max_shift`, and local `impact_score ≥ 0.25`. A 403/409 falls through to `/approvals`. Both public grades show `auto_applied: 0`, which is consistent with welcome-message shifts being outside the allowed dims or above `max_shift`.

### Prompt validation and post-application verification

`select_validated_candidate` keeps completed runs with `pass_rate ≥ min_validation_pass_rate` and a `validation_run_id`, then sorts by descending pass rate and ascending `prompt_version`. `build_prompt_proposal` puts that id in `evidence` and refuses any diff other than `prompt_version`. Dry-run text says validation is not engagement proof. Mocked tests cover the happy path, below-threshold refusal, credit failure, bad payload, and malformed suggestions.

`_verify_applied_prompt_fixes` resolves the incident only if an applied `prompt_incident` proposal's stored `halluc_rate` is below `HALLUC_ABS_FLOOR` (0.03). The snapshot can be the long "recent" or "all" window, so pre-fix calls dilute it. Nothing calls this on public_02, because the prompt proposals lapsed. No unit test calls the function.

### Caller ID incident handling

`_caller_id_signals` compares each caller id's pickup with the workflow pickup on the `all` window, minimum 100 dials, drop of 0.08. `test_caller_id_health_on_known_gap` checks the signal. `sync_incidents_from_signals` will open an incident. No builder changes `caller_id` weights. `EXPERIMENT_DIMS` excludes it. Policy on `ks_cancel` allows auto-apply of `caller_id` up to `max_shift` 0.5, so a rotation would have been legal. public_02 left the flagged DID near a 0.5 share for the rest of the run.

### Credits and fair scheduling

`CreditBudget` holds a local reserve of 30 and an experiment cap of 50. `can_spend(..., purpose="emergency_prompt")` is used before suggest-fixes. Experiment code never calls `can_spend`. public_03's stored budget shows `experiment_spent: 0` while credits remaining stayed 400, which matches "no LLM on the load run" and also shows the cap is not on the hot path.

`FairScheduler` is a deterministic weighted round-robin with urgency boosts for hallucination, high severity, and approved-but-unapplied work. Tests show it does not starve a workflow when `select_next` is called alone. `order_workflows` then hands the entire ordered list to the prompt and governance cycles, so the boost only changes who is processed first. Approver attention is a per-workflow open-proposal count, not a cross-tenant budget.

### Audit records and observability

`audit()` writes `audit_decisions` with a hash id and idempotency parts. Kinds include `proposal_built`, `approval_submitted`, `approval_reconciled`, `applied`, `auto_applied`, `decay_detected`, `prompt_proposal_routed`, `credit_spend`, `crash_recovery_completed`, `scheduler_selected`.

`observability.py` has counters and alerts. There is no field for ingest lag or event-timestamp to decision-timestamp. `SUBMISSION.md` §8 and `reports/public_03_load.md` both say the lag histogram does not exist. Alerts that page are described in the submission (`apply` failures, credit exhaustion, ingest integrity). `recovery_ran` is severity low. The dashboard can show the summary only if someone starts it.

`credit_spend` idempotency includes a wall-clock minute (`_now()[:16]`), so two spends in the same minute can collapse or, across minutes, duplicate. That is a minor audit-quality issue.

### Crash recovery and duplicate-apply prevention

`apply_approved` only selects `status='approved'`. After a successful apply it writes `applied`. A crash after the simulator applied and before the local write would POST the same `approval_id` again. The unit test `test_pending_approval_reconcile_no_duplicate_apply` mocks one apply. The live recovery log shows `applied: 0` because the three approvals were still pending, then the resume log shows `applied=3` once at cycle 3 and `applied=1` once at cycle 15. The grade counts 4 applies. Those numbers agree for this run. They do not show a kill during the apply POST.

`run_crash_recovery` sets `duplicate_proposals` to `len(ids) - len(set(ids))` over `proposals.proposal_id`. That column is the primary key, so the value is always 0. The log line `duplicate_proposals: 0` does not prove the simulator rejected a duplicate POST.

Pending approvals were not lost: recovery reconciled 3, and the resume did not submit 3 new explore proposals on the next cycles (`gov_submitted=0` until a later winner). That part of the story is supported by `logs/public01_controller_resume.log`.

### Continuous `run.sh` behavior

`run.sh` requires a simulator URL, picks a venv Python or `PYTHON`, sets `PYTHONUTF8` and `PYTHONUNBUFFERED`, and execs `controller.main` with only `--url`. The process advances until `finished` or a `SimulatorError` / `IngestIntegrityError`. It does not start the simulator. `--once` is not passed, so the loop is on. Default advance is 60 minutes (`controller/settings.py`).

The public grade evidence was not produced by that command. Phase 9 notes and the reports say `--advance-minutes 240` and `360`. A hidden-scenario run of `./run.sh` will tick four times faster in simulated time than the public_01/02 evidence and will spend more wall time per simulated day than those runs if each cycle stays expensive. `run.sh` does not document Docker. The README says to say so if Docker is required. It is not required here.

`main()` prints a full summary every 12 cycles. Compact lines still print every cycle. A crash in the loop returns exit code 1 and does not resume by itself. Restarting `run.sh` against the same `data/controller.sqlite` is what resumes it. The script does not pass `--db`, so the path is stable only as long as the working directory and `CONTROLLER_DB` stay the same.

---

## 5. Public scenario evidence

### public_01

**What `reports/public_01.md` proves**

The grader compared a finished controller run with a no-op baseline of `public_01_dev`.

| workflow | conclusion Δ | pickup Δ | engagement Δ |
|---|---|---|---|
| ks_cancel | +2.7% | +2.2% | -0.0% |
| nw_kyc | +0.9% | -2.2% | +3.3% |
| nw_renewal | +1.0% | -0.1% | +2.0% |
| ALL | +1.6% | +0.5% | +1.3% |

Four controller-approved `welcome_message` applies: `nw_kyc` v2 beneficial (engageΔ=+0.039), `ks_cancel` v2 harmful (engageΔ=−0.023), `nw_renewal` v2 beneficial (+0.066), `nw_renewal` v3 beneficial (+0.014). Governance: 4 requests, 4 approved, 0 expired, 0 lapsed, 0 auto-applied, 0 conflicts. No incidents. Credits, validations, and LLM calls are 0. `nw_renewal:welcome_message=W3` stays on about 54–56% of in-population calls from day 3 through day 7 after onset. Engagement-gap captured for ALL is 2%. `ks_cancel` captured −16%.

**What the logs prove**

`logs/public01_crash_recovery.log`: recovery ran, 3 approvals reconciled, 0 applied, ingest inserted 0, 3 proposals, 187,400 events, credits remaining 200.

`logs/public01_controller_resume.log`: same DB resumed with 240-minute advances; cycle 3 `applied=3`; cycle 10–14 submitted one more proposal; cycle 15 `applied=1`; `done cycles=48 wall_seconds=174.3 ... minute=11520 finished=true`. The final experiment block says all three welcome experiments are `no_winner` with `promotion=observe_only_not_promoted` and `decay_hits=0`.

**What is only claimed**

`SUBMISSION.md` §10's audit rows for `apr_00004` → v3 are not in a committed query output. The interpretation that W3 promotion "helped" is the grade's beneficial label, not a log line that names W3.

**What remains unverified**

Whether `./run.sh` (60-minute ticks, default DB) reproduces these four applies. Whether the harmful `ks_cancel` explore would still be sent under a guardrail check. Whether decay would have moved W3 off. The resume log's final state says the controller no longer believes it has a winner.

### public_02

**What `reports/public_02.md` proves**

ALL conclusion −1.7%, pickup −0.6%, engagement −0.5%. `nw_kyc` conclusion −4.4% and engagement-gap captured −46%.

Four welcome applies: `nw_kyc` v2 harmful (engageΔ=−0.040), `ks_cancel` v2 beneficial (+0.016), `nw_renewal` v2 beneficial (+0.059), `nw_renewal` v3 beneficial (+0.036). Governance: 10 requests, 10 approved, 4 applied, 6 lapsed, 0 expired, 0 auto-applied, 0 conflicts. Peak requests/24h stayed at or under the policy caps in the YAML (renewal 2 of 4, cancel 1 of 3, kyc 1 of 2).

Incidents the grader scored:

- `did_flagged` on `ks_cancel` at D2 10:00. Flagged DID share stays between 0.494 and 0.517 through D9.
- `supervisor_edit` on `ks_cancel` at D3 11:30. `mttr_minutes: null`, `fixed_by: null`.
- `supervisor_edit` on `nw_renewal` at D5 15:00, with no MTTR field.

Credits used 54, validation runs 18, LLM fix calls 6. W3 decaying share stays about 0.53–0.56. Hallucinated calls are 415 versus a baseline of 416, so the prompt incident was not fixed in the outcome either.

**What the logs prove**

`logs/public02_controller.log` is 115 lines of repeating workflow/config INFO lines (`prompt_version` P1, later `actor: controller_approved`). It does not contain cycle summaries, lapse reasons, validation ids, or a `finished=true` line. It does show the controller observed approved config versions while the prompt was still P1.

**What is only claimed**

`SUBMISSION.md` §7 says proposals "P3/P5/…" were submitted and lapsed. The grade proves lapses, credits, and a null MTTR. It does not name P3 or P5. Those ids are not in the committed log.

**What remains unverified**

Why each of the six approvals lapsed (apply window versus tick, approver latency, or a bug that never called apply). Whether a 60-minute `run.sh` tick would have applied them. Whether the supervisor edit on `nw_renewal` was preserved. DID rotation, which the grade shows did not happen.

### public_03

**What `logs/public03_controller.err` proves**

- Start: `scenario=public_03_load`, `sim_minute=0`, `finished=false`, `last_event_id=5537379`, `credits_remaining=400`, 23 chunks.
- History ingest: 23 chunks, `inserted=5537379`, `duplicates=0`, `issues=0`.
- Advances of 360 minutes to `sim_minute` 360, 720, then 1080. At 1080: `finished=false`, `last_event_id=6215415`, `credits_remaining=400`, 27 chunks.
- The file ends after that state line. There is no `finished=true`.

**What `reports/public_03_load.md` claims beyond the log**

Wall clock about 80 minutes / 4800 seconds, peak working set about 188 MB, DB about 7396.7 MB, 4,679,295 calls, 1,536,000 leads, 6,215,415 events, throughput 1294.9 events/s including bootstrap, 3 completed cycles, 12 proposals, 12 approvals applied, extrapolated ~106.7 wall-minutes per simulated day. The report itself says the 5-day run was not finished and that decision lag was not instrumented. Those extra figures are not in `public03_controller.err`. `logs/public03_controller.log` is empty (0 bytes).

**What this is not**

It is not a completed load test. It is not one full simulated day (1440 minutes). 1080 minutes is 0.75 day, which the report states. It does not meet the SRE bar of one simulated day in under 10 wall minutes. The 1295 events/s figure includes history bootstrap and cannot be read as steady-state live ingest. Memory is a sampled Windows working set, not a 4-vCPU/8 GB Linux measurement, and it does not include the OS cache for a multi-gigabyte SQLite file. Event-to-decision lag is absent, not zero.

### Crash recovery

**What the report and logs prove**

The controller on public_01 died after cycle 1 with `AttributeError` on `sqlite3.Row.get` while checking open proposals (`reports/crash_recovery.md`). The simulator was left at minute 240. `python -m controller.recovery` reconciled 3 pending approvals, applied 0, and did not advance time. The same DB was started again with `--advance-minutes 240` and reached `finished=true` at minute 11520 with 4 grade-visible applies and no second copy of the initial 3 explore submits in the following cycles.

**What they do not prove**

- A deliberate kill at an arbitrary point. The crash was a bug that has since been fixed, so this exact failure cannot be repeated.
- `scripts/crash_recovery.ps1` only launches `controller.recovery`. It does not kill a process.
- No duplicate simulator apply. `duplicate_proposals: 0` is a primary-key identity.
- Recovery of in-flight LLM calls, lapsed approvals, or a half-written apply. public_01 used no credits.
- That a restart forgets nothing learned. The final resume log has wiped promotion status back to `observe_only_not_promoted`.

---

## 6. Stakeholder review

Policies below are the public scenario YAML. Hidden scenarios can differ. The submission is right to trust the contract over the memos, and a few replies still over-claim what the code does.

### 01 Sales (Nikhil) — `stakeholders/01_sales.md`

He wants one blended lift number, same-day winner rollout, auto-apply on all of Kestrel and on Northwind KYC, fewer approval requests, and a "persuasive" prompt wherever it is available.

`SUBMISSION.md` correctly refuses a blended KPI and correctly flags Northwind KYC: `scenarios/public_01_dev.yaml` and `public_02_incidents.yaml` set `nw_kyc` `auto_apply.enabled: false`. It misses a second false claim. Kestrel has not "turned on auto-apply for everything". `ks_cancel` allows only `schedule` and `caller_id`. Welcome, voice, and prompt are not in `allowed_dims`. The reply says "Kestrel stays on the auto-apply dims already in policy", which is the right pushback, but the "wrong claim" bullet names only KYC.

The reply promises speed from exploration and same-day apply windows. public_02's prompt approvals lapsed inside a 2-hour apply window. Same-day rollout is not what the recorded run did. No code rolls out a prompt because another account liked it. That refusal is sound and is only implicit.

### 02 Compliance (Meera) — `stakeholders/02_client_compliance.md`

She requires a named human approval for anything the customer hears (opening line, voice, script), validation evidence on scripts, a 24-hour answer down to the variant a customer heard, a preference for slow and right, and approver hours 10:00–19:00 (KYC 10:00–18:00).

The hours match `approver_hours` in the public YAML. The submission accepts them and does not implement a clock that waits for those hours. The simulator enforces them. Sending at night still queues, which she asked not to impress people with.

The reply says welcome, voice, and prompt go through `/approvals`. Prompt is true on these policies (`prompt_version` is not an auto-apply dim). Welcome and voice are auto-apply dims on `nw_renewal` when `max_shift` is 0.2 and the local impact floor passes. Large explorations miss that shift and fall through to approval, which is why the public applies say `controller_approved`. The reply states a stronger rule than the policy. A small welcome shift on renewal can still auto-apply.

"Why did this customer hear this variant" is not answered. `audit_decisions` is per workflow and proposal. The call row has `variant`, `cohort_id`, and `config_version`, but nothing in the submission shows a person-level query, and the 24-hour operational path is "look in SQLite" with no runbook query checked in.

She would rather be slow than briefly worse. The controller applies exploration before a winner, and both public grades include a harmful apply. The reply does not mention that.

### 03 Platform/SRE (Kabir) — `stakeholders/03_platform_sre.md`

He requires a kill/restart test, idempotency, and actionable alerts. He prefers no new stateful service, one public_03 simulated day in under 10 wall minutes on 4 vCPU / 8 GB, a 10× break point, and loop health without reading logs.

The submission is honest that the 10-minute day was not met. SQLite WAL is an existing-file choice, not a new service. The 10× answer (single-writer SQLite and full-table recompute) matches the partial run.

The kill/restart claim is stronger than the evidence (see §5). Idempotency of chunk insert is real. Idempotency of apply-at-any-kill-point is not demonstrated. Loop health without logs requires the dashboard, which `run.sh` does not start, and the dashboard does not show ingest lag because the controller does not record it. Alerts exist as rows. Nothing pages an engineer.

### 04 Ops supervisor (Anita) — `stakeholders/04_client_ops_supervisor.md`

She edits prompts during the day and does not want them undone. She wants at most two requests a day, never before 11:00, five changes batched into one request, notice before a running campaign changes, one screen, and no compliance-script "fix" that kills engagement.

`ks_cancel` `max_requests_per_24h` is 3, not 2. Approver hours start at 10:00, not 11:00. The submission is right that "never before 11:00" is not in the policy. It still does not batch dimensions into one proposal. Explore, winner, prompt, and rollback are separate proposals.

`ks_cancel` is `mid_flight: allowed`, so a config apply changes the running campaign on the next tick. There is no "tell Anita first" step beyond the approval itself. The dashboard is one screen and was not part of the graded run.

The lifeless-script concern is accepted in the reply. The code treats validation pass rate as safety only, which is the right reading of `docs/SIMULATOR.md` §5. public_02 then failed to apply the validated prompt, so the bad supervisor prompt stayed in place. That is the opposite failure: the harmful script was not replaced.

Supervisor edits are not silently overwritten in the sense that apply uses the approved body and a moved `base_version` becomes `stale`. There is no live trace of the D3 or D5 edits being rebased.

### 05 Product (Tara) — `stakeholders/05_product.md`

Separate KPIs: held in the metrics and in the submission. Blended score is not computed.

One shared cohort object: not held once cohorts exist. The code and the unit test keep historical call cohorts and recomputed assignments apart. Detection does not use the assignment table.

Minimum size: 100 on the primary sample (dials for pickup, connects for engagement). The submission states the speed-versus-false-winner trade. It does not give a false-winner probability, only Holm at alpha 0.05 plus a 2-point effect floor, with the Holm step-down applied independently.

Proposals carry impact, diff, affected leads, and a dry run. Explore impact is a constant. The dry run for explore is not a historical replay.

Expired proposals: `expired` updates experiment promotion to `promotion_expired`. `stale` and `lapsed` do not start a fresh proposal id by themselves. Tara asked to re-queue with fresh data and not to resubmit the stale body. The stale path does not re-queue.

Only observed arms count: an underpowered arm cannot be a winner. Unseen arms are still included in the planned allocation and can force another explore submit.

Per-person call time is cut, and the submission says so. Caller-ID health and the supervisor view were her other P0s. Caller ID is detect-only. The view exists and is unwired from `run.sh`.

### 06 CTO (Aryamann) — `stakeholders/06_cto.md`

He asks for explainable changes, credits spent only where they change a decision, an honest scale break, a real choice with a written no, and docs for the on-call. He also says some memos are wrong.

The submission does say no to C4 and keeps C5 partial. It still built scaffolding for almost every other capability, and several of those scaffolds do not survive contact with the public incidents (decay, prompt apply, DID). That is closer to "a bit of everything" than the reply admits.

Credits on public_02 (54) did not change the live prompt. That spends compute without a decision, which is the thing he said to avoid.

The on-call section (§14) is a list of future work. `docs/DASHBOARD.md` and the phase notes are not a page-time runbook. The audit query in §10 is the right idea and has no pasted output.

Trusting policy over Sales on KYC auto-apply is the correct call. The same standard should have flagged Kestrel "auto-apply for everything".

---

## 7. Submission quality

All 15 template headings, 0 through 14, are present in `SUBMISSION.md`. None were deleted.

| Heading | Issue |
|---|---|
| 0. TL;DR | The public_01/02 numbers match the grade files. "fair scheduling" and "crash recovery" are stronger than the evidence. The cut of C4 and the partial C5 are stated. |
| 1. Assumptions | Useful and mostly checkable. Assumption 4 (min sample 100, z-test, Holm, ~2 points) matches the constants. Assumption 9 matches the code. The Holm description does not mention that rejection is not sequential. |
| 2. Scope table | C3, C6, C9, C10, C11, C12, C13, C14, C15 are marked Built. C9 is missing stale re-proposal and hour timing. C13's live proof is one bug crash. C14 has no lag. C15 does not ration. C7 "little decay action" understates a path that the loop disables. C1 Partial is fair. |
| 3. Stakeholders | Replies are within the spirit of the 120-word limit. Wrong-claim coverage is incomplete for Sales and too generous for Compliance. See §6. |
| 4. Architecture | The ASCII diagram matches `run_cycle` order. "Restart: reopen DB → idempotent ingest → reconcile → apply" matches `run_crash_recovery` and the next `run_cycle`. It does not mention that planning overwrites `promotion_status`. |
| 5. Learning loop | "shared `cohort_assignments`" is only half of the read path. "Decay/rollback: promoted winners monitored" conflicts with `decay_hits=0` at the end of the public_01 log and with the grade's flat W3 share. Guardrail text describes winner blocking, not explore applies. |
| 6. Governance | The state machine lists `stale` and `expired`. It does not say stale stops without a new proposal. "Batching via open-proposal soft cap" is a count of local open rows, not one request with five changes. |
| 7. Incidents | The public_02 timeline matches the grade on DID share, null MTTR, 6 lapses, 54 credits, 18 validations, 6 LLM calls. Naming "P3/P5" is not supported by the grade or the log. |
| 8. Scale | The honesty note matches the report: partial, 1080/7200, ~107 min/day, lag missing. The report's memory, DB size, and throughput are not in the stderr log. Do not present them as if this review re-measured them. The 10× paragraph is a design claim, consistent with the bottleneck, not a measurement at 10×. |
| 9. Reliability | Points at `reports/crash_recovery.md`. Does not say the crash was an attribute error rather than a scheduled kill. "4xx mapped to typed errors; 5xx retried" matches `SimulatorClient._request` and `test_retries_5xx_then_succeeds` / `test_does_not_retry_4xx`. |
| 10. Audit | The SQL is a real query shape for `audit_decisions`. The sample rows are narrated. There is no command output. A reviewer who asks this live will be using the database, not this paragraph. |
| 11. Results | The pasted tables match the grade files, including the harmful explores. The interpretation is consistent with the grade. It does not say the runs were not `./run.sh`. |
| 12. Trade-offs | The four rows are real decisions (no blended KPI, no blanket auto-apply, frequentist tests, SQLite). Bayesian-versus-Holm is fine. The Holm implementation caveat is missing. |
| 13. Known weaknesses | This is the strongest section. Harmful exploration, lapsed prompts, no DID rotation, missed 10-minute day, no lag histogram, and rare decay are all true. "Decay rollback rarely fired" should say the live loop overwrites promotion status so it does not fire. |
| 14. Production plan | A sensible order. It is future work, not evidence. |

Contradictions to leave visible:

- `docs/IMPLEMENTATION_STATUS.md` says phases 0–9 are complete for the floor. The floor includes public_01 and public_02 with no manual steps via `run.sh`. The recorded runs used a different advance and, on public_01, a crash plus a hand-started recovery command.
- `SUBMISSION.md` C13 "Built" versus `reports/crash_recovery.md` describing a bug crash, a separate recovery process, and a unit test that "covers ... without a live world".
- public_01 grade says a W3 promotion was applied and remained the decaying variant. The end of `logs/public01_controller_resume.log` says that experiment is `no_winner` and `observe_only_not_promoted`.
- Compliance reply says welcome and voice always wait for approval. `nw_renewal` policy allows those dims inside `max_shift` 0.2.

---

## 8. Prioritized findings

> **9 Nov label:** Findings 1–3 below are historical (8 Nov). Re-read §0 before acting on them. Finding 1’s “overwrite” root cause is **fixed** in current `experiments.py`; residual W3 share after rollback remains a real outcome gap. Finding 2’s public_02 lapse/null-MTTR numbers are **wrong** against the locked grade (0 lapsed; MTTR 1455). Finding 3’s “no builder” claim is **wrong**; the graded non-firing outcome still stands. Finding 4 (harmful exploration) remains valid as an outcome.

### Critical

**1. Decay rollback cannot see a promotion, and the public runs left a decaying variant in place**

Why it matters: hidden-scenario scoring explicitly includes decay rollback. Both public grade reports show `nw_renewal:welcome_message=W3` stuck near a 55% share after it starts decaying. The winner builder itself installs about a 55% share (`build_winner_proposal`).

Evidence: `plan_experiment` sets `promotion_status="observe_only_not_promoted"`. `Store.upsert_experiment` writes that column on conflict. `run_cycle` calls `run_all_experiments` before `detect_promoted_decay`. `detect_promoted_decay` ignores every other status. public_01 resume log ends at `decay_hits=0`. No test references `detect_promoted_decay`.

Suggested fix: when upserting an experiment, preserve `promoted`, `promoted_auto_apply`, and `decay_detected` unless a new decision explicitly supersedes them. Run decay detection against that preserved status. On a hit, submit one rollback that moves the decaying variant down, and do not require the experiment row to still say `winner`. Add a regression that promotes, re-runs `run_all_experiments`, and still gets a rollback proposal when the recent window drops by ≥ 0.05.

Proof: `pytest tests/unit/test_decay.py` (new) plus a public_01 grade whose decaying-variant share falls after onset instead of sitting at ~0.55.

**2. Prompt and other approvals lapse before apply, so incident MTTR is null**

Why it matters: incident time-to-fix and governance hygiene are scored. public_02 approved 10 requests, applied 4, lapsed 6, and did not fix the supervisor prompt. `apply_window_hours` is 2. The evidence loop advanced 240 minutes at a time, and `run.sh` advances 60. Either tick can miss a short window if apply happens only after the next full advance.

Evidence: `reports/public_02.md` governance block and `supervisor_edit.mttr_minutes: null`. `apply_approved` runs inside `run_governance_cycle`, which runs after `advance` returns. `should_start_attempt` / stale handling do not mint a new proposal id when status is `lapsed` or `stale`.

Suggested fix: poll approvals on a cadence shorter than the workflow's `apply_window_hours` (15-minute ticks, or apply as soon as `GET /approvals/{id}` says approved, before the next large advance). On `lapsed` or `stale_base_version`, build a new `proposal_id` from the current version and the same evidence if it is still valid. Do not POST the old id again.

Proof: a mocked test where an approval becomes approved with `apply_by` inside the next 15 minutes and `apply_approved` is called before the following advance; a second test where `stale_base_version` produces a new id. Re-run public_02 with `./run.sh` and show `mttr_minutes` set or an honest remaining lapse reason from the audit table.

**3. Flagged caller IDs are detected and never rotated**

Why it matters: public_02's graded incident is `did_flagged`, and the share stays at ~0.5 for eight days. `ks_cancel` policy allows `caller_id` auto-apply up to `max_shift` 0.5. Hidden scenarios will include this incident class.

Evidence: `_caller_id_signals` in `controller/services/detector.py`. `EXPERIMENT_DIMS = ("welcome_message", "voice")` in `controller/services/experiments.py`. No `build_*` function writes `caller_id` weights. Grade `flagged_did_share_by_day` stays near one half. `test_caller_id_health_on_known_gap` stops at the signal.

Suggested fix: when a `caller_id_health` incident is open and the dim is in `allowed_dims`, propose a shift that moves weight off that id without exceeding `max_shift`, with a control/explore floor so one bad id is not replaced by an unmeasured one. Keep it on the approval path when auto-apply is off.

Proof: a unit test from a known pickup gap to a proposal whose `caller_id` weight for the bad id is lower, and a public_02 grade whose flagged share drops after D2.

**4. Exploration applies are allowed to be harmful**

Why it matters: scoring gives no credit for lift that comes with harmful applies, and both mandatory scenarios contain one. `ks_cancel` engageΔ=−0.023 (public_01) and `nw_kyc` engageΔ=−0.040 (public_02) are the harmful rows, both `welcome_message` explores.

Evidence: `build_explore_proposal` sets `guardrail_ok: True` and a fixed impact of 0.25. `decide_challenger`'s guardrail check is only on the winner path.

Suggested fix: do not submit an explore that puts unmeasured challengers above a small floor, or require the same guardrail non-inferiority check on any arm that already has samples. Prefer a shift inside `max_shift` so the first apply is reversible. Record the pre-explore snapshot and roll it back if the next window's guardrail or primary drops by the decay thresholds, even before a winner exists.

Proof: `tests/unit/test_governance.py` gains a case where a challenger already below the guardrail does not produce an explore proposal. A new public_01 grade shows `harmful: 0` or explains any remaining harmful row from the audit.

### High

**5. `./run.sh` is not what was graded**

Why it matters: hidden scenarios execute `./run.sh`, not `--advance-minutes 240`. Apply timing, credit spend, and wall clock will differ. The floor says no manual steps.

Evidence: `run.sh` passes only `--url`. `Settings.advance_minutes` defaults to 60. Reports document 240 and 360. public_01 also needed a hand-run `controller.recovery` because of a crash.

Suggested fix: make the default tick small enough for `apply_window_hours` (15 or 30), keep it in `run.sh` with no extra flags, and re-grade public_01 and public_02 with that command only. Put those grade files in `reports/`.

Proof: the report header or a committed log line showing `minutes=60` or whatever `run.sh` actually uses, and `done ... finished=true` with no second manual process.

**6. Cohort identity splits across tables**

Why it matters: C2 is a product requirement from Tara, and hidden workflows can define real cohorts. Experiments will optimise the dialler cohort while affected-lead counts and the dashboard use a recomputed id. `mid_flight: next_campaign_only` makes the dialler lock cohorts per campaign; the joiner uses the latest config for every new lead.

Evidence: `project_events` / `_upsert_lead` versus `Store.variant_call_totals` filtering `calls.cohort_id`. `test_recomputed_vs_historical_id_are_separate_concepts`.

Suggested fix: pick the call event's `cohort_id` as the only cohort key for metrics, experiments, detection, affected leads, and the dashboard. Use `assign_cohort` only when proposing a new cohort definition, and label that as a proposal, not as history.

Proof: a test with two cohort ids where a call's `cohort_id` is `senior_ta` and the current config would assign `young`, and every consumer reports `senior_ta`.

**7. public_03 is a partial run and the decision lag is missing**

Why it matters: 20% of engineering score and the SRE memo ask for throughput, lag, memory, and a 10× story on a full simulated day. The committed run is 1080 of 7200 minutes.

Evidence: `logs/public03_controller.err` last state is `sim_minute: 1080`, `finished: false`, `last_event_id: 6215415`. `reports/public_03_load.md` states the extrapolation and the missing lag. DB size and RSS are report-only.

Suggested fix: stop full-table experiment scans. Maintain rolling aggregates by workflow, day, cohort, and variant as rows are projected. Measure event time → first decision time. Re-run at least one simulated day with `./run.sh` on the load scenario, or keep the partial label and delete any sentence that can be read as a completed load test.

Proof: a log line with `sim_minute` ≥ 1440 and a table of lag percentiles computed from stored timestamps, plus process RSS sampled on that run.

**8. Crash evidence does not match "kill at any point"**

Why it matters: C13 and Kabir's non-negotiable are a kill/restart, not a one-off exception that was then fixed.

Evidence: `reports/crash_recovery.md` names `sqlite3.Row.get`. `scripts/crash_recovery.ps1` only calls `controller.recovery`. `duplicate_proposals` cannot be non-zero.

Suggested fix: add a harness that starts `run.sh`, kills the process after the first `approval_submitted` audit row, restarts `run.sh` on the same DB, and asserts one apply in the simulator and one `applied` audit row. Delete the primary-key duplicate counter or replace it with a count of `duplicate_proposal_id` responses.

Proof: the harness log plus `sim grade` showing the same apply count as `audit_decisions` rows of kind `applied`.

**9. The audit answer a reviewer will ask is not in the repo**

Why it matters: 10% communication, and the review plan is to pick one hidden decision and ask for it from the store.

Evidence: `SUBMISSION.md` §10 narrates `apr_00004` without output. `audit_decisions` exists.

Suggested fix: check in the exact SQL and its output for the public_01 W3 apply, including the approval id, the diff, and the next metric snapshot. Point the dashboard at that query.

Proof: the output block matches a row a reviewer can reproduce from `data/ctrl_public01.sqlite` if that DB is still available, or from a small fixture exported into the repo.

### Medium

**10. Holm is not step-down**

Why it matters: false winners are part of the design score. With two or three challengers the independent threshold can crown a variant Holm would not.

Evidence: `holm_adjust` and the per-arm loop in `evaluate_experiment`. `test_holm_adjust_order` does not check sequential stopping.

Suggested fix: sort p-values, reject while `p <= alpha / (m - rank)`, and stop. Only rejected arms may be winners.

Proof: a unit test with p-values `(0.04, 0.03)` at alpha 0.05 and m=2, where classical Holm rejects neither if 0.03 > 0.05/2, and the code matches that.

**11. Fair scheduling does not change who gets served**

Why it matters: C15 is marked Built. Hidden multi-tenant load can starve a tenant only if the code actually skips work.

Evidence: `order_workflows` returns the selected workflow plus every other workflow. `run_cycle` iterates that full list.

Suggested fix: either cap submissions per tick to the selected tenant, or stop calling the design "fair allocation" in the submission and describe it as a sort key.

Proof: a test that with three workflows and a cap of one governance action per cycle, each workflow is submitted at least once across N cycles and never twice in a row when weights are equal.

**12. Post-apply prompt verification can close an incident on a diluted window, and it is untested**

Why it matters: C8's last step is "verify after apply". A long window can look healthy while the new prompt is still bad, or the reverse.

Evidence: `_verify_applied_prompt_fixes` uses `window_label` `recent` or `all` and `HALLUC_ABS_FLOOR`. No test name calls it. public_02 never applied a prompt.

Suggested fix: compare hallucination on calls whose `config_version` is the applied version, with the same minimum sample as detection, and only then resolve.

Proof: a unit test with high hallucination before the version and low after, and the opposite case that must stay open.

**13. Dashboard and docs over-claim completeness**

Why it matters: a reviewer reading `docs/IMPLEMENTATION_STATUS.md` ("phases 0–9 are complete") will not see the lapses, the dead decay path, or the `run.sh` mismatch until they open the grade files.

Evidence: that status file, `SUBMISSION.md` scope table, this review.

Suggested fix: one paragraph at the top of the status doc that repeats the known failures in §13 of the submission, plus the promotion-status bug. Do not mark C7, C8, C13, or C15 as Built until the proofs above exist.

Proof: the status doc and `SUBMISSION.md` §2 use the same status words as §2 of this review.

---

## 9. Final assessment

The controller is a real HTTP client with a durable SQLite loop: history ingest, joins, separate KPIs, statistical winner checks, approval submit/apply, a prompt-validation path, and a read-only dashboard. public_01 finished with a small positive conclusion delta (+1.6%) and one harmful explore. That meets the shape of the mandatory floor, with reservations.

What is complete enough to defend:

- HTTP-only access from `controller/`.
- Idempotent chunk and event insert, including a large history bootstrap (5,537,379 events in the public_03 log).
- Separate pickup, engagement, and conclusion rates.
- Winner math that does not ask the LLM who won, with a minimum sample and a guardrail check on that path.
- Governance that, on the recorded runs, did not auto-apply outside the grade's view (`auto_applied: 0`) and did not exceed the recorded per-workflow request peaks.
- An audit table that can answer a review question if someone runs the SQL.
- An honest write-up of the harmful applies, the lapsed prompts, the missed DID rotation, and the unfinished load run.

What is partial:

- Cohorts, caller-ID handling, decay/rollback, prompt apply and verification, fair scheduling, loop lag, and crash recovery.
- public_02 as an outcome: conclusion −1.7%, incident MTTR null, DID share unchanged, six lapsed approvals.
- The supervisor view, which is not on the `run.sh` path and has no public-run screenshot or snapshot in the evidence set.

What is a known failure:

- Decay rollback did not run on the public scenarios, and the implementation overwrites the promotion flag that decay depends on.
- Caller-ID rotation is not implemented.
- public_03 did not finish a simulated day, missed the 10-minute target by the report's own extrapolation (~107 minutes), and has no event-to-decision lag.
- The crash demonstration is one bug, a manual recovery command, and a resume. It is not "kill at any point".
- The graded commands are not `./run.sh`.

Readiness: the repository is submittable as an honest partial system. It is not ready to be described as a finished learning loop. The highest evaluation risk on hidden scenarios is harmful exploration, missed incident fixes (prompt lapse and DID), and no decay rollback. Those three are exactly the outcome penalties named in `README.md` §9.

---

## Five fixes to make next

1. Preserve promotion status across `run_all_experiments` and make decay rollback actually change the allocation. Prove it with a unit test and a grade where the decaying W3 share falls.
2. Apply approvals inside `apply_window_hours`, and re-propose on lapse or stale base with a new id. Re-run public_02 with `./run.sh` and show a non-null MTTR or a precise audit reason.
3. Rotate a flagged caller id when policy allows it. Prove it with a proposal test and a public_02 grade whose flagged DID share drops after D2.
4. Stop submitting exploration that can be graded harmful: guardrail-check any arm that already has samples, and keep the first shift inside `max_shift`.
5. Re-grade public_01 and public_02 with unmodified `./run.sh`, and replace the public_03 claim with either one full simulated day plus a lag histogram or a clearly labeled partial measurement and a rolling-aggregate plan.
