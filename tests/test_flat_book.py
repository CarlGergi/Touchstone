"""The flat, tick-indexed order book: it must give exactly what the std::map book gives, faster."""

import random

import numpy as np
import pytest
import touchstone_engine as engine

from test_engine import market_frame
from touchstone.products import TICK_E8
from touchstone.replay import stream_arrays

BID, ASK = engine.Side.BID, engine.Side.ASK
TICK = TICK_E8["BTC-USD"]


def assert_same(flat, ref, prices):
    for side in (BID, ASK):
        assert flat.best(side) == ref.best(side)
        assert flat.depth(side) == ref.depth(side)
        assert flat.levels(side) == ref.levels(side)
        assert flat.levels(side, 3) == ref.levels(side, 3)
        for price in prices:
            assert flat.size_at(side, price) == ref.size_at(side, price)


@pytest.mark.parametrize("window", [2, 64, 4096, 1 << 20])
def test_random_operations_match_the_map_book(window):
    """Trending prices, far levels, prices off the tick grid and clears, after every operation."""
    rng = random.Random(window)
    flat, ref = engine.FlatBook(TICK, window), engine.OrderBook()
    mid = 100_000 * 10**8
    seen = set()
    for step in range(6000):
        mid += rng.choice((-1, 0, 1)) * rng.randint(0, 200) * TICK  # drifts across many windows
        roll = rng.random()
        if roll < 0.002:
            flat.clear()
            ref.clear()
            continue
        side = rng.choice((BID, ASK))
        sign = -1 if side == BID else 1
        if roll < 0.05:
            price = mid + sign * rng.randint(10**6, 10**9) * TICK  # far from the touch
        elif roll < 0.07:
            # Off the tick grid.
            price = mid + sign * rng.randint(1, 50) * TICK + rng.randint(1, TICK - 1)
        else:
            price = mid + sign * rng.randint(0, 300) * TICK
        size = 0 if rng.random() < 0.35 else rng.randint(1, 10**9)
        flat.set(side, price, size)
        ref.set(side, price, size)
        seen.add(price)
        # Reading the best price is what moves the window, so read it after every operation.
        for s in (BID, ASK):
            assert flat.best(s) == ref.best(s), f"step {step}"
        if step % 97 == 0:
            assert_same(flat, ref, rng.sample(sorted(seen), min(50, len(seen))))
    assert_same(flat, ref, seen)


def test_window_follows_the_price():
    book = engine.FlatBook(TICK, 64)
    for i in range(1000):
        book.set(ASK, (100 + i) * TICK, 1)
        book.set(ASK, (99 + i) * TICK, 0)
        assert book.best(ASK) == ((100 + i) * TICK, 1)
    assert book.recenters > 1, "a price walking 1000 ticks must leave a 64-tick window"
    assert book.depth(ASK) == 1


def test_off_grid_best_price_does_not_recentre_on_every_read():
    book = engine.FlatBook(TICK, 64)
    book.set(BID, 100 * TICK, 1)
    book.best(BID)
    book.set(BID, 100 * TICK + 1, 2)  # one unit above, off the grid: lives in the map
    for _ in range(100):
        assert book.best(BID) == (100 * TICK + 1, 2)
    assert book.recenters <= 2


def test_negative_and_zero_prices_are_kept_exactly():
    flat, ref = engine.FlatBook(TICK, 64), engine.OrderBook()
    for price in (-5 * TICK, -1, 0, 3 * TICK):
        flat.set(BID, price, 7)
        ref.set(BID, price, 7)
        assert flat.best(BID) == ref.best(BID)
    assert flat.levels(BID) == ref.levels(BID)


def test_bad_tick_or_window_is_refused():
    with pytest.raises(ValueError, match="tick"):
        engine.FlatBook(0)
    with pytest.raises(ValueError, match="window"):
        engine.FlatBook(TICK, 1)


def test_replay_is_identical_with_either_book():
    frame, _, _ = market_frame(steps=3000, extra_sessions=3)
    arrays = stream_arrays(frame)
    by_map = engine.replay_top(*arrays.values())
    for window in (64, 1 << 20):
        by_flat = engine.replay_top(*arrays.values(), tick=TICK, window=window)
        for key, values in by_map.items():
            assert np.array_equal(np.asarray(by_flat[key]), np.asarray(values)), key


def test_pipeline_and_rate_run_on_the_flat_book():
    frame, _, _ = market_frame(steps=2000, extra_sessions=1)
    arrays = stream_arrays(frame)
    by_map = engine.pipeline(*arrays.values(), 0)
    by_flat = engine.pipeline(*arrays.values(), 0, tick=TICK)
    for key in ("bid", "ask", "checksum"):
        assert by_flat[key] == by_map[key]
    assert engine.replay_rate(*arrays.values(), tick=TICK) > 0
