"""The C++ order book, replay and ring buffer, checked against Python and the fake feed."""

import numpy as np
import polars as pl
import pytest
import touchstone_engine as engine

from fake_feed import FakeMarket
from touchstone.recorder.protocol import parse_fixed
from touchstone.recorder.storage import SCHEMA
from touchstone.replay import check_against_audits, python_book_rate, replay_top, stream_arrays

BID, ASK = engine.Side.BID, engine.Side.ASK


def test_book_keeps_levels_in_price_order_and_removes_zeros():
    book = engine.OrderBook()
    for price, size in ((100, 5), (102, 1), (101, 3)):
        book.set(BID, price, size)
    for price, size in ((105, 2), (103, 4)):
        book.set(ASK, price, size)
    assert book.best(BID) == (102, 1)
    assert book.best(ASK) == (103, 4)
    assert book.levels(BID) == [(102, 1), (101, 3), (100, 5)]
    assert book.levels(ASK, 1) == [(103, 4)]
    book.set(BID, 102, 0)
    assert book.best(BID) == (101, 3)
    assert book.size_at(BID, 102) == 0
    assert book.depth(BID) == 2


def test_absolute_sizes_replace_rather_than_add():
    book = engine.OrderBook()
    book.set(BID, 100, 5)
    book.set(BID, 100, 2)
    assert book.size_at(BID, 100) == 2


def test_empty_book_has_no_best_price():
    book = engine.OrderBook()
    assert book.best(BID) is None
    book.set(ASK, 100, 1)
    book.clear()
    assert book.best(ASK) is None


def market_frame(steps=300, audit_every=0, seed=0, extra_sessions=0):
    """Rows as the recorder writes them, built from a fake market whose truth is known."""
    market = FakeMarket("BTC-USD", seed=seed)
    rows, audits = [], []
    session, seq = 1, 0

    def snapshot(label, sess, sq):
        for (side, price), size in sorted(market.book.items()):
            rows.append(
                (
                    market.now_ns,
                    market.now_ns,
                    market.now_ns,
                    sess,
                    sq,
                    label,
                    side,
                    price,
                    size,
                    None,
                )
            )

    snapshot("snapshot", session, seq)
    for step in range(1, steps + 1):
        updates, _ = market.step()
        seq += 1
        for update in updates:
            side = "bid" if update["side"] == "bid" else "ask"
            rows.append(
                (
                    market.now_ns,
                    market.now_ns,
                    market.now_ns,
                    session,
                    seq,
                    "update",
                    side,
                    parse_fixed(update["price_level"]),
                    parse_fixed(update["new_quantity"]),
                    None,
                )
            )
        if audit_every and step % audit_every == 0:
            audits.append(market.now_ns)
            snapshot("audit_snapshot", 1000 + step, 0)
        if extra_sessions and step % (steps // (extra_sessions + 1)) == 0:
            session, seq = session + 1, 0
            snapshot("snapshot", session, seq)
    return pl.DataFrame(rows, schema=SCHEMA, orient="row"), market, audits


def test_replay_matches_the_truth_after_every_message():
    frame, market, _ = market_frame(steps=400, extra_sessions=2)
    top = replay_top(stream_arrays(frame))
    for ts, bid, ask in top.select("exchange_ts_ns", "bid", "ask").iter_rows():
        truth = market.history[ts]
        assert bid == max(p for s, p in truth if s == "bid")
        assert ask == min(p for s, p in truth if s == "ask")


def test_python_reference_runs_on_the_same_rows():
    frame, _, _ = market_frame(steps=200)
    assert python_book_rate(stream_arrays(frame)) > 0
    assert engine.replay_rate(*stream_arrays(frame).values()) > 0


def test_book_matches_every_audit_snapshot():
    frame, _, _ = market_frame(steps=300, audit_every=25)
    check = check_against_audits(frame)
    assert check.audits == 12
    assert check.checked == 11, "the audit after the last message has no following message"
    assert check.exact == check.checked
    assert check.passed


def test_corrupted_replay_fails_the_audit_check():
    frame, _, _ = market_frame(steps=300, audit_every=25)
    # A stale level far from the market, which no later update touches, as a lost removal leaves.
    first = frame.row(0, named=True)
    stale = (
        first["local_ts_ns"],
        first["server_ts_ns"],
        first["exchange_ts_ns"],
        first["session"],
        first["seq"],
        "snapshot",
        "bid",
        100_000_000,
        7,
        None,
    )
    broken = pl.concat([pl.DataFrame([stale], schema=SCHEMA, orient="row"), frame])
    check = check_against_audits(broken)
    assert not check.passed
    assert check.unexplained_levels == check.checked


def test_audit_taken_mid_batch_is_consistent_but_not_exact():
    frame, market, _ = market_frame(steps=50)
    # An audit timed just before message 31, holding part of it already.
    batch = frame.filter((pl.col("event") == "update") & (pl.col("seq") == 31))
    at = int(batch["exchange_ts_ns"][0]) - 1
    before = {(s, p): q for (s, p), q in market.history[at - (at % 50_000_000)].items()}
    first = batch.row(0, named=True)
    before[(first["side"], first["price_e8"])] = first["size_e8"]
    audit = [
        (at, at, at, 999, 0, "audit_snapshot", side, price, size, None)
        for (side, price), size in before.items()
        if size
    ]
    frame = pl.concat([frame, pl.DataFrame(audit, schema=SCHEMA, orient="row")])
    check = check_against_audits(frame)
    assert check.checked == 1
    assert check.consistent == 1


def pipeline(frame, rate):
    return engine.pipeline(*stream_arrays(frame).values(), rate)


def test_ring_buffer_delivers_every_row_in_order():
    frame, _, _ = market_frame(steps=2000, extra_sessions=1)
    arrays = stream_arrays(frame)
    result = pipeline(frame, 0)
    top = replay_top(arrays)
    assert result["bid"] == top["bid"][-1]
    assert result["ask"] == top["ask"][-1]
    assert len(result["latency_ns"]) == len(arrays["event"])
    # The checksum covers order as well as content.
    expected = 0
    for price, size in zip(arrays["price"].tolist(), arrays["size"].tolist(), strict=True):
        expected = (expected * 1099511628211 + ((price ^ size) % 2**64)) % 2**64
    assert result["checksum"] == expected


def test_ring_buffer_wraps_around_many_times():
    frame, _, _ = market_frame(steps=100_000)
    assert len(stream_arrays(frame)["event"]) > 3 * 2**16
    result = pipeline(frame, 0)
    assert result["bid"] == replay_top(stream_arrays(frame))["bid"][-1]


def test_paced_pipeline_reports_handoff_latency():
    frame, _, _ = market_frame(steps=500)
    result = pipeline(frame, 200_000)
    latency = np.asarray(result["latency_ns"])
    assert (latency > 0).all()
    assert np.percentile(latency, 50) < 1_000_000, "a paced handoff should take well under 1 ms"


def test_columns_of_different_lengths_are_refused():
    arrays = stream_arrays(market_frame(steps=5)[0])
    arrays["price"] = arrays["price"][:-1]
    with pytest.raises(ValueError, match="differ in length"):
        engine.replay_top(*arrays.values())
