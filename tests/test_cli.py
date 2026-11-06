"""The command line, including real process kills against the fake feed."""

import asyncio
import json
import os
import signal
import sys
import time

import polars as pl

from fake_feed import FakeFeed
from helpers import assert_sessions_match_truth, eventually
from touchstone.recorder.__main__ import main
from touchstone.recorder.health import read_health, write_status
from touchstone.recorder.storage import DayStore, read_day, recover, utc_day


async def spawn_recorder(data_dir, url, *extra):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "touchstone.recorder",
        "run",
        "--data-dir",
        str(data_dir),
        "--products",
        "BTC-USD",
        "--url",
        url,
        "--audit-interval",
        "0",
        *extra,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


def today():
    return utc_day(time.time_ns())


async def test_sigterm_flushes_everything_and_exits_cleanly(tmp_path):
    async with FakeFeed() as feed:
        process = await spawn_recorder(tmp_path, feed.url, "--flush-interval", "3600")
        await feed.wait_for_streams(timeout_s=30)
        await feed.step(20)
        await feed.heartbeat()
        await asyncio.sleep(0.2)
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), 15) == 0

    rows = read_day(tmp_path, "BTC-USD", today()).collect()
    assert rows.filter(pl.col("event") == "update")["exchange_ts_ns"].max() == (
        feed.markets["BTC-USD"].now_ns
    )
    events = [r["event"] for r in read_health(tmp_path)]
    assert events[0] == "start"
    assert events[-1] == "stop"


async def test_sigkill_keeps_every_flushed_part_readable_and_recording_can_resume(tmp_path):
    async with FakeFeed() as feed:
        process = await spawn_recorder(tmp_path, feed.url, "--flush-interval", "0.02")
        await feed.wait_for_streams(timeout_s=30)
        spool = tmp_path / ".spool" / "BTC-USD" / today()

        async def several_parts():
            await feed.step(5)
            return spool.exists() and len(list(spool.glob("part-*.parquet"))) >= 5

        await eventually(several_parts, timeout_s=30, describe="parts to be flushed")
        os.kill(process.pid, signal.SIGKILL)
        await process.wait()

        recover(tmp_path)
        survived = read_day(tmp_path, "BTC-USD", today()).collect()
        assert survived.height > 0
        assert_sessions_match_truth(survived, feed.markets["BTC-USD"])

        process = await spawn_recorder(tmp_path, feed.url, "--flush-interval", "3600")
        await feed.wait_for_streams(timeout_s=30)
        await feed.step(5)
        await feed.heartbeat()
        await asyncio.sleep(0.2)
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), 15) == 0

    rows = read_day(tmp_path, "BTC-USD", today()).collect()
    assert rows.height > survived.height
    assert rows.filter(pl.col("event") == "snapshot")["session"].n_unique() == 2
    assert_sessions_match_truth(rows, feed.markets["BTC-USD"])


def test_healthcheck_exit_codes(tmp_path, capsys):
    assert main(["healthcheck", "--data-dir", str(tmp_path)]) == 1
    assert "does not exist" in capsys.readouterr().err

    now = time.time_ns()
    write_status(
        tmp_path,
        {"updated_ns": now, "products": {"BTC-USD": {"state": "live", "last_message_ns": now}}},
    )
    assert main(["healthcheck", "--data-dir", str(tmp_path)]) == 0


async def test_verify_command_reports_a_good_day(tmp_path, capsys):
    store = DayStore(tmp_path, "BTC-USD")
    store.append(
        "2026-11-04",
        [
            (10, 5, 1, 1, 0, "snapshot", "bid", 99, 5, None),
            (10, 5, 1, 1, 0, "snapshot", "ask", 101, 5, None),
        ],
    )
    await store.flush()
    code = await asyncio.to_thread(
        main, ["verify", "BTC-USD", "2026-11-04", "--data-dir", str(tmp_path)]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["rows"] == 2


async def test_verify_command_fails_on_a_bad_day(tmp_path, capsys):
    store = DayStore(tmp_path, "BTC-USD")
    store.append("2026-11-04", [(10, 5, 1, 1, 0, "update", "bid", 99, 5, None)])
    await store.flush()
    code = await asyncio.to_thread(
        main, ["verify", "BTC-USD", "2026-11-04", "--data-dir", str(tmp_path)]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["problems"]


async def test_compact_command_merges_past_days_only(tmp_path, capsys):
    store = DayStore(tmp_path, "BTC-USD")
    row = (10, 5, 1, 1, 0, "snapshot", "bid", 99, 5, None)
    store.append("2020-01-01", [row])
    store.append(today(), [row])
    await store.flush()
    assert await asyncio.to_thread(main, ["compact", "--data-dir", str(tmp_path)]) == 0
    assert (tmp_path / "BTC-USD" / "2020-01-01.parquet").exists()
    assert not (tmp_path / "BTC-USD" / f"{today()}.parquet").exists()
    assert "2020-01-01: 1 rows" in capsys.readouterr().out


async def test_compact_can_include_today_once_recording_has_stopped(tmp_path, capsys):
    store = DayStore(tmp_path, "BTC-USD")
    row = (10, 5, 1, 1, 0, "snapshot", "bid", 99, 5, None)
    store.append(today(), [row])
    await store.flush()
    args = ["compact", "--data-dir", str(tmp_path), "--include-today"]
    assert await asyncio.to_thread(main, args) == 0
    assert (tmp_path / "BTC-USD" / f"{today()}.parquet").exists()
    assert not list((tmp_path / ".spool").glob("BTC-USD/*/part-*.parquet"))
