"""Confidence intervals shared by the experiments."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import numpy as np

# Two-sided 95% Student t quantiles by degrees of freedom; beyond 30 the normal value is close.
_T95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    12: 2.179,
    15: 2.131,
    20: 2.086,
    25: 2.060,
    30: 2.042,
}


def _t95(dof: int) -> float:
    if dof <= 0:
        return math.nan
    usable = [d for d in _T95 if d <= dof]
    return _T95[max(usable)] if dof <= 30 else 1.96


def mean_ci(samples: Sequence[float]) -> dict[str, float | int]:
    """Mean with a 95% t interval, treating each sample as one independent observation."""
    values = np.asarray(samples, dtype=float)
    n = len(values)
    mean = float(values.mean()) if n else math.nan
    half = _t95(n - 1) * float(values.std(ddof=1)) / math.sqrt(n) if n > 1 else math.nan
    return {"value": mean, "ci95_low": mean - half, "ci95_high": mean + half, "n": n}


def bootstrap_ci(
    groups: Sequence[np.ndarray],
    statistic: Callable[[np.ndarray], float],
    *,
    resamples: int = 2000,
    seed: int = 0,
) -> dict[str, float | int]:
    """A statistic with a 95% interval from resampling whole groups, such as days.

    Resampling groups rather than single observations keeps the dependence inside a day, which
    single observations would ignore, and so gives intervals that are honestly wide.
    """
    rng = np.random.default_rng(seed)
    point = statistic(np.concatenate(groups))
    draws = []
    for _ in range(resamples):
        chosen = rng.integers(0, len(groups), len(groups))
        draws.append(statistic(np.concatenate([groups[i] for i in chosen])))
    low, high = np.percentile(draws, [2.5, 97.5])
    return {
        "value": float(point),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "n": len(groups),
    }


def paired_overstatement(
    naive: np.ndarray, realistic: np.ndarray, *, resamples: int = 2000, seed: int = 0
) -> dict[str, float | int]:
    """How much ``naive`` overstates ``realistic``, in percent: 100 * (N - R) / |R| of the totals.

    Each product-day is one paired observation, and the interval resamples product-days, keeping
    each pair together.
    """
    naive, realistic = np.asarray(naive, dtype=float), np.asarray(realistic, dtype=float)

    def statistic(index: np.ndarray) -> float:
        n, r = naive[index].sum(), realistic[index].sum()
        return 100.0 * (n - r) / abs(r) if r != 0 else math.nan

    rng = np.random.default_rng(seed)
    everything = np.arange(len(naive))
    draws = [statistic(rng.integers(0, len(naive), len(naive))) for _ in range(resamples)]
    low, high = np.nanpercentile(draws, [2.5, 97.5])
    return {
        "value": statistic(everything),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "n": len(naive),
    }
