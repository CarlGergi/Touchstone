"""Reads recorded data for research, and keeps the held-out period out of it.

Development data is everything before 2026-12-01 00:00 UTC. The week from 2026-12-01 to 2026-12-07
UTC is held out for the final evaluation. ``load`` refuses timestamps from the cutoff onward unless
it is given ``final=True``, which research commands expose as ``--final``.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from touchstone.recorder.storage import recorded_days, scan_days, utc_day

DEVELOPMENT_END = datetime(2026, 12, 1, tzinfo=UTC)
HOLDOUT_END = datetime(2026, 12, 8, tzinfo=UTC)

_TIMESTAMPS = ("local_ts_ns", "server_ts_ns", "exchange_ts_ns")

When = str | datetime


class HoldoutError(PermissionError):
    """Something asked for held-out data without ``final=True``."""


def _to_ns(when: When) -> int:
    """Converts a time to epoch nanoseconds. A time with no offset is taken to be UTC."""
    parsed = datetime.fromisoformat(when) if isinstance(when, str) else when
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # Whole seconds and microseconds are added separately to stay exact.
    delta = parsed - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 10**9 + delta.microseconds * 1_000


DEVELOPMENT_END_NS = _to_ns(DEVELOPMENT_END)


def load(
    root: Path,
    product: str,
    *,
    start: When | None = None,
    end: When | None = None,
    final: bool = False,
) -> pl.LazyFrame:
    """Loads rows received in ``[start, end)``, with the held-out period refused or included.

    Without ``final=True``, ``end`` defaults to the end of development data, and a range reaching
    past it raises ``HoldoutError``. Held-out files are not opened. Rows are also filtered on all
    three clocks, because clock skew can give a row received just before the cutoff an exchange
    time after it.

    Anything computed from future data, such as a forward return near the cutoff, cannot be
    computed from what this returns, which is the point.
    """
    start_ns = None if start is None else _to_ns(start)
    end_ns = None if end is None else _to_ns(end)
    include_holdout = final is True

    if include_holdout:
        warnings.warn("reading held-out data: this is for the final evaluation only", stacklevel=2)
    else:
        if end_ns is None:
            end_ns = DEVELOPMENT_END_NS
        if end_ns > DEVELOPMENT_END_NS or (start_ns is not None and start_ns >= DEVELOPMENT_END_NS):
            raise HoldoutError(
                f"timestamps from {DEVELOPMENT_END.isoformat()} onward are held out: "
                "pass final=True (--final) only for the final evaluation"
            )
    if start_ns is not None and end_ns is not None and start_ns >= end_ns:
        raise ValueError("start must be before end")

    days = [d for d in recorded_days(root, product) if _in_range(d, start_ns, end_ns)]
    if not days:
        raise FileNotFoundError(f"no data for {product} in that range under {root}")

    frame = scan_days(root, product, days)
    if start_ns is not None:
        frame = frame.filter(pl.col("local_ts_ns") >= start_ns)
    if end_ns is not None:
        frame = frame.filter(pl.col("local_ts_ns") < end_ns)
    if not include_holdout:
        frame = frame.filter(pl.max_horizontal(*_TIMESTAMPS) < DEVELOPMENT_END_NS)
    return frame


def _in_range(day: str, start_ns: int | None, end_ns: int | None) -> bool:
    if start_ns is not None and day < utc_day(start_ns):
        return False
    return end_ns is None or day <= utc_day(end_ns - 1)


def add_final_flag(parser: argparse.ArgumentParser) -> None:
    """Gives a research command the one switch that unlocks held-out data."""
    parser.add_argument(
        "--final",
        action="store_true",
        help="include held-out data from 2026-12-01 onward; for the final evaluation only",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m touchstone.data")
    commands = parser.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("summary", help="count rows in a range, without showing prices")
    summary.add_argument("product")
    summary.add_argument("--data-dir", type=Path, default=Path("data"))
    summary.add_argument("--start")
    summary.add_argument("--end")
    add_final_flag(summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        frame = load(
            args.data_dir, args.product, start=args.start, end=args.end, final=args.final is True
        )
    except HoldoutError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    rows, first, last = (
        frame.select(
            pl.len(),
            pl.col("local_ts_ns").min().alias("first"),
            pl.col("local_ts_ns").max().alias("last"),
        )
        .collect(engine="streaming")
        .row(0)
    )
    print(json.dumps({"product": args.product, "rows": rows, "first_ns": first, "last_ns": last}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
