"""Deterministic synthetic OHLCV scenarios for PluginEvaluator (M5b).

Locked decisions from M5a:
- Hand-curated parameters, np-seeded. No data-derived thresholds (see
  [[feedback-no-data-derived-thresholds]]).
- Each scenario name pins a fixed seed so re-runs produce byte-identical
  parquets — failures stay diagnosable post-mortem.

Layout of every returned DataFrame:
    timestamp (datetime64[ns, UTC]) — 1-minute spacing
    open, high, low, close (float64)
    volume (float64, non-negative)
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd  # type: ignore[import-untyped]  # pandas ships no type stubs

_BARS_DEFAULT = 500
_START_TS = pd.Timestamp("2026-01-01T00:00:00Z")
_SPACING = pd.Timedelta(minutes=1)


def _build_ohlcv(close: np.ndarray, *, vol_base: float, rng: np.random.Generator) -> pd.DataFrame:
    """Wrap a close-series into a full OHLCV frame using small noise wings."""
    n = len(close)
    spread = np.abs(rng.normal(0.0, 0.15, size=n))  # half-spread, always positive
    # Unused, but kept to preserve rng's draw sequence for byte-identical
    # reruns (see module docstring) - removing it would shift every
    # subsequent draw for callers sharing this rng instance.
    _direction = rng.choice([-1.0, 1.0], size=n)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    # Open/close must lie inside [low, high] — keep them clipped.
    open_ = np.minimum(np.maximum(open_, low), high)
    close = np.minimum(np.maximum(close, low), high)
    volume = np.maximum(rng.normal(vol_base, vol_base * 0.2, size=n), 0.0)
    ts = pd.date_range(_START_TS, periods=n, freq=_SPACING)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def gen_trending_up() -> pd.DataFrame:
    """Linear drift up + low-vol noise. Seed pinned to 20260624."""
    rng = np.random.default_rng(20260624)
    n = _BARS_DEFAULT
    drift = np.linspace(100.0, 120.0, n)
    noise = rng.normal(0.0, 0.20, size=n)
    close = drift + noise
    return _build_ohlcv(close, vol_base=1000.0, rng=rng)


def gen_trending_down() -> pd.DataFrame:
    """Mirror of trending_up. Seed 20260625."""
    rng = np.random.default_rng(20260625)
    n = _BARS_DEFAULT
    drift = np.linspace(120.0, 100.0, n)
    noise = rng.normal(0.0, 0.20, size=n)
    close = drift + noise
    return _build_ohlcv(close, vol_base=1000.0, rng=rng)


def gen_sideways() -> pd.DataFrame:
    """Mean-reverting OU-style around 100. Seed 20260626."""
    rng = np.random.default_rng(20260626)
    n = _BARS_DEFAULT
    theta = 0.05
    mean = 100.0
    sigma = 0.25
    close = np.empty(n)
    close[0] = mean
    for i in range(1, n):
        close[i] = close[i - 1] + theta * (mean - close[i - 1]) + sigma * rng.normal()
    return _build_ohlcv(close, vol_base=900.0, rng=rng)


def gen_high_vola() -> pd.DataFrame:
    """Drift-free baseline plus a strong random-walk component — close.std()
    must be at least ~3x the sideways generator's std. Seed 20260627."""
    rng = np.random.default_rng(20260627)
    n = _BARS_DEFAULT
    base = np.full(n, 100.0)
    steps = rng.normal(0.0, 1.0, size=n)
    close = base + np.cumsum(steps)  # full-amplitude random walk
    return _build_ohlcv(close, vol_base=1500.0, rng=rng)


def gen_sparse_data() -> pd.DataFrame:
    """80 bars with gaps every ~10 positions. Seed 20260628."""
    rng = np.random.default_rng(20260628)
    n = 80
    drift = np.linspace(100.0, 105.0, n)
    noise = rng.normal(0.0, 0.30, size=n)
    close = drift + noise
    df = _build_ohlcv(close, vol_base=600.0, rng=rng)
    # Stretch every 10th timestamp by an extra 5 minutes to create gaps.
    extra = pd.to_timedelta(
        ((df.index % 10 == 0).astype(int).cumsum()) * 5, unit="m"
    )
    df["timestamp"] = df["timestamp"] + extra
    return df


SCENARIO_GENERATORS: dict[str, Callable[[], pd.DataFrame]] = {
    "trending_up": gen_trending_up,
    "trending_down": gen_trending_down,
    "sideways": gen_sideways,
    "high_vola": gen_high_vola,
    "sparse_data": gen_sparse_data,
}


def generate_scenario(name: str) -> pd.DataFrame:
    """Dispatch to a named generator. Raises KeyError for unknown names."""
    try:
        return SCENARIO_GENERATORS[name]()
    except KeyError:
        raise KeyError(
            f"unknown scenario {name!r}; valid: {sorted(SCENARIO_GENERATORS)}"
        ) from None
