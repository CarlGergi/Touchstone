"""A recorded day prepared for the simulator, and its P&L, end to end on fake-feed data."""

import numpy as np
import polars as pl
import pytest

from test_engine import market_frame
from touchstone.recorder.storage import SCHEMA
from touchstone.simulation import fixed, naive, pnl, prepare, simulate


def with_trades(frame, market):
    rows = [
        (
            ts,
            ts,
            ts,
            1,
            10_000 + i,
            "trade",
            "bid" if t["side"] == "BUY" else "ask",
            int(round(float(t["price"]) * 1e8)),
            int(round(float(t["size"]) * 1e8)),
            int(t["trade_id"]),
        )
        for i, t in enumerate(market.tape)
        for ts in [int(np.datetime64(t["time"].rstrip("Z"), "ns").astype(np.int64))]
    ]
    return pl.concat([frame, pl.DataFrame(rows, schema=SCHEMA, orient="row")])


@pytest.fixture(scope="module")
def day():
    frame, market, _ = market_frame(steps=3000, extra_sessions=1)
    return prepare(with_trades(frame, market), "BTC-USD")


def test_every_book_message_gets_a_seen_time(day):
    assert len(day.message_exchange_ts) == len(day.mid) == len(day.bid)
    assert day.message_exchange_ts[0] == day.arrays["ts"][0]


def test_trades_are_in_time_order(day):
    assert (np.diff(day.trades["ts"]) >= 0).all()
    assert len(day.trades["ts"]) > 100


def test_naive_fills_at_least_as_often_as_the_queue_model(day):
    touched = simulate(day, naive(), naive_fills=True)
    queued = simulate(day, fixed(0))
    assert len(touched["fill_ts"]) >= len(queued["fill_ts"]) > 0


def test_attribution_adds_up_to_the_total(day):
    result = pnl(day, simulate(day, fixed(5)), fee_bps=1.0)
    parts = (
        result["spread_capture"]
        + result["adverse_selection"]
        + result["inventory"]
        + result["fees"]
    )
    assert parts == pytest.approx(result["total"])
    assert result["fees"] < 0


def test_no_fills_means_no_pnl(day):
    empty = {
        "fill_ts": np.array([], np.int64),
        "fill_price": np.array([], np.int64),
        "fill_qty": np.array([], np.int64),
    }
    assert pnl(day, empty)["total"] == 0.0
