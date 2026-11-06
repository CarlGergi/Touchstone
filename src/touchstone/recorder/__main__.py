"""Command line for the recorder: ``python -m touchstone.recorder <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

from touchstone.recorder.config import DEFAULT_PRODUCTS, DEFAULT_URL, RecorderConfig
from touchstone.recorder.health import check_status, read_health


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m touchstone.recorder")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="record until stopped")
    run.add_argument("--data-dir", type=Path, default=Path("data"))
    run.add_argument("--products", default=",".join(DEFAULT_PRODUCTS))
    run.add_argument("--url", default=DEFAULT_URL)
    defaults = RecorderConfig(data_dir=Path("."))
    run.add_argument("--flush-interval", type=float, default=defaults.flush_interval_s)
    run.add_argument("--status-interval", type=float, default=defaults.status_interval_s)
    run.add_argument("--audit-interval", type=float, default=defaults.audit_interval_s)
    run.add_argument("--rtt-interval", type=float, default=defaults.rtt_interval_s)

    health = commands.add_parser(
        "healthcheck", help="exit non-zero if the recorder looks unhealthy"
    )
    health.add_argument("--data-dir", type=Path, default=Path("data"))
    health.add_argument("--max-age", type=float, default=120.0)

    compact = commands.add_parser("compact", help="merge finished days into their daily files")
    compact.add_argument("--data-dir", type=Path, default=Path("data"))
    compact.add_argument(
        "--include-today",
        action="store_true",
        help="also merge today, which is only safe once recording has stopped for good",
    )

    report = commands.add_parser("report", help="summarize the health log")
    report.add_argument("--data-dir", type=Path, default=Path("data"))
    report.add_argument("--since", help="only include this UTC day (YYYY-MM-DD) onward")

    daily = commands.add_parser("daily-summary", help="one finished day's counts, for the push")
    daily.add_argument("day")
    daily.add_argument("--data-dir", type=Path, default=Path("data"))
    daily.add_argument("--products", default=",".join(DEFAULT_PRODUCTS))

    verify = commands.add_parser("verify", help="check a recorded day's invariants")
    verify.add_argument("product")
    verify.add_argument("day")
    verify.add_argument("--data-dir", type=Path, default=Path("data"))
    return parser


async def _run(config: RecorderConfig) -> None:
    from touchstone.recorder.recorder import Recorder

    recorder = Recorder(config)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, recorder.stop)
    await recorder.run()


def _command_run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = RecorderConfig(
        data_dir=args.data_dir,
        products=tuple(args.products.split(",")),
        url=args.url,
        flush_interval_s=args.flush_interval,
        status_interval_s=args.status_interval,
        audit_interval_s=args.audit_interval,
        rtt_interval_s=args.rtt_interval,
    )
    asyncio.run(_run(config))
    return 0


def _command_healthcheck(args: argparse.Namespace) -> int:
    problems = check_status(args.data_dir, max_age_s=args.max_age)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def _command_compact(args: argparse.Namespace) -> int:
    from touchstone.recorder import storage
    from touchstone.recorder.recorder import acquire_lock

    today = storage.utc_day(time.time_ns())
    with acquire_lock(args.data_dir):
        storage.recover(args.data_dir)
        spool = args.data_dir / storage.SPOOL_DIR
        products = [p.name for p in spool.iterdir() if p.is_dir()] if spool.exists() else []
        for product in products:
            for day in storage.spooled_days(args.data_dir, product):
                if day < today or args.include_today:
                    rows = storage.compact_day(args.data_dir, product, day)
                    print(f"{product} {day}: {rows} rows")
    return 0


def _command_report(args: argparse.Namespace) -> int:
    from touchstone.recorder.report import summarize

    print(json.dumps(summarize(read_health(args.data_dir), since=args.since), indent=2))
    return 0


def _command_daily_summary(args: argparse.Namespace) -> int:
    from touchstone.recorder.report import daily_summary

    print(json.dumps(daily_summary(args.data_dir, args.day, args.products.split(","))))
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    # Prints integrity counts and no prices, so unlike research code it may read held-out days:
    # the recording has to stay checkable during the held-out week.
    from touchstone.recorder.storage import read_day
    from touchstone.recorder.verify import verify_day

    report = verify_day(read_day(args.data_dir, args.product, args.day))
    print(json.dumps(asdict(report), indent=2))
    return 1 if report.problems else 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {
        "run": _command_run,
        "healthcheck": _command_healthcheck,
        "compact": _command_compact,
        "report": _command_report,
        "daily-summary": _command_daily_summary,
        "verify": _command_verify,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
