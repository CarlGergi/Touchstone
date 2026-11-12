"""Choosing which recorded days to copy down, and sizing the copy before it starts."""

import pytest

from touchstone import pull


def test_days_are_half_open():
    assert pull.days("2026-11-12", "2026-11-15") == ["2026-11-12", "2026-11-13", "2026-11-14"]
    with pytest.raises(ValueError):
        pull.days("2026-11-15", "2026-11-12")


def test_filters_exclude_everything_then_add_each_day_back():
    assert pull.filters("2026-11-10", "2026-11-12") == [
        "--exclude",
        "*",
        "--include",
        "*2026-11-10*",
        "--include",
        "*2026-11-11*",
    ]
    assert pull.filters(None, None) == []
    with pytest.raises(ValueError):
        pull.filters(None, "2026-11-12")


def listing(*rows):
    return [f"recordings/{key}\t{size}\n" for key, size in rows]


def test_the_development_pull_never_includes_held_out_days(tmp_path):
    rows = listing(
        ("BTC-USD/2026-11-11.parquet", 100),
        ("BTC-USD/2026-11-12.parquet", 1000),
        ("health/2026-11-11.jsonl", 10),
        ("health/2026-11-12.jsonl", 10_000),
    )
    assert pull.plan(rows, tmp_path, "2026-11-01", "2026-11-12") == (2, 110)
    assert pull.plan(rows, tmp_path, "2026-11-12", "2026-11-15") == (2, 11_000)
    assert pull.plan(rows, tmp_path, None, None) == (4, 11_110)


def test_files_already_held_at_the_same_size_are_not_counted(tmp_path):
    (tmp_path / "BTC-USD").mkdir()
    (tmp_path / "BTC-USD" / "2026-11-11.parquet").write_bytes(b"x" * 100)
    (tmp_path / "BTC-USD" / "2026-11-10.parquet").write_bytes(b"x" * 5)
    rows = listing(("BTC-USD/2026-11-11.parquet", 100), ("BTC-USD/2026-11-10.parquet", 100))
    assert pull.plan(rows, tmp_path, None, None) == (1, 100)


def test_an_empty_bucket_plans_nothing(tmp_path):
    assert pull.plan(["None\n"], tmp_path, None, None) == (0, 0)


def test_plan_refuses_above_the_limit(tmp_path, monkeypatch, capsys):
    import io

    monkeypatch.setattr(
        "sys.stdin", io.StringIO("recordings/BTC-USD/2026-11-12.parquet\t3000000000\n")
    )
    assert pull.main(["plan", "--dest", str(tmp_path), "--max-gb", "2"]) == 1
    assert "refusing" in capsys.readouterr().err
