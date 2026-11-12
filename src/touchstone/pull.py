"""Which recordings scripts/pull-data.sh copies down, and how much that is.

python -m touchstone.pull filters [--from DAY] [--to DAY]
    prints the aws s3 sync filter arguments, one per line, for days in [from, to)
python -m touchstone.pull plan --dest data-server [--from DAY] [--to DAY] [--max-gb N] < listing
    reads `aws s3api list-objects-v2 --query 'Contents[].[Key,Size]' --output text` and prints
    how many files and bytes are not yet in --dest at the same size. Exits 1 above --max-gb.

Every recorded file under recordings/ has its UTC day in its name, a daily Parquet file or a daily
health log, so a day filter selects whole days and nothing else.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
PREFIX = "recordings/"


def days(start: str, end: str) -> list[str]:
    """Every day from start up to, not including, end."""
    first, stop = date.fromisoformat(start), date.fromisoformat(end)
    if stop <= first:
        raise ValueError(f"empty range: {start} to {end}")
    return [(first + timedelta(n)).isoformat() for n in range((stop - first).days)]


def in_range(key: str, start: str | None, end: str | None) -> bool:
    if start is None and end is None:
        return True
    found = DAY.search(key.rsplit("/", 1)[-1])
    if found is None:
        return False
    day = found.group()
    return (start is None or day >= start) and (end is None or day < end)


def filters(start: str | None, end: str | None) -> list[str]:
    """aws s3 sync arguments that keep only files named for a day in the range.

    In aws s3 sync later filters win, so everything is excluded first and each day added back.
    """
    if start is None and end is None:
        return []
    if start is None or end is None:
        raise ValueError("give both --from and --to, or neither")
    out = ["--exclude", "*"]
    for day in days(start, end):
        out += ["--include", f"*{day}*"]
    return out


def plan(listing: Iterable[str], dest: Path, start: str | None, end: str | None) -> tuple[int, int]:
    """Files and bytes in the listing, within the range, that dest does not already hold."""
    count = size = 0
    for line in listing:
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0].startswith(PREFIX):
            continue  # "None" for an empty bucket, or a blank line
        key, nbytes = parts[0], int(parts[1])
        if not in_range(key, start, end):
            continue
        local = dest / key.removeprefix(PREFIX)
        if local.exists() and local.stat().st_size == nbytes:
            continue
        count += 1
        size += nbytes
    return count, size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m touchstone.pull")
    parser.add_argument("action", choices=["filters", "plan"])
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    parser.add_argument("--dest", type=Path, default=Path("data-server"))
    parser.add_argument("--max-gb", type=float, default=20.0)
    args = parser.parse_args(argv)
    if args.action == "filters":
        print("\n".join(filters(args.start, args.end)))
        return 0
    count, size = plan(sys.stdin, args.dest, args.start, args.end)
    print(f"{count} file(s), {size / 1e9:.2f} GB")
    if size > args.max_gb * 1e9:
        print(f"refusing: more than the {args.max_gb:g} GB limit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
