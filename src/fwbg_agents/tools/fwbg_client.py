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
- POST /api/data/ensure        body: {symbol, timeframe?, ...}
                               returns: {status: ready|downloading, task_id?}
- GET  /api/data/ensure/{task_id}
                               returns: {status, ...}

Strategy bodies are NOT accepted inline by fwbg's run endpoints — strategies
must exist in fwbg's strategies_dir as `<strategy_name>.json`. They are
created there via POST /api/strategies (`create_strategy`), which refuses to
overwrite an existing file (409).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Transient transport errors on idempotent GETs are retried. Observed live:
# uvicorn's default keep-alive timeout (5s) races the Runner's 5s poll
# interval — the server closes the idle connection exactly as the client
# reuses it (httpx.RemoteProtocolError / ReadError), and a single such blip
# used to kill a backtest that was still running fine on the fwbg side.
_GET_RETRIES = 3
_GET_RETRY_BACKOFF_SECONDS = 1.0


def safe_fwbg_strategy_name(slug: str, iteration: int) -> str:
    """fwbg validates names against [\\w\\-]; keep ASCII + drop punctuation.

    Child slugs already end in `__itNNN` (see `generate_child_slug`); those
    are returned as-is instead of getting a second iteration suffix.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", slug)
    if re.search(r"__it\d{3,}$", cleaned):
        return cleaned
    return f"{cleaned}__it{iteration:03d}"


class FwbgClientError(RuntimeError):
    """Raised when fwbg returns a non-2xx response."""

    def __init__(self, status: int, body: str):
        """Initialize."""
        self.status = status
        self.body = body
        super().__init__(f"fwbg returned {status}: {body}")


class FwbgClient:
    """Async HTTP client for the fwbg REST API."""

    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None):
        """Initialize."""
        self.base_url = base_url
        self._http = http if http is not None else httpx.AsyncClient(base_url=base_url)
        self._owns_http = http is None

    async def aclose(self) -> None:
        """Close the underlying HTTP client if it was created internally."""
        if self._owns_http:
            await self._http.aclose()

    async def _get(self, path: str) -> dict[str, Any]:
        """Perform a GET request with retry on transient transport errors."""
        # GETs are idempotent — retry transient transport errors instead of
        # letting one dropped keep-alive connection abort a running backtest.
        for attempt in range(1, _GET_RETRIES + 1):
            try:
                r = await self._http.get(path)
            except httpx.TransportError as exc:
                if attempt == _GET_RETRIES:
                    raise
                log.warning(
                    "GET %s failed with %s (attempt %d/%d), retrying",
                    path,
                    type(exc).__name__,
                    attempt,
                    _GET_RETRIES,
                )
                await asyncio.sleep(_GET_RETRY_BACKOFF_SECONDS * attempt)
                continue
            if r.status_code // 100 != 2:
                raise FwbgClientError(r.status_code, r.text)
            return r.json()
        raise AssertionError("unreachable")

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Perform a POST request and return the JSON response."""
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
        """Start a fwbg backtest run for a strategy (POST /api/runs/start)."""
        body: dict[str, Any] = {"strategy_name": strategy_name}
        if asset_classes is not None:
            body["asset_classes"] = asset_classes
        if assets is not None:
            body["assets"] = assets
        if description is not None:
            body["description"] = description
        return await self._post("/api/runs/start", body)

    async def get_plugins(self) -> list[dict[str, Any]]:
        """Return all registered plugins (GET /api/plugins).

        Each entry carries name/fqn/phase/description/param_schema/defaults.
        Phases include indicators, preprocessing, feature_selection,
        data_loading, exit_strategies, risk_management and model.
        """
        data = await self._get("/api/plugins")
        return data if isinstance(data, list) else data.get("plugins", [])

    async def get_plugin_source(self, fqn: str) -> dict[str, Any]:
        """Return a plugin's Python source (GET /api/plugins/{fqn}/source).

        Response shape: {"fqn", "filename", "source"}. Raises FwbgClientError
        on any non-2xx response (incl. 404 for an unknown fqn or a plugin whose
        source file cannot be located).
        """
        return await self._get(f"/api/plugins/{fqn}/source")

    async def get_exit_modifiers(self) -> list[dict[str, Any]]:
        """Return available exit modifiers (GET /api/exit-modifiers)."""
        data = await self._get("/api/exit-modifiers")
        return data if isinstance(data, list) else data.get("exit_modifiers", [])

    async def get_entry_modifiers(self) -> list[dict[str, Any]]:
        """Return available entry modifiers (GET /api/entry-modifiers)."""
        data = await self._get("/api/entry-modifiers")
        return data if isinstance(data, list) else data.get("entry_modifiers", [])

    async def get_timeframes(self) -> list[str]:
        """Supported OHLCV timeframes (GET /api/data/timeframes)."""
        data = await self._get("/api/data/timeframes")
        return data.get("timeframes", [])

    async def get_dukascopy_instruments(self) -> list[dict[str, Any]]:
        """Downloadable instruments + per-granularity history starts
        (GET /api/dukascopy/instruments): [{symbol, description, group,
        historyStart: {minute, hourly, daily}}]."""
        data = await self._get("/api/dukascopy/instruments")
        return data if isinstance(data, list) else data.get("instruments", [])

    async def get_datasources(self) -> list[dict[str, Any]]:
        """Return the datasources configured in fwbg (GET /api/datasources).

        Each entry carries name/type/path/…; only actually-configured sources
        can feed a backtest, so strategies must reference one of these names.
        """
        data = await self._get("/api/datasources")
        return data if isinstance(data, list) else data.get("datasources", [])

    async def get_datasource_assets(self) -> dict[str, Any]:
        """Return data availability (GET /api/datasources/assets):
        {"assets": [{symbol, timeframes, source, ...}], "by_source": {...}}."""
        return await self._get("/api/datasources/assets")

    async def get_presets(self, section: str) -> list[dict[str, Any]]:
        """Return workspace presets for a section (GET /api/presets/{section}).

        Sections: pipelines, models, validations, filters, resources,
        exit_params, regime_filters, risk_params. Each entry: {id, meta, content}.
        """
        data = await self._get(f"/api/presets/{section}")
        return data if isinstance(data, list) else data.get("presets", [])

    async def create_strategy(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        """Create a NEW strategy file in fwbg (POST /api/strategies).

        fwbg answers 409 (FwbgClientError.status == 409) when a strategy with
        that name already exists — it never overwrites. Returns
        {"filename", "name", "status": "created"} on success.
        """
        return await self._post("/api/strategies", {"name": name, "data": data})

    async def list_runs(self) -> list[dict[str, Any]]:
        """List fwbg runs (GET /api/runs). Items carry run_id, status,
        strategy_name, is_active."""
        resp = await self._get("/api/runs")
        return resp.get("items", resp) if isinstance(resp, dict) else resp

    async def get_progress(self, run_id: str) -> dict[str, Any]:
        """Return live progress data for a run (GET /api/runs/{run_id}/progress)."""
        return await self._get(f"/api/runs/{run_id}/progress")

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """Return full run details (GET /api/runs/{run_id})."""
        return await self._get(f"/api/runs/{run_id}")

    async def ensure_data(
        self,
        symbol: str,
        *,
        timeframe: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Ask fwbg to guarantee OHLCV data for a symbol (POST /api/data/ensure).

        Returns {"status": "ready", ...} if cached, or {"status": "downloading",
        "task_id": ...} if a background download was started. Raises
        FwbgClientError for an unknown/undownloadable symbol (404) or when no
        CSV datasource is configured (503).
        """
        body: dict[str, Any] = {"symbol": symbol}
        if timeframe is not None:
            body["timeframe"] = timeframe
        if date_from is not None:
            body["date_from"] = date_from
        if date_to is not None:
            body["date_to"] = date_to
        return await self._post("/api/data/ensure", body)

    async def get_ensure_status(self, task_id: str) -> dict[str, Any]:
        """Poll the status of a data-ensure background task (GET /api/data/ensure/{task_id})."""
        return await self._get(f"/api/data/ensure/{task_id}")

    async def get_asset_classes(self) -> list[str]:
        """Return the list of known asset class strings from fwbg's registry."""
        data = await self._get("/api/assets/classes")
        return data.get("classes", [])

    async def get_assets(self) -> list[dict[str, Any]]:
        """Return all assets with symbol/asset_class/currencies from fwbg's registry."""
        data = await self._get("/api/assets")
        return data.get("assets", [])

    async def register_plugin(
        self,
        *,
        slug: str,
        python_code: str,
        kind: str,
        description: str = "",
        spec_md: str = "",
        tests_code: str = "",
        version: str = "1.0.0",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Register a verified plugin with fwbg (POST /api/plugins).

        Writes the plugin into fwbg's user-plugins directory and refreshes the
        registry, so it appears immediately in GET /api/plugins as
        ``agent-authored:<slug>``.

        Returns ``{"fqn": "agent-authored:<slug>", "category": ..., "slug": ...}``.
        Raises FwbgClientError on any non-2xx response (incl. 422 when fwbg's
        own validation rejects the code).
        """
        return await self._post(
            "/api/plugins",
            {
                "slug": slug,
                "python_code": python_code,
                "kind": kind,
                "description": description,
                "spec_md": spec_md,
                "tests_code": tests_code,
                "version": version,
                "overwrite": overwrite,
            },
        )
