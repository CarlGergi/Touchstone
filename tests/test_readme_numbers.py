"""The README's numbers come from the results file, and only the numbers change."""

import json

import pytest

from touchstone.experiments import readme

RESULTS = {
    "engine": {
        "days": {"BTC-USD": ["2026-11-06", "2026-11-11"]},
        "timing_days": {"BTC-USD": ["2026-11-10", "2026-11-11"]},
        "replay_events_per_s_cpp": {"value": 9.26e7, "ci95_low": 8.59e7, "ci95_high": 9.93e7},
        "ring_latency_ns_p50": {"value": 125.0, "ci95_low": 125.0, "ci95_high": 125.0},
        "ring_latency_ns_p99": {"value": 11583.0, "ci95_low": 8249.0, "ci95_high": 16043.0},
        "book_vs_audit_snapshots": {"consistent": 754, "checked": 754},
        "machine": {"load_average_1m_before": 1.2, "load_average_1m": 1.5},
    },
    "fill_model": {
        "fill_rate_overstatement_pct": {"value": 316.7, "ci95_low": 208.4, "ci95_high": 493.0},
    },
    "strategies": {"edge_disappears_ms": {"symmetric": {"value": 0.0}}},
}


def test_numbers_are_filled_and_the_prose_is_kept():
    text = "Claims <!--n:fill.ratio-->old<!--/n--> the volume, over <!--n:dev.days-->x<!--/n-->."
    assert readme.render(text, RESULTS) == (
        "Claims <!--n:fill.ratio-->4.2×<!--/n--> the volume, over "
        "<!--n:dev.days-->2026-11-06 to 2026-11-11<!--/n-->."
    )


def test_rendering_twice_changes_nothing():
    text = "".join(f"<!--n:{name}-->?<!--/n-->\n" for name in readme.NUMBERS)
    once = readme.render(text, RESULTS)
    assert readme.render(once, RESULTS) == once


def test_a_number_not_computed_yet_reads_pending():
    text = "<!--n:holdout.fill.ratio-->old<!--/n-->"
    assert readme.render(text, RESULTS) == "<!--n:holdout.fill.ratio-->pending<!--/n-->"


def test_an_unknown_marker_is_an_error():
    with pytest.raises(KeyError):
        readme.render("<!--n:no.such.number-->x<!--/n-->", RESULTS)


def test_latency_keeps_one_unit_per_interval():
    out = readme.render("<!--n:ring.p99-->x<!--/n-->", RESULTS)
    assert "11.6 µs (95% CI 8.2 µs to 16.0 µs)" in out
    assert readme.render("<!--n:edge-->x<!--/n-->", RESULTS) == "<!--n:edge-->0 ms<!--/n-->"


def test_timing_days_are_reported_separately():
    assert readme.NUMBERS["bench.days"](RESULTS) == "2026-11-10 to 2026-11-11"
    assert readme.NUMBERS["bench.load"](RESULTS) == "1.2 before, 1.5 after"


def test_every_marker_in_the_readme_is_known():
    text = (readme.README if readme.README.is_absolute() else _repo() / readme.README).read_text()
    for name in readme.MARKER.findall(text):
        assert name[0] in readme.NUMBERS


def test_main_rewrites_the_file(tmp_path):
    (tmp_path / "r.json").write_text(json.dumps(RESULTS))
    (tmp_path / "README.md").write_text("<!--n:audits-->?<!--/n-->")
    args = ["--results", str(tmp_path / "r.json"), "--readme", str(tmp_path / "README.md")]
    assert readme.main(args) == 0
    assert (tmp_path / "README.md").read_text() == "<!--n:audits-->754 / 754<!--/n-->"


def _repo():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent
