"""The fill simulator, on small markets where the right answer can be worked out by hand.

Our strategy here joins the best bid and ask with one unit. The book starts with 5 units bid at 100
and 5 offered at 102. Times are in nanoseconds, and nothing happens at the same instant unless a
test says so.
"""

import numpy as np
import pytest
import touchstone_engine as engine

BID, ASK = 0, 1
MS = 1_000_000


class Market:
    def __init__(self):
        self.rows: list[tuple] = []
        self.messages: list[int] = []
        self.trades: list[tuple] = []
        self._seq = 0
        self._session = 1
        self.message(0, [(BID, 100, 5), (ASK, 102, 5)], snapshot=True)

    def message(self, ts, levels, *, snapshot=False, session=None):
        if session is not None:
            self._session, self._seq = session, 0
        self._seq += 1
        for side, price, size in levels:
            self.rows.append(
                (0 if snapshot else 1, side, price, size, self._seq, self._session, ts)
            )
        self.messages.append(ts)
        return self

    def trade(self, ts, maker_side, price, size):
        self.trades.append((ts, maker_side, price, size))
        return self

    def run(self, *, naive=False, delay=0, cancel_share=0.5, seen_offset=0, strategy=0, **params):
        config = engine.SimConfig()
        config.naive = naive
        config.cancel_share = cancel_share
        config.order_size = 1
        config.tick = 1
        config.max_inventory_lots = 100
        config.strategy = strategy
        for key, value in params.items():
            setattr(config, key, value)
        rows = list(zip(*self.rows, strict=True))
        trades = list(zip(*self.trades, strict=True)) if self.trades else [[], [], [], []]
        return engine.simulate(
            np.array(rows[0], np.uint8),
            np.array(rows[1], np.uint8),
            np.array(rows[2], np.int64),
            np.array(rows[3], np.int64),
            np.array(rows[4], np.int64),
            np.array(rows[5], np.int64),
            np.array(rows[6], np.int64),
            np.array(self.messages, np.int64) + seen_offset,
            np.zeros(len(self.messages)),
            np.array(trades[0], np.int64),
            np.array(trades[1], np.uint8),
            np.array(trades[2], np.int64),
            np.array(trades[3], np.int64),
            np.array([delay], np.int64),
            config,
        )


def bought(result):
    return int(sum(q for q in result["fill_qty"] if q > 0))


def sold(result):
    return int(-sum(q for q in result["fill_qty"] if q < 0))


def test_trades_must_get_through_the_queue_ahead_before_filling_us():
    market = Market().trade(10, BID, 100, 3).trade(20, BID, 100, 2)
    assert bought(market.run()) == 0, "5 ahead of us, only 5 traded"
    market.trade(30, BID, 100, 1)
    assert bought(market.run()) == 1


def test_a_trade_bigger_than_the_queue_fills_us_partly_then_fully():
    market = Market().trade(10, BID, 100, 6)
    result = market.run()
    assert bought(result) == 1
    assert list(result["fill_price"]) == [100]


def test_trade_through_our_price_means_the_level_was_swept():
    market = Market().trade(10, BID, 99, 1)
    assert bought(market.run()) == 1


def test_trades_at_a_better_price_do_not_touch_us():
    market = Market().trade(10, BID, 101, 50)
    assert bought(market.run()) == 0


def test_trades_on_the_other_side_fill_the_other_order():
    market = Market().trade(10, ASK, 102, 6)
    result = market.run()
    assert sold(result) == 1
    assert bought(result) == 0


@pytest.mark.parametrize(("share", "expected"), [(1.0, 1), (0.5, 0), (0.0, 0)])
def test_cancellations_move_us_forward_by_the_configured_share(share, expected):
    # 5 are ahead of us, then 4 more join behind us, then 4 cancel. Counting all of them as ahead
    # leaves 1 in front, so a trade of 2 fills us. Counting half leaves 3, and none leaves 5.
    market = (
        Market().message(10, [(BID, 100, 9)]).message(15, [(BID, 100, 5)]).trade(20, BID, 100, 2)
    )
    assert bought(market.run(cancel_share=share)) == expected


def test_volume_that_traded_is_not_also_counted_as_cancelled():
    # The level falls from 5 to 2 because 3 traded. Nothing was cancelled, so 2 are still ahead.
    market = Market().trade(10, BID, 100, 3).message(20, [(BID, 100, 2)]).trade(30, BID, 100, 2)
    assert bought(market.run(cancel_share=1.0)) == 0
    market.trade(40, BID, 100, 1)
    assert bought(market.run(cancel_share=1.0)) == 1


