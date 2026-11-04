"""Storage, including a crash at every step of the merge."""

import asyncio
import os
import shutil
import threading

import polars as pl
import pytest

from touchstone.recorder.storage import (
    SCHEMA,
    SPOOL_DIR,
    DayStore,
    compact_day,
    daily_path,
    read_day,
    recover,
    spooled_days,
    utc_day,
)

DAY = "2026-11-04"
PRODUCT = "BTC-USD"


def book_row(seq, price=100, size=5, session=1, event="update"):
    return (seq, seq, seq, session, seq, event, "bid", price, size, None)


def trade_row(seq, trade_id, session=1, event="trade"):
    return (seq, seq, seq, session, seq, event, "ask", 100, 5, trade_id)


async def write_parts(root, *batches, day=DAY):
    store = DayStore(root, PRODUCT)
    for rows in batches:
        store.append(day, rows)
        await store.flush()
    return store


def spool_dir(root, day=DAY):
    return root / SPOOL_DIR / PRODUCT / day


def seqs(root, day=DAY):
    return read_day(root, PRODUCT, day).collect()["seq"].to_list()


@pytest.mark.parametrize(
    ("ts_ns", "day"),
    [
        (1_793_750_399_999_999_999, "2026-11-03"),
        (1_793_750_400_000_000_000, "2026-11-04"),
    ],
)
def test_utc_day_is_exact_at_midnight(ts_ns, day):
    assert utc_day(ts_ns) == day


async def test_rows_round_trip_with_their_schema(tmp_path):
    await write_parts(tmp_path, [book_row(1), trade_row(2, 77)])
    frame = read_day(tmp_path, PRODUCT, DAY).collect()
    assert frame.schema == SCHEMA
    assert frame["trade_id"].to_list() == [None, 77]
    assert frame["event"].to_list() == ["update", "trade"]


async def test_flush_with_nothing_pending_writes_nothing(tmp_path):
    store = DayStore(tmp_path, PRODUCT)
    assert await store.flush() == 0
    assert spooled_days(tmp_path, PRODUCT) == []


def slow_writes(store, monkeypatch):
    """Makes each part write block until the returned event is set, as a busy disk would."""
    release = threading.Event()
    started = threading.Event()
    real_write = store._write_part

    def write(day, index, rows):
        started.set()
        assert release.wait(10)
        real_write(day, index, rows)

    monkeypatch.setattr(store, "_write_part", write)
    return started, release


async def test_flush_waits_for_a_flush_already_in_progress(tmp_path, monkeypatch):
    store = DayStore(tmp_path, PRODUCT)
    started, release = slow_writes(store, monkeypatch)
    store.append(DAY, [book_row(1)])
    first = asyncio.create_task(store.flush())
    await asyncio.to_thread(started.wait, 10)

    second = asyncio.create_task(store.flush())
    await asyncio.sleep(0.01)
    assert not second.done(), "flush returned while earlier rows were still being written"

    release.set()
    await asyncio.gather(first, second)
    assert seqs(tmp_path) == [1]


async def test_cancelling_a_flush_does_not_lose_rows_for_any_day(tmp_path, monkeypatch):
    store = DayStore(tmp_path, PRODUCT)
    started, release = slow_writes(store, monkeypatch)
    store.append("2026-11-04", [book_row(1)])
    store.append("2026-11-05", [book_row(2)])
    flush = asyncio.create_task(store.flush())
    await asyncio.to_thread(started.wait, 10)

    flush.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flush

    release.set()
    await store.flush()
    assert seqs(tmp_path, "2026-11-04") == [1]
    assert seqs(tmp_path, "2026-11-05") == [2]


async def test_failed_write_keeps_rows_in_order_for_a_retry(tmp_path, monkeypatch):
    store = DayStore(tmp_path, PRODUCT)
    real_write = store._write_part

    def disk_full(*_args):
        raise OSError(28, "No space left on device")

    store.append(DAY, [book_row(1)])
    monkeypatch.setattr(store, "_write_part", disk_full)
    with pytest.raises(OSError, match="No space left"):
        await store.flush()

    monkeypatch.setattr(store, "_write_part", real_write)
    store.append(DAY, [book_row(2)])
    await store.flush()
    assert seqs(tmp_path) == [1, 2]


async def test_store_keeps_days_apart(tmp_path):
    store = DayStore(tmp_path, PRODUCT)
    store.append("2026-11-03", [book_row(1)])
    store.append("2026-11-04", [book_row(2)])
    await store.flush()
    assert seqs(tmp_path, "2026-11-03") == [1]
    assert seqs(tmp_path, "2026-11-04") == [2]


async def test_a_new_store_continues_after_existing_parts(tmp_path):
    await write_parts(tmp_path, [book_row(1)], [book_row(2)])
    await write_parts(tmp_path, [book_row(3)])
    assert seqs(tmp_path) == [1, 2, 3]
    names = sorted(p.name for p in spool_dir(tmp_path).iterdir())
    assert names == [f"part-{i:08d}.parquet" for i in (1, 2, 3)]


