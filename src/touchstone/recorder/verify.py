"""Structural checks on a recorded day, run against real recordings as well as in tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl

_STREAM_BOOK = ("snapshot", "update")
_TRADES = ("trade", "trade_snapshot")


@dataclass(slots=True)
class DayReport:
    rows: int
    sessions: int
    rows_by_event: dict[str, int]
    latency_ms: dict[str, float | None]
    # Trade ids rise by one per product, so a hole means a trade was missed, as in an outage. This
    # is reported rather than treated as a problem, because outages are real and already logged.
    missing_trade_ids: int = 0
    problems: list[str] = field(default_factory=list)


def verify_day(frame: pl.LazyFrame) -> DayReport:
    """Checks the invariants that replay depends on, and returns what it found."""
    df = frame.collect(engine="streaming")
    problems: list[str] = []

    book = df.filter(pl.col("event").is_in(_STREAM_BOOK))
    trades = df.filter(pl.col("event").is_in(_TRADES))

    first_events = book.group_by("session", maintain_order=True).agg(
        pl.col("event").first().alias("first_event")
    )
    bad_start = first_events.filter(pl.col("first_event") != "snapshot").height
    if bad_start:
        problems.append(f"{bad_start} session(s) have book updates before their snapshot")

    regressions = df.group_by("session", maintain_order=True).agg(
        (pl.col("seq").diff() < 0).sum().alias("regressions")
    )
    bad_seq = regressions.filter(pl.col("regressions") > 0).height
    if bad_seq:
        problems.append(f"{bad_seq} session(s) have a sequence number that goes backwards")

    if df.filter(pl.col("price_e8") <= 0).height:
        problems.append("non-positive price")
    if df.filter(pl.col("size_e8") < 0).height:
        problems.append("negative size")
    if df.filter(pl.col("side").is_null()).height:
        problems.append("missing side")

    if trades.filter(pl.col("trade_id").is_null()).height:
        problems.append("trade without a trade_id")
    duplicates = trades.height - trades["trade_id"].n_unique()
    if duplicates:
        problems.append(f"{duplicates} duplicate trade_id(s)")
    if trades.filter(pl.col("size_e8") == 0).height:
        problems.append("trade with zero size")

    problems += _crossed_snapshots(df)

    updates = df.filter(pl.col("event") == "update")
    return DayReport(
        rows=df.height,
        sessions=df["session"].n_unique(),
        rows_by_event=_rows_by_event(df),
        latency_ms=_latency_ms(updates),
        missing_trade_ids=_missing_trade_ids(trades),
        problems=problems,
    )


def _missing_trade_ids(trades: pl.DataFrame) -> int:
    ids = trades["trade_id"].drop_nulls()
    return ids.max() - ids.min() + 1 - ids.n_unique() if len(ids) else 0


def _crossed_snapshots(df: pl.DataFrame) -> list[str]:
    snapshots = df.filter(
        pl.col("event").is_in(("snapshot", "audit_snapshot")) & (pl.col("size_e8") > 0)
    )
    tops = snapshots.group_by("session", "seq").agg(
        pl.col("price_e8").filter(pl.col("side") == "bid").max().alias("best_bid"),
        pl.col("price_e8").filter(pl.col("side") == "ask").min().alias("best_ask"),
    )
    crossed = tops.filter(pl.col("best_bid") >= pl.col("best_ask")).height
    return [f"{crossed} snapshot(s) have a crossed book"] if crossed else []


def _rows_by_event(df: pl.DataFrame) -> dict[str, int]:
    counts = df.group_by("event").len().sort("event")
    return {str(event): count for event, count in counts.iter_rows()}


def _latency_ms(updates: pl.DataFrame) -> dict[str, Any]:
    """How far local receipt trails the exchange's clock. Clock skew shows up as a negative p50."""
    if updates.is_empty():
        return {"p50": None, "p99": None}
    latency = (updates["local_ts_ns"] - updates["exchange_ts_ns"]) / 1e6
    return {"p50": latency.quantile(0.5), "p99": latency.quantile(0.99)}
