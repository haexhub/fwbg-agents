"""Async HTTP client for the fwbg backtest API.

Pure transport layer — no lifecycle logic. The Runner agent owns the
"start → poll → fetch results" choreography; this wrapper only renders
HTTP calls and parses JSON.

fwbg's API (see ~/Projekte/fwbg/src/fwbg/api/runs.py):
- POST /api/runs/start         body: {strategy_name, asset_classes?, ...}
                               returns: {job_id, status, ...}
- GET  /api/runs/{run_id}/progress
                               returns: {status, progress?, phase?, ...}
- GET  /api/runs/{run_id}      returns: {run_id, status, assets, ...}

Strategy bodies are NOT accepted inline by fwbg — strategies must live on
disk in fwbg's strategies_dir as `<strategy_name>.json`. The Runner writes
that file before calling `start_run`.
"""

from __future__ import annotations

from typing import Any

import httpx


class FwbgClientError(RuntimeError):
    """Raised when fwbg returns a non-2xx response."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"fwbg returned {status}: {body}")


class FwbgClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None):
        self.base_url = base_url
        self._http = http if http is not None else httpx.AsyncClient(base_url=base_url)
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _get(self, path: str) -> dict[str, Any]:
        r = await self._http.get(path)
        if r.status_code // 100 != 2:
            raise FwbgClientError(r.status_code, r.text)
        return r.json()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = await self._http.post(path, json=body)
        if r.status_code // 100 != 2:
            raise FwbgClientError(r.status_code, r.text)
        return r.json()

    async def start_run(
        self,
        strategy_name: str,
        *,
        asset_classes: list[str] | None = None,
        assets: list[str] | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"strategy_name": strategy_name}
        if asset_classes is not None:
            body["asset_classes"] = asset_classes
        if assets is not None:
            body["assets"] = assets
        if description is not None:
            body["description"] = description
        return await self._post("/api/runs/start", body)

    async def get_progress(self, run_id: str) -> dict[str, Any]:
        return await self._get(f"/api/runs/{run_id}/progress")

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._get(f"/api/runs/{run_id}")

    async def get_asset_classes(self) -> list[str]:
        """Return the list of known asset class strings from fwbg's registry."""
        data = await self._get("/api/assets/classes")
        return data.get("classes", [])

    async def get_assets(self) -> list[dict[str, Any]]:
        """Return all assets with symbol/asset_class/currencies from fwbg's registry."""
        data = await self._get("/api/assets")
        return data.get("assets", [])