async def test_compaction_produces_one_file_in_arrival_order(tmp_path):
    await write_parts(tmp_path, [book_row(1), book_row(2)], [book_row(3)], [book_row(4)])
    assert compact_day(tmp_path, PRODUCT, DAY) == 4
    assert not spool_dir(tmp_path).exists()
    daily = daily_path(tmp_path, PRODUCT, DAY)
    assert daily == tmp_path / PRODUCT / f"{DAY}.parquet"
    frame = pl.read_parquet(daily)
    assert frame.schema == SCHEMA
    assert frame["seq"].to_list() == [1, 2, 3, 4]


async def test_compaction_removes_trades_repeated_by_a_reconnect(tmp_path):
    await write_parts(
        tmp_path,
        [trade_row(1, 10), trade_row(2, 11)],
        [trade_row(3, 11, session=2, event="trade_snapshot"), trade_row(4, 12, session=2)],
    )
    assert compact_day(tmp_path, PRODUCT, DAY) == 3
    frame = pl.read_parquet(daily_path(tmp_path, PRODUCT, DAY))
    assert frame["trade_id"].to_list() == [10, 11, 12]
    assert frame["event"].to_list() == ["trade", "trade", "trade"]


async def test_reading_an_unmerged_day_also_removes_repeated_trades(tmp_path):
    await write_parts(tmp_path, [trade_row(1, 10)], [trade_row(2, 10, session=2)])
    assert read_day(tmp_path, PRODUCT, DAY).collect()["trade_id"].to_list() == [10]


async def test_book_rows_are_never_treated_as_duplicates(tmp_path):
    await write_parts(tmp_path, [book_row(1), book_row(1), book_row(1)])
    assert compact_day(tmp_path, PRODUCT, DAY) == 3


async def test_late_parts_are_merged_into_an_existing_daily_file(tmp_path):
    await write_parts(tmp_path, [book_row(1)])
    compact_day(tmp_path, PRODUCT, DAY)
    await write_parts(tmp_path, [book_row(2)])
    assert seqs(tmp_path) == [1, 2]
    assert compact_day(tmp_path, PRODUCT, DAY) == 2
    assert pl.read_parquet(daily_path(tmp_path, PRODUCT, DAY))["seq"].to_list() == [1, 2]


def test_compacting_a_day_with_no_parts_is_harmless(tmp_path):
    assert compact_day(tmp_path, PRODUCT, DAY) == 0


def test_reading_a_missing_day_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_day(tmp_path, PRODUCT, DAY)


async def test_half_written_part_is_ignored_and_cleaned_up(tmp_path):
    await write_parts(tmp_path, [book_row(1)])
    torn = spool_dir(tmp_path) / "part-00000002.tmp"
    torn.write_bytes(b"PAR1 truncated")
    assert seqs(tmp_path) == [1]
    recover(tmp_path)
    assert not torn.exists()
    assert seqs(tmp_path) == [1]


class _Crash(Exception):
    pass


@pytest.mark.parametrize("crash_at", [1, 2], ids=["before_commit", "after_commit"])
async def test_crash_during_merge_never_loses_or_duplicates_rows(tmp_path, monkeypatch, crash_at):
    await write_parts(tmp_path, [book_row(1)])
    compact_day(tmp_path, PRODUCT, DAY)
    await write_parts(tmp_path, [book_row(2)], [book_row(3)])

    real_replace = os.replace
    calls = {"count": 0}

    def crashing_replace(src, dst):
        calls["count"] += 1
        if calls["count"] == crash_at:
            raise _Crash
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", crashing_replace)
    with pytest.raises(_Crash):
        compact_day(tmp_path, PRODUCT, DAY)
    monkeypatch.setattr(os, "replace", real_replace)

    recover(tmp_path)
    assert seqs(tmp_path) == [1, 2, 3]
    assert compact_day(tmp_path, PRODUCT, DAY) == 3
    assert seqs(tmp_path) == [1, 2, 3]
    assert not list(tmp_path.glob("*/*.tmp"))


async def test_crash_while_removing_merged_parts_never_duplicates_rows(tmp_path, monkeypatch):
    await write_parts(tmp_path, [book_row(1)], [book_row(2)])
    real_rmtree = shutil.rmtree

    def crashing_rmtree(path, *args, **kwargs):
        raise _Crash

    monkeypatch.setattr(shutil, "rmtree", crashing_rmtree)
    with pytest.raises(_Crash):
        compact_day(tmp_path, PRODUCT, DAY)
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)

    recover(tmp_path)
    assert seqs(tmp_path) == [1, 2]
    assert spooled_days(tmp_path, PRODUCT) == []


async def test_recover_discards_an_uncommitted_merge_output(tmp_path):
    await write_parts(tmp_path, [book_row(1)])
    daily = daily_path(tmp_path, PRODUCT, DAY)
    daily.parent.mkdir(parents=True)
    tmp = daily.with_name(daily.name + ".tmp")
    tmp.write_bytes(b"partial")
    recover(tmp_path)
    assert not tmp.exists()
    assert seqs(tmp_path) == [1]
