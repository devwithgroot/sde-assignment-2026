"""CLI: python -m controller.recovery --url URL --db PATH [--no-governance]

Reconciles after an unexpected stop. Does not advance simulator time.
"""

from __future__ import annotations

import argparse
import json
import sys

from controller.api.client import SimulatorClient, SimulatorError
from controller.main import observe
from controller.persistence.repositories import IngestIntegrityError, Store
from controller.services.crash_recovery import run_crash_recovery
from controller.services.observability import build_supervisor_summary, summary_as_dict
from controller.settings import Settings


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Crash recovery reconciliation (no sim advance).")
    p.add_argument("--url", default=None)
    p.add_argument("--db", default=None)
    p.add_argument("--no-governance", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(
            args.url,
            db_path=args.db,
            governance_enabled=False if args.no_governance else None,
        )
    except ValueError as exc:
        print(f"usage: python -m controller.recovery --url <SIM_URL>\n{exc}", file=sys.stderr)
        return 2
    try:
        with SimulatorClient(
            settings.sim_url,
            timeout_seconds=settings.timeout_seconds,
            chunk_timeout_seconds=settings.chunk_timeout_seconds,
            max_retries=settings.max_retries,
        ) as client:
            report = observe(client)
            store = Store(settings.db_path)
            try:
                recovery = run_crash_recovery(
                    client,
                    store,
                    report.workflows,
                    report.configs,
                    mutate=settings.governance_enabled,
                )
                summary = build_supervisor_summary(store)
            finally:
                store.close()
    except (SimulatorError, IngestIntegrityError) as exc:
        print(f"recovery_error: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "event": "crash_recovery",
                "mutate": settings.governance_enabled,
                "ingest": recovery.ingested_kinds,
                "approvals_reconciled": recovery.approvals_reconciled,
                "applied": recovery.applied,
                "duplicate_proposals": recovery.duplicate_proposals,
                "notes": recovery.notes + recovery.prompt_resumed_notes,
                "supervisor_health": summary.health,
            },
            sort_keys=True,
        )
    )
    print(json.dumps(summary_as_dict(summary), sort_keys=True, default=str)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
