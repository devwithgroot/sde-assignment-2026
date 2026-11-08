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

    @classmethod
    def from_env(cls, sim_url: str | None = None, db_path: str | None = None) -> Settings:
        url = (sim_url or os.environ.get("SIM_URL") or "").strip().rstrip("/")
        if not url:
            raise ValueError("SIM_URL is required: pass --url or set SIM_URL")
        timeout = float(os.environ.get("CONTROLLER_TIMEOUT_SECONDS", "30"))
        chunk_timeout = float(os.environ.get("CONTROLLER_CHUNK_TIMEOUT_SECONDS", "180"))
        retries = int(os.environ.get("CONTROLLER_MAX_RETRIES", "2"))
        level = os.environ.get("CONTROLLER_LOG_LEVEL", "INFO").upper()
        database = db_path or os.environ.get("CONTROLLER_DB") or "data/controller.sqlite"
        if timeout <= 0 or chunk_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if retries < 0:
            raise ValueError("CONTROLLER_MAX_RETRIES must be >= 0")
        return cls(
            sim_url=url,
            timeout_seconds=timeout,
            chunk_timeout_seconds=chunk_timeout,
            max_retries=retries,
            log_level=level,
            db_path=database,
        )
