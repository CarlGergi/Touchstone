"""Signals, statistics and walk-forward selection, on inputs whose right answers are known."""

import math

import numpy as np
import pytest

from touchstone.experiments.signal_study import walk_forward
from touchstone.experiments.stats import bootstrap_ci, mean_ci, paired_overstatement
from touchstone.experiments.strategies import LATENCIES_MS, _edge_disappears_ms
from touchstone.signals import (
    Book,
    features,
    forward_change,
    order_flow,
    rank_ic,
    trailing_sum,
    value_at,
)

S = 1_000_000_000


def book(bids, asks, bid_sizes, ask_sizes, seen=None):
    n = len(bids)
    seen = np.arange(n, dtype=np.int64) * S if seen is None else np.asarray(seen, dtype=np.int64)
    return Book(
        seen, np.asarray(bids), np.asarray(asks), np.asarray(bid_sizes), np.asarray(ask_sizes)
    )


def test_order_flow_follows_cont_kukanov_stoikov():
    b = book(
        bids=[100, 100, 101, 100, 100],
        asks=[102, 102, 102, 102, 101],
        bid_sizes=[5, 7, 3, 4, 4],
        ask_sizes=[5, 5, 5, 5, 2],
    )
    # Same bid, size 5 to 7: +2. Bid up a tick: +3 new size. Bid down: -3 the old size.
    # Ask down a tick to 101 with 2 resting: -2.
    assert order_flow(b).tolist() == [0, 2, 3, -3, -2]


def test_trailing_sum_only_looks_back():
    times = np.array([0, 1, 2, 10], dtype=np.int64) * S
    values = np.array([1.0, 2.0, 4.0, 8.0])
    assert trailing_sum(times, values, 2 * S).tolist() == [1.0, 3.0, 6.0, 8.0]


def test_queue_imbalance_is_bounded_and_signed():
    b = book([100] * 3, [101] * 3, [9, 5, 0], [1, 5, 4])
    qi = features(b)["queue_imbalance"]
    assert qi.tolist() == [0.8, 0.0, -1.0]


def test_a_signal_uses_only_what_had_arrived():
    # A message received at 1.5 s is not known at 1.0 s.
    b = book([100, 105], [101, 106], [1, 1], [1, 1], seen=[0, int(1.5 * S)])
    assert value_at(b, b.bid, np.array([1 * S])).tolist() == [100]
    assert value_at(b, b.bid, np.array([2 * S])).tolist() == [105]


def test_forward_change_is_in_ticks_over_the_horizon():
    b = book([100, 102, 104], [102, 104, 106], [1] * 3, [1] * 3)
    assert forward_change(b, np.array([0], dtype=np.int64), 1.0, 2).tolist() == [1.0]


def test_rank_ic_handles_ties_and_direction():
    x = np.array([1.0, 2.0, 3.0, 4.0])
    assert rank_ic(x, x * 10) == pytest.approx(1.0)
    assert rank_ic(x, -x) == pytest.approx(-1.0)
    ties = rank_ic(np.array([1.0, 1.0, 2.0, 2.0]), np.array([0.0, 0.0, 1.0, 1.0]))
    assert ties == pytest.approx(1.0)
    assert math.isnan(rank_ic(x, np.zeros(4)))


def test_mean_ci_matches_the_t_interval():
    result = mean_ci([1.0, 2.0, 3.0, 4.0, 5.0])
    assert result["value"] == 3.0
    half = 2.776 * np.std([1, 2, 3, 4, 5], ddof=1) / math.sqrt(5)
    assert result["ci95_low"] == pytest.approx(3.0 - half)
    assert result["ci95_high"] == pytest.approx(3.0 + half)


def test_bootstrap_interval_contains_the_point_estimate():
    groups = [np.full(100, v, dtype=float) for v in (1.0, 2.0, 3.0, 4.0)]
    result = bootstrap_ci(groups, np.median)
    assert result["ci95_low"] <= result["value"] <= result["ci95_high"]
    assert result["n"] == 4


def test_overstatement_is_relative_to_the_realistic_total():
    result = paired_overstatement(np.array([30.0, 30.0]), np.array([10.0, 10.0]))
    assert result["value"] == pytest.approx(200.0)
    loss = paired_overstatement(np.array([10.0]), np.array([-10.0]))
    assert loss["value"] == pytest.approx(200.0), "a loss as the base still divides by its size"


def test_walk_forward_chooses_on_earlier_days_only():
    # Variant a wins on the first three days, and b wins on the test day. Choosing with hindsight
    # would pick b; walk-forward must pick a and score it on the test day.
    rows = []
    for product in ("BTC-USD", "ETH-USD"):
        for day, (a, b) in zip(
            ("d1", "d2", "d3", "d4"), ((0.3, 0.1), (0.3, 0.1), (0.3, 0.1), (0.0, 0.9)), strict=True
        ):
            for horizon in (0.1, 1.0, 10.0):
                rows.append(
                    {"product": product, "day": day, "horizon_s": horizon, "variant": "a", "ic": a}
                )
                rows.append(
                    {"product": product, "day": day, "horizon_s": horizon, "variant": "b", "ic": b}
                )
    result = walk_forward(rows, min_train_days=3)
    assert result["1s"]["variants_chosen"] == {"a": 2}
    assert result["1s"]["out_of_sample_ic"]["value"] == pytest.approx(0.0)


def sweep(values_by_latency):
    days = ["d1", "d2", "d3"]
    return {f"{ms}ms": dict(zip(days, values_by_latency(ms), strict=True)) for ms in LATENCIES_MS}


def test_edge_disappears_at_the_first_latency_without_profit():
    per_day = sweep(lambda ms: [10 - ms] * 3)
    result = _edge_disappears_ms(per_day, np.random.default_rng(0), resamples=50)
    assert result["value"] == 10.0


def test_no_edge_at_all_is_zero():
    result = _edge_disappears_ms(
        sweep(lambda ms: [-1.0] * 3), np.random.default_rng(0), resamples=50
    )
    assert result["value"] == 0.0


def test_edge_that_survives_the_sweep_is_reported_as_none():
    result = _edge_disappears_ms(
        sweep(lambda ms: [5.0] * 3), np.random.default_rng(0), resamples=50
    )
    assert result["value"] is None
    assert result["ci95_high"] is None
