"""End-to-end tests: a real Recorder, a real WebSocket, and a fake feed that misbehaves."""

import asyncio
import json
import random

import polars as pl
import pytest

from fake_feed import START_NS, FakeFeed
from helpers import Harness, apply_book_rows, assert_sessions_match_truth, eventually
from touchstone.recorder.protocol import parse_timestamp_ns
from touchstone.recorder.recorder import Recorder, RecorderAlreadyRunning
from touchstone.recorder.storage import daily_path
from touchstone.recorder.verify import verify_day


async def test_clean_run_records_everything_and_replays_to_the_truth(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(200)
        await feed.heartbeat()
        await harness.settle()
        rows = await harness.rows()

    market = feed.markets["BTC-USD"]
    assert assert_sessions_match_truth(rows, market) == 201
    trades = rows.filter(pl.col("event") == "trade")
    assert trades["trade_id"].to_list() == [int(t["trade_id"]) for t in market.tape]
    assert harness.recorder.feeds["BTC-USD"].status.gaps == 0
    assert harness.health("gap") == []
    assert feed.total_connections == 1


async def test_channels_interleaving_on_one_sequence_is_not_mistaken_for_a_gap(tmp_path):
    # Treating sequence_num as per channel or per product, as the overview page suggests, would
    # report a gap on nearly every message here.
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        for _ in range(50):
            await feed.step()
            await feed.heartbeat()
        await harness.settle()
        status = harness.recorder.feeds["BTC-USD"].status
    assert status.gaps == 0
    assert status.resyncs == 0
    assert feed.markets["BTC-USD"].tape, "the scenario should have included trades"


async def test_every_recorded_timestamp_kind_is_present(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(5)
        await harness.settle()
        before = harness.now_ns()
        rows = await harness.rows()
    update = rows.filter(pl.col("event") == "update").row(0, named=True)
    assert update["exchange_ts_ns"] == START_NS + 50_000_000
    assert update["server_ts_ns"] == update["exchange_ts_ns"] + 1_000_000
    assert abs(before - update["local_ts_ns"]) < 60 * 10**9


async def test_dropped_book_update_triggers_a_resync_and_the_book_stays_correct(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(10)
        await harness.settle()
        feed.drop_next("l2_data")
        await feed.step(10)
        await harness.settle()
        await feed.step(10)
        await harness.settle()
        rows = await harness.rows()

    market = feed.markets["BTC-USD"]
    assert assert_sessions_match_truth(rows, market) > 0
    [gap] = harness.health("gap")
    assert gap["got"] - gap["expected"] == gap["lost"] == 1
    assert feed.dropped[0]["seq"] == gap["expected"]
    [resync] = harness.health("resync")
    assert resync["reason"] == "gap"
    assert resync["expected"] == gap["expected"]
    assert feed.total_connections == 2
    assert rows.filter(pl.col("event") == "snapshot")["session"].n_unique() == 2
    assert len(harness.health("synced")) == 2


async def test_rows_after_the_gap_never_reach_the_file(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(5)
        await harness.settle()
        feed.drop_next("l2_data")
        await feed.step(3)
        await harness.settle()
        rows = await harness.rows()
    [gap] = harness.health("gap")
    first_session = rows["session"][0]
    last_seq = rows.filter(pl.col("session") == first_session)["seq"].max()
    assert last_seq < gap["expected"]


async def test_dropped_trade_also_forces_a_resync(tmp_path):
    # The sequence is shared, so a lost trade is detectable even though the book is unaffected.
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(3)
        await harness.settle()
        feed.drop_next("market_trades")
        while not feed.dropped:
            await feed.step()
        await feed.step(1)
        await harness.settle()
        rows = await harness.rows()
    assert_sessions_match_truth(rows, feed.markets["BTC-USD"])
    assert harness.health("gap")


async def test_dropped_final_message_is_caught_by_the_next_heartbeat(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(3)
        await harness.settle()
        feed.drop_next("l2_data")
        await feed.step()
        await feed.heartbeat()
        await eventually(lambda: harness.recorder.feeds["BTC-USD"].status.gaps == 1)
        await harness.settle()
        rows = await harness.rows()
    assert_sessions_match_truth(rows, feed.markets["BTC-USD"])


async def test_burst_of_drops_is_reported_with_its_size(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(3)
        await harness.settle()
        feed.stream_connections()[0].next_seq += 7
        await feed.step(1)
        await harness.settle()
    [gap] = harness.health("gap")
    assert gap["lost"] == 7
    assert gap["got"] == gap["expected"] + 7


async def test_gap_in_one_product_does_not_disturb_the_other(tmp_path):
    products = ("BTC-USD", "ETH-USD")
    async with (
        FakeFeed(products) as feed,
        Harness(tmp_path, feed, products=products) as harness,
    ):
        await feed.step(5)
        await harness.settle("BTC-USD")
        await harness.settle("ETH-USD")
        btc_connection = feed.stream_connections("BTC-USD")[0]
        btc_connection.next_seq += 1  # BTC's next message arrives with a hole in front of it.
        await feed.step(5)
        await harness.settle("BTC-USD")
        await harness.settle("ETH-USD")
        btc = await harness.rows("BTC-USD")
        eth = await harness.rows("ETH-USD")
        statuses = {p: f.status for p, f in harness.recorder.feeds.items()}
    assert statuses["BTC-USD"].gaps == 1
    assert statuses["ETH-USD"].gaps == 0
    assert statuses["ETH-USD"].resyncs == 0
    assert_sessions_match_truth(btc, feed.markets["BTC-USD"])
    assert_sessions_match_truth(eth, feed.markets["ETH-USD"])
    assert eth["session"].n_unique() == 1


async def test_aborted_connection_is_reconnected_and_resynced(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(5)
        await harness.settle()
        await feed.abort_connections()
        await eventually(lambda: feed.total_connections == 2 and feed.stream_connections())
        await feed.step(5)
        await harness.settle()
        rows = await harness.rows()
    assert_sessions_match_truth(rows, feed.markets["BTC-USD"])
    assert harness.health("resync")[0]["reason"] == "connection_lost"


async def test_silent_connection_is_detected_by_the_watchdog(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed, stale_after_s=0.05) as harness:
        await feed.step(3)
        await harness.settle()
        feed.freeze_connections()
        await eventually(lambda: harness.health("resync"))
        await feed.wait_for_streams()
        await eventually(lambda: feed.total_connections == 2)
    assert harness.health("resync")[0]["reason"] == "stale"


async def test_book_going_quiet_while_heartbeats_continue_forces_a_resync(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed, book_stale_after_s=0.05) as harness:
        await feed.step(1)
        await harness.settle()
        await asyncio.sleep(0.06)
        await feed.heartbeat()
        await eventually(lambda: harness.health("resync"))
    assert harness.health("resync")[0]["reason"] == "book_stale"


async def test_missing_snapshot_times_out(tmp_path):
    async with FakeFeed() as feed:
        feed.withhold_snapshots = True
        async with Harness(tmp_path, feed, snapshot_timeout_s=0.05) as harness:
            await eventually(lambda: feed.stream_connections())
            await asyncio.sleep(0.06)
            await feed.heartbeat()
            await eventually(lambda: harness.health("resync"))
    assert harness.health("resync")[0]["reason"] == "snapshot_timeout"


async def test_update_before_any_snapshot_forces_a_resync_and_is_not_recorded(tmp_path):
    async with FakeFeed() as feed:
        feed.withhold_snapshots = True
        async with Harness(tmp_path, feed) as harness:
            await feed.step()
            await eventually(lambda: harness.health("resync"))
            feed.withhold_snapshots = False
            await feed.step(2)
            await harness.settle()
            rows = await harness.rows()
    assert harness.health("resync")[0]["reason"] == "update_before_snapshot"
    assert_sessions_match_truth(rows, feed.markets["BTC-USD"])


@pytest.mark.parametrize(
    "garbage",
    [
        "{not json",
        '{"channel":"l2_data","sequence_num":"nope","timestamp":"2026-11-04T00:00:00Z","events":[]}',
        '{"channel":"l2_data","events":[]}',
    ],
    ids=["invalid_json", "bad_sequence", "no_sequence"],
)
async def test_malformed_message_is_treated_as_a_loss(tmp_path, garbage):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(3)
        await harness.settle()
        await feed.send_raw(garbage)
        await eventually(lambda: harness.health("resync"))
        await feed.step(3)
        await harness.settle()
        rows = await harness.rows()
    assert harness.health("resync")[0]["reason"] == "malformed"
    assert_sessions_match_truth(rows, feed.markets["BTC-USD"])


async def test_server_error_message_forces_a_resync(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(1)
        await harness.settle()
        await feed.send_raw('{"type":"error","message":"slow consumer"}')
        await eventually(lambda: harness.health("resync"))
    resync = harness.health("resync")[0]
    assert resync["reason"] == "server_error"
    assert resync["error"] == "slow consumer"


async def test_unknown_message_kinds_are_ignored_without_a_resync(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(1)
        await harness.settle()
        await feed.send_raw('{"type":"new_kind_of_message"}')
        await feed.step(1)
        await harness.settle()
        status = harness.recorder.feeds["BTC-USD"].status
    assert status.ignored == 1
    assert status.resyncs == 0


async def test_stale_sequence_number_is_skipped_without_a_resync(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(3)
        await harness.settle()
        await feed.send_with_seq(-2)
        await feed.step(3)
        await harness.settle()
        status = harness.recorder.feeds["BTC-USD"].status
        rows = await harness.rows()
    assert status.out_of_order == 1
    assert status.resyncs == 0
    assert_sessions_match_truth(rows, feed.markets["BTC-USD"])
    assert harness.health("out_of_order")[0]["got"] < harness.health("out_of_order")[0]["expected"]


async def test_replayed_book_update_is_not_recorded_twice(tmp_path):
    # Applied late, an old absolute size would overwrite a newer one and corrupt the book.
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(3)
        await harness.settle()
        await feed.resend_last_book_message()
        await feed.step(3)
        await harness.settle()
        status = harness.recorder.feeds["BTC-USD"].status
        rows = await harness.rows()
    assert status.out_of_order == 1
    assert status.resyncs == 0
    assert verify_day(rows.lazy()).problems == []
    assert_sessions_match_truth(rows, feed.markets["BTC-USD"])


async def test_live_trades_labelled_as_a_snapshot_are_kept(tmp_path):
    # Seen on the live feed: a batch of new trades in a "snapshot" event, mid-session, that is
    # delivered nowhere else. Discarding snapshot-labelled trades would lose them silently.
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(3)
        await harness.settle()
        ids = await feed.send_live_trades_labelled_snapshot("BTC-USD")
        await feed.heartbeat()
        await feed.step(3)
        await harness.settle()
        status = harness.recorder.feeds["BTC-USD"].status
        rows = await harness.rows()
    kept = rows.filter(pl.col("trade_id").is_in([int(i) for i in ids]))
    assert kept["event"].to_list() == ["trade_snapshot"] * len(ids)
    assert status.resyncs == 0
    market = feed.markets["BTC-USD"]
    trades = rows.filter(pl.col("event").is_in(("trade", "trade_snapshot")))
    assert sorted(trades["trade_id"].to_list()) == [int(t["trade_id"]) for t in market.tape]
    assert verify_day(rows.lazy()).missing_trade_ids == 0


async def test_book_snapshot_in_the_middle_of_a_session_resets_replay(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(3)
        await harness.settle()
        await feed.send_book_snapshot("BTC-USD")
        await feed.step(3)
        await harness.settle()
        status = harness.recorder.feeds["BTC-USD"].status
        rows = await harness.rows()
    assert status.resyncs == 0
    assert rows["session"].n_unique() == 1
    snapshots = rows.filter(pl.col("event") == "snapshot")["seq"].n_unique()
    assert snapshots == 2
    assert_sessions_match_truth(rows, feed.markets["BTC-USD"])


async def test_refused_connections_are_retried_with_backoff(tmp_path):
    async with FakeFeed() as feed:
        feed.refuse_next_connections(5)
        async with Harness(tmp_path, feed) as harness:
            await feed.step(2)
            await harness.settle()
            rows = await harness.rows()
    resyncs = harness.health("resync")
    assert len(resyncs) == 5
    assert {r["reason"] for r in resyncs} == {"connection_lost"}
    assert_sessions_match_truth(rows, feed.markets["BTC-USD"])


async def test_snapshot_larger_than_the_default_frame_limit_is_accepted(tmp_path):
    # The real BTC-USD snapshot is about 4 MB. The WebSocket library's default limit is 1 MiB.
    async with FakeFeed(extra_levels=15_000) as feed, Harness(tmp_path, feed) as harness:
        await feed.step(1)
        await harness.settle()
        rows = await harness.rows()
    assert rows.filter(pl.col("event") == "snapshot").height > 20_000
    assert harness.health("resync") == []


async def test_trades_repeated_by_a_reconnect_are_removed(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(40)
        await harness.settle()
        await feed.abort_connections()
        await eventually(lambda: feed.total_connections == 2 and feed.stream_connections())
        await feed.step(40)
        await harness.settle()
        rows = await harness.rows()
    trades = rows.filter(pl.col("event").is_in(("trade", "trade_snapshot")))
    assert trades["trade_id"].n_unique() == trades.height
    assert sorted(trades["trade_id"].to_list()) == [
        int(t["trade_id"]) for t in feed.markets["BTC-USD"].tape
    ]


async def test_trades_missed_during_an_outage_are_recovered_from_the_trade_snapshot(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        await feed.step(3)
        await harness.settle()
        feed.freeze_connections()
        market = feed.markets["BTC-USD"]
        before = len(market.tape)
        while len(market.tape) == before:
            await feed.step()
        missed = market.tape[-1]["trade_id"]
        await feed.abort_connections()
        await eventually(lambda: feed.total_connections == 2 and feed.stream_connections())
        await feed.step(1)
        await harness.settle()
        rows = await harness.rows()
    row = rows.filter(pl.col("trade_id") == int(missed))
    assert row["event"].to_list() == ["trade_snapshot"]


async def test_audit_snapshots_are_recorded_and_match_the_stream(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed, audit_interval_s=0.02) as harness:
        for _ in range(20):
            await feed.step(5)
            await asyncio.sleep(0.005)
        await eventually(lambda: harness.recorder.feeds["BTC-USD"].status.audits >= 3)
        await feed.step(1)
        await harness.settle()
        rows = await harness.rows()

    market = feed.markets["BTC-USD"]
    audits = rows.filter(pl.col("event") == "audit_snapshot")
    assert audits["session"].n_unique() >= 3
    stream_sessions = rows.filter(pl.col("event") == "snapshot")["session"].n_unique()
    assert stream_sessions == 1, "an audit must not disturb the recording session"
    assert_sessions_match_truth(rows, market)
    for (_session,), audit in audits.partition_by("session", as_dict=True).items():
        book = apply_book_rows({}, audit)
        assert book == market.history[audit["exchange_ts_ns"][0]]


async def test_failed_audit_is_logged_and_does_not_stop_recording(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed, audit_interval_s=0.02) as harness:
        await feed.step(1)
        await harness.settle()
        feed.refuse_next_connections(1)
        await eventually(lambda: harness.health("audit_failed"))
        await feed.step(1)
        await harness.settle()
        status = harness.recorder.feeds["BTC-USD"].status
    assert status.audit_failures >= 1
    assert status.resyncs == 0


async def test_round_trip_times_are_logged(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed, rtt_interval_s=0.01) as harness:
        await eventually(lambda: len(harness.health("rtt")) >= 3)
    sample = harness.health("rtt")[0]
    assert sample["product"] == "BTC-USD"
    assert 0 < sample["rtt_ms"] < 5_000


async def test_status_file_is_written_while_running_and_on_stop(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed, status_interval_s=0.01) as harness:
        await feed.step(1)
        await harness.settle()
        status_path = tmp_path / "status.json"
        await eventually(status_path.exists)
    status = json.loads(status_path.read_text())
    assert status["products"]["BTC-USD"]["state"] == "live"
    assert status["products"]["BTC-USD"]["last_message_ns"] > 0
    assert status["disk_free_bytes"] > 0


async def test_stats_are_emitted_periodically(tmp_path):
    async with (
        FakeFeed() as feed,
        Harness(tmp_path, feed, status_interval_s=0.005, stats_every_status=2) as harness,
    ):
        await eventually(lambda: harness.health("stats"))
    assert "disk_free_bytes" in harness.health("stats")[0]


async def test_low_disk_is_reported(tmp_path):
    async with (
        FakeFeed() as feed,
        Harness(
            tmp_path,
            feed,
            status_interval_s=0.005,
            stats_every_status=1,
            min_free_disk_bytes=2**62,
        ) as harness,
    ):
        await eventually(lambda: harness.health("low_disk"))


async def test_second_recorder_on_the_same_directory_is_refused(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed) as harness:
        with pytest.raises(RecorderAlreadyRunning):
            await Recorder(harness.config).run()


async def test_a_stopped_recorder_releases_its_lock_and_a_new_one_appends(tmp_path):
    async with FakeFeed() as feed:
        async with Harness(tmp_path, feed) as harness:
            await feed.step(3)
            await harness.settle()
        async with Harness(tmp_path, feed) as second:
            await feed.step(3)
            await second.settle()
            rows = await second.rows()
    assert rows.filter(pl.col("event") == "snapshot")["session"].n_unique() == 2
    assert_sessions_match_truth(rows, feed.markets["BTC-USD"])


async def test_flush_loop_writes_part_files_while_running(tmp_path):
    async with FakeFeed() as feed, Harness(tmp_path, feed, flush_interval_s=0.01) as harness:
        await feed.step(3)
        await harness.settle()
        spool = tmp_path / ".spool" / "BTC-USD"
        await eventually(lambda: list(spool.glob("*/part-*.parquet")))


async def test_unexpected_failure_stops_the_recorder_and_is_logged(tmp_path, monkeypatch):
    async with FakeFeed() as feed:
        harness = Harness(tmp_path, feed, flush_interval_s=0.01)
        await harness.__aenter__()
        await feed.step(1)
        await harness.settle()

        def disk_full(*_args):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(harness.recorder.stores["BTC-USD"], "_write_part", disk_full)
        await feed.step(1)
        task, harness.task = harness.task, None
        with pytest.raises(OSError, match="No space left"):
            await asyncio.wait_for(task, 5)
        fatal = harness.health("fatal")
    assert "No space left" in fatal[0]["error"]


class Clock:
    def __init__(self, now_ns):
        self.now_ns = now_ns

    def __call__(self):
        self.now_ns += 1000
        return self.now_ns


DAY_ONE_END = parse_timestamp_ns("2026-11-04T23:59:59Z")
DAY_TWO_START = parse_timestamp_ns("2026-11-05T00:00:01Z")


async def test_day_rollover_gives_each_day_its_own_self_contained_file(tmp_path):
    clock = Clock(DAY_ONE_END)
    async with (
        FakeFeed() as feed,
        Harness(tmp_path, feed, now_ns=clock, flush_interval_s=0.01) as harness,
    ):
        await feed.step(5)
        await harness.settle()
        clock.now_ns = DAY_TWO_START
        await feed.step(5)
        await harness.settle()
        await eventually(lambda: harness.health("compacted"), describe="compaction")
        daily = daily_path(tmp_path, "BTC-USD", "2026-11-04")
        assert daily.exists()
        second_day = await harness.rows(day="2026-11-05")

    market = feed.markets["BTC-USD"]
    first_day = pl.read_parquet(daily)
    assert first_day["event"][0] == "snapshot"
    assert second_day["event"][0] == "snapshot"
    assert_sessions_match_truth(first_day, market)
    assert_sessions_match_truth(second_day, market)
    resync = harness.health("resync")[0]
    assert resync["reason"] == "day_rollover"
    assert harness.health("gap") == []
    [compacted] = harness.health("compacted")
    assert compacted["rows"] == first_day.height


async def test_finished_days_left_by_a_previous_run_are_merged_at_startup(tmp_path):
    clock = Clock(DAY_ONE_END)
    async with FakeFeed() as feed:
        async with Harness(tmp_path, feed, now_ns=clock) as first:
            await feed.step(5)
            await first.settle()
        assert not daily_path(tmp_path, "BTC-USD", "2026-11-04").exists()

        clock.now_ns = DAY_TWO_START
        async with Harness(tmp_path, feed, now_ns=clock) as second:
            await eventually(lambda: second.health("compacted"), describe="compaction")
    assert pl.read_parquet(daily_path(tmp_path, "BTC-USD", "2026-11-04")).height > 0


@pytest.mark.parametrize("seed", range(5))
async def test_random_faults_never_corrupt_a_recorded_session(tmp_path, seed):
    """The recorder's central guarantee, under a seeded storm of everything that can go wrong."""
    rng = random.Random(seed)
    async with (
        FakeFeed(seed=seed) as feed,
        Harness(tmp_path, feed, audit_interval_s=0.01) as harness,
    ):
        for _ in range(150):
            action = rng.random()
            if action < 0.70:
                await feed.step(rng.randint(1, 4))
            elif action < 0.80:
                feed.drop_next(rng.choice(("l2_data", "market_trades", "heartbeats")))
            elif action < 0.87:
                await feed.heartbeat()
            elif action < 0.92:
                await feed.abort_connections()
            elif action < 0.96:
                await feed.send_raw("{garbage")
            else:
                await feed.send_with_seq(-1)
            if rng.random() < 0.3:
                await asyncio.sleep(0.001)

        await feed.step(1)
        await harness.settle()
        rows = await harness.rows()

    market = feed.markets["BTC-USD"]
    assert assert_sessions_match_truth(rows, market) > 0
    assert harness.health("resync"), "the storm should have forced at least one resync"
    report = verify_day(rows.lazy())
    assert report.problems == []
    last_session = rows.filter(pl.col("event") == "snapshot")["session"][-1]
    final = rows.filter(
        (pl.col("session") == last_session) & pl.col("event").is_in(("snapshot", "update"))
    )
    book = {}
    for message in final.partition_by("seq", maintain_order=True):
        book = apply_book_rows(book, message)
    assert book == market.book
