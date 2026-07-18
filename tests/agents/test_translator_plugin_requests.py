"""Pre-backtest plugin-request sidecar reconciliation (Ebene 2).

The Translator declares a needed-but-missing capability in `plugin_requests`;
run_fresh strips it from strategy.json and writes/clears an
add_indicator_request.json sidecar the orchestrator authors before backtest.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from fwbg_agents.agents.translator import _reconcile_plugin_request_sidecar


def _strategy():
    return SimpleNamespace(id=7, slug="sig__forex__001")


def test_writes_sidecar_for_first_request(tmp_path):
    _reconcile_plugin_request_sidecar(
        tmp_path,
        _strategy(),
        [
            {
                "name": "turn_of_month_entry",
                "phase": "indicators",
                "category": "indicator",
                "capability": "turn-of-month entry signal",
                "reasoning": "hypothesis needs a calendar entry the catalog lacks",
            }
        ],
    )
    sidecar = tmp_path / "add_indicator_request.json"
    assert sidecar.is_file()
    data = json.loads(sidecar.read_text())
    assert data["kind"] == "add_indicator"
    assert data["phase"] == "indicators"
    assert data["capability"] == "turn-of-month entry signal"
    assert data["strategy_id"] == 7
    assert data["strategy_slug"] == "sig__forex__001"
    assert "requested_at" in data


def test_empty_requests_clears_stale_sidecar(tmp_path):
    sidecar = tmp_path / "add_indicator_request.json"
    sidecar.write_text("{}")
    _reconcile_plugin_request_sidecar(tmp_path, _strategy(), [])
    assert not sidecar.exists()


def test_empty_requests_is_noop_when_no_sidecar(tmp_path):
    _reconcile_plugin_request_sidecar(tmp_path, _strategy(), [])
    assert not (tmp_path / "add_indicator_request.json").exists()


def test_unrecognised_phase_is_coerced(tmp_path):
    """AddIndicator coerces an unknown phase to a valid default rather than
    failing — the sidecar is still written with a valid phase."""
    _reconcile_plugin_request_sidecar(
        tmp_path,
        _strategy(),
        [{"phase": "not_a_real_phase", "category": "indicator", "capability": "x"}],
    )
    data = json.loads((tmp_path / "add_indicator_request.json").read_text())
    assert data["phase"] == "indicators"
