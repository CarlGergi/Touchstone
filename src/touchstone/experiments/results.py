"""The single results file. Each experiment writes its own section and leaves the rest alone."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

RESULTS = Path("results/results.json")


def update(section: str, values: dict[str, Any], path: Path = RESULTS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(path.read_text()) if path.exists() else {}
    current[section] = values
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def headline(path: Path = RESULTS) -> dict[str, Any]:
    """The numbers asked for, gathered from the experiments' sections, each with its interval."""
    r = json.loads(path.read_text())
    engine, fills = r.get("engine", {}), r.get("fill_model", {})
    signals, strategies = r.get("signals", {}), r.get("strategies", {})
    return {
        "replay_events_per_s": engine.get("replay_events_per_s_cpp"),
        "ring_buffer_latency_ns": {
            "p50": engine.get("ring_latency_ns_p50"),
            "p99": engine.get("ring_latency_ns_p99"),
            "p99_9": engine.get("ring_latency_ns_p99_9"),
        },
        "total_events_recorded": engine.get("total_events_recorded"),
        "touch_fill_overstatement_pct": {
            "pnl": fills.get("pnl_overstatement_pct"),
            "fill_rate": fills.get("fill_rate_overstatement_pct"),
        },
        "edge_disappears_latency_ms": strategies.get("edge_disappears_ms"),
        "best_signal_ic_by_horizon": signals.get("best_signal_ic_by_horizon"),
        "best_signal_by_horizon": {
            h: max(v["variants_chosen"], key=v["variants_chosen"].get)
            for h, v in signals.get("by_horizon", {}).items()
        },
        "how_to_read": (
            "value with ci95_low and ci95_high. total_events_recorded is an exact count. An edge "
            "latency of 0 means no strategy made money even with no latency."
        ),
    }
