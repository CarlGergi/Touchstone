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

from touchstone import products
from touchstone.experiments.days import day_frames, research_days
from touchstone.experiments.stats import paired_overstatement
from touchstone.simulation import naive, pnl, prepare, profile, simulate

PRODUCTS = products.ALL
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
    rows: list[dict[str, Any]],
    realistic: str = "fast_trader_cancel_0.5",
    headline: tuple[str, ...] = products.ORIGINAL,
) -> dict[str, Any]:
    """Overstatement on the headline products; every product appears in the breakdown."""
    main = [r for r in rows if r["product"] in headline]

    def by_variant(name: str, key: str, chosen=main) -> np.ndarray:
        return np.array([r[key] for r in chosen if r["variant"] == name])

    def at_fee(name: str, bps: float, chosen=main) -> float:
        return float(
            (
                by_variant(name, "total", chosen)
                - bps / 1e4 * by_variant(name, "volume_usd", chosen)
            ).sum()
        )

    naive_pnl, real_pnl = by_variant("naive", "total"), by_variant(realistic, "total")
    naive_volume, real_volume = (
        by_variant("naive", "volume_usd"),
        by_variant(realistic, "volume_usd"),
    )
    naive_fills, real_fills = by_variant("naive", "fills"), by_variant(realistic, "fills")
    variants = sorted({r["variant"] for r in rows})
    return {
        "headline_products": list(headline),
        "compared": {"naive": "touch fills, no latency", "realistic": realistic},
        "pnl_overstatement_pct": paired_overstatement(naive_pnl, real_pnl),
        # Filled volume, because the queue model splits orders into partial fills, which would
        # inflate a count of fills and hide most of the gap.
        "fill_rate_overstatement_pct": paired_overstatement(naive_volume, real_volume),
        "fill_count_overstatement_pct": paired_overstatement(naive_fills, real_fills),
        "by_variant": {
            name: {
                "total_pnl_usd_by_fee": {
                    fee: at_fee(name, bps) for fee, bps in products.FEE_SCENARIOS_BPS.items()
                },
                "fills": int(by_variant(name, "fills").sum()),
                "volume_usd": float(by_variant(name, "volume_usd").sum()),
            }
            for name in variants
        },
        "by_product": {
            product: {
                name: {
                    fee: at_fee(name, bps, [r for r in rows if r["product"] == product])
                    for fee, bps in products.FEE_SCENARIOS_BPS.items()
                }
                for name in ("naive", realistic)
            }
            for product in sorted({r["product"] for r in rows})
        },
        "fee_scenarios_bps": products.FEE_SCENARIOS_BPS,
        "product_days": len(naive_pnl),
        "per_product_day": rows,
        "notes": (
            "Overstatement is (naive - realistic) / |realistic|, from totals over headline "
            "product-days before fees, with a 95% interval from resampling product-days. "
            "Strategy: join the best bid and ask with the order sizes in touchstone.products, at "
            "most 10 lots of inventory. P&L is also given at each maker fee scenario."
        ),
    }


def run(root: Path) -> dict[str, Any]:
    return summarize(runs(root))
