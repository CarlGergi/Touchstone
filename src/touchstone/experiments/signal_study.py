"""Milestone 4: how well order flow imbalance and queue imbalance predict the mid.

Walk-forward by day: for each test day, the variant used at each horizon is the one with the best
mean information coefficient on the earlier days only, and it is scored on the test day. Splits are
whole days, and samples stop one horizon before the end of each day, so no target window crosses
into the next day: the purge is built into the sampling. Every variant's score is reported too, so
nothing is selected silently.
"""

from __future__ import annotations

import gc
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from touchstone import products
from touchstone.experiments.days import day_frames, research_days
from touchstone.experiments.stats import mean_ci
from touchstone.signals import HORIZONS_S, features, forward_change, rank_ic, sample_times, value_at
from touchstone.simulation import prepare, top_of_book

PRODUCTS = products.ALL
SAMPLE_STEP_S = 0.1


def day_ics(market) -> dict[tuple[float, str], float]:
    """The IC of every variant at every horizon on one prepared day."""
    book = top_of_book(market)
    values = features(book)
    times = sample_times(book, SAMPLE_STEP_S, max(HORIZONS_S))
    out = {}
    for horizon in HORIZONS_S:
        target = forward_change(book, times, horizon, products.TICK_E8[market.product])
        for name, series in values.items():
            out[(horizon, name)] = rank_ic(value_at(book, series, times), target)
    return out


def daily_ics(root: Path) -> list[dict[str, Any]]:
    """The IC of every variant at every horizon, for every product-day."""
    rows = []
    for product in PRODUCTS:
        for day, frame in day_frames(root, product, research_days(root, product)):
            market = prepare(frame, product)
            del frame
            gc.collect()
            for (horizon, name), ic in day_ics(market).items():
                rows.append(
                    {
                        "product": product,
                        "day": day,
                        "horizon_s": horizon,
                        "variant": name,
                        "ic": ic,
                    }
                )
    return rows


def walk_forward(rows: list[dict[str, Any]], min_train_days: int = 2) -> dict[str, Any]:
    by_key: dict[tuple, float] = {
        (r["product"], r["day"], r["horizon_s"], r["variant"]): r["ic"] for r in rows
    }
    variants = sorted({r["variant"] for r in rows})
    result: dict[str, Any] = {}
    for horizon in HORIZONS_S:
        out_of_sample, chosen = [], Counter()
        for product in PRODUCTS:
            days = sorted({r["day"] for r in rows if r["product"] == product})
            for k in range(min_train_days, len(days)):
                train, test = days[:k], days[k]
                scores = {
                    v: np.nanmean([by_key[(product, d, horizon, v)] for d in train])
                    for v in variants
                }
                best = max(scores, key=lambda v: scores[v])
                chosen[best] += 1
                out_of_sample.append(by_key[(product, test, horizon, best)])
        result[f"{horizon:g}s"] = {
            "out_of_sample_ic": mean_ci(out_of_sample),
            "variants_chosen": dict(chosen),
            "all_variants_mean_ic": {
                v: mean_ci(
                    [r["ic"] for r in rows if r["horizon_s"] == horizon and r["variant"] == v]
                )
                for v in variants
            },
        }
    return result


def run(root: Path) -> dict[str, Any]:
    rows = daily_ics(root)
    per_horizon = walk_forward(rows)
    best = {h: v["out_of_sample_ic"] for h, v in per_horizon.items()}
    return {
        "best_signal_ic_by_horizon": best,
        "by_horizon": per_horizon,
        "variants_tried": sorted({r["variant"] for r in rows}),
        "per_product_day": rows,
        "notes": (
            "Information coefficient is the Spearman correlation between the signal, as known at "
            "each 100 ms sample by local receive time, and the mid change in ticks over the "
            "horizon. Out of sample: for each test day, the variant chosen on earlier days only, "
            "with a 95% t interval over test product-days. Samples within a day overlap and are "
            "not independent, so the interval treats each test day, not each sample, as one "
            "observation."
        ),
    }
