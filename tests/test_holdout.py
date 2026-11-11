"""The held-out evaluations: what each one reads, what it fits on, and that it runs only once."""

import json
from datetime import UTC, datetime

import polars as pl
import pytest

from fake_feed import START_NS
from test_engine import market_frame
from test_simulation import with_trades
from touchstone.experiments import holdout
from touchstone.recorder.health import HealthLog
from touchstone.recorder.storage import DayStore, daily_path

SECOND = 10**9
TIMESTAMPS = ("local_ts_ns", "server_ts_ns", "exchange_ts_ns")


def ns(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()) * SECOND


async def record(root, product, day, seed):
    """A fake day of book and trades, moved to an hour into the given UTC day."""
    frame, market, _ = market_frame(steps=1500, seed=seed)
    frame = with_trades(frame, market)
    shift = ns(day) + 3600 * SECOND - START_NS
    frame = frame.with_columns([(pl.col(c) + shift) for c in TIMESTAMPS])
    store = DayStore(root, product)
    store.append(day, frame.rows())
    await store.flush()


def round_trips(root, ms, at):
    log = HealthLog(root, now_ns=lambda: at)
    for value in ms:
        log.emit("rtt", product="BTC-USD", session=1, rtt_ms=value)
    log.close()


@pytest.fixture
async def recorded(tmp_path):
    for i, day in enumerate(["2026-11-12", "2026-11-13", "2026-11-14", "2026-11-15", "2026-11-16"]):
        await record(tmp_path, "BTC-USD", day, seed=i)
    # A wider-spread product, recorded through the final window and into December.
    for i, day in enumerate(["2026-11-13", "2026-11-14", "2026-11-16", "2026-12-02"]):
        await record(tmp_path, "ZEC-USD", day, seed=10 + i)
    await record(tmp_path, "BTC-USD", "2026-12-02", seed=20)
    # A day between the windows that must never be opened.
    gap = daily_path(tmp_path, "BTC-USD", "2026-11-25")
    gap.parent.mkdir(parents=True, exist_ok=True)
    gap.write_bytes(b"not parquet: reading this file would fail")
    round_trips(tmp_path, [14.0, 15.0, 16.0], ns("2026-11-10"))
    round_trips(tmp_path / "latency-profiles" / "home", [36.0, 37.0, 40.0], ns("2026-11-10"))
    return tmp_path


async def test_a_window_must_be_chosen(recorded):
    with pytest.raises(ValueError, match="exactly one"):
        holdout.run(recorded, results_path=recorded / "r.json")
    with pytest.raises(ValueError, match="exactly one"):
        holdout.run(recorded, final=True, confirm=True, results_path=recorded / "r.json")


async def test_final_fits_on_development_days_and_tests_on_the_four_days(recorded):
    out = holdout.run(recorded, final=True, results_path=recorded / "r.json")
    [choice] = out["choices"]
    assert choice["product"] == "BTC-USD"
    assert choice["fit_days"] == ["2026-11-12", "2026-11-13", "2026-11-14"]
    assert choice["test_days"] == ["2026-11-15", "2026-11-16"]
    assert set(choice["signal_by_horizon"]) == {"0.1s", "1s", "10s"}
    assert out["window"]["test"] == ["2026-11-15T00:00:00+00:00", "2026-11-19T00:00:00+00:00"]


async def test_wider_spread_products_stay_out_of_the_final_window(recorded):
    out = holdout.run(recorded, final=True, results_path=recorded / "r.json")
    assert out["products"] == ["BTC-USD"]
    tested = {r["product"] for r in out["fill_model"]["per_product_day"]}
    assert tested == {"BTC-USD"}


async def test_result_has_every_number_with_its_interval(recorded):
    out = holdout.run(recorded, final=True, results_path=recorded / "r.json")
    assert {"value", "ci95_low", "ci95_high"} <= set(out["fill_model"]["pnl_overstatement_pct"])
    assert set(out["signals"]) == {"0.1s", "1s", "10s"}
    assert set(out["strategies"]["ladder"]) == {
        "symmetric",
        "avellaneda_stoikov",
        "avellaneda_stoikov_skewed",
    }
    assert "retail_maker_50bps" in out["strategies"]["ladder"]["symmetric"]["total_by_fee"]


async def test_each_window_runs_only_once(recorded):
    path = recorded / "r.json"
    holdout.run(recorded, final=True, results_path=path)
    first = json.loads(path.read_text())["holdout_final"]
    with pytest.raises(RuntimeError, match="already been run"):
        holdout.run(recorded, final=True, results_path=path)
    assert json.loads(path.read_text())["holdout_final"] == first


async def test_confirmation_includes_the_wider_spread_products(recorded):
    out = holdout.run(recorded, confirm=True, results_path=recorded / "r.json")
    by_product = {c["product"]: c for c in out["choices"]}
    assert set(by_product) == {"BTC-USD", "ZEC-USD"}
    # ZEC fits on everything before the final window ends, which for it was never held out.
    assert by_product["ZEC-USD"]["fit_days"] == ["2026-11-13", "2026-11-14", "2026-11-16"]
    assert by_product["ZEC-USD"]["test_days"] == ["2026-12-02"]
    assert by_product["BTC-USD"]["test_days"] == ["2026-12-02"]


async def test_product_without_enough_fitting_days_is_skipped_and_reported(recorded):
    await record(recorded, "NEAR-USD", "2026-12-02", seed=30)
    out = holdout.run(recorded, confirm=True, results_path=recorded / "r.json")
    assert [s["product"] for s in out["skipped"]] == ["ETH-USD", "NEAR-USD"]


def test_command_insists_on_a_window(capsys):
    with pytest.raises(SystemExit):
        holdout.main([])
    assert "--final or --confirm" in capsys.readouterr().err
