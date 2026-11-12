"""Reads recorded data for research, and keeps the held-out periods out of it.

Two windows, both in UTC:

* Development: everything before 2026-11-12 00:00. All research.
* Held-out evaluation: 2026-11-12 00:00 to 2026-11-15 00:00, three full days, evaluated once.
  Opened only with ``final=True`` (``--final``).

Anything from 2026-11-15 onward is never read. A range that touches a window that is not open is
refused, rather than quietly cut short.
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

DEVELOPMENT_END = datetime(2026, 11, 12, tzinfo=UTC)
FINAL_START = DEVELOPMENT_END
FINAL_END = datetime(2026, 11, 15, tzinfo=UTC)

_TIMESTAMPS = ("local_ts_ns", "server_ts_ns", "exchange_ts_ns")
_FOREVER = 2**63 - 1

When = str | datetime


class HoldoutError(PermissionError):
    """Something asked for data from a window that was not opened."""


def _to_ns(when: When) -> int:
    """Converts a time to epoch nanoseconds. A time with no offset is taken to be UTC."""
    parsed = datetime.fromisoformat(when) if isinstance(when, str) else when
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # Whole seconds and microseconds are added separately to stay exact.
    delta = parsed - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 10**9 + delta.microseconds * 1_000


DEVELOPMENT_END_NS = _to_ns(DEVELOPMENT_END)
FINAL_END_NS = _to_ns(FINAL_END)


def forbidden(*, final: bool = False) -> list[tuple[int, int]]:
    """The half-open time ranges, in nanoseconds, that stay closed. Only ``True`` opens a window."""
    if final is True:
        return [(FINAL_END_NS, _FOREVER)]
    return [(DEVELOPMENT_END_NS, _FOREVER)]


def allowed(ts_ns: int, *, final: bool = False) -> bool:
    return not any(low <= ts_ns < high for low, high in forbidden(final=final))


def load(
    root: Path,
    product: str,
    *,
    start: When | None = None,
    end: When | None = None,
    final: bool = False,
) -> pl.LazyFrame:
    """Loads rows received in ``[start, end)`` from the windows that are open.

    ``end`` defaults to the end of the last open window. A range that touches a closed window
    raises ``HoldoutError``, and files from closed windows are never opened. Rows are filtered on
    all three clocks too, because clock skew can give a row received just before a boundary an
    exchange time after it.

    Anything computed from future data, such as a forward return near a boundary, cannot be
    computed from what this returns, which is the point.
    """
    closed = forbidden(final=final)
    if final is True:
        warnings.warn("reading the held-out window: for the one evaluation only", stacklevel=2)
        default_end = FINAL_END_NS
    else:
        default_end = DEVELOPMENT_END_NS

    start_ns = -_FOREVER if start is None else _to_ns(start)
    end_ns = default_end if end is None else _to_ns(end)
    # Windows first: a start inside a closed window is a held-out request, whatever the end.
    reach = max(end_ns, start_ns + 1)
    for low, high in closed:
        if start_ns < high and reach > low:
            raise HoldoutError(
                "that range reaches held-out data that is not open: development data ends "
                f"{DEVELOPMENT_END.isoformat()}; --final opens to {FINAL_END.isoformat()}, and "
                "nothing later is ever read"
            )
    if start_ns >= end_ns:
        raise ValueError("start must be before end")

    days = [d for d in recorded_days(root, product) if _in_range(d, start_ns, end_ns)]
    if not days:
        raise FileNotFoundError(f"no data for {product} in that range under {root}")

    frame = scan_days(root, product, days)
    frame = frame.filter((pl.col("local_ts_ns") >= start_ns) & (pl.col("local_ts_ns") < end_ns))
    for low, high in closed:
        frame = frame.filter(
            ~pl.any_horizontal([(pl.col(c) >= low) & (pl.col(c) < high) for c in _TIMESTAMPS])
        )
    return frame


def _in_range(day: str, start_ns: int, end_ns: int) -> bool:
    if start_ns > 0 and day < utc_day(start_ns):
        return False
    return day <= utc_day(end_ns - 1)


def add_holdout_flags(parser: argparse.ArgumentParser) -> None:
    """Gives a research command the switch that opens the held-out window."""
    parser.add_argument(
        "--final",
        action="store_true",
        help="also read 2026-11-12 to 2026-11-15, for the held-out evaluation only",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m touchstone.data")
    commands = parser.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("summary", help="count rows in a range, without showing prices")
    summary.add_argument("product")
    summary.add_argument("--data-dir", type=Path, default=Path("data"))
    summary.add_argument("--start")
    summary.add_argument("--end")
    add_holdout_flags(summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        frame = load(
            args.data_dir,
            args.product,
            start=args.start,
            end=args.end,
            final=args.final is True,
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
