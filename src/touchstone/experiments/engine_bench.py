"""Milestone 2: the replayed book against the audit snapshots, replay speed, and ring latency."""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
from pathlib import Path
from typing import Any

import numpy as np
import touchstone_engine as engine

from touchstone.experiments import results
from touchstone.experiments.days import day_frames, research_days
from touchstone.experiments.stats import bootstrap_ci, mean_ci
from touchstone.products import TICK_E8
from touchstone.replay import check_against_audits, python_book_rate, stream_arrays

PRODUCTS = ("BTC-USD", "ETH-USD")
RUNS = 5
LATENCY_RUNS = 3
PYTHON_ROWS = 2_000_000
PIPELINE_ROWS = 2_000_000
# Far above the recorded rate of about 1,000 rows a second, but below the ring's capacity, so the
# latency measured is handoff, not queueing.
PIPELINE_RATE = 200_000


TIMING_KEYS = (
    "replay_events_per_s_cpp",
    "replay_events_per_s_python",
    "replay_events_per_s_cpp_map_book",
    "replay_speedup_flat_over_map_book",
    "replay_speedup_cpp_over_python",
    "ring_pipeline_rate_per_s",
    "ring_latency_ns_p50",
    "ring_latency_ns_p99",
    "ring_latency_ns_p99_9",
    "machine",
)


def run(root: Path, *, last_days: int | None = None, checks: bool = True) -> dict[str, Any]:
    """Speed and latency on every development day, or on each product's last few.

    checks=False skips the event count and the audit check, which do not depend on the machine,
    so a short benchmark on an idle machine can replace only the timing.
    """
    load_before = os.getloadavg()[0]
    events = 0
    cpp_rates, map_rates, python_rates = [], [], []
    latency_runs: list[np.ndarray] = []
    audits = {"audits": 0, "checked": 0, "exact": 0, "consistent": 0, "unexplained_levels": 0}
    days_used: dict[str, list[str]] = {}

    for product in PRODUCTS:
        days = research_days(root, product)
        if last_days is not None:
            days = days[-last_days:]
        days_used[product] = days
        for _day, frame in day_frames(root, product, days):
            if checks:
                events += frame.filter(~frame["event"].is_in(["audit_snapshot"])).height
                check = check_against_audits(frame)
                for key in audits:
                    audits[key] += getattr(check, key)
            arrays = stream_arrays(frame)
            del frame
            gc.collect()

            # Latency first, before the Python baseline fills memory with millions of objects,
            # which measurably disturbed the handoff when it ran afterwards.
            tick = TICK_E8[product]
            head = {k: np.ascontiguousarray(v[:PIPELINE_ROWS]) for k, v in arrays.items()}
            for _ in range(LATENCY_RUNS):
                result = engine.pipeline(*head.values(), PIPELINE_RATE, tick=tick)
                latency_runs.append(np.asarray(result["latency_ns"]))

            # The two books take turns, so a change in machine load hits both alike.
            flat, by_map = [], []
            for _ in range(RUNS):
                flat.append(engine.replay_rate(*arrays.values(), tick=tick))
                by_map.append(engine.replay_rate(*arrays.values()))
            cpp_rates.append(float(np.mean(flat)))
            map_rates.append(float(np.mean(by_map)))
            head = {k: v[:PYTHON_ROWS] for k, v in arrays.items()}
            python_rates.append(python_book_rate(head))
            del head, arrays
            gc.collect()

    def percentile(share: float):
        return lambda values: float(np.percentile(values, share))

    cpp = mean_ci(cpp_rates)
    by_map = mean_ci(map_rates)
    python = mean_ci(python_rates)
    passed = audits["checked"] > 0 and audits["consistent"] == audits["checked"]
    out = {
        "days": days_used,
        "timing_days": days_used,
        "total_events_recorded": events,
        "book_vs_audit_snapshots": audits | {"passed": passed},
        "replay_events_per_s_cpp": cpp,
        "replay_events_per_s_python": python,
        "replay_events_per_s_cpp_map_book": by_map,
        "replay_speedup_flat_over_map_book": mean_ci(
            [f / m for f, m in zip(cpp_rates, map_rates, strict=True)]
        ),
        "replay_speedup_cpp_over_python": cpp["value"] / python["value"],
        "ring_pipeline_rate_per_s": PIPELINE_RATE,
        "ring_latency_ns_p50": bootstrap_ci(latency_runs, percentile(50)),
        "ring_latency_ns_p99": bootstrap_ci(latency_runs, percentile(99)),
        "ring_latency_ns_p99_9": bootstrap_ci(latency_runs, percentile(99.9)),
        "machine": {
            "platform": platform.platform(),
            "load_average_1m_before": load_before,
            "load_average_1m": os.getloadavg()[0],
        },
        "notes": (
            "Replay speed counts rows per second, reading the top of the book after every message, "
            "with a 95% interval over product-days. The C++ figure uses the flat, tick-indexed "
            "book; the std::map book is reported beside it, run in alternation. Python keeps its "
            "book in a SortedDict. Ring latency is push to applied, on the flat book, at the "
            "stated rate, with intervals from resampling runs, three per product-day. Both threads "
            "ask macOS for fast cores, and the tails still depend on what else the machine is "
            "doing. total_events_recorded counts book and trade rows, excluding audit snapshots, "
            "on the days listed. Timing is from timing_days, which may be a shorter, separate run "
            "on an idle machine (make bench); load averages are recorded before and after."
        ),
    }
    if not checks:
        return {k: out[k] for k in TIMING_KEYS} | {"timing_days": days_used}
    return out


def bench(results_path: Path, root: Path, last_days: int) -> dict[str, Any]:
    """Replaces only the timing in the engine section, keeping the counts and audits."""
    current = json.loads(results_path.read_text()).get("engine") if results_path.exists() else None
    if not current:
        raise SystemExit("run make experiments first: there is no engine section to update")
    timing = run(root, last_days=last_days, checks=False)
    results.update("engine", current | timing, results_path)
    results.update("headline", results.headline(results_path), results_path)
    return timing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m touchstone.experiments.engine_bench")
    parser.add_argument("--data-dir", type=Path, default=Path("data-server"))
    parser.add_argument("--last-days", type=int, default=2, help="per product")
    args = parser.parse_args(argv)
    timing = bench(results.RESULTS, args.data_dir, args.last_days)
    rate, ratio = timing["replay_events_per_s_cpp"], timing["replay_speedup_flat_over_map_book"]
    print(f"replay {rate['value'] / 1e6:.1f}M/s, {ratio['value']:.2f}x std::map")
    print(f"ring p50 {timing['ring_latency_ns_p50']['value']:.0f} ns, load {timing['machine']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
