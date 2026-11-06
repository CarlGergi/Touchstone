"""The two labelled round-trip profiles used by the latency sweep."""

import json

import pytest

from touchstone.data import DEVELOPMENT_END_NS
from touchstone.latency import PROFILES, main, round_trips, summary
from touchstone.recorder.health import HealthLog

BEFORE = DEVELOPMENT_END_NS - 10**9
HELD_OUT = DEVELOPMENT_END_NS + 10**9


def log_round_trips(root, ts_ns, *samples):
    log = HealthLog(root, now_ns=lambda: ts_ns)
    for sample in samples:
        log.emit("rtt", product="BTC-USD", session=1, rtt_ms=sample)
    log.emit("stats", rows=1)
    log.close()


@pytest.fixture
def recorded(tmp_path):
    log_round_trips(tmp_path, BEFORE, 1.0, 2.0, 3.0)
    log_round_trips(tmp_path / "latency-profiles" / "home", BEFORE, 30.0, 37.0, 200.0)
    return tmp_path


def test_both_profiles_exist_and_are_labelled():
    assert set(PROFILES) == {"fast_trader", "home"}
    assert PROFILES["fast_trader"].label == "fast_trader (us-east-2)"
    assert "Home" in PROFILES["home"].label


def test_each_profile_reads_its_own_samples(recorded):
    assert round_trips(recorded, "fast_trader") == [1.0, 2.0, 3.0]
    assert round_trips(recorded, "home") == [30.0, 37.0, 200.0]


def test_unknown_profile_is_refused(recorded):
    with pytest.raises(KeyError, match="fast_trader"):
        round_trips(recorded, "colocated")


def test_summary_labels_each_profile(recorded):
    result = summary(recorded)
    assert result["home"]["p50_ms"] == 37.0
    assert result["home"]["samples"] == 3
    assert "Home" in result["home"]["label"]
    assert result["fast_trader"]["p50_ms"] == 2.0


def test_empty_profile_is_reported_as_empty(tmp_path):
    log_round_trips(tmp_path, BEFORE, 1.0)
    assert summary(tmp_path)["home"] == {
        "label": PROFILES["home"].label,
        "samples": 0,
        "p50_ms": None,
        "p99_ms": None,
    }


def test_held_out_samples_stay_out_unless_final(tmp_path):
    log_round_trips(tmp_path, BEFORE, 1.0)
    log_round_trips(tmp_path, HELD_OUT, 9.0)
    assert round_trips(tmp_path, "fast_trader") == [1.0]
    assert round_trips(tmp_path, "fast_trader", final="yes") == [1.0]
    assert round_trips(tmp_path, "fast_trader", final=True) == [1.0, 9.0]


def test_command_shows_both_profiles(recorded, capsys):
    assert main(["summary", "--data-dir", str(recorded)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"fast_trader", "home"}
