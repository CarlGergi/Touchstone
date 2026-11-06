import json

from touchstone.recorder.__main__ import main
from touchstone.recorder.health import HealthLog
from touchstone.recorder.report import daily_summary, summarize
from touchstone.recorder.storage import DayStore, compact_day

DAY_ONE = 1_793_750_400_000_000_000
DAY_TWO = DAY_ONE + 86_400 * 10**9
P = "BTC-USD"


def rec(ts, event, **fields):
    return {"ts_ns": ts, "event": event, "product": P, **fields}


def test_empty_log():
    assert summarize([]) == {
        "first_ns": None,
        "last_ns": None,
        "process_starts": 0,
        "fatal": [],
        "low_disk_warnings": 0,
        "products": {},
    }


def test_rollover_is_paired_with_the_outage_that_followed_it():
    summary = summarize(
        [
            rec(DAY_ONE, "synced", session=1, outage_ms=None),
            rec(DAY_TWO, "resync", session=1, reason="day_rollover", day="2026-11-05"),
            rec(DAY_TWO + 1, "synced", session=2, outage_ms=850.5),
        ]
    )["products"][P]
    assert summary["rollovers"] == [{"day": "2026-11-05", "outage_ms": 850.5}]
    assert summary["sessions"] == 2
    assert summary["resyncs"] == {"day_rollover": 1}
    assert summary["longest_outage_ms"] == 850.5


def test_outage_after_another_reason_is_not_called_a_rollover():
    summary = summarize(
        [
            rec(DAY_ONE, "resync", session=1, reason="connection_lost"),
            rec(DAY_ONE + 1, "synced", session=2, outage_ms=900.0),
        ]
    )["products"][P]
    assert summary["rollovers"] == []
    assert summary["longest_outage_ms"] == 900.0


def test_gaps_keep_their_size():
    records = [
        rec(DAY_ONE, "gap", session=1, expected=5, got=8, lost=3),
        rec(DAY_ONE + 1, "resync", session=1, reason="gap", expected=5, got=8, lost=3),
    ]
    summary = summarize(records)["products"][P]
    assert summary["gaps"] == [{"ts_ns": DAY_ONE, "lost": 3, "session": 1}]
    assert summary["resyncs"] == {"gap": 1}


def test_counts_and_merges():
    summary = summarize(
        [
            {"ts_ns": DAY_ONE, "event": "start", "products": [P]},
            rec(DAY_ONE, "audit", levels=10),
            rec(DAY_ONE, "audit", levels=10),
            rec(DAY_ONE, "audit_failed", error="x"),
            rec(DAY_ONE, "out_of_order", session=1, expected=2, got=1),
            rec(DAY_TWO, "compacted", day="2026-11-04", rows=99),
            rec(DAY_TWO, "compaction_failed", day="2026-11-03", error="boom"),
            {"ts_ns": DAY_TWO, "event": "fatal", "error": "disk full"},
            {"ts_ns": DAY_TWO, "event": "low_disk", "disk_free_bytes": 1},
        ]
    )
    product = summary["products"][P]
    assert summary["process_starts"] == 1
    assert summary["fatal"] == [{"day": "2026-11-05", "error": "disk full"}]
    assert summary["low_disk_warnings"] == 1
    assert product["audits"] == 2
    assert product["audit_failures"] == 1
    assert product["out_of_order"] == 1
    assert product["compactions"] == [{"day": "2026-11-04", "rows": 99}]
    assert product["compaction_failures"] == [{"day": "2026-11-03", "error": "boom"}]


def test_round_trip_percentiles():
    records = [rec(DAY_ONE + i, "rtt", session=1, rtt_ms=float(i)) for i in range(1, 101)]
    assert summarize(records)["products"][P]["rtt_ms"] == {
        "samples": 100,
        "p50": 51.0,
        "p99": 100.0,
    }


def test_since_keeps_only_later_days():
    summary = summarize(
        [rec(DAY_ONE, "audit", levels=1), rec(DAY_TWO, "audit", levels=1)], since="2026-11-05"
    )
    assert summary["products"][P]["audits"] == 1
    assert summary["first_ns"] == DAY_TWO


def test_command_prints_json(tmp_path, capsys):
    log = HealthLog(tmp_path, now_ns=lambda: DAY_ONE)
    log.emit("gap", product=P, session=1, expected=1, got=3, lost=2)
    log.close()
    assert main(["report", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["products"][P]["gaps"][0]["lost"] == 2


DAY = "2026-11-05"


def row(seq, event, trade_id=None):
    return (seq, seq, seq, 1, seq, event, "bid", 100, 5, trade_id)


async def recorded_day(root, product=P, trade_ids=(10, 11, 14)):
    store = DayStore(root, product)
    rows = [row(0, "snapshot"), row(1, "update"), row(2, "update")]
    rows += [row(3 + i, "trade", trade_id) for i, trade_id in enumerate(trade_ids)]
    store.append(DAY, rows)
    await store.flush()
    compact_day(root, product, DAY)


def log_events(root, *records):
    log = HealthLog(root, now_ns=lambda: DAY_TWO)
    for event, fields in records:
        log.emit(event, **fields)
    log.close()


async def test_daily_summary_counts_events_gaps_and_missing_trades(tmp_path):
    await recorded_day(tmp_path)
    log_events(
        tmp_path,
        ("gap", {"product": P, "session": 1, "expected": 1, "got": 4, "lost": 3}),
        ("resync", {"product": P, "session": 1, "reason": "gap"}),
        ("synced", {"product": P, "session": 2, "outage_ms": 900.0}),
        ("resync", {"product": P, "session": 2, "reason": "connection_lost"}),
        ("resync", {"product": P, "session": 3, "reason": "day_rollover", "day": DAY}),
    )
    summary = daily_summary(tmp_path, DAY, [P])
    assert summary["day"] == DAY
    assert summary["problems"] == []
    stats = summary["products"][P]
    assert stats["rows"] == {"snapshot": 1, "update": 2, "trade": 3}
    assert stats["gaps"] == 1
    assert stats["lost_messages"] == 3
    assert stats["reconnects"] == 2, "the midnight rollover is routine and not counted"
    assert stats["longest_outage_ms"] == 900.0
    assert stats["missing_trade_ids"] == 2


async def test_daily_summary_only_uses_that_days_health_log(tmp_path):
    await recorded_day(tmp_path)
    log = HealthLog(tmp_path, now_ns=lambda: DAY_TWO + 86_400 * 10**9)
    log.emit("gap", product=P, session=1, expected=1, got=2, lost=1)
    log.close()
    assert daily_summary(tmp_path, DAY, [P])["products"][P]["gaps"] == 0


def test_daily_summary_reports_a_missing_file_as_a_problem(tmp_path):
    summary = daily_summary(tmp_path, DAY, [P])
    assert summary["problems"] == [f"{P}: no merged file for {DAY}."]
    assert summary["products"][P]["rows"] == {}


async def test_daily_summary_reports_failures_and_low_disk(tmp_path):
    await recorded_day(tmp_path)
    log = HealthLog(tmp_path, now_ns=lambda: DAY_TWO)
    log.emit("fatal", error="disk full")
    log.emit("low_disk", disk_free_bytes=1)
    log.emit("start", products=[P])
    log.close()
    summary = daily_summary(tmp_path, DAY, [P])
    assert summary["process_starts"] == 1
    assert "Recorder failed: disk full" in summary["problems"]
    assert "The server is low on disk." in summary["problems"]


async def test_daily_summary_holds_no_prices(tmp_path):
    await recorded_day(tmp_path)
    text = json.dumps(daily_summary(tmp_path, DAY, [P]))
    assert "price" not in text


async def test_daily_summary_with_no_trades(tmp_path):
    await recorded_day(tmp_path, trade_ids=())
    assert daily_summary(tmp_path, DAY, [P])["products"][P]["missing_trade_ids"] == 0


async def test_daily_summary_command_prints_json(tmp_path, capsys):
    await recorded_day(tmp_path)
    argv = ["daily-summary", DAY, "--data-dir", str(tmp_path), "--products", P]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["products"][P]["rows"]["update"] == 2
