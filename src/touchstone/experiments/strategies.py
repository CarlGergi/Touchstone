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

from touchstone import products
from touchstone.experiments.days import day_frames, research_days
from touchstone.experiments.stats import mean_ci
from touchstone.signals import features, forward_change, rank_ic, sample_times, value_at
from touchstone.simulation import fixed, pnl, prepare, profile, simulate, top_of_book

PRODUCTS = products.ALL
# The grid is set in market units rather than as raw risk aversion: how many ticks the quotes shift
# for each lot of inventory. Risk aversion then follows from the day's volatility, as
# gamma = shift / (sigma^2 * horizon). Raw values do not transfer between BTC, whose cent tick
# moves about 50 ticks a second, and ETH.
SHIFTS_TICKS_PER_LOT = (0.5, 2.0, 8.0)
KAPPAS = (0.5, 2.0)
HORIZON_S = 60.0
SIGNAL_HORIZON_S = 1.0
LATENCIES_MS = (0, 1, 2, 5, 10, 20, 50, 100)
MIN_TRAIN_DAYS = 2


def _gamma(shift_ticks_per_lot: float, sigma_ticks: float) -> float:
    return shift_ticks_per_lot / max(sigma_ticks**2 * HORIZON_S, 1e-9)


def day_statistics(market) -> dict[str, Any]:
    """What later days may learn from this one: volatility, and each signal's IC and slope."""
    book = top_of_book(market)
    tick = products.TICK_E8[market.product]
    times = sample_times(book, 1.0, SIGNAL_HORIZON_S)
    one_second = forward_change(book, times, 1.0, tick)
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
    spread = (market.ask - market.bid).astype(float)
    mid = (market.ask + market.bid).astype(float) / 2
    valid = (market.bid > 0) & (market.ask > market.bid)
    return {
        "sigma_ticks": float(np.nanstd(one_second)),
        "signals": fits,
        "spread_ticks": float(np.median(spread[valid] / tick)),
        "spread_bps": float(np.median(spread[valid] / mid[valid] * 1e4)),
    }


def grid_day(market, fast, sigma: float) -> dict[tuple[float, float], float]:
    """P&L of every Avellaneda-Stoikov parameter combination on one day."""
    grid = {}
    for shift, kappa in itertools.product(SHIFTS_TICKS_PER_LOT, KAPPAS):
        result = simulate(
            market,
            fast,
            strategy=1,
            gamma=_gamma(shift, sigma),
            kappa=kappa,
            sigma_ticks=sigma,
            horizon_s=HORIZON_S,
        )
        grid[(shift, kappa)] = pnl(market, result)["total"]
    return grid


def choose(history: list[dict[str, Any]], today: dict[str, Any]) -> dict[str, Any]:
    """Parameters for a test day, from earlier days only: the best grid point on average, the
    signal with the best mean IC, its mean slope, and the mean volatility."""
    sigma = float(np.mean([h["stats"]["sigma_ticks"] for h in history]))
    shift, kappa = max(history[0]["grid"], key=lambda k: np.mean([h["grid"][k] for h in history]))
    signal = max(
        today["signals"],
        key=lambda n: np.nanmean([h["stats"]["signals"][n]["ic"] for h in history]),
    )
    slope = float(np.mean([h["stats"]["signals"][signal]["slope"] for h in history]))
    return {
        "sigma_ticks": sigma,
        "shift_ticks_per_lot": shift,
        "gamma": _gamma(shift, sigma),
        "kappa": kappa,
        "signal": signal,
        "slope": slope,
    }


