"""The held-out windows, which must stay out of research code unless explicitly opened."""

import ast
import pathlib
import warnings
from datetime import UTC, datetime, timedelta, timezone

import polars as pl
import pytest

import touchstone
from touchstone.data import (
    CONFIRM_END,
    CONFIRM_START,
    DEVELOPMENT_END,
    DEVELOPMENT_END_NS,
    FINAL_END,
    FINAL_START,
    HoldoutError,
    allowed,
    load,
    main,
)
from touchstone.recorder.storage import DayStore, compact_day, daily_path

PRODUCT = "BTC-USD"
SECOND = 10**9
DAY = 86_400 * SECOND
CUT = DEVELOPMENT_END_NS
FINAL_CUT = CUT + 4 * DAY
CONFIRM = CUT + 16 * DAY


def ns(moment):
    return int(moment.timestamp()) * SECOND


def row(local, *, exchange=None, server=None, event="update", trade_id=None):
    exchange = local if exchange is None else exchange
    server = local if server is None else server
    return (local, server, exchange, 1, local, event, "bid", 100, 5, trade_id)


def day_of(ts):
    return datetime.fromtimestamp(ts // SECOND, UTC).strftime("%Y-%m-%d")


async def store(root, *rows):
    days = DayStore(root, PRODUCT)
    for r in rows:
        days.append(day_of(r[0]), [r])
    await days.flush()


async def recorded_everywhere(root):
    """One row just inside and just outside every boundary."""
    await store(
        root,
        row(CUT - 1),
        row(CUT),
        row(FINAL_CUT - 1),
        row(FINAL_CUT),
        row(CONFIRM - 1),
        row(CONFIRM),
        row(ns(CONFIRM_END) - 1),
        row(ns(CONFIRM_END)),
    )


def locals_of(frame):
    return frame.collect()["local_ts_ns"].to_list()


def test_windows_are_the_agreed_dates():
    assert datetime(2026, 11, 15, tzinfo=UTC) == DEVELOPMENT_END
    assert FINAL_START == DEVELOPMENT_END
    assert datetime(2026, 11, 19, tzinfo=UTC) == FINAL_END, "four full days, evaluated on Nov 19"
    assert datetime(2026, 12, 1, tzinfo=UTC) == CONFIRM_START
    assert timedelta(days=7) == CONFIRM_END - CONFIRM_START
    assert ns(FINAL_END) == FINAL_CUT
    assert ns(CONFIRM_START) == CONFIRM


async def test_development_only_by_default(tmp_path):
    await recorded_everywhere(tmp_path)
    assert locals_of(load(tmp_path, PRODUCT)) == [CUT - 1]


async def test_final_opens_the_four_days_and_nothing_after(tmp_path):
    await recorded_everywhere(tmp_path)
    with pytest.warns(UserWarning, match="final evaluation"):
        frame = load(tmp_path, PRODUCT, final=True)
    assert locals_of(frame) == [CUT - 1, CUT, FINAL_CUT - 1]


async def test_confirm_opens_the_december_week_but_never_the_gap(tmp_path):
    await recorded_everywhere(tmp_path)
    with pytest.warns(UserWarning, match="confirmation"):
        december = locals_of(load(tmp_path, PRODUCT, start=CONFIRM_START, confirm=True))
    assert december == [CONFIRM, ns(CONFIRM_END) - 1]
    with pytest.warns(UserWarning):
        earlier = locals_of(load(tmp_path, PRODUCT, end=FINAL_END, confirm=True))
    assert earlier == [CUT - 1, CUT, FINAL_CUT - 1]


@pytest.mark.parametrize(
    ("kwargs", "flags"),
    [
        ({"end": "2026-11-15T00:00:00.000001Z"}, {}),
        ({"start": "2026-11-15"}, {}),
        ({"end": "2026-11-14T19:00:00.000001-05:00"}, {}),
        ({"end": datetime(2026, 11, 14, 20, 0, tzinfo=timezone(timedelta(hours=-5)))}, {}),
        ({"start": "2026-12-01"}, {}),
        ({"end": "2026-11-19T00:00:00.000001Z"}, {"final": True}),
        ({"start": "2026-12-01"}, {"final": True}),
        ({"start": "2026-11-20", "end": "2026-11-21"}, {"final": True}),
        ({"start": "2026-11-20", "end": "2026-11-21"}, {"confirm": True}),
        ({"end": "2026-12-08"}, {"confirm": True}),
        ({"start": "2026-12-01", "end": "2026-12-08T00:00:00.000001Z"}, {"confirm": True}),
    ],
)
async def test_any_range_reaching_a_closed_window_is_refused(tmp_path, kwargs, flags):
    await recorded_everywhere(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(HoldoutError, match="not open"):
            load(tmp_path, PRODUCT, **kwargs, **flags)


async def test_refusal_needs_no_data_on_disk(tmp_path):
    with pytest.raises(HoldoutError):
        load(tmp_path, PRODUCT, start="2026-11-15")


@pytest.mark.parametrize("end", ["2026-11-15", "2026-11-14T19:00:00-05:00", DEVELOPMENT_END])
async def test_a_range_ending_exactly_at_the_cutoff_is_allowed(tmp_path, end):
    await recorded_everywhere(tmp_path)
    assert locals_of(load(tmp_path, PRODUCT, end=end)) == [CUT - 1]


async def test_closed_windows_files_are_not_even_opened(tmp_path):
    await store(tmp_path, row(CUT - 1))
    for day in ("2026-11-15", "2026-11-20", "2026-12-01"):
        path = daily_path(tmp_path, PRODUCT, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not parquet: reading this file would fail")
    assert locals_of(load(tmp_path, PRODUCT)) == [CUT - 1]
    with pytest.warns(UserWarning), pytest.raises(pl.exceptions.ComputeError):
        load(tmp_path, PRODUCT, final=True).collect()


@pytest.mark.parametrize("clock", ["exchange", "server"])
async def test_rows_past_a_boundary_on_any_clock_are_removed(tmp_path, clock):
    # Clock skew can give a row received just before midnight a later exchange or server time.
    await store(tmp_path, row(CUT - 9), row(CUT - 5, **{clock: CUT + 5}))
    assert locals_of(load(tmp_path, PRODUCT)) == [CUT - 9]


@pytest.mark.parametrize("value", ["yes", "true", 1, "False", [1]])
async def test_only_true_opens_a_window(tmp_path, value):
    await recorded_everywhere(tmp_path)
    with pytest.raises(HoldoutError):
        load(tmp_path, PRODUCT, end="2026-11-16", final=value)
    with pytest.raises(HoldoutError):
        load(tmp_path, PRODUCT, start="2026-12-01", confirm=value)


def test_allowed_matches_the_windows():
    assert allowed(CUT - 1) and not allowed(CUT)
    assert allowed(FINAL_CUT - 1, final=True) and not allowed(FINAL_CUT, final=True)
    assert not allowed(CONFIRM, final=True)
    assert allowed(CONFIRM, confirm=True) and not allowed(CONFIRM - 1, confirm=True)
    assert not allowed(ns(CONFIRM_END), confirm=True)


async def test_no_warning_on_an_ordinary_load(tmp_path):
    await recorded_everywhere(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        load(tmp_path, PRODUCT)


async def test_range_is_half_open_on_the_local_clock(tmp_path):
    start, end = CUT - 100_000, CUT - 50_000
    await store(tmp_path, row(start - 1), row(start, exchange=start - 1000), row(end - 1), row(end))
    frame = load(tmp_path, PRODUCT, start=_iso(start), end=_iso(end))
    assert locals_of(frame) == [start, end - 1]


def _iso(ts_ns):
    seconds, nanos = divmod(ts_ns, 10**9)
    assert nanos % 1000 == 0
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=nanos // 1000).isoformat()


async def test_trades_repeated_across_a_day_boundary_are_removed(tmp_path):
    first = CUT - 2 * DAY
    await store(
        tmp_path,
        row(first, event="trade", trade_id=7),
        row(first + DAY, event="trade_snapshot", trade_id=7),
        row(first + DAY + 1, event="trade", trade_id=8),
    )
    trades = load(tmp_path, PRODUCT).collect()
    assert trades["trade_id"].to_list() == [7, 8]


async def test_merged_and_unmerged_days_read_the_same(tmp_path):
    await store(tmp_path, row(CUT - 5 * DAY), row(CUT - 1))
    before = locals_of(load(tmp_path, PRODUCT))
    compact_day(tmp_path, PRODUCT, day_of(CUT - 5 * DAY))
    assert locals_of(load(tmp_path, PRODUCT)) == before


def test_missing_data_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match=PRODUCT):
        load(tmp_path, PRODUCT, end="2026-11-14")


async def test_backwards_range_is_an_error(tmp_path):
    await recorded_everywhere(tmp_path)
    with pytest.raises(ValueError, match="before end"):
        load(tmp_path, PRODUCT, start="2026-11-14", end="2026-11-13")


async def test_command_refuses_without_a_flag(tmp_path, capsys):
    await recorded_everywhere(tmp_path)
    assert main(["summary", PRODUCT, "--data-dir", str(tmp_path), "--end", "2026-11-19"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not open" in captured.err


async def test_command_reads_development_data_by_default(tmp_path, capsys):
    await recorded_everywhere(tmp_path)
    assert main(["summary", PRODUCT, "--data-dir", str(tmp_path)]) == 0
    assert '"rows": 1' in capsys.readouterr().out


async def test_final_and_confirm_flags_open_their_windows(tmp_path, capsys):
    await recorded_everywhere(tmp_path)
    with pytest.warns(UserWarning):
        assert main(["summary", PRODUCT, "--data-dir", str(tmp_path), "--final"]) == 0
    assert '"rows": 3' in capsys.readouterr().out
    argv = ["summary", PRODUCT, "--data-dir", str(tmp_path), "--start", "2026-12-01", "--confirm"]
    with pytest.warns(UserWarning):
        assert main(argv) == 0
    assert '"rows": 2' in capsys.readouterr().out


def test_final_and_confirm_cannot_be_combined():
    with pytest.raises(SystemExit):
        main(["summary", PRODUCT, "--final", "--confirm"])


_RAW_READERS = {"read_day", "scan_days", "read_parquet", "scan_parquet"}
_ALLOWED = {"data.py", "recorder"}


def test_nothing_outside_the_recorder_reads_recordings_around_the_loader():
    package = pathlib.Path(touchstone.__file__).parent
    offenders = []
    for path in package.rglob("*.py"):
        if path.relative_to(package).parts[0] in _ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.alias):
                name = node.name.rsplit(".", 1)[-1]
            if name in _RAW_READERS:
                offenders.append(f"{path.relative_to(package)}:{node.lineno}: {name}")
    assert offenders == [], "research code must read through touchstone.data.load"
