"""Writes the measured numbers into README.md from results/results.json.

python -m touchstone.experiments.readme [--results results/results.json] [--readme README.md]

Each number in the README sits between markers, <!--n:name-->...<!--/n-->, and only the text
between them is replaced, so the prose around the numbers is written by hand and the numbers
cannot drift from the results file. A number whose section has not been computed yet, such as
the held-out window before its one evaluation, reads "pending".
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from touchstone.experiments import results

README = Path("README.md")
MARKER = re.compile(r"<!--n:([\w.]+)-->(.*?)<!--/n-->", re.DOTALL)
PENDING = "pending"

Cell = dict[str, float]
STRATEGIES = {
    "symmetric": "Symmetric",
    "avellaneda_stoikov": "Avellaneda-Stoikov",
    "avellaneda_stoikov_skewed": "A-S skewed by signal",
}
FILL_VARIANTS = {
    "naive": "Touch fills, no latency (naive)",
    "fast_trader_cancel_0": "Queue model, 0% of decreases cancel",
    "fast_trader_cancel_0.5": "Queue model, 50% cancel",
    "fast_trader_cancel_1": "Queue model, 100% cancel",
    "home_cancel_0.5": "Queue model, 50% cancel, home latency",
}


def _ci(cell: Cell, fmt: Callable[[float], str], sep: str = " to ") -> str:
    return f"{fmt(cell['value'])} (95% CI {fmt(cell['ci95_low'])}{sep}{fmt(cell['ci95_high'])})"


def _ratio(pct: Cell) -> Cell:
    """An overstatement in percent as a multiple: +317% is 4.2 times."""
    return {k: 1 + pct[k] / 100 for k in ("value", "ci95_low", "ci95_high")}


def _times(x: float) -> str:
    return f"{x:.1f}×"


def _pct(x: float) -> str:
    return f"{x:+.0f}%".replace("-", "−")


def _ic(x: float) -> str:
    return f"{x:.3f}"


def _usd(x: float) -> str:
    sign = "−" if x < 0 else ""
    return f"{sign}${abs(x):,.0f}"


def _millions(x: float) -> str:
    return f"{x / 1e6:.1f}M"


def _latency(cell: Cell) -> str:
    """One unit for a value and its interval, chosen by the value."""
    if cell["value"] < 10_000:
        return _ci(cell, lambda x: f"{x:,.0f} ns")
    if cell["value"] < 1_000_000:
        return _ci(cell, lambda x: f"{x / 1e3:,.1f} µs")
    return _ci(cell, lambda x: f"{x / 1e6:,.1f} ms")


def _edge(edges: dict[str, Cell]) -> str:
    values = {name: cell["value"] for name, cell in edges.items()}
    if not values:
        raise KeyError("no latency sweep")
    if values and all(v == 0 for v in values.values()):
        return "0 ms"
    return ", ".join(f"{STRATEGIES.get(n, n)} {v:g} ms" for n, v in values.items())


def _days(days: dict[str, list[str]]) -> str:
    every = sorted({d for ds in days.values() for d in ds})
    return f"{every[0]} to {every[-1]}" if every else PENDING


def _fill_table(fm: dict[str, Any]) -> str:
    rows = [
        "| Fill model | Fills | Filled volume | P&L before fees | At 40 bps maker fee |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, label in FILL_VARIANTS.items():
        v = fm["by_variant"].get(name)
        if v is None:
            continue
        pnl = v["total_pnl_usd_by_fee"]
        rows.append(
            f"| {label} | {v['fills']:,} | ${v['volume_usd'] / 1e6:,.1f}M "
            f"| {_usd(pnl['no_fees'])} | {_usd(pnl['retail_maker_40bps'])} |"
        )
    return "\n" + "\n".join(rows) + "\n"


def _strategy_table(st: dict[str, Any]) -> str:
    rows = [
        "| Strategy | Spread captured | Adverse selection | Inventory | Total before fees "
        "| Total at 40 bps |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, label in STRATEGIES.items():
        s = st["ladder"].get(name)
        if s is None:
            continue
        cells = [_usd(s[k]["value"]) for k in ("spread_capture", "adverse_selection", "inventory")]
        total = _ci(s["total"], _usd)
        fee = _usd(s["total_by_fee"]["retail_maker_40bps"]["value"])
        rows.append(f"| {label} | {' | '.join(cells)} | {total} | {fee} |")
    return "\n" + "\n".join(rows) + "\n"


def _holdout(r: dict[str, Any]) -> dict[str, Any]:
    return r["holdout_final"]


NUMBERS: dict[str, Callable[[dict[str, Any]], str]] = {
    # Development data
    "dev.days": lambda r: _days(r["engine"]["days"]),
    "dev.product_days": lambda r: str(r["fill_model"]["product_days"]),
    "events": lambda r: _millions(r["engine"]["total_events_recorded"]),
    "audits": lambda r: "{consistent:,} / {checked:,}".format(
        **r["engine"]["book_vs_audit_snapshots"]
    ),
    "fill.ratio": lambda r: _times(_ratio(r["fill_model"]["fill_rate_overstatement_pct"])["value"]),
    "fill.ratio_ci": lambda r: "{} to {}".format(
        *(
            _times(_ratio(r["fill_model"]["fill_rate_overstatement_pct"])[k])
            for k in ("ci95_low", "ci95_high")
        )
    ),
    "fill.pnl": lambda r: _ci(r["fill_model"]["pnl_overstatement_pct"], _pct),
    "fill.table": lambda r: _fill_table(r["fill_model"]),
    "edge": lambda r: _edge(r["strategies"]["edge_disappears_ms"]),
    "strategies.table": lambda r: _strategy_table(r["strategies"]),
    "ic.0.1s": lambda r: _ci(r["signals"]["best_signal_ic_by_horizon"]["0.1s"], _ic),
    "ic.1s": lambda r: _ci(r["signals"]["best_signal_ic_by_horizon"]["1s"], _ic),
    "ic.10s": lambda r: _ci(r["signals"]["best_signal_ic_by_horizon"]["10s"], _ic),
    # Engine
    "replay.flat": lambda r: _ci(r["engine"]["replay_events_per_s_cpp"], _millions),
    "replay.map": lambda r: _ci(r["engine"]["replay_events_per_s_cpp_map_book"], _millions),
    "replay.python": lambda r: _ci(r["engine"]["replay_events_per_s_python"], _millions),
    "replay.speedup": lambda r: _ci(r["engine"]["replay_speedup_flat_over_map_book"], _times),
    "replay.speedup_value": lambda r: _times(
        r["engine"]["replay_speedup_flat_over_map_book"]["value"]
    ),
    "replay.flat_value": lambda r: _millions(r["engine"]["replay_events_per_s_cpp"]["value"]),
    "ring.p50": lambda r: _latency(r["engine"]["ring_latency_ns_p50"]),
    "ring.p99": lambda r: _latency(r["engine"]["ring_latency_ns_p99"]),
    "ring.p99_9": lambda r: _latency(r["engine"]["ring_latency_ns_p99_9"]),
    "bench.days": lambda r: _days(r["engine"].get("timing_days", r["engine"]["days"])),
    "bench.load": lambda r: "{:.1f} before, {:.1f} after".format(
        r["engine"]["machine"].get("load_average_1m_before", float("nan")),
        r["engine"]["machine"]["load_average_1m"],
    ),
    # The held-out window, after its one evaluation
    "holdout.product_days": lambda r: str(_holdout(r)["fill_model"]["product_days"]),
    "holdout.fill.ratio": lambda r: _ci(
        _ratio(_holdout(r)["fill_model"]["fill_rate_overstatement_pct"]), _times
    ),
    "holdout.fill.pnl": lambda r: _ci(_holdout(r)["fill_model"]["pnl_overstatement_pct"], _pct),
    "holdout.ic.0.1s": lambda r: _ci(_holdout(r)["signals"]["0.1s"], _ic),
    "holdout.ic.1s": lambda r: _ci(_holdout(r)["signals"]["1s"], _ic),
    "holdout.ic.10s": lambda r: _ci(_holdout(r)["signals"]["10s"], _ic),
    "holdout.edge": lambda r: _edge(_holdout(r)["strategies"]["edge_disappears_ms"]),
    "holdout.strategies.table": lambda r: _strategy_table(_holdout(r)["strategies"]),
}


def render(text: str, out: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in NUMBERS:
            raise KeyError(f"README marker for an unknown number: {name}")
        try:
            value = NUMBERS[name](out)
        except (KeyError, TypeError, IndexError):
            value = PENDING
        return f"<!--n:{name}-->{value}<!--/n-->"

    return MARKER.sub(replace, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m touchstone.experiments.readme")
    parser.add_argument("--results", type=Path, default=results.RESULTS)
    parser.add_argument("--readme", type=Path, default=README)
    args = parser.parse_args(argv)
    text = args.readme.read_text()
    updated = render(text, json.loads(args.results.read_text()))
    args.readme.write_text(updated)
    filled = sum(1 for m in MARKER.finditer(updated) if m.group(2) != PENDING)
    print(f"README: {filled} of {len(MARKER.findall(updated))} numbers filled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
