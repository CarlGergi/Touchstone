"""Milestone 5: the strategy ladder, P&L attribution, and the latency sweep.

Three strategies, all under the queue-and-latency model:

1. Symmetric: join the best bid and ask.
2. Avellaneda-Stoikov with an inventory penalty.
3. Avellaneda-Stoikov with its reservation price skewed by the best 1 s signal from milestone 4.

Walk-forward by day, as in milestone 4: anything fitted for a test day, which is the
Avellaneda-Stoikov parameters, the volatility, the signal variant and its coefficient, uses only the
earlier days. Every parameter combination tried is reported.
"""

from __future__ import annotations

import gc
import itertools
from pathlib import Path
from typing import Any

import numpy as np

from touchstone.experiments.days import day_frames, research_days
from touchstone.experiments.stats import mean_ci
from touchstone.signals import features, forward_change, rank_ic, sample_times, value_at
from touchstone.simulation import fixed, pnl, prepare, profile, simulate, top_of_book

PRODUCTS = ("BTC-USD", "ETH-USD")
# The grid is set in market units rather than as raw risk aversion: how many ticks the quotes shift
# for each lot of inventory. Risk aversion then follows from the day's volatility, as
# gamma = shift / (sigma^2 * horizon). Raw values do not transfer between BTC, whose cent tick
# moves about 50 ticks a second, and ETH.
SHIFTS_TICKS_PER_LOT = (0.5, 2.0, 8.0)
KAPPAS = (0.5, 2.0)
HORIZON_S = 60.0
SIGNAL_HORIZON_S = 1.0
LATENCIES_MS = (0, 1, 2, 5, 10, 20, 50, 100)
TICK_E8 = 1_000_000
MIN_TRAIN_DAYS = 2


def _gamma(shift_ticks_per_lot: float, sigma_ticks: float) -> float:
    return shift_ticks_per_lot / max(sigma_ticks**2 * HORIZON_S, 1e-9)


def _day_statistics(market) -> dict[str, Any]:
    """What later days may learn from this one: volatility, and each signal's IC and slope."""
    book = top_of_book(market)
    times = sample_times(book, 1.0, SIGNAL_HORIZON_S)
    one_second = forward_change(book, times, 1.0, TICK_E8)
    signals = features(book)
    fits = {}
    for name, series in signals.items():
        x = value_at(book, series, times)
        keep = np.isfinite(x) & np.isfinite(one_second)
        slope = (
            float(np.polyfit(x[keep], one_second[keep], 1)[0])
            if keep.sum() > 10 and x[keep].std() > 0
            else 0.0
        )
        fits[name] = {"ic": rank_ic(x, one_second), "slope": slope}
    return {"sigma_ticks": float(np.nanstd(one_second)), "signals": fits}


def run(root: Path) -> dict[str, Any]:
    fast = profile(root, "fast_trader")
    home = profile(root, "home")
    rows: list[dict[str, Any]] = []
    grid_rows: list[dict[str, Any]] = []
    choices: list[dict[str, Any]] = []

    for product in PRODUCTS:
        history: list[dict[str, Any]] = []  # Statistics and grid results of earlier days.
        for day, frame in day_frames(root, product, research_days(root, product)):
            market = prepare(frame, product)
            del frame
            gc.collect()
            today = _day_statistics(market)
            sigma = (
                float(np.mean([h["stats"]["sigma_ticks"] for h in history]))
                if history
                else today["sigma_ticks"]
            )

            # Every parameter combination runs on every day, so later days can choose among them.
            grid = {}
            for shift, kappa in itertools.product(SHIFTS_TICKS_PER_LOT, KAPPAS):
                gamma = _gamma(shift, sigma)
                result = simulate(
                    market,
                    fast,
                    strategy=1,
                    gamma=gamma,
                    kappa=kappa,
                    sigma_ticks=sigma,
                    horizon_s=HORIZON_S,
                )
                grid[(shift, kappa)] = pnl(market, result)["total"]
                grid_rows.append(
                    {
                        "product": product,
                        "day": day,
                        "shift_ticks_per_lot": shift,
                        "gamma": gamma,
                        "kappa": kappa,
                        "total": grid[(shift, kappa)],
                    }
                )

            if len(history) >= MIN_TRAIN_DAYS:
                shift, kappa = max(grid, key=lambda key: np.mean([h["grid"][key] for h in history]))
                gamma = _gamma(shift, sigma)
                signal_name = max(
                    today["signals"],
                    key=lambda n: np.nanmean([h["stats"]["signals"][n]["ic"] for h in history]),
                )
                slope = float(
                    np.mean([h["stats"]["signals"][signal_name]["slope"] for h in history])
                )
                choices.append(
                    {
                        "product": product,
                        "day": day,
                        "gamma": gamma,
                        "kappa": kappa,
                        "signal": signal_name,
                        "slope": slope,
                    }
                )
                signal = features(top_of_book(market))[signal_name]
                ladder = {
                    "symmetric": {"strategy": 0},
                    "avellaneda_stoikov": {
                        "strategy": 1,
                        "gamma": gamma,
                        "kappa": kappa,
                        "sigma_ticks": sigma,
                        "horizon_s": HORIZON_S,
                    },
                    "avellaneda_stoikov_skewed": {
                        "strategy": 1,
                        "gamma": gamma,
                        "kappa": kappa,
                        "sigma_ticks": sigma,
                        "horizon_s": HORIZON_S,
                        "skew_ticks_per_unit": slope,
                    },
                }
                latencies = {"fast_trader": fast, "home": home} | {
                    f"{ms}ms": fixed(ms) for ms in LATENCIES_MS
                }
                for name, settings in ladder.items():
                    extra = {"signal": signal} if name.endswith("skewed") else {}
                    for label, latency in latencies.items():
                        result = simulate(market, latency, **settings, **extra)
                        rows.append(
                            {"product": product, "day": day, "strategy": name, "latency": label}
                            | pnl(market, result)
                        )
            history.append({"stats": today, "grid": grid})
            del market
            gc.collect()

    return summarize(rows, grid_rows, choices)


