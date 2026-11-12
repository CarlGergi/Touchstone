"""The whole experiment run, as make reproduce does it, on fake recorded days.

It checks that one command produces every headline number, that a product with too little
development data is reported rather than breaking the run, and that held-out days are never opened.
"""

import json

import pytest

from test_holdout import record, round_trips
from touchstone.experiments import __main__ as experiments
from touchstone.experiments import engine_bench, figures
from touchstone.recorder.storage import daily_path


@pytest.fixture
async def recorded(tmp_path):
    for i, day in enumerate(["2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11"]):
        await record(tmp_path, "BTC-USD", day, seed=i)
        await record(tmp_path, "ETH-USD", day, seed=10 + i)
    # A wider-spread product added on the last development day: one day only.
    await record(tmp_path, "ZEC-USD", "2026-11-11", seed=20)
    # Held-out days that development must never open.
    for product in ("BTC-USD", "ZEC-USD"):
        path = daily_path(tmp_path, product, "2026-11-12")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not parquet: reading this file would fail")
    round_trips(tmp_path, [14.0, 15.0, 16.0], 0)
    round_trips(tmp_path / "latency-profiles" / "home", [36.0, 37.0, 40.0], 0)
    return tmp_path


@pytest.fixture
def quick(monkeypatch):
    """Fewer benchmark repetitions: this checks the plumbing, not the speed."""
    monkeypatch.setattr(engine_bench, "RUNS", 1)
    monkeypatch.setattr(engine_bench, "LATENCY_RUNS", 1)


async def test_one_command_writes_every_headline_number(recorded, quick, monkeypatch):
    monkeypatch.chdir(recorded)
    assert experiments.main(["--data-dir", str(recorded)]) == 0
    out = json.loads((recorded / "results" / "results.json").read_text())

    headline = out["headline"]
    assert headline["replay_events_per_s"]["value"] > 0
    assert set(headline["ring_buffer_latency_ns"]) == {"p50", "p99", "p99_9"}
    assert headline["total_events_recorded"] > 0
    assert set(headline["touch_fill_overstatement_pct"]) == {"pnl", "fill_rate"}
    assert set(headline["best_signal_ic_by_horizon"]) == {"0.1s", "1s", "10s"}
    assert set(headline["edge_disappears_latency_ms"]) == {
        "symmetric",
        "avellaneda_stoikov",
        "avellaneda_stoikov_skewed",
    }

    # Development days only, and ZEC-USD's single day is kept as development-only data.
    assert out["engine"]["days"]["BTC-USD"][-1] == "2026-11-11"
    zec = [r for r in out["fill_model"]["per_product_day"] if r["product"] == "ZEC-USD"]
    assert [r["day"] for r in zec] == ["2026-11-11"] * len(zec)
    assert "ZEC-USD" in out["fill_model"]["by_product"]


async def test_figures_are_drawn_from_the_results_and_repeat_exactly(recorded, quick, monkeypatch):
    monkeypatch.chdir(recorded)
    assert experiments.main(["--data-dir", str(recorded)]) == 0
    out = json.loads((recorded / "results" / "results.json").read_text())

    first = figures.draw(out, recorded / "a")
    second = figures.draw(out, recorded / "b")
    # No held-out section yet, so no held-out figure.
    assert [p.name for p in first] == [n for n, *_ in figures.FIGURES if n != "holdout_ic.png"]
    for a, b in zip(first, second, strict=True):
        assert a.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert a.read_bytes() == b.read_bytes()

    # Once the held-out evaluation has run, its figure is drawn too.
    out["holdout_final"] = {"signals": out["signals"]["best_signal_ic_by_horizon"]}
    names = [p.name for p in figures.draw(out, recorded / "c")]
    assert names[-1] == "holdout_ic.png"


def test_every_figure_has_alt_text():
    for name, _, alt, _ in figures.FIGURES:
        assert name.endswith(".png")
        assert len(alt) > 40