def test_queue_ahead_can_never_exceed_what_rests_at_the_level():
    market = Market().message(10, [(BID, 100, 1)]).trade(20, BID, 100, 2)
    # Even counting no cancellations, only 1 can be ahead once the level holds 1.
    assert bought(market.run(cancel_share=0.0)) == 1


def test_naive_model_fills_on_any_touch_regardless_of_queue():
    market = Market().trade(10, BID, 100, 1)
    assert bought(market.run()) == 0
    assert bought(market.run(naive=True)) == 1


def test_an_order_cannot_fill_before_it_reaches_the_exchange():
    # The order takes 50 ms to arrive, and the trade comes at 30 ms.
    market = Market().trade(30 * MS, BID, 99, 1)
    assert bought(market.run(delay=50 * MS)) == 0
    assert bought(market.run(delay=10 * MS)) == 1


def test_fill_before_the_cancel_arrives_counts():
    # The best bid moves up at 10 ms, so we cancel our bid at 100 and requote. The cancel takes
    # 15 ms, landing at 25 ms, so a sweep through 100 at 20 ms catches the old order first.
    market = Market().message(10 * MS, [(BID, 101, 5)]).trade(20 * MS, BID, 99, 1)
    result = market.run(delay=15 * MS)
    assert 100 in list(result["fill_price"])
    assert result["cancels_sent"] >= 1


def test_cancel_that_arrives_first_prevents_the_fill():
    market = Market().message(10 * MS, [(BID, 101, 5)]).trade(40 * MS, BID, 100, 50)
    result = market.run(delay=1 * MS)
    assert 100 not in list(result["fill_price"])


def test_late_market_data_means_late_reactions():
    # The bid moves away at 10 ms. Seeing everything 30 ms late, our first order only arrives at
    # 31 ms, and we still rest at 100 when the level is swept at 35 ms.
    market = Market().message(10 * MS, [(BID, 101, 5)]).trade(35 * MS, BID, 100, 50)
    assert 100 not in list(market.run(delay=1 * MS)["fill_price"])
    assert 100 in list(market.run(delay=1 * MS, seen_offset=30 * MS)["fill_price"])


def test_a_recording_outage_cancels_resting_orders():
    market = Market().message(10, [(BID, 100, 5), (ASK, 102, 5)], snapshot=True, session=2)
    market.trade(5, BID, 99, 1).trade(20, BID, 99, 1)
    result = market.run()
    assert result["cancelled_by_outage"] == 1, "the bid had already filled, so only the ask is left"
    assert bought(result) == 2, "one fill before the outage, one on the new order after"


def test_inventory_limit_stops_quoting_the_side_that_would_exceed_it():
    market = Market()
    for i in range(1, 10):
        market.trade(i * 10, BID, 99, 1).message(i * 10 + 5, [(BID, 100, 5)])
    result = market.run(max_inventory_lots=3)
    assert bought(result) == 3
    assert result["final_inventory"] == 3


def test_avellaneda_stoikov_moves_quotes_away_from_inventory():
    # Long inventory lowers the reservation price, so after buying we bid lower than before.
    market = Market()
    market.message(1, [(BID, 100, 5), (ASK, 110, 5)])
    for i in range(1, 6):
        market.trade(i * 10, BID, 0, 1).message(i * 10 + 5, [(ASK, 110, 5)])
    params = {"gamma": 0.5, "kappa": 1.0, "sigma_ticks": 1.0, "horizon_s": 1.0}
    prices = list(market.run(strategy=1, **params)["fill_price"])
    assert len(prices) >= 2
    assert prices == sorted(prices, reverse=True)
    assert prices[-1] < prices[0]


def test_quotes_never_cross_the_visible_book():
    market = Market().trade(10, BID, 0, 1).trade(20, ASK, 10_000, 1)
    params = {"gamma": 0.001, "kappa": 1000.0, "sigma_ticks": 0.0, "horizon_s": 1.0}
    result = market.run(strategy=1, **params)
    for price, qty in zip(result["fill_price"], result["fill_qty"], strict=True):
        assert (qty > 0 and price < 102) or (qty < 0 and price > 100)


def test_same_inputs_give_the_same_fills():
    market = Market().trade(10, BID, 100, 6).trade(20, ASK, 103, 1)
    first, second = market.run(delay=3), market.run(delay=3)
    assert list(first["fill_ts"]) == list(second["fill_ts"])