def ladder_day(market, choice: dict[str, Any], fast, home) -> list[dict[str, Any]]:
    """Every strategy under every latency on one day, with the given parameters."""
    a_s = {
        "strategy": 1,
        "gamma": choice["gamma"],
        "kappa": choice["kappa"],
        "sigma_ticks": choice["sigma_ticks"],
        "horizon_s": HORIZON_S,
    }
    ladder = {
        "symmetric": ({"strategy": 0}, None),
        "avellaneda_stoikov": (a_s, None),
        "avellaneda_stoikov_skewed": (
            a_s | {"skew_ticks_per_unit": choice["slope"]},
            features(top_of_book(market))[choice["signal"]],
        ),
    }
    latencies = {"fast_trader": fast, "home": home} | {f"{ms}ms": fixed(ms) for ms in LATENCIES_MS}
    rows = []
    for name, (settings, signal) in ladder.items():
        extra = {} if signal is None else {"signal": signal}
        for label, latency in latencies.items():
            result = simulate(market, latency, **settings, **extra)
            rows.append({"strategy": name, "latency": label} | pnl(market, result))
    return rows


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
            today = day_statistics(market)
            sigma = (
                float(np.mean([h["stats"]["sigma_ticks"] for h in history]))
                if history
                else today["sigma_ticks"]
            )
            # Every combination runs on every day, so later days can choose among them.
            grid = grid_day(market, fast, sigma)
            for (shift, kappa), total in grid.items():
                grid_rows.append(
                    {
                        "product": product,
                        "day": day,
                        "shift_ticks_per_lot": shift,
                        "kappa": kappa,
                        "total": total,
                    }
                )
            if len(history) >= MIN_TRAIN_DAYS:
                choice = choose(history, today)
                choices.append({"product": product, "day": day} | choice)
                spread = {"spread_ticks": today["spread_ticks"], "spread_bps": today["spread_bps"]}
                for r in ladder_day(market, choice, fast, home):
                    rows.append({"product": product, "day": day} | spread | r)
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


def _at_fee(row: dict[str, Any], bps: float) -> float:
    """P&L at a maker fee, from P&L before fees: fees are a fixed share of traded notional."""
    return row["total"] - bps / 1e4 * row["volume_usd"]


def summarize(rows, grid_rows, choices, headline=products.ORIGINAL) -> dict[str, Any]:
    """Headline numbers on the original products; every product, including the wider-spread
    ones, appears in the by-product breakdown."""
    rng = np.random.default_rng(0)
    strategies = sorted({r["strategy"] for r in rows})
    main = [r for r in rows if r["product"] in headline]
    out: dict[str, Any] = {
        "headline_products": list(headline),
        "ladder": {},
        "latency_sweep": {},
        "edge_disappears_ms": {},
        "by_product": {},
    }
    for strategy in strategies:
        mine = [r for r in main if r["strategy"] == strategy]
        realistic = [r for r in mine if r["latency"] == "fast_trader"]
        if not realistic:
            continue
        out["ladder"][strategy] = {
            key: mean_ci([r[key] for r in realistic])
            for key in ("total", "spread_capture", "adverse_selection", "inventory", "fills")
        } | {
            "home_total": mean_ci([r["total"] for r in mine if r["latency"] == "home"]),
            "total_by_fee": {
                name: mean_ci([_at_fee(r, bps) for r in realistic])
                for name, bps in products.FEE_SCENARIOS_BPS.items()
            },
        }
        per_day = {}
        for r in mine:
            per_day.setdefault(r["latency"], {})[(r["product"], r["day"])] = r["total"]
        out["latency_sweep"][strategy] = {
            f"{ms}ms": mean_ci(list(per_day[f"{ms}ms"].values())) for ms in LATENCIES_MS
        }
        out["edge_disappears_ms"][strategy] = _edge_disappears_ms(
            {k: v for k, v in per_day.items() if k.endswith("ms")}, rng
        )
    for product in sorted({r["product"] for r in rows}):
        mine = [r for r in rows if r["product"] == product and r["latency"] == "fast_trader"]
        out["by_product"][product] = {
            "spread_ticks_median": float(np.median([r["spread_ticks"] for r in mine])),
            "spread_bps_median": float(np.median([r["spread_bps"] for r in mine])),
            "test_days": len({r["day"] for r in mine}),
        } | {
            strategy: {
                key: mean_ci([r[key] for r in mine if r["strategy"] == strategy])
                for key in ("total", "spread_capture", "adverse_selection", "inventory")
            }
            for strategy in strategies
        }
    out |= {
        "fee_scenarios_bps": products.FEE_SCENARIOS_BPS,
        "parameters_chosen": choices,
        "every_parameter_combination": grid_rows,
        "per_product_day": rows,
        "notes": (
            "P&L in dollars per product-day, with 95% t intervals over test product-days. "
            "total is before fees; total_by_fee subtracts maker fees on traded notional for each "
            "scenario in fee_scenarios_bps. Attribution: spread capture against the mid when "
            "filled, adverse selection as the mid's move against the fill over the next 10 s, "
            "inventory as the rest, to the end-of-day mark. Profiles: fast_trader (us-east-2) and "
            "home use measured round trips; the sweep fixes both market data and order delay at "
            "the stated latency. Headline numbers use the headline products; by_product shows "
            "every product with its median spread, to show how the edge depends on spread width."
        ),
    }
    return out
