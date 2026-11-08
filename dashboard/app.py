"""FastAPI app for the read-only monitor.

GET only against the optional simulator. No approvals, no config writes, no advance.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from controller.api.client import SimulatorClient, SimulatorError
from dashboard.queries import load_snapshot
from dashboard.render import render_html


def create_app(db_path: str, sim_url: str | None = None) -> FastAPI:
    app = FastAPI(title="Controller monitor", version="0.1.0")
    app.state.db_path = str(Path(db_path))
    app.state.sim_url = (sim_url or "").rstrip("/") or None

    def _simulator_state() -> dict | None:
        if not app.state.sim_url:
            return None
        try:
            with SimulatorClient(app.state.sim_url, timeout_seconds=10, max_retries=0) as client:
                health = client.health()
                state = client.state()
                return {
                    "ok": health.ok,
                    "scenario": state.scenario,
                    "sim_minute": state.sim_minute,
                    "ts": state.ts,
                    "finished": state.finished,
                    "credits_remaining": state.credits_remaining,
                    "chunks": state.chunks,
                    "last_event_id": state.last_event_id,
                }
        except SimulatorError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.get("/health")
    def health():
        return {"ok": True, "readonly": True, "db_path": app.state.db_path}

    @app.get("/api/snapshot")
    def api_snapshot():
        snap = load_snapshot(app.state.db_path, simulator=_simulator_state())
        return JSONResponse(
            {
                "ok": snap.ok,
                "db_path": snap.db_path,
                "schema_version": snap.schema_version,
                "errors": snap.errors,
                "warnings": snap.warnings,
                "counts": snap.counts,
                "checkpoints": snap.checkpoints,
                "projection_cursor": snap.projection_cursor,
                "workflow_kpis": snap.workflow_kpis,
                "caller_kpis": snap.caller_kpis,
                "signals": snap.signals,
                "config_changes": snap.config_changes,
                "approval_decisions": snap.approval_decisions,
                "ingest_issues": snap.ingest_issues,
                "cohort_summary": snap.cohort_summary,
                "experiments_available": snap.experiments_available,
                "experiments": snap.experiments,
                "experiment_arms": snap.experiment_arms,
                "incidents_available": snap.incidents_available,
                "incidents": snap.incidents,
                "proposals_available": snap.proposals_available,
                "proposals": snap.proposals,
                "approvals_available": snap.approvals_available,
                "controller_approvals": snap.controller_approvals,
                "audit_available": snap.audit_available,
                "audit_decisions": snap.audit_decisions,
                "unimplemented": snap.unimplemented,
                "simulator": snap.simulator,
            }
        )

    @app.get("/", response_class=HTMLResponse)
    def index(db: str | None = Query(default=None)):
        path = db or app.state.db_path
        snap = load_snapshot(path, simulator=_simulator_state())
        return HTMLResponse(render_html(snap))

    return app
