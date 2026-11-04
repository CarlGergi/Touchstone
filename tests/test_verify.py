import polars as pl

from touchstone.recorder.storage import SCHEMA
from touchstone.recorder.verify import verify_day


def frame(rows):
    return pl.DataFrame(rows, schema=SCHEMA, orient="row").lazy()


def row(seq, event, *, session=1, side="bid", price=100, size=5, trade_id=None):
    return (seq + 10, seq + 5, seq, session, seq, event, side, price, size, trade_id)


GOOD = [
    row(0, "snapshot", side="bid", price=99),
    row(0, "snapshot", side="ask", price=101),
    row(1, "update", side="bid", price=99, size=0),
    row(2, "trade", side="ask", price=101, trade_id=7),
]


def test_good_day_has_no_problems_and_is_summarised():
    report = verify_day(frame(GOOD))
    assert report.problems == []
    assert report.rows == 4
    assert report.sessions == 1
    assert report.rows_by_event == {"snapshot": 2, "update": 1, "trade": 1}
    assert report.latency_ms["p50"] is not None


def problems(rows):
    return verify_day(frame(rows)).problems


def test_update_before_snapshot_is_reported():
    assert "before their snapshot" in problems([row(0, "update")])[0]


def test_sequence_going_backwards_is_reported():
    assert "backwards" in problems([*GOOD, row(0, "update")])[0]


def test_sessions_are_checked_separately_for_sequence():
    second = [
        row(0, "snapshot", session=2, side="bid", price=99),
        row(0, "snapshot", session=2, side="ask", price=101),
    ]
    assert problems([*GOOD, *second]) == []


def test_bad_values_are_reported():
    assert "non-positive price" in problems([*GOOD, row(3, "update", price=0)])
    assert "negative size" in problems([*GOOD, row(3, "update", size=-1)])
    assert "missing side" in problems([*GOOD, row(3, "update", side=None)])


def test_trade_problems_are_reported():
    assert "trade without a trade_id" in problems([*GOOD, row(3, "trade")])
    assert "1 duplicate trade_id(s)" in problems([*GOOD, row(3, "trade", trade_id=7)])
    assert "trade with zero size" in problems([*GOOD, row(3, "trade", size=0, trade_id=8)])


def test_holes_in_trade_ids_are_counted_across_both_trade_labels():
    rows = [
        *GOOD,
        row(3, "trade_snapshot", trade_id=8),
        row(4, "trade", trade_id=11),
    ]
    report = verify_day(frame(rows))
    assert report.missing_trade_ids == 2
    assert report.problems == []


def test_day_without_trades_has_no_missing_ids():
    assert verify_day(frame(GOOD[:3])).missing_trade_ids == 0


def test_crossed_snapshot_is_reported():
    crossed = [row(0, "snapshot", side="bid", price=102), row(0, "snapshot", side="ask", price=101)]
    assert "1 snapshot(s) have a crossed book" in problems(crossed)


def test_crossed_audit_snapshot_is_reported():
    crossed = [
        row(0, "audit_snapshot", session=9, side="bid", price=102),
        row(0, "audit_snapshot", session=9, side="ask", price=101),
    ]
    assert "1 snapshot(s) have a crossed book" in problems([*GOOD, *crossed])


def test_zero_size_levels_do_not_count_as_crossing():
    snapshot = [
        row(0, "snapshot", side="bid", price=102, size=0),
        row(0, "snapshot", side="bid", price=99),
        row(0, "snapshot", side="ask", price=101),
    ]
    assert problems(snapshot) == []
