"""Measured round-trip times, kept as two labelled profiles for the latency model and sweep.

Round trips are WebSocket pings to Coinbase, recorded in the recorder's health log. Which trader
they represent depends on where the recorder ran, so each profile names its source:

* ``fast_trader``: the always-on AWS server in us-east-2.
* ``home``: a Mac on a home connection, saved when the recorder moved off it.

Both are used in the latency sweep. Samples from held-out windows are refused like any other
held-out data unless the window is opened.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from touchstone.data import add_holdout_flags, allowed
from touchstone.recorder.health import read_health


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    label: str
    # Where its health logs are, relative to a directory holding a copy of the S3 bucket's data.
    directory: str


PROFILES = {
    "fast_trader": Profile("fast_trader", "fast_trader (us-east-2)", "."),
    "home": Profile("home", "Home (Mac on a home connection)", "latency-profiles/home"),
}


def round_trips(
    root: Path, name: str, *, final: bool = False, confirm: bool = False
) -> list[float]:
    """Returns one profile's round-trip samples in milliseconds, in the order they were taken."""
    if name not in PROFILES:
        raise KeyError(f"unknown latency profile {name!r}: expected one of {sorted(PROFILES)}")
    records = read_health(root / PROFILES[name].directory)
    return [
        record["rtt_ms"]
        for record in records
        if record["event"] == "rtt" and allowed(record["ts_ns"], final=final, confirm=confirm)
    ]


def _percentile(ordered: list[float], share: float) -> float | None:
    return ordered[min(int(share * len(ordered)), len(ordered) - 1)] if ordered else None


def summary(
    root: Path, *, final: bool = False, confirm: bool = False
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for name, profile in PROFILES.items():
        samples = sorted(round_trips(root, name, final=final, confirm=confirm))
        result[name] = {
            "label": profile.label,
            "samples": len(samples),
            "p50_ms": _percentile(samples, 0.50),
            "p99_ms": _percentile(samples, 0.99),
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m touchstone.latency")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("summary", help="show each profile's round-trip times")
    show.add_argument("--data-dir", type=Path, default=Path("data-server"))
    add_holdout_flags(show)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            summary(args.data_dir, final=args.final is True, confirm=args.confirm is True),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
