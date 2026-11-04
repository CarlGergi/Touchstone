"""Crash-safe Parquet storage: one file per product per day.

A Parquet file is unreadable until its footer is written, so a process that holds one daily file
open would lose the whole day if it died. Rows are spooled instead as small, complete part files,
which are merged into the daily file once the day is over::

    data/.spool/BTC-USD/2026-11-04/part-00000001.parquet
    data/BTC-USD/2026-11-04.parquet

The merge commits by renaming the spool directory, so a crash at any point leaves either the parts
or the daily file as the single source of truth, and never both.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from touchstone.recorder.protocol import Row

EVENTS = pl.Enum(["snapshot", "update", "trade", "trade_snapshot", "audit_snapshot"])
SIDES = pl.Enum(["bid", "ask"])

# Column order matches protocol.Row.
SCHEMA = pl.Schema(
    {
        "local_ts_ns": pl.Int64,
        "server_ts_ns": pl.Int64,
        "exchange_ts_ns": pl.Int64,
        "session": pl.Int64,
        "seq": pl.Int64,
        "event": EVENTS,
        "side": SIDES,
        "price_e8": pl.Int64,
        "size_e8": pl.Int64,
        "trade_id": pl.Int64,
    }
)

SPOOL_DIR = ".spool"
_MERGED_SUFFIX = ".merged"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def utc_day(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns // 10**9, UTC).strftime("%Y-%m-%d")


def daily_path(root: Path, product: str, day: str) -> Path:
    return root / product / f"{day}.parquet"


def _spool_path(root: Path, product: str, day: str) -> Path:
    return root / SPOOL_DIR / product / day


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class DayStore:
    """Buffers one product's rows in memory and writes them out as part files."""

    def __init__(self, root: Path, product: str) -> None:
        self.root = root
        self.product = product
        self._pending: dict[str, list[Row]] = {}
        self._next_part: dict[str, int] = {}
        self._flush_lock = asyncio.Lock()

    def append(self, day: str, rows: list[Row]) -> None:
        self._pending.setdefault(day, []).extend(rows)

    async def flush(self) -> int:
        """Returns once every row appended before this call is durable.

        Shielded so that cancelling the caller, as shutdown does, cannot abandon rows that have
        already been taken out of the buffer.
        """
        return await asyncio.shield(asyncio.ensure_future(self._flush_pending()))

    async def _flush_pending(self) -> int:
        async with self._flush_lock:
            pending, self._pending = self._pending, {}
            written = 0
            try:
                while pending:
                    day, rows = next(iter(pending.items()))
                    index = self._allocate_part(day)
                    await asyncio.to_thread(self._write_part, day, index, rows)
                    del pending[day]
                    written += len(rows)
            finally:
                # Rows that could not be written go back in front of anything newer.
                for day, rows in pending.items():
                    self._pending[day] = rows + self._pending.get(day, [])
            return written

    def _allocate_part(self, day: str) -> int:
        if day not in self._next_part:
            self._next_part[day] = _next_part_index(_spool_path(self.root, self.product, day))
        index = self._next_part[day]
        self._next_part[day] = index + 1
        return index

    def _write_part(self, day: str, index: int, rows: list[Row]) -> None:
        directory = _spool_path(self.root, self.product, day)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"part-{index:08d}.parquet"
        tmp = path.with_suffix(".tmp")
        frame = pl.DataFrame(rows, schema=SCHEMA, orient="row")
        frame.write_parquet(tmp, compression="zstd")
        _fsync(tmp)
        os.replace(tmp, path)
        _fsync(directory)


def _next_part_index(directory: Path) -> int:
    parts = sorted(directory.glob("part-*.parquet")) if directory.exists() else []
    return int(parts[-1].stem.removeprefix("part-")) + 1 if parts else 1


