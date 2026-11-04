"""Shared helpers for the recorder tests."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import polars as pl

from fake_feed import Book, FakeFeed, FakeMarket
from touchstone.recorder.config import RecorderConfig
from touchstone.recorder.health import read_health
from touchstone.recorder.recorder import Recorder
from touchstone.recorder.storage import read_day, utc_day


async def eventually(
    predicate: Callable[[], Any], *, timeout_s: float = 10.0, describe: str = "condition"
) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {describe}")
        await asyncio.sleep(0.002)


def fast_config(data_dir: Path, url: str, **overrides: Any) -> RecorderConfig:
    """Settings that keep tests quick. Timers that would interfere are pushed far away."""
    settings: dict[str, Any] = {
        "data_dir": data_dir,
        "products": ("BTC-USD",),
        "url": url,
        "stale_after_s": 30.0,
        "book_stale_after_s": 3600.0,
        "snapshot_timeout_s": 30.0,
        "backoff_base_s": 0.001,
        "backoff_cap_s": 0.01,
        "outbound_per_s": 100_000.0,
        "flush_interval_s": 3600.0,
        "status_interval_s": 3600.0,
        "audit_interval_s": 0.0,
        "rtt_interval_s": 0.0,
    }
    return RecorderConfig(**{**settings, **overrides})


class Harness:
    """Runs a real Recorder against a FakeFeed and reads back what it wrote."""

    def __init__(self, tmp_path: Path, feed: FakeFeed, *, now_ns=time.time_ns, **overrides) -> None:
        self.feed = feed
        self.config = fast_config(tmp_path, feed.url, **overrides)
        self.now_ns = now_ns
        self.recorder = Recorder(self.config, now_ns=now_ns)
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Harness:
        self.task = asyncio.create_task(self.recorder.run())
        await self.feed.wait_for_streams(len(self.config.products))
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def stop(self) -> None:
        if self.task is None:
            return
        self.recorder.stop()
        task, self.task = self.task, None
        await task

    async def settle(self, product: str = "BTC-USD") -> None:
        """Waits until the recorder is live and has caught up with the market.

        It keeps the heartbeats coming, as the real feed does, so that a message lost at the end
        of a burst is still noticed.
        """
        feed = self.recorder.feeds[product]
        market = self.feed.markets[product]

        async def caught_up() -> bool:
            await self.feed.heartbeat()
            return feed.status.state == "live" and feed.status.last_exchange_ts_ns == market.now_ns

        await eventually(caught_up, describe=f"{product} to catch up")

    async def rows(self, product: str = "BTC-USD", day: str | None = None) -> pl.DataFrame:
        await self.recorder.flush()
        day = day or utc_day(self.now_ns())
        return read_day(self.config.data_dir, product, day).collect()

    def health(self, event: str | None = None) -> list[dict[str, Any]]:
        records = read_health(self.config.data_dir)
        return [r for r in records if event is None or r["event"] == event]


def apply_book_rows(book: Book, rows: pl.DataFrame) -> Book:
    """Applies one message's rows to a book, the way replay will."""
    if rows["event"][0] in ("snapshot", "audit_snapshot"):
        book = {}
    for side, price, size in rows.select("side", "price_e8", "size_e8").iter_rows():
        if size:
            book[side, price] = size
        else:
            book.pop((side, price), None)
    return book


def assert_sessions_match_truth(rows: pl.DataFrame, market: FakeMarket) -> int:
    """Replays each recorded session and compares the book to the truth after every message.

    This is the property the recorder exists to provide: whatever went wrong on the wire, nothing
    in the file can lead replay to a wrong book. Returns the number of messages checked.
    """
    checked = 0
    stream = rows.filter(pl.col("event").is_in(("snapshot", "update")))
    for (session,), session_rows in stream.partition_by(
        "session", maintain_order=True, as_dict=True
    ).items():
        assert session_rows["event"][0] == "snapshot", f"session {session} has no leading snapshot"
        book: Book = {}
        for (seq,), message in session_rows.partition_by(
            "seq", maintain_order=True, as_dict=True
        ).items():
            book = apply_book_rows(book, message)
            exchange_ts = message["exchange_ts_ns"][0]
            assert book == market.history[exchange_ts], (
                f"session {session} diverged from the truth at seq {seq}"
            )
            checked += 1
    return checked