def _edge_disappears_ms(
    per_day: dict[str, dict[str, float]], rng: np.random.Generator, resamples: int = 2000
):
    """The smallest swept latency at which mean P&L is no longer positive, with a day bootstrap.

    Zero means there was no edge even at zero latency. None means the edge survived 100 ms.
    """
    days = sorted(next(iter(per_day.values())))

    def crossing(chosen: list[str]) -> float | None:
        for ms in LATENCIES_MS:
            if np.mean([per_day[f"{ms}ms"][d] for d in chosen]) <= 0:
                return float(ms)
        return None

    point = crossing(days)
    draws = [
        crossing([days[i] for i in rng.integers(0, len(days), len(days))]) for _ in range(resamples)
    ]
    finite = [d if d is not None else np.inf for d in draws]
    # Nearest rank rather than interpolation, which would subtract infinity from infinity.
    low, high = np.percentile(finite, [2.5, 97.5], method="nearest")
    return {
        "value": point,
        "ci95_low": None if np.isinf(low) else float(low),
        "ci95_high": None if np.isinf(high) else float(high),
        "n": len(days),
    }


def summarize(rows, grid_rows, choices) -> dict[str, Any]:
    rng = np.random.default_rng(0)
    strategies = sorted({r["strategy"] for r in rows})
    out: dict[str, Any] = {"ladder": {}, "latency_sweep": {}, "edge_disappears_ms": {}}
    for strategy in strategies:
        mine = [r for r in rows if r["strategy"] == strategy]
        realistic = [r for r in mine if r["latency"] == "fast_trader"]
        out["ladder"][strategy] = {
            key: mean_ci([r[key] for r in realistic])
            for key in (
                "total",
                "spread_capture",
                "adverse_selection",
                "inventory",
                "fees",
                "fills",
            )
        } | {"home_total": mean_ci([r["total"] for r in mine if r["latency"] == "home"])}
        per_day = {}
        for r in mine:
            per_day.setdefault(r["latency"], {})[(r["product"], r["day"])] = r["total"]
        out["latency_sweep"][strategy] = {
            f"{ms}ms": mean_ci(list(per_day[f"{ms}ms"].values())) for ms in LATENCIES_MS
        }
        out["edge_disappears_ms"][strategy] = _edge_disappears_ms(
            {k: v for k, v in per_day.items() if k.endswith("ms")}, rng
        )
    out |= {
        "parameters_chosen": choices,
        "every_parameter_combination": grid_rows,
        "per_product_day": rows,
        "notes": (
            "P&L in dollars per product-day, with 95% t intervals over test product-days, "
            "gross of fees. Attribution: spread capture against the mid when filled, adverse "
            "selection as the mid's move against the fill over the next 10 s, inventory as the "
            "rest, to the end-of-day mark. Profiles: fast_trader (us-east-2) and home use measured "
            "round trips; the sweep fixes both market data and order delay at the stated latency. "
            "The final evaluation on the held-out week has not been run."
        ),
    }
    return out
