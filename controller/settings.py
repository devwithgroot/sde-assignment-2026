"""Runtime settings. The simulator URL comes from the CLI or SIM_URL."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    sim_url: str
    timeout_seconds: float = 30.0
    chunk_timeout_seconds: float = 180.0
    max_retries: int = 2
    log_level: str = "INFO"
    db_path: str = "data/controller.sqlite"
    governance_enabled: bool = True
    advance_minutes: int = 60
    run_until_finished: bool = True

    @classmethod
    def from_env(
        cls,
        sim_url: str | None = None,
        db_path: str | None = None,
        *,
        governance_enabled: bool | None = None,
        advance_minutes: int | None = None,
        run_until_finished: bool | None = None,
    ) -> Settings:
        url = (sim_url or os.environ.get("SIM_URL") or "").strip().rstrip("/")
        if not url:
            raise ValueError("SIM_URL is required: pass --url or set SIM_URL")
        timeout = float(os.environ.get("CONTROLLER_TIMEOUT_SECONDS", "30"))
        chunk_timeout = float(os.environ.get("CONTROLLER_CHUNK_TIMEOUT_SECONDS", "180"))
        retries = int(os.environ.get("CONTROLLER_MAX_RETRIES", "2"))
        level = os.environ.get("CONTROLLER_LOG_LEVEL", "INFO").upper()
        database = db_path or os.environ.get("CONTROLLER_DB") or "data/controller.sqlite"
        if governance_enabled is None:
            raw = os.environ.get("CONTROLLER_GOVERNANCE", "1").strip().lower()
            governance = raw not in {"0", "false", "no", "off"}
        else:
            governance = governance_enabled
        if advance_minutes is None:
            advance_minutes = int(os.environ.get("CONTROLLER_ADVANCE_MINUTES", "60"))
        if run_until_finished is None:
            raw_loop = os.environ.get("CONTROLLER_UNTIL_FINISHED", "1").strip().lower()
            until_finished = raw_loop not in {"0", "false", "no", "off"}
        else:
            until_finished = run_until_finished
        if timeout <= 0 or chunk_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if retries < 0:
            raise ValueError("CONTROLLER_MAX_RETRIES must be >= 0")
        if advance_minutes <= 0 or advance_minutes % 15 != 0:
            raise ValueError("CONTROLLER_ADVANCE_MINUTES must be a positive multiple of 15")
        return cls(
            sim_url=url,
            timeout_seconds=timeout,
            chunk_timeout_seconds=chunk_timeout,
            max_retries=retries,
            log_level=level,
            db_path=database,
            governance_enabled=governance,
            advance_minutes=advance_minutes,
            run_until_finished=until_finished,
        )
