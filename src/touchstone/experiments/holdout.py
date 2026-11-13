"""The held-out evaluation, run once: python -m touchstone.experiments.holdout --final.

It tests on 2026-11-12 00:00 to 2026-11-13 17:59:50 UTC, when the recorder was stopped, BTC-USD and
ETH-USD only, with everything fitted on development data before 2026-11-12. ZEC-USD and NEAR-USD,
added on 2026-11-11, have too little development data to fit on, so they are used for development
only.

Fitting reuses the development experiments' own code: the signal variant at each horizon is the one
with the best mean IC on the fitting days, and the Avellaneda-Stoikov parameters, volatility and
skew coefficient are chosen as walk-forward chooses them. Nothing is fitted on a test day.

The result is written once. Running it again is refused, so the held-out days cannot be looked at
repeatedly until something looks good. To rerun on purpose, delete the section from the
results file, and say so wherever the result is reported.
"""

from __future__ import annotations

import argparse
import gc
import json
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from touchstone import products
from touchstone.data import DEVELOPMENT_END, FINAL_END, FINAL_START, add_holdout_flags, load
from touchstone.experiments import fill_model, results, strategies
from touchstone.experiments.signal_study import day_ics
from touchstone.experiments.stats import mean_ci
from touchstone.recorder.storage import recorded_days
from touchstone.signals import HORIZONS_S
from touchstone.simulation import naive, pnl, prepare, profile, simulate

FIRST_SERVER_DAY = "2026-11-06"
MIN_FIT_DAYS = 2

WINDOWS = {
    "final": {
        "test": (FINAL_START, FINAL_END),
        "fit_before": DEVELOPMENT_END,
        "products": products.ORIGINAL,
    },
}


def _day_start(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def _days(root: Path, product: str, start, end) -> list[str]:
    """Recorded days that overlap [start, end), including a last day the window ends inside."""
    first = start.date().isoformat()
    return [d for d in recorded_days(root, product) if first <= d and _day_start(d) < end]


def _market(root: Path, product: str, day: str, flags: dict[str, bool]):
    # A window can end inside a day, as the held-out one ends when the recorder stopped.
    end = min(_day_start(day) + timedelta(days=1), FINAL_END)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        frame = load(root, product, start=day, end=end, **flags).collect()
    market = prepare(frame, product)
    del frame
    gc.collect()
    return market


def run(
    root: Path,
    *,
    final: bool = False,
    results_path: Path = results.RESULTS,
) -> dict[str, Any]:
    if final is not True:
        raise ValueError("the held-out window opens only with final=True")
    window = "final"
    section = f"holdout_{window}"
    existing = json.loads(results_path.read_text()) if results_path.exists() else {}
    if section in existing:
        raise RuntimeError(
            f"{section} has already been run, and a held-out window is evaluated once. To rerun "
            "on purpose, delete that section from the results file and report that you did."
        )
    spec = WINDOWS[window]
    flags = {"final": True}
    test_start, test_end = spec["test"]
    fast, home = profile(root, "fast_trader"), profile(root, "home")

    fill_rows, ladder_rows, choices, ic_rows, skipped = [], [], [], [], []
    for product in spec["products"]:
        fit_days = [
            d
            for d in recorded_days(root, product)
            if FIRST_SERVER_DAY <= d < spec["fit_before"].date().isoformat()
        ]
        test_days = _days(root, product, test_start, test_end)
        if len(fit_days) < MIN_FIT_DAYS or not test_days:
            skipped.append({"product": product, "fit_days": fit_days, "test_days": test_days})
            continue

        # Fit on the earlier days only.
        history, fit_ics = [], []
        for day in fit_days:
            market = _market(root, product, day, flags)
            stats = strategies.day_statistics(market)
            sigma = (
                float(np.mean([h["stats"]["sigma_ticks"] for h in history]))
                if history
                else stats["sigma_ticks"]
            )
            history.append({"stats": stats, "grid": strategies.grid_day(market, fast, sigma)})
            fit_ics.append(day_ics(market))
            del market
        choice = strategies.choose(history, history[-1]["stats"])
        signal_by_horizon = {
            h: max(
                {name for (hh, name) in fit_ics[0] if hh == h},
                key=lambda name, h=h: np.nanmean([ics[(h, name)] for ics in fit_ics]),
            )
            for h in HORIZONS_S
        }
        choices.append(
            {"product": product, "fit_days": fit_days, "test_days": test_days}
            | choice
            | {"signal_by_horizon": {f"{h:g}s": v for h, v in signal_by_horizon.items()}}
        )

        # Then test, with nothing refitted.
        for day in test_days:
            market = _market(root, product, day, flags)
            stats = strategies.day_statistics(market)
            for name, result in (
                ("naive", simulate(market, naive(), naive_fills=True)),
                ("fast_trader_cancel_0.5", simulate(market, fast, cancel_share=0.5)),
            ):
                fill_rows.append(
                    {"product": product, "day": day, "variant": name} | pnl(market, result)
                )
            spread = {"spread_ticks": stats["spread_ticks"], "spread_bps": stats["spread_bps"]}
            for row in strategies.ladder_day(market, choice, fast, home):
                ladder_rows.append({"product": product, "day": day} | spread | row)
            ics = day_ics(market)
            for h, name in signal_by_horizon.items():
                ic_rows.append(
                    {
                        "product": product,
                        "day": day,
                        "horizon_s": h,
                        "variant": name,
                        "ic": ics[(h, name)],
                    }
                )
            del market
            gc.collect()

    if not fill_rows:
        raise FileNotFoundError(f"no {window} window days with enough fitting data: {skipped}")
    headline = tuple(p for p in spec["products"] if any(r["product"] == p for r in fill_rows))
    out = {
        "window": {
            "test": [test_start.isoformat(), test_end.isoformat()],
            "fit_before": spec["fit_before"].isoformat(),
        },
        "products": list(headline),
        "skipped": skipped,
        "choices": choices,
        "fill_model": fill_model.summarize(fill_rows, headline=headline),
        "signals": {
            f"{h:g}s": mean_ci([r["ic"] for r in ic_rows if r["horizon_s"] == h])
            for h in HORIZONS_S
        },
        "strategies": strategies.summarize(ladder_rows, [], choices, headline=headline),
        "signal_rows": ic_rows,
    }
    results.update(section, out, results_path)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m touchstone.experiments.holdout")
    parser.add_argument("--data-dir", type=Path, default=Path("data-server"))
    add_holdout_flags(parser)
    args = parser.parse_args(argv)
    if not args.final:
        parser.error("the held-out evaluation needs --final, to open the held-out window")
    run(args.data_dir, final=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