def _merged(paths: list[Path]) -> pl.LazyFrame:
    # A reconnect replays recent trades as a snapshot, so a trade can arrive twice. The first
    # arrival is kept, which also keeps the file in arrival order.
    trade_id = pl.col("trade_id")
    return pl.scan_parquet(paths).filter(trade_id.is_null() | trade_id.is_first_distinct())


def _day_inputs(root: Path, product: str, day: str) -> list[Path]:
    daily = daily_path(root, product, day)
    spool = _spool_path(root, product, day)
    inputs = [daily] if daily.exists() else []
    if spool.exists():
        inputs += sorted(spool.glob("part-*.parquet"))
    return inputs


def scan_days(root: Path, product: str, days: list[str]) -> pl.LazyFrame:
    """Reads days' rows, whether or not they have been merged into their daily files yet.

    This does not enforce the held-out period, so it is for the recorder's own tooling. Research
    code reads through ``touchstone.data.load``.
    """
    inputs = [path for day in days for path in _day_inputs(root, product, day)]
    if not inputs:
        raise FileNotFoundError(f"no data for {product} on {', '.join(days)} under {root}")
    return _merged(inputs)


def read_day(root: Path, product: str, day: str) -> pl.LazyFrame:
    return scan_days(root, product, [day])


def spooled_days(root: Path, product: str) -> list[str]:
    directory = root / SPOOL_DIR / product
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_dir() and _DAY.match(p.name))


def recorded_days(root: Path, product: str) -> list[str]:
    daily = {p.stem for p in (root / product).glob("*.parquet") if _DAY.match(p.stem)}
    return sorted(daily | set(spooled_days(root, product)))


def _finish_interrupted_merge(root: Path, product: str, day: str) -> None:
    spool = _spool_path(root, product, day)
    merged = spool.with_name(day + _MERGED_SUFFIX)
    daily = daily_path(root, product, day)
    tmp = daily.with_name(daily.name + ".tmp")
    if merged.exists():
        # The rename to .merged is the commit point, and it only happens once tmp is durable.
        if tmp.exists():
            os.replace(tmp, daily)
        shutil.rmtree(merged)
    elif tmp.exists():
        tmp.unlink()


def compact_day(root: Path, product: str, day: str) -> int:
    """Merges a finished day's parts into its daily file and returns the file's row count.

    Only call this for a day that no longer receives rows.
    """
    _finish_interrupted_merge(root, product, day)
    spool = _spool_path(root, product, day)
    daily = daily_path(root, product, day)
    parts = sorted(spool.glob("part-*.parquet")) if spool.exists() else []
    if not parts:
        if spool.exists():
            shutil.rmtree(spool)
        return _row_count(daily) if daily.exists() else 0

    daily.parent.mkdir(parents=True, exist_ok=True)
    tmp = daily.with_name(daily.name + ".tmp")
    inputs = ([daily] if daily.exists() else []) + parts
    _merged(inputs).sink_parquet(tmp, compression="zstd")
    _fsync(tmp)

    merged = spool.with_name(day + _MERGED_SUFFIX)
    os.replace(spool, merged)
    _fsync(spool.parent)
    os.replace(tmp, daily)
    _fsync(daily.parent)
    shutil.rmtree(merged)
    return _row_count(daily)


def _row_count(path: Path) -> int:
    return pl.scan_parquet(path).select(pl.len()).collect().item()


def recover(root: Path) -> None:
    """Cleans up after a crash: finishes interrupted merges and removes half-written parts."""
    spool_root = root / SPOOL_DIR
    if not spool_root.exists():
        return
    for product_dir in spool_root.iterdir():
        if not product_dir.is_dir():
            continue
        days = {p.name.removesuffix(_MERGED_SUFFIX) for p in product_dir.iterdir() if p.is_dir()}
        for day in days:
            _finish_interrupted_merge(root, product_dir.name, day)
        for tmp in product_dir.glob("*/part-*.tmp"):
            tmp.unlink()
    # A daily .tmp with no spool directory left can only be an abandoned, uncommitted merge.
    for tmp in root.glob("*/*.parquet.tmp"):
        tmp.unlink()
