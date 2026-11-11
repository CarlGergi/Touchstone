"""Milestone 3: how much a touch-fill backtest overstates a market maker's P&L and fill rate.

The same strategy, joining the best bid and ask with a small order, runs four ways on each
product-day: touch fills with no latency (naive), and the queue-and-latency model with the
measured fast_trader and home round trips, plus two settings of the cancellation assumption.
"""

from __future__ import annotations

import gc
from pathlib import Path
from typing import Any

import numpy as np

from touchstone.experiments.days import day_frames, research_days
from touchstone.experiments.stats import paired_overstatement
from touchstone.simulation import naive, pnl, prepare, profile, simulate

PRODUCTS = ("BTC-USD", "ETH-USD")
CANCEL_SHARES = (0.0, 0.5, 1.0)


def runs(root: Path) -> list[dict[str, Any]]:
    """Every product-day under every model variant."""
    fast = profile(root, "fast_trader")
    home = profile(root, "home")
    out = []
    for product in PRODUCTS:
        for day, frame in day_frames(root, product, research_days(root, product)):
            market = prepare(frame, product)
            del frame
            gc.collect()
            variants = {"naive": simulate(market, naive(), naive_fills=True)}
            for share in CANCEL_SHARES:
                variants[f"fast_trader_cancel_{share:g}"] = simulate(
                    market, fast, cancel_share=share
                )
            variants["home_cancel_0.5"] = simulate(market, home, cancel_share=0.5)
            for name, result in variants.items():
                out.append({"product": product, "day": day, "variant": name} | pnl(market, result))
            del market
            gc.collect()
    return out


def summarize(
    rows: list[dict[str, Any]], realistic: str = "fast_trader_cancel_0.5"
) -> dict[str, Any]:
    def by_variant(name: str, key: str) -> np.ndarray:
        return np.array([r[key] for r in rows if r["variant"] == name])

    naive_pnl, real_pnl = by_variant("naive", "total"), by_variant(realistic, "total")
    naive_volume, real_volume = (
        by_variant("naive", "volume_usd"),
        by_variant(realistic, "volume_usd"),
    )
    naive_fills, real_fills = by_variant("naive", "fills"), by_variant(realistic, "fills")
    variants = sorted({r["variant"] for r in rows})
    return {
        "compared": {"naive": "touch fills, no latency", "realistic": realistic},
        "pnl_overstatement_pct": paired_overstatement(naive_pnl, real_pnl),
        # Filled volume, because the queue model splits orders into partial fills, which would
        # inflate a count of fills and hide most of the gap.
        "fill_rate_overstatement_pct": paired_overstatement(naive_volume, real_volume),
        "fill_count_overstatement_pct": paired_overstatement(naive_fills, real_fills),
        "by_variant": {
            name: {
                "total_pnl_usd": float(by_variant(name, "total").sum()),
                "fills": int(by_variant(name, "fills").sum()),
                "volume_usd": float(by_variant(name, "volume_usd").sum()),
            }
            for name in variants
        },
        "product_days": len(naive_pnl),
        "per_product_day": rows,
        "notes": (
            "Overstatement is (naive - realistic) / |realistic|, from totals over product-days, "
            "with a 95% interval from resampling product-days. Strategy: join the best bid and ask "
            "with 0.001 BTC or 0.01 ETH, at most 10 lots of inventory. Gross of fees."
        ),
    }


def run(root: Path) -> dict[str, Any]:
    return summarize(runs(root))
