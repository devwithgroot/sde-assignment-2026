"""Controller entrypoint.

Default mode advances simulated time until the scenario finishes, ingesting events
and applying governed config changes each cycle. Use --once for a single pass
without advancing (tests / observe). Use --no-governance to skip mutations.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass

from controller.api.client import SimulatorClient, SimulatorError
from controller.api.models import ChunkMeta, SimState, Workflow, WorkflowConfig
from controller.persistence.repositories import IngestIntegrityError, Store
from controller.services.credits import CreditBudget
from controller.services.decay import detect_promoted_decay
from controller.services.detector import detect_all
from controller.services.experiments import run_all_experiments
from controller.services.fairness import ACTION_GOVERNANCE, ACTION_PROMPT_INCIDENT, FairScheduler
from controller.services.governance import GovernanceReport, next_advance_minutes, run_governance_cycle
from controller.services.incidents import link_incident_experiment, sync_incidents_from_signals
from controller.services.ingestion import ingest_available
from controller.services.joiner import WorkflowConfigRef, project_events
from controller.services.metrics import compute_and_store_workflow_views
from controller.services.observability import build_supervisor_summary, bump_counter, summary_as_dict
from controller.services.prompt_incident import PromptIncidentReport, run_prompt_incident_cycle
from controller.settings import Settings

log = logging.getLogger("controller")


@dataclass(frozen=True)
class Observation:
    state: SimState
    workflows: list[Workflow]
    configs: list[WorkflowConfig]
    history_chunks: list[ChunkMeta]
    event_chunks: list[ChunkMeta]


@dataclass
class CycleResult:
    report: Observation
    ingested: list
    projected: object
    summary: dict
    kpi_slices: list
    signals: list
    experiments: list
    incidents: list
    decay_hits: list
    prompt_report: PromptIncidentReport
    governance_report: GovernanceReport
    supervisor: object | None


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


def run_cycle(
    client: SimulatorClient,
    store: Store,
    settings: Settings,
    *,
    verbose_observe: bool = True,
) -> CycleResult:
    report = observe(client) if verbose_observe else _refresh(client)
    ingested = ingest_available(client, store)
    projected = project_events(
        store,
        [
            WorkflowConfigRef(workflow_id=cfg.workflow_id, version=cfg.version, config=cfg.config)
            for cfg in report.configs
        ],
    )
    summary = {
        "campaigns": store.table_count("campaigns"),
        "leads": store.table_count("leads"),
        "calls": store.table_count("calls"),
        "unmatched_calls": store.unmatched_call_count(),
        "cohort_assignments": store.table_count("cohort_assignments"),
        "returning_people": store.returning_person_count(),
        "concluded_leads": store.concluded_lead_count(),
        "projection_cursor": store.projection_cursor(),
    }
    workflow_ids = [cfg.workflow_id for cfg in report.configs]
    kpi_slices = compute_and_store_workflow_views(store, workflow_ids)
    signals = detect_all(store, workflow_ids)
    experiments = run_all_experiments(store, report.workflows, report.configs)
    tenants = {w.workflow_id: w.tenant_id for w in report.workflows}
    incidents = sync_incidents_from_signals(store, signals, tenant_by_workflow=tenants)
    for exp in experiments:
        for incident in incidents:
            if incident.workflow_id == exp.workflow_id and incident.status in {
                "detected",
                "investigating",
                "mitigating",
            }:
                link_incident_experiment(store, incident.incident_id, exp.experiment_id)
    decay_hits = detect_promoted_decay(store)
    credit_budget = CreditBudget(store)
    credit_budget.refresh_from_sim(float(report.state.credits_remaining))
    scheduler = FairScheduler(store)
    scheduler.ensure_workflows(report.workflows)
    prompt_workflows = scheduler.order_workflows(report.workflows, action_kind=ACTION_PROMPT_INCIDENT)
    prompt_report = run_prompt_incident_cycle(
        client,
        store,
        prompt_workflows,
        report.configs,
        report.state,
        mutate=settings.governance_enabled,
        credit_budget=credit_budget,
        scheduler=scheduler,
    )
    gov_workflows = scheduler.order_workflows(report.workflows, action_kind=ACTION_GOVERNANCE)
    exp_by_wf: dict[str, list] = {}
    for e in experiments:
        exp_by_wf.setdefault(e.workflow_id, []).append(e)
    ordered_experiments = []
    for wf in gov_workflows:
        ordered_experiments.extend(exp_by_wf.get(wf.workflow_id, []))
    for e in experiments:
        if e not in ordered_experiments:
            ordered_experiments.append(e)
    governance_report = run_governance_cycle(
        client,
        store,
        gov_workflows,
        report.configs,
        ordered_experiments,
        decay_hits=decay_hits,
        mutate=settings.governance_enabled,
    )
    bump_counter(store, "controller.cycles")
    supervisor = build_supervisor_summary(store)
    return CycleResult(
        report=report,
        ingested=ingested,
        projected=projected,
        summary=summary,
        kpi_slices=kpi_slices,
        signals=signals,
        experiments=experiments,
        incidents=incidents,
        decay_hits=decay_hits,
        prompt_report=prompt_report,
        governance_report=governance_report,
        supervisor=supervisor,
    )


def _refresh(client: SimulatorClient) -> Observation:
    """Lighter refresh used on subsequent loop iterations (same GET surface as observe)."""
    return observe(client)


def print_cycle_summary(settings: Settings, cycle: CycleResult, *, cycle_n: int) -> None:
    report = cycle.report
    print(
        f"cycle={cycle_n} scenario={report.state.scenario} minute={report.state.sim_minute} "
        f"finished={str(report.state.finished).lower()} workflows={len(report.workflows)} "
        f"history_chunks={len(report.history_chunks)} event_chunks={len(report.event_chunks)}"
    )
    for item in cycle.ingested:
        print(
            f"ingest kind={item.kind} fetched={item.chunks_fetched} committed={item.chunks_committed} "
            f"inserted={item.inserted} duplicates={item.duplicates} issues={item.issues}"
        )
    projected = cycle.projected
    print(
        f"project processed={projected.processed} campaigns={projected.campaigns} leads={projected.leads} "
        f"calls={projected.calls} repaired={projected.repaired_calls} assignments={projected.cohort_assignments} "
        f"last_event_id={projected.last_event_id}"
    )
    summary = cycle.summary
    print(
        f"join campaigns={summary['campaigns']} leads={summary['leads']} calls={summary['calls']} "
        f"unmatched={summary['unmatched_calls']} assignments={summary['cohort_assignments']} "
        f"returning_people={summary['returning_people']} concluded_leads={summary['concluded_leads']} "
        f"cursor={summary['projection_cursor']}"
    )
    overall = [s for s in cycle.kpi_slices if s.window_label == "all" and s.cohort_id is None and s.caller_id is None]
    for slice_ in overall:
        pickup = "n/a" if slice_.pickup is None else f"{slice_.pickup:.4f}"
        engagement = "n/a" if slice_.engagement is None else f"{slice_.engagement:.4f}"
        conclusion = "n/a" if slice_.conclusion_rate is None else f"{slice_.conclusion_rate:.4f}"
        print(
            f"kpi {slice_.workflow_id} dials={slice_.dials} connected={slice_.connected} "
            f"pickup={pickup} engagement={engagement} conclusion={conclusion} "
            f"uploaded={slice_.uploaded_leads} concluded={slice_.concluded_leads}"
        )
    print(f"signals open={len(cycle.signals)}")
    for signal in cycle.signals:
        print(
            f"  signal {signal.workflow_id} {signal.signal_type} severity={signal.severity} "
            f"{signal.message}"
        )
    print(f"experiments={len(cycle.experiments)}")
    for exp in cycle.experiments:
        print(
            f"  experiment {exp.experiment_id} status={exp.status} decision={exp.decision} "
            f"winner={exp.winner_variant or '-'} promotion={exp.promotion_status or '-'} "
            f"reason={exp.decision_reason}"
        )
    print(f"incidents={len(cycle.incidents)}")
    for incident in cycle.incidents:
        print(
            f"  incident {incident.incident_id} {incident.workflow_id} "
            f"status={incident.status} severity={incident.severity} type={incident.signal_type}"
        )
    print(f"decay_hits={len(cycle.decay_hits)}")
    for hit in cycle.decay_hits:
        print(f"  decay {hit.get('workflow_id')} {hit.get('reason')}")
    pr = cycle.prompt_report
    print(
        f"prompt_incidents eligible={pr.eligible} attempted={pr.attempted} "
        f"suggested={pr.suggested} validated={pr.validated} "
        f"proposed={pr.proposed} submitted={pr.submitted} "
        f"skipped_no_mutate={pr.skipped_no_mutate} "
        f"skipped_dup={pr.skipped_duplicate} credit_blocked={pr.credit_blocked}"
    )
    gr = cycle.governance_report
    print(
        f"governance mutate={str(settings.governance_enabled).lower()} "
        f"proposals={gr.proposals_built} submitted={gr.proposals_submitted} "
        f"auto_applied={gr.auto_applied} applied={gr.applied} "
        f"reconciled={gr.approvals_reconciled} "
        f"rollbacks={gr.rollbacks_proposed}"
    )
    supervisor = cycle.supervisor
    if supervisor is not None:
        print(
            f"supervisor health={supervisor.health} incidents_open={supervisor.incidents_open} "
            f"proposals_pending={supervisor.proposals_pending} "
            f"approvals_pending={supervisor.approvals_pending} "
            f"starvation={len(supervisor.starvation_workflows)} "
            f"deferred={len(supervisor.deferred_open)}"
        )
        print(
            _line(
                "supervisor_summary",
                **{
                    k: v
                    for k, v in summary_as_dict(supervisor).items()
                    if k
                    in {
                        "health",
                        "incidents_open",
                        "proposals_pending",
                        "approvals_pending",
                        "actions_by_tenant",
                        "credit_budget",
                        "starvation_workflows",
                    }
                },
            )
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Controller loop: ingest, measure, experiment, then governed config changes."
    )
    parser.add_argument("--url", default=None, help="Simulator base URL. Falls back to SIM_URL.")
    parser.add_argument("--db", default=None, help="SQLite path. Falls back to CONTROLLER_DB or data/controller.sqlite.")
    parser.add_argument(
        "--no-governance",
        action="store_true",
        help="Skip proposal/approval/apply (observe + local incident/experiment analysis only).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Single pass without advancing simulated time (tests / observe).",
    )
    parser.add_argument(
        "--advance-minutes",
        type=int,
        default=None,
        help="Minutes per advance (positive multiple of 15). Default CONTROLLER_ADVANCE_MINUTES or 60.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(
            args.url,
            db_path=args.db,
            governance_enabled=False if args.no_governance else None,
            advance_minutes=args.advance_minutes,
            run_until_finished=False if args.once else None,
        )
    except ValueError as exc:
        print(f"usage: python -m controller.main --url <SIM_URL>\n{exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    wall_start = time.perf_counter()
    cycles = 0
    last: CycleResult | None = None
    try:
        with SimulatorClient(
            settings.sim_url,
            timeout_seconds=settings.timeout_seconds,
            chunk_timeout_seconds=settings.chunk_timeout_seconds,
            max_retries=settings.max_retries,
        ) as client:
            store = Store(settings.db_path)
            try:
                # Bootstrap: process whatever is already available (history + any live chunks).
                last = run_cycle(client, store, settings, verbose_observe=True)
                cycles = 1
                print_cycle_summary(settings, last, cycle_n=cycles)
                if not settings.run_until_finished or last.report.state.finished:
                    log.info(_line("controller_done", cycles=cycles, reason="once_or_already_finished"))
                else:
                    while True:
                        # While approvals are pending/approved, tick 15m so apply_by windows are not skipped.
                        minutes = next_advance_minutes(store, settings.advance_minutes)
                        adv = client.advance(minutes)
                        finished = bool(adv.get("finished"))
                        log.info(
                            _line(
                                "advance",
                                minutes=minutes,
                                configured_minutes=settings.advance_minutes,
                                sim_minute=adv.get("sim_minute"),
                                ts=adv.get("ts"),
                                finished=finished,
                                new_chunks=len(adv.get("new_chunks") or []),
                            )
                        )
                        last = run_cycle(client, store, settings, verbose_observe=True)
                        cycles += 1
                        # Keep stdout readable on long runs: full summary every cycle is noisy;
                        # print a compact line each cycle and a fuller dump every 12 cycles.
                        if cycles % 12 == 0 or finished or last.report.state.finished:
                            print_cycle_summary(settings, last, cycle_n=cycles)
                        else:
                            print(
                                f"cycle={cycles} minute={last.report.state.sim_minute} "
                                f"finished={str(last.report.state.finished).lower()} "
                                f"leads={last.summary['leads']} calls={last.summary['calls']} "
                                f"gov_submitted={last.governance_report.proposals_submitted} "
                                f"applied={last.governance_report.applied}"
                            )
                        if finished or last.report.state.finished:
                            break
                wall_s = time.perf_counter() - wall_start
                bump_counter(store, "controller.wall_seconds", int(wall_s))
                print(
                    f"done cycles={cycles} wall_seconds={wall_s:.1f} "
                    f"scenario={last.report.state.scenario} minute={last.report.state.sim_minute} "
                    f"finished={str(last.report.state.finished).lower()} "
                    f"db={settings.db_path}"
                )
            finally:
                store.close()
    except (SimulatorError, IngestIntegrityError) as exc:
        if isinstance(exc, SimulatorError):
            log.error(_line("simulator_error", endpoint=exc.endpoint, status=exc.status, code=exc.code, detail=exc.detail))
        else:
            log.error(_line("ingest_error", chunk=exc.chunk, detail=exc.detail))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
