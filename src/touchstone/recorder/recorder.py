"""Runs every product's feed, flushes rows to disk, and keeps the status file current."""

from __future__ import annotations

import asyncio
import fcntl
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from touchstone.recorder import storage
from touchstone.recorder.config import RecorderConfig
from touchstone.recorder.feed import Pacer, ProductFeed
from touchstone.recorder.health import HealthLog, write_status
from touchstone.recorder.storage import DayStore, utc_day


class RecorderAlreadyRunning(RuntimeError):
    """Another recorder holds the lock on this data directory."""


def acquire_lock(data_dir: Path) -> IO[str]:
    """Holds an exclusive lock on the data directory until the returned file is closed."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = (data_dir / ".lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock.close()
        raise RecorderAlreadyRunning(str(data_dir)) from exc
    return lock


class Recorder:
    def __init__(self, config: RecorderConfig, *, now_ns: Callable[[], int] = time.time_ns) -> None:
        self.config = config
        self.now_ns = now_ns
        self.health = HealthLog(config.data_dir, now_ns)
        self.stores = {p: DayStore(config.data_dir, p) for p in config.products}
        pacer = Pacer(config.outbound_per_s)
        self.feeds = {
            p: ProductFeed(p, config, self.stores[p], self.health, pacer, now_ns)
            for p in config.products
        }
        self._stop = asyncio.Event()
        self._compaction: asyncio.Task[None] | None = None

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        lock = acquire_lock(self.config.data_dir)
        try:
            storage.recover(self.config.data_dir)
            self.health.emit("start", products=list(self.config.products), url=self.config.url)
            self._start_compaction()
            await self._supervise()
            self.health.emit("stop")
        except Exception as exc:
            self.health.emit("fatal", error=repr(exc))
            raise
        finally:
            self.health.close()
            lock.close()

    async def _supervise(self) -> None:
        tasks = [
            asyncio.create_task(self._flush_loop(), name="flush"),
            asyncio.create_task(self._status_loop(), name="status"),
        ]
        for product, feed in self.feeds.items():
            tasks.append(asyncio.create_task(feed.run(), name=f"feed:{product}"))
            tasks.append(asyncio.create_task(feed.run_audits(), name=f"audit:{product}"))
        # run_audits returns at once when audits are disabled, which must not end the recorder.
        essential = [t for t in tasks if not t.get_name().startswith("audit:")]
        stopping = asyncio.create_task(self._stop.wait(), name="stop")
        try:
            done, _ = await asyncio.wait(
                [*essential, stopping], return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in [*tasks, stopping]:
                task.cancel()
            await asyncio.gather(*tasks, stopping, return_exceptions=True)
            try:
                await self.flush()
            finally:
                if self._compaction is not None:
                    await asyncio.gather(self._compaction, return_exceptions=True)
                self._write_status()
        for task in done:
            if task is not stopping and task.exception() is not None:
                raise task.exception()

    async def flush(self) -> None:
        for store in self.stores.values():
            await store.flush()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.flush_interval_s)
            await self.flush()
            self._start_compaction()

    def _start_compaction(self) -> None:
        """Merges finished days in the background; today's flushes do not wait for it."""
        if self._compaction is not None and not self._compaction.done():
            return
        today = utc_day(self.now_ns())
        due = [
            (product, day)
            for product in self.config.products
            for day in storage.spooled_days(self.config.data_dir, product)
            if day < today
        ]
        if due:
            self._compaction = asyncio.create_task(self._compact(due), name="compact")

    async def _compact(self, due: list[tuple[str, str]]) -> None:
        for product, day in due:
            try:
                rows = await asyncio.to_thread(
                    storage.compact_day, self.config.data_dir, product, day
                )
            except Exception as exc:
                # The parts are still intact, so this is retried after the next flush.
                self.health.emit("compaction_failed", product=product, day=day, error=repr(exc))
            else:
                self.health.emit("compacted", product=product, day=day, rows=rows)

    async def _status_loop(self) -> None:
        ticks = 0
        while True:
            self._write_status()
            ticks += 1
            if ticks % self.config.stats_every_status == 0:
                self._emit_stats()
            await asyncio.sleep(self.config.status_interval_s)

    def _status(self) -> dict[str, Any]:
        return {
            "updated_ns": self.now_ns(),
            "disk_free_bytes": shutil.disk_usage(self.config.data_dir).free,
            "products": {p: feed.status_dict() for p, feed in self.feeds.items()},
        }

    def _write_status(self) -> None:
        write_status(self.config.data_dir, self._status())

    def _emit_stats(self) -> None:
        status = self._status()
        self.health.emit("stats", **status)
        if status["disk_free_bytes"] < self.config.min_free_disk_bytes:
            self.health.emit("low_disk", disk_free_bytes=status["disk_free_bytes"])
