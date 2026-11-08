"""HTTP client for the simulator.

GET paths are always available. Governance writes (approvals / apply / auto-apply)
are explicit methods used only by Phase 6+. This client never advances simulated time.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from controller.api.models import ChunkMeta, Health, SimState, Workflow, WorkflowConfig


class SimulatorError(Exception):
    def __init__(self, *, status: int, code: str, detail: Any, endpoint: str, body: Any = None):
        self.status = status
        self.code = code
        self.detail = detail
        self.endpoint = endpoint
        self.body = body
        super().__init__(f"{endpoint} -> {status} {code}: {detail}")


class SimulatorClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        chunk_timeout_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
    ):
        self._max_retries = max_retries
        self._chunk_timeout = chunk_timeout_seconds if chunk_timeout_seconds is not None else timeout_seconds
        self._sleeper = sleeper or time.sleep
        self._rng = rng or random.Random()
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SimulatorClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def health(self) -> Health:
        return Health.model_validate(self._get_json("/health"))

    def state(self) -> SimState:
        return SimState.model_validate(self._get_json("/sim/state"))

    def workflows(self) -> list[Workflow]:
        payload = self._get_json("/workflows")
        if not isinstance(payload, list):
            raise SimulatorError(status=200, code="bad_response", detail="workflows was not a list", endpoint="/workflows", body=payload)
        return [Workflow.model_validate(item) for item in payload]

    def workflow_config(self, workflow_id: str) -> WorkflowConfig:
        path = f"/workflows/{workflow_id}/config"
        return WorkflowConfig.model_validate(self._get_json(path))

    def chunks(self, kind: str, *, after: int = -1) -> list[ChunkMeta]:
        if kind not in ("events", "history"):
            raise ValueError("kind must be 'events' or 'history'")
        payload = self._get_json("/events/chunks", params={"kind": kind, "after": after})
        if not isinstance(payload, list):
            raise SimulatorError(
                status=200, code="bad_response", detail="chunk list was not a list", endpoint="/events/chunks", body=payload
            )
        return [ChunkMeta.model_validate(item) for item in payload]

    def chunk_text(self, n: int) -> str:
        """NDJSON body of one chunk. Does not open the path field from the metadata."""
        path = f"/events/chunks/{n}"
        response = self._request("GET", path, timeout=self._chunk_timeout)
        return response.text

    def list_approvals(self, *, workflow_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if workflow_id is not None:
            params["workflow_id"] = workflow_id
        if status is not None:
            params["status"] = status
        payload = self._get_json("/approvals", params=params or None)
        if not isinstance(payload, list):
            raise SimulatorError(
                status=200, code="bad_response", detail="approvals was not a list", endpoint="/approvals", body=payload
            )
        return payload

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        path = f"/approvals/{approval_id}"
        payload = self._get_json(path)
        if not isinstance(payload, dict):
            raise SimulatorError(status=200, code="bad_response", detail="approval was not an object", endpoint=path, body=payload)
        return payload

    def request_approval(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = self._json_request("POST", "/approvals", body)
        if not isinstance(payload, dict):
            raise SimulatorError(
                status=200, code="bad_response", detail="approval create was not an object", endpoint="/approvals", body=payload
            )
        return payload

    def apply_approval(self, workflow_id: str, approval_id: str) -> dict[str, Any]:
        path = f"/workflows/{workflow_id}/apply"
        payload = self._json_request("POST", path, {"approval_id": approval_id})
        if not isinstance(payload, dict):
            raise SimulatorError(status=200, code="bad_response", detail="apply was not an object", endpoint=path, body=payload)
        return payload

    def auto_apply(self, workflow_id: str, body: dict[str, Any]) -> dict[str, Any]:
        path = f"/workflows/{workflow_id}/config"
        payload = self._json_request("PUT", path, body)
        if not isinstance(payload, dict):
            raise SimulatorError(
                status=200, code="bad_response", detail="auto-apply was not an object", endpoint=path, body=payload
            )
        return payload

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._request("GET", path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise SimulatorError(
                status=response.status_code, code="bad_json", detail=str(exc), endpoint=path, body=response.text
            ) from exc

    def _json_request(self, method: str, path: str, body: dict[str, Any]) -> Any:
        response = self._request(method, path, json_body=body)
        try:
            return response.json()
        except ValueError as exc:
            raise SimulatorError(
                status=response.status_code, code="bad_json", detail=str(exc), endpoint=path, body=response.text
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        last_transport: httpx.TransportError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.request(
                    method, path, params=params, timeout=timeout, json=json_body
                )
            except httpx.TransportError as exc:
                last_transport = exc
                if attempt >= self._max_retries:
                    raise SimulatorError(status=0, code="transport_error", detail=str(exc), endpoint=path) from exc
                self._backoff(attempt, None)
                continue
            if response.status_code >= 500 and attempt < self._max_retries:
                self._backoff(attempt, response)
                continue
            if response.status_code >= 400:
                self._raise_http(path, response)
            return response
        raise SimulatorError(
            status=0, code="transport_error", detail=str(last_transport), endpoint=path
        )

    def _raise_http(self, path: str, response: httpx.Response) -> None:
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text
        if isinstance(body, dict) and "error" in body:
            code = str(body.get("error"))
            detail = body.get("detail")
        else:
            code = f"http_{response.status_code}"
            detail = body
        raise SimulatorError(status=response.status_code, code=code, detail=detail, endpoint=path, body=body)

    def _backoff(self, attempt: int, response: httpx.Response | None) -> None:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    self._sleeper(min(float(retry_after), 30.0))
                    return
                except ValueError:
                    pass
        delay = min(2**attempt, 8) + self._rng.random() * 0.05
        self._sleeper(delay)
