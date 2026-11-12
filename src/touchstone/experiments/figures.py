"""Draws the README figures from results/results.json.

python -m touchstone.experiments.figures [--results results/results.json] [--out docs/images]

Every figure is drawn only from the results file, so the images change only when the results do.
The PNGs carry no timestamps, so drawing the same results twice gives identical files.
"""

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from touchstone.experiments import results  # noqa: E402

OUT = Path("docs/images")

# One style for every figure: white background, one font, recessive axes, and a fixed
# categorical order, so a hue always means the same thing in its figure.
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e6e5e1"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]  # blue, orange, aqua, yellow
WIDTH = 7.2
DPI = 200

STYLE = {
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.facecolor": "white",
    "font.family": "DejaVu Sans",
    "font.size": 10,
    "axes.edgecolor": INK_2,
    "axes.labelcolor": INK,
    "axes.labelsize": 10,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "xtick.color": INK_2,
    "ytick.color": INK_2,
    "legend.frameon": False,
    "legend.fontsize": 9,
    "svg.hashsalt": "touchstone",
}

STRATEGIES = {
    "symmetric": "Symmetric",
    "avellaneda_stoikov": "Avellaneda-Stoikov",
    "avellaneda_stoikov_skewed": "A-S skewed by signal",
}
FILL_VARIANTS = {
    "naive": "Touch fills (naive backtest)",
    "fast_trader_cancel_0": "Queue model, 0% of decreases cancel",
    "fast_trader_cancel_0.5": "Queue model, 50% cancel",
    "fast_trader_cancel_1": "Queue model, 100% cancel",
    "home_cancel_0.5": "Queue model, 50% cancel, home latency",
}
SIGNALS = {
    "ofi_0.1s": "OFI, 100 ms window",
    "ofi_1s": "OFI, 1 s window",
    "ofi_10s": "OFI, 10 s window",
    "queue_imbalance": "Queue imbalance",
}
HORIZONS = {"0.1s": "100 ms", "1s": "1 s", "10s": "10 s"}


def _errors(cells: list[dict[str, float]]) -> list[list[float]]:
    """Asymmetric error bars from {value, ci95_low, ci95_high} cells."""
    return [
        [c["value"] - c["ci95_low"] for c in cells],
        [c["ci95_high"] - c["value"] for c in cells],
    ]


def _new(height: float) -> tuple[Figure, Axes]:
    return plt.subplots(figsize=(WIDTH, height), layout="constrained")


def fill_volume(out: dict[str, Any]) -> Figure:
    """The hero: filled volume under touch fills against the queue model's variants."""
    fm = out["fill_model"]
    names = [n for n in FILL_VARIANTS if n in fm["by_variant"]]
    volume = [fm["by_variant"][n]["volume_usd"] / 1e6 for n in names]
    realistic = fm["by_variant"][fm["compared"]["realistic"]]["volume_usd"] / 1e6
    fig, ax = _new(3.2)
    y = list(range(len(names)))[::-1]
    colors = [SERIES[1] if n == "naive" else SERIES[0] for n in names]
    ax.barh(y, volume, height=0.62, color=colors)
    ax.set_yticks(y, [FILL_VARIANTS[n] for n in names])
    ax.tick_params(axis="y", length=0, labelcolor=INK)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("Filled volume, $ millions")
    for yy, v, n in zip(y, volume, names, strict=True):
        ax.text(v, yy, f"  ${v:,.1f}M", va="center", ha="left", color=INK, fontsize=9)
        if n == "naive":
            ratio = f"{v / realistic:.1f}× the 50%-cancel queue model  "
            ax.text(v, yy, ratio, va="center", ha="right", color="white", fontsize=9)
    ax.set_xlim(0, max(volume) * 1.18)
    return fig


