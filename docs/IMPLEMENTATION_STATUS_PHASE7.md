# Implementation status — Phase 7

Phase 7 is done. Hallucination incidents can drive an untrusted LLM suggest-fixes call, per-candidate validation runs, deterministic selection by validation threshold, and a governed prompt proposal that reuses Phase 6 approval/apply rules. Phase 8 has not started.

Checked on 8 Nov 2026 with `.venv` Python 3.11.9.

## What was implemented

1. **Incident-driven eligibility** — open `hallucination_spike` incidents only; skip when a proposal is already in flight/applied or a blocking attempt exists; retry only after `credit_exhausted` / `transport_error` / `failed_retryable`.
2. **LLM suggest-fixes** — `POST /llm/suggest-fixes` via `SimulatorClient.suggest_fixes`; persist attempt before the call; ignore unsupported prompt versions; treat payload as untrusted.
3. **Validation** — `POST /validation/runs` per candidate; persist each result; invalid/failed/below-threshold never become approved changes.
4. **Selection** — highest `pass_rate` among candidates ≥ `min_validation_pass_rate`, then `prompt_version` ascending (no LLM ranking).
5. **Proposal + governance** — `build_prompt_proposal` attaches `evidence.validation_run_id`; routes through existing `submit_proposal` (approval path; auto-apply usually denied for prompt dims).
6. **Recovery** — pending attempt persisted pre-network; proposed-but-unsubmitted attempts resumed; duplicate suggestion prevention; idempotent proposal ids.
7. **Post-apply check** — if an applied prompt proposal’s workflow `halluc_rate` falls below the detector absolute floor, resolve the linked incident.
8. **`--no-governance`** — skips LLM, validation, and proposal submit entirely (no credit spend, no approvals/apply).

## Files

Created:

- `controller/services/prompt_incident.py`
- `tests/unit/test_prompt_incident.py`
- `docs/IMPLEMENTATION_STATUS_PHASE7.md`

Modified:

- `controller/persistence/db.py` — schema v6: `llm_suggestion_attempts`, `validation_run_records`
- `controller/persistence/repositories.py` — upsert/list helpers
- `controller/api/client.py` — `suggest_fixes`, `run_validation`
- `controller/services/change_builder.py` — `build_prompt_proposal`
- `controller/main.py` — run prompt cycle before governance
- `dashboard/queries.py`, `dashboard/render.py`, `dashboard/app.py`
- `docs/DASHBOARD.md`, `docs/IMPLEMENTATION_STATUS.md`
- `tests/unit/test_dashboard.py`

## Schema (v6)

| Table | Purpose |
|---|---|
| `llm_suggestion_attempts` | Per-incident suggestion attempts, status, selected prompt/validation, linked proposal |
| `validation_run_records` | Per-candidate validation outcomes, threshold flag, errors |

## Tests and results

```text
.\.venv\Scripts\python.exe -m pytest -q tests
86 passed
git diff --check
(clean)
```

Covered with **mocked** simulator HTTP:

- Happy path: suggest → validate → proposal with `validation_run_id` → approval submit
- Duplicate prevention on restart/re-run
- Below-threshold → `no_safe_candidate`, no proposal
- Credit exhaustion (402) → retryable
- Invalid validation payload
- Malformed suggestions list
- `--no-governance` / `mutate=False` makes zero paid calls
- Deterministic candidate selection on pass_rate ties

## Limitations / not verified live

- **Not verified against a live simulator** in this session (no credit spend on a running world). Contract matches `docs/SIMULATOR.md` and `sim/server.py` shapes; all Phase 7 path checks are mocked.
- Continuous `/sim/advance` loop still absent (single-shot `main`).
- Post-apply verification uses stored metric snapshots only; no dedicated long observation window scheduler.
- Approver fatigue / hour deferral still simulator-enforced rather than a local scheduler.
- Phase 8 fairness + crash-recovery script not started.
