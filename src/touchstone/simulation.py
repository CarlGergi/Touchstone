"""Our orders replayed against a recorded day, and the P&L that results.

The fill model lives in the C++ engine (engine/include/touchstone/simulator.hpp). This module
prepares a day for it, says when the strategy saw each book message and how long its orders take
to arrive, and turns the fills into P&L split four ways.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import polars as pl
import touchstone_engine as engine

from touchstone.latency import round_trips
from touchstone.replay import stream_arrays

E8 = 1e8
TRADES = ("trade", "trade_snapshot")
# Order sizes worth about $75 and $25, small next to the size usually resting at the best price.
ORDER_SIZE_E8 = {"BTC-USD": 100_000, "ETH-USD": 1_000_000}


@dataclass(frozen=True)
class Market:
    """One product-day, prepared for the simulator."""

    product: str
    arrays: dict[str, np.ndarray]
    message_exchange_ts: np.ndarray
    message_local_ts: np.ndarray
    mid: np.ndarray  # Mid price after each message, in dollars.
    bid: np.ndarray  # Best prices after each message, scaled by 1e8.
    ask: np.ndarray
    bid_size: np.ndarray
    ask_size: np.ndarray
    trades: dict[str, np.ndarray]


def prepare(frame: pl.DataFrame, product: str) -> Market:
    arrays = stream_arrays(frame)
    book = frame.filter(pl.col("event").is_in(("snapshot", "update")))
    starts = book.with_row_index().filter(
        # The first row has no row before it, which compares as null, and is a message start.
        (
            (pl.col("seq") != pl.col("seq").shift(1))
            | (pl.col("session") != pl.col("session").shift(1))
        ).fill_null(True)
    )
    top = pl.DataFrame(engine.replay_top(*arrays.values()))
    mid = ((top["bid"] + top["ask"]) / 2 / E8).to_numpy()
    trades = frame.filter(pl.col("event").is_in(TRADES)).sort("exchange_ts_ns", maintain_order=True)
    return Market(
        product=product,
        arrays=arrays,
        message_exchange_ts=starts["exchange_ts_ns"].to_numpy(),
        message_local_ts=starts["local_ts_ns"].to_numpy(),
        mid=mid,
        bid=top["bid"].to_numpy(),
        ask=top["ask"].to_numpy(),
        bid_size=top["bid_size"].to_numpy(),
        ask_size=top["ask_size"].to_numpy(),
        trades={
            "ts": trades["exchange_ts_ns"].to_numpy(),
            "side": trades["side"].to_physical().cast(pl.UInt8).to_numpy(),
            "price": trades["price_e8"].to_numpy(),
            "size": trades["size_e8"].to_numpy(),
        },
    )


@dataclass(frozen=True)
class Latency:
    """When the strategy sees each message, and how long its orders and cancels take to arrive.

    ``naive`` is the touch-fill backtest: no delay at all. A profile uses measured round trips,
    taking half of one as the one-way order delay. A fixed latency, for the sweep, delays both the
    market data and the orders by the same amount.
    """

    name: str
    seen_offset_ns: int = 0
    use_local_clock: bool = False
    delays_ns: np.ndarray = field(default_factory=lambda: np.zeros(1, dtype=np.int64))

    def seen(self, market: Market) -> np.ndarray:
        base = market.message_local_ts if self.use_local_clock else market.message_exchange_ts
        return base + self.seen_offset_ns


def naive() -> Latency:
    return Latency("naive")


def fixed(latency_ms: float) -> Latency:
    ns = int(latency_ms * 1e6)
    return Latency(
        f"{latency_ms:g} ms", seen_offset_ns=ns, delays_ns=np.array([ns], dtype=np.int64)
    )


def profile(root: Path, name: str, *, reference: str = "fast_trader") -> Latency:
    """A measured profile. Market data arrives when the server received it, plus, for a slower
    connection, half the difference in median round trip to the server's."""
    samples = np.asarray(round_trips(root, name))
    if not len(samples):
        raise ValueError(f"no round trips recorded for {name}")
    reference_median = float(np.median(round_trips(root, reference)))
    extra_ms = max(0.0, (float(np.median(samples)) - reference_median) / 2)
    return Latency(
        name,
        seen_offset_ns=int(extra_ms * 1e6),
        use_local_clock=True,
        delays_ns=(samples / 2 * 1e6).astype(np.int64),
    )


def simulate(
    market: Market,
    latency: Latency,
    *,
    naive_fills: bool = False,
    cancel_share: float = 0.5,
    strategy: int = 0,
    signal: np.ndarray | None = None,
    seed: int = 1,
    **parameters: float,
) -> dict[str, np.ndarray | int]:
    config = engine.SimConfig()
    config.naive = naive_fills
    config.cancel_share = cancel_share
    config.order_size = ORDER_SIZE_E8[market.product]
    config.strategy = strategy
    config.seed = seed
    for key, value in parameters.items():
        setattr(config, key, value)
    seen = latency.seen(market).astype(np.int64)
    signal = np.zeros(len(seen)) if signal is None else np.asarray(signal, dtype=np.float64)
    t = market.trades
    return engine.simulate(
        *market.arrays.values(),
        seen,
        signal,
        t["ts"],
        t["side"],
        t["price"],
        t["size"],
        latency.delays_ns,
        config,
    )


def pnl(
    market: Market, result: dict, *, fee_bps: float = 0.0, adverse_horizon_s: float = 10.0
) -> dict[str, float]:
    """P&L in dollars, split into spread capture, adverse selection, inventory and fees.

    * Spread capture: the edge against the mid when filled.
    * Adverse selection: how far the mid moved against the fill over the following
      ``adverse_horizon_s`` seconds.
    * Inventory: everything else from holding the position, to the end-of-day mark.
    * Fees: maker fees on traded notional.

    The four add up to the total exactly.
    """
    ts = np.asarray(result["fill_ts"])
    price = np.asarray(result["fill_price"]) / E8
    qty = np.asarray(result["fill_qty"]) / E8
    if not len(ts):
        return {
            k: 0.0 for k in ("total", "spread_capture", "adverse_selection", "inventory", "fees")
        } | {"fills": 0, "volume_usd": 0.0}

    def mid_at(times: np.ndarray) -> np.ndarray:
        index = np.searchsorted(market.message_exchange_ts, times, side="right") - 1
        return market.mid[np.clip(index, 0, len(market.mid) - 1)]

    mid_fill = mid_at(ts)
    mid_later = mid_at(ts + int(adverse_horizon_s * 1e9))
    notional = np.abs(qty * price)
    fees = float(fee_bps / 1e4 * notional.sum())
    spread = float(np.sum(qty * (mid_fill - price)))
    adverse = float(np.sum(qty * (mid_later - mid_fill)))
    total = float(-np.sum(qty * price) + qty.sum() * market.mid[-1]) - fees
    return {
        "total": total,
        "spread_capture": spread,
        "adverse_selection": adverse,
        "inventory": total + fees - spread - adverse,
        "fees": -fees,
        "fills": int(len(ts)),
        "volume_usd": float(notional.sum()),
    }


def top_of_book(market: Market):
    """The book's top, timed by when each message was received, for the signals."""
    from touchstone.signals import Book

    return Book(market.message_local_ts, market.bid, market.ask, market.bid_size, market.ask_size)