def pnl_attribution(out: dict[str, Any]) -> Figure:
    """Where each strategy's P&L comes from, before fees, per product-day."""
    ladder = out["strategies"]["ladder"]
    parts = {
        "spread_capture": "Spread captured",
        "adverse_selection": "Adverse selection",
        "inventory": "Inventory",
        "total": "Total",
    }
    names = [n for n in STRATEGIES if n in ladder]
    fig, ax = _new(3.4)
    width = 0.8 / len(parts)
    for i, (key, label) in enumerate(parts.items()):
        cells = [ladder[n][key] for n in names]
        x = [j + (i - (len(parts) - 1) / 2) * width for j in range(len(names))]
        ax.bar(
            x,
            [c["value"] for c in cells],
            width=width * 0.9,
            color=SERIES[i],
            label=label,
            yerr=_errors(cells),
            error_kw={"ecolor": INK_2, "elinewidth": 1, "capsize": 2},
        )
    ax.axhline(0, color=INK_2, linewidth=0.8)
    ax.set_xticks(range(len(names)), [STRATEGIES[n] for n in names])
    ax.tick_params(axis="x", length=0, labelcolor=INK)
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("$ per product-day, before fees")
    ax.legend(ncols=len(parts), loc="lower center", bbox_to_anchor=(0.5, 1.0))
    return fig


def latency_sweep(out: dict[str, Any]) -> Figure:
    """Total P&L before fees as the delay grows, for each strategy."""
    sweep = out["strategies"]["latency_sweep"]
    fig, ax = _new(3.4)
    for i, name in enumerate(n for n in STRATEGIES if n in sweep):
        points = sorted(sweep[name].items(), key=lambda kv: float(kv[0].removesuffix("ms")))
        x = list(range(len(points)))
        cells = [c for _, c in points]
        offset = (i - 1) * 0.12
        ax.errorbar(
            [xx + offset for xx in x],
            [c["value"] for c in cells],
            yerr=_errors(cells),
            color=SERIES[i],
            linewidth=2,
            marker="o",
            markersize=4,
            elinewidth=1,
            capsize=2,
            label=STRATEGIES[name],
        )
        ax.set_xticks(x, [k.replace("ms", " ms") for k, _ in points])
    ax.axhline(0, color=INK_2, linewidth=0.8)
    ax.set_xlabel("Latency, market data and orders (not to scale)")
    ax.set_ylabel("Total P&L, $ per product-day")
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncols=3)
    return fig


def signal_ic(out: dict[str, Any]) -> Figure:
    """Mean information coefficient of each signal at each horizon."""
    by_h = out["signals"]["by_horizon"]
    horizons = [h for h in HORIZONS if h in by_h]
    names = [n for n in SIGNALS if all(n in by_h[h]["all_variants_mean_ic"] for h in horizons)]
    fig, ax = _new(3.4)
    width = 0.8 / len(names)
    for i, name in enumerate(names):
        cells = [by_h[h]["all_variants_mean_ic"][name] for h in horizons]
        x = [j + (i - (len(names) - 1) / 2) * width for j in range(len(horizons))]
        ax.bar(
            x,
            [c["value"] for c in cells],
            width=width * 0.9,
            color=SERIES[i],
            label=SIGNALS[name],
            yerr=_errors(cells),
            error_kw={"ecolor": INK_2, "elinewidth": 1, "capsize": 2},
        )
    ax.axhline(0, color=INK_2, linewidth=0.8)
    ax.set_xticks(range(len(horizons)), [f"Next {HORIZONS[h]}" for h in horizons])
    ax.tick_params(axis="x", length=0, labelcolor=INK)
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("Information coefficient (Spearman)")
    ax.legend(ncols=len(names), loc="lower center", bbox_to_anchor=(0.5, 1.0))
    return fig


def replay_speed(out: dict[str, Any]) -> Figure:
    """Replay throughput of the three books on the same days."""
    eng = out["engine"]
    rows = [
        ("Python, SortedDict", eng["replay_events_per_s_python"], SERIES[1]),
        ("C++, std::map", eng["replay_events_per_s_cpp_map_book"], SERIES[2]),
        ("C++, flat tick-indexed book", eng["replay_events_per_s_cpp"], SERIES[0]),
    ]
    fig, ax = _new(2.4)
    y = list(range(len(rows)))
    cells = [r[1] for r in rows]
    ax.barh(
        y,
        [c["value"] / 1e6 for c in cells],
        height=0.6,
        color=[r[2] for r in rows],
        xerr=[[e / 1e6 for e in side] for side in _errors(cells)],
        error_kw={"ecolor": INK_2, "elinewidth": 1, "capsize": 2},
    )
    ax.set_yticks(y, [r[0] for r in rows])
    ax.tick_params(axis="y", length=0, labelcolor=INK)
    ax.grid(axis="y", visible=False)
    for yy, c in zip(y, cells, strict=True):
        ax.text(c["ci95_high"] / 1e6, yy, f"  {c['value'] / 1e6:,.1f}M/s", va="center", fontsize=9)
    ax.set_xlim(0, max(c["ci95_high"] for c in cells) / 1e6 * 1.2)
    ax.set_xlabel("Events replayed per second, millions")
    return fig


