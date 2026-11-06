"""Summarizes the health log: what went wrong, how long it lasted, and whether it recovered."""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Any


def _percentile(ordered: list[float], share: float) -> float | None:
    return ordered[min(int(share * len(ordered)), len(ordered) - 1)] if ordered else None


def _day(ts_ns: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts_ns // 10**9))


def _new_product() -> dict[str, Any]:
    return {
        "sessions": 0,
        "resyncs": Counter(),
        "gaps": [],
        "rollovers": [],
        "longest_outage_ms": None,
        "audits": 0,
        "audit_failures": 0,
        "out_of_order": 0,
        "compactions": [],
        "compaction_failures": [],
    }


def summarize(records: list[dict[str, Any]], *, since: str | None = None) -> dict[str, Any]:
    """Turns health records into a summary by product. ``since`` is a UTC day, inclusive."""
    if since is not None:
        records = [r for r in records if _day(r["ts_ns"]) >= since]

    products: dict[str, dict[str, Any]] = {}
    round_trips: dict[str, list[float]] = {}
    last_resync: dict[str, dict[str, Any]] = {}
    starts = 0
    fatal = []
    low_disk = 0

    for record in records:
        event = record["event"]
        if event == "start":
            starts += 1
        elif event == "fatal":
            fatal.append({"day": _day(record["ts_ns"]), "error": record.get("error")})
        elif event == "low_disk":
            low_disk += 1

        name = record.get("product")
        if name is None:
            continue
        product = products.setdefault(name, _new_product())
        if event == "synced":
            product["sessions"] += 1
            outage = record.get("outage_ms")
            if outage is not None:
                longest = product["longest_outage_ms"]
                product["longest_outage_ms"] = max(outage, longest or 0)
                resync = last_resync.pop(name, None)
                if resync and resync["reason"] == "day_rollover":
                    product["rollovers"].append({"day": resync["day"], "outage_ms": outage})
        elif event == "resync":
            product["resyncs"][record["reason"]] += 1
            last_resync[name] = {"reason": record["reason"], "day": record.get("day")}
        elif event == "gap":
            product["gaps"].append(
                {"ts_ns": record["ts_ns"], "lost": record["lost"], "session": record["session"]}
            )
        elif event == "out_of_order":
            product["out_of_order"] += 1
        elif event == "audit":
            product["audits"] += 1
        elif event == "audit_failed":
            product["audit_failures"] += 1
        elif event == "compacted":
            product["compactions"].append({"day": record["day"], "rows": record["rows"]})
        elif event == "compaction_failed":
            product["compaction_failures"].append(
                {"day": record["day"], "error": record.get("error")}
            )
        elif event == "rtt":
            round_trips.setdefault(name, []).append(record["rtt_ms"])

    for name, product in products.items():
        product["resyncs"] = dict(product["resyncs"])
        samples = sorted(round_trips.get(name, []))
        product["rtt_ms"] = {
            "samples": len(samples),
            "p50": _percentile(samples, 0.50),
            "p99": _percentile(samples, 0.99),
        }

    return {
        "first_ns": records[0]["ts_ns"] if records else None,
        "last_ns": records[-1]["ts_ns"] if records else None,
        "process_starts": starts,
        "fatal": fatal,
        "low_disk_warnings": low_disk,
        "products": products,
    }


def daily_summary(root: Path, day: str, products: list[str]) -> dict[str, Any]:
    """One finished day's counts and problems, for the daily push. It holds no prices."""
    import polars as pl

    from touchstone.recorder.health import read_health
    from touchstone.recorder.storage import daily_path

    health = summarize(read_health(root, day=day))
    problems = [f"Recorder failed: {failure['error']}" for failure in health["fatal"]]
    if health["low_disk_warnings"]:
        problems.append("The server is low on disk.")

    result: dict[str, Any] = {}
    for product in products:
        stats = health["products"].get(product, _new_product())
        rows: dict[str, int] = {}
        missing = 0
        path = daily_path(root, product, day)
        if path.exists():
            scan = pl.scan_parquet(path)
            counts = scan.group_by("event").len().collect(engine="streaming")
            rows = {str(event): count for event, count in counts.iter_rows()}
            first, last, unique = (
                scan.filter(pl.col("trade_id").is_not_null())
                .select(
                    pl.col("trade_id").min().alias("first"),
                    pl.col("trade_id").max().alias("last"),
                    pl.col("trade_id").n_unique().alias("unique"),
                )
                .collect(engine="streaming")
                .row(0)
            )
            missing = last - first + 1 - unique if unique else 0
        else:
            problems.append(f"{product}: no merged file for {day}.")
        resyncs = stats["resyncs"]
        result[product] = {
            "rows": rows,
            "gaps": len(stats["gaps"]),
            "lost_messages": sum(gap["lost"] for gap in stats["gaps"]),
            "reconnects": sum(
                count for reason, count in resyncs.items() if reason != "day_rollover"
            ),
            "longest_outage_ms": stats["longest_outage_ms"],
            "missing_trade_ids": missing,
            "audit_failures": stats["audit_failures"],
        }
    return {
        "day": day,
        "process_starts": health["process_starts"],
        "problems": problems,
        "products": result,
    }
