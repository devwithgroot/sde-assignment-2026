"""Observe-only entrypoint.

Reads health, clock, workflows, current configs, and chunk metadata, then exits.
It does not advance time and does not send approvals or config changes.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass

from controller.api.client import SimulatorClient, SimulatorError
from controller.api.models import ChunkMeta, SimState, Workflow, WorkflowConfig
from controller.persistence.repositories import IngestIntegrityError, Store
from controller.services.ingestion import ingest_available
from controller.settings import Settings

log = logging.getLogger("controller")


@dataclass(frozen=True)
class Observation:
    state: SimState
    workflows: list[Workflow]
    configs: list[WorkflowConfig]
    history_chunks: list[ChunkMeta]
    event_chunks: list[ChunkMeta]


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(levelname)s %(name)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _line(event: str, **fields: object) -> str:
    return json.dumps({"event": event, **fields}, default=str, sort_keys=True)


def observe(client: SimulatorClient) -> Observation:
    health = client.health()
    log.info(_line("health", ok=health.ok))
    if not health.ok:
        raise SimulatorError(status=200, code="unhealthy", detail=health.model_dump(), endpoint="/health")

    state = client.state()
    log.info(
        _line(
            "state",
            scenario=state.scenario,
            sim_minute=state.sim_minute,
            ts=state.ts,
            finished=state.finished,
            tick_minutes=state.tick_minutes,
            credits_remaining=state.credits_remaining,
            chunks=state.chunks,
            last_event_id=state.last_event_id,
        )
    )

    workflows = client.workflows()
    configs: list[WorkflowConfig] = []
    for workflow in workflows:
        log.info(
            _line(
                "workflow",
                workflow_id=workflow.workflow_id,
                tenant_id=workflow.tenant_id,
                primary=workflow.objective.primary,
                guardrail=workflow.objective.guardrail,
                mid_flight=workflow.mid_flight,
                current_version=workflow.current_version,
                auto_apply=workflow.governance.auto_apply.enabled,
                allowed_dims=workflow.governance.auto_apply.allowed_dims,
            )
        )
        config = client.workflow_config(workflow.workflow_id)
        configs.append(config)
        log.info(
            _line(
                "config",
                workflow_id=config.workflow_id,
                version=config.version,
                actor=config.actor,
                prompt_version=config.config.get("prompt_version"),
                cohorts=len(config.config.get("cohorts") or []),
            )
        )

    history = client.chunks("history")
    events = client.chunks("events")
    log.info(_line("chunks", kind="history", count=len(history), events=sum(c.events for c in history)))
    log.info(_line("chunks", kind="events", count=len(events), events=sum(c.events for c in events)))
    return Observation(state=state, workflows=workflows, configs=configs, history_chunks=history, event_chunks=events)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Observe the simulator without changing it.")
    parser.add_argument("--url", default=None, help="Simulator base URL. Falls back to SIM_URL.")
    parser.add_argument("--db", default=None, help="SQLite path. Falls back to CONTROLLER_DB or data/controller.sqlite.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(args.url, db_path=args.db)
    except ValueError as exc:
        print(f"usage: python -m controller.main --url <SIM_URL>\n{exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
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
                ingested = ingest_available(client, store)
            finally:
                store.close()
    except (SimulatorError, IngestIntegrityError) as exc:
        if isinstance(exc, SimulatorError):
            log.error(_line("simulator_error", endpoint=exc.endpoint, status=exc.status, code=exc.code, detail=exc.detail))
        else:
            log.error(_line("ingest_error", chunk=exc.chunk, detail=exc.detail))
        return 1
    print(
        f"observe scenario={report.state.scenario} minute={report.state.sim_minute} "
        f"finished={str(report.state.finished).lower()} workflows={len(report.workflows)} "
        f"history_chunks={len(report.history_chunks)} event_chunks={len(report.event_chunks)}"
    )
    for item in ingested:
        print(
            f"ingest kind={item.kind} fetched={item.chunks_fetched} committed={item.chunks_committed} "
            f"inserted={item.inserted} duplicates={item.duplicates} issues={item.issues}"
        )
    for workflow, config in zip(report.workflows, report.configs, strict=True):
        print(
            f"  {workflow.workflow_id} version={config.version} primary={workflow.objective.primary} "
            f"mid_flight={workflow.mid_flight}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
