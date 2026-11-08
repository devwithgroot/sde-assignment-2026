# Implementation status — Phase 6

Phase 6 is done. The controller opens population-signal incidents, builds governed config proposals from experiment winners, tracks approvals, reconciles after crashes, applies only with `approval_id`, supports policy-checked auto-apply, detects promoted-winner decay, and records an audit trail. The dashboard shows these stores read-only. Phase 7 (LLM suggest-fixes + validation prompt loop) has not started.

Checked on 8 Nov 2026 with `.venv` Python 3.11.9. Full suite: `78 passed, 1 skipped` (live ingest skipped when simulator is down).

## Modules and responsibilities

| Module | Role |
|---|---|
| `controller/services/incidents.py` | Open/refresh incidents from detection signals; investigate → mitigate → resolve → close; link experiments/proposals |
| `controller/services/change_builder.py` | Build winner/rollback proposals with diff, dry-run, impact score; auto-apply policy pre-check; never invent options |
| `controller/services/governance.py` | Persist-then-submit proposals; approval reconcile; apply; duplicate-proposal recovery; stale/reject/expire handling |
| `controller/services/decay.py` | Detect primary/guardrail decay on promoted experiments; mark for governed rollback |
| `controller/services/audit.py` | Idempotent `audit_decisions` writes |
| `controller/api/client.py` | Added `request_approval`, `get_approval`, `list_approvals`, `apply_approval`, `auto_apply` (still no `/sim/advance`) |

## Schema (v5)

New tables: `incidents`, `proposals`, `controller_approvals`, `audit_decisions`, `config_snapshots`.

## Incident response workflow

1. Phase 4 signals → `sync_incidents_from_signals` creates `detected` incidents (one per fingerprint).
2. Matching workflow experiments → `investigating` + linked experiment id.
3. Winner proposal submission → `mitigating` + linked proposal id.
4. Successful apply / auto-apply → `resolved` with resolution text; optional `closed`.
5. No LLM / validation path (Phase 7). Drift without a safe winner stays open with evidence only — nothing fabricated.

## Governance and approval lifecycle

1. Experiment `status=winner` → `build_winner_proposal` (allocations only; no prompt dims).
2. Persist proposal **before** network call (idempotent `proposal_id`).
3. If auto-apply policy permits (enabled, allowed dims, max_shift, local impact floor) → `PUT /workflows/{id}/config`.
4. Else → `POST /approvals` with full required fields.
5. On restart: `GET /approvals/{id}` + ingested `approval_decided` events reconcile local rows.
6. `duplicate_proposal_id` → list approvals by `proposal_id` instead of re-POSTing blindly.
7. `approved` → `POST /workflows/{id}/apply` with **only** `{"approval_id"}`.
8. Rejected / expired / lapsed / stale recorded on proposal + approval + audit; never applied.
9. Pre/post config snapshots retained as rollback candidates; decay proposes rollback through the same governance path.

CLI: `--no-governance` or `CONTROLLER_GOVERNANCE=0` skips mutate while still computing local incidents/experiments.

## Dashboard changes

Extended the existing monitor (not a new app):

- Incidents (severity/status/links/resolution)
- Proposals (rationale, impact, dims, dry-run note, status)
- Controller approvals (expiry, apply_by, reasons, applied version)
- Audit decisions
- Warnings when tables empty or operations pending
- Remains read-only: no submit/apply/resolve from the UI

See `docs/DASHBOARD.md`.

## Tests

```text
.\.venv\Scripts\python.exe -m pytest -q tests
78 passed, 1 skipped
```

Includes: incident open/idempotency/mitigate/close; proposal build; auto-apply gate; approval submit/reconcile/apply; duplicate proposal recovery; reject; stale apply; mutate-disabled cycle; dashboard Phase 6 data; client still has no advance helper.

## Verified against the simulator

Not re-run live in this session (simulator was not listening; integration test skipped). Contract coverage matches `docs/SIMULATOR.md` and prior Phase 0 live notes: required approval fields, apply body shape, duplicate/stale/lapse error codes. Unit tests exercise those shapes via mocked HTTP.

## Remains incomplete / out of scope

- Phase 7: hallucination incident → `/llm/suggest-fixes` → `/validation/runs` → prompt proposal
- Continuous controller loop with `/sim/advance` (still single-shot `main`)
- Approver-hour deferral scheduler / 24h batching policy beyond fatigue errors from the simulator
- Phase 8 fairness + crash-recovery shell script
- Live public scenario end-to-end apply verification under a running world