def holdout_ic(out: dict[str, Any]) -> Figure:
    """Out-of-sample IC on development days against the one held-out window."""
    dev = out["signals"]["best_signal_ic_by_horizon"]
    held = out["holdout_final"]["signals"]
    horizons = [h for h in HORIZONS if h in dev and h in held]
    fig, ax = _new(3.0)
    width = 0.36
    for i, (label, source) in enumerate(
        [("Development, walk-forward", dev), ("Held-out window, Nov 12-14", held)]
    ):
        cells = [source[h] for h in horizons]
        x = [j + (i - 0.5) * width for j in range(len(horizons))]
        ax.bar(
            x,
            [c["value"] for c in cells],
            width=width * 0.9,
            color=SERIES[i],
            label=label,
            yerr=_errors(cells),
            error_kw={"ecolor": INK_2, "elinewidth": 1, "capsize": 2},
        )
    ax.axhline(0, color=INK_2, linewidth=0.8)
    ax.set_xticks(range(len(horizons)), [f"Next {HORIZONS[h]}" for h in horizons])
    ax.tick_params(axis="x", length=0, labelcolor=INK)
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("Information coefficient, chosen signal")
    ax.legend(ncols=2, loc="lower center", bbox_to_anchor=(0.5, 1.0))
    return fig


# Name, drawing function, alt text, and the section it needs in the results file. The README
# uses these files with the same alt text.
FIGURES: list[tuple[str, Callable[[dict[str, Any]], Figure], str, str]] = [
    (
        "fill_volume.png",
        fill_volume,
        "Horizontal bars of filled dollar volume: the touch-fill backtest fills several times "
        "the volume of every queue-model variant.",
        "fill_model",
    ),
    (
        "pnl_attribution.png",
        pnl_attribution,
        "Grouped bars of spread captured, adverse selection, inventory and total P&L for three "
        "strategies, with 95% intervals; adverse selection is the largest loss for each.",
        "strategies",
    ),
    (
        "latency_sweep.png",
        latency_sweep,
        "Lines of total P&L against latency from 0 to 100 ms for three strategies, with 95% "
        "intervals; every line stays below zero.",
        "strategies",
    ),
    (
        "signal_ic.png",
        signal_ic,
        "Grouped bars of information coefficient for order-flow imbalance and queue imbalance "
        "at three horizons, with 95% intervals; queue imbalance is highest at each horizon.",
        "signals",
    ),
    (
        "replay_speed.png",
        replay_speed,
        "Horizontal bars of replay throughput for a Python book, a C++ std::map book and the "
        "C++ flat book, with 95% intervals.",
        "engine",
    ),
    (
        "holdout_ic.png",
        holdout_ic,
        "Paired bars of the chosen signal's information coefficient on development days and "
        "on the held-out window, at three horizons, with 95% intervals.",
        "holdout_final",
    ),
]


def draw(out: dict[str, Any], directory: Path) -> list[Path]:
    """Draw every figure whose section is present. Returns the files written."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    with plt.rc_context(STYLE):
        for name, function, alt, section in FIGURES:
            if section not in out:
                continue
            fig = function(out)
            fig.get_axes()[0].set_label(alt)
            path = directory / name
            fig.savefig(path, dpi=DPI, metadata={"Software": None})
            plt.close(fig)
            written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m touchstone.experiments.figures")
    parser.add_argument("--results", type=Path, default=results.RESULTS)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    for path in draw(json.loads(args.results.read_text()), args.out):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
