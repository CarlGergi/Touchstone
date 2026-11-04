import json

from touchstone.recorder.health import HealthLog, check_status, read_health, write_status

NOW = 1_793_751_274_000_000_000
SECOND = 10**9


def status(updated=NOW, last_message=NOW, state="live"):
    return {
        "updated_ns": updated,
        "products": {"BTC-USD": {"state": state, "last_message_ns": last_message}},
    }


def test_records_are_appended_as_json_lines(tmp_path):
    log = HealthLog(tmp_path, now_ns=lambda: NOW)
    log.emit("gap", product="BTC-USD", expected=5, got=7)
    log.emit("resync", reason="gap")
    log.close()
    path = tmp_path / "health" / "2026-11-04.jsonl"
    first, second = [json.loads(line) for line in path.read_text().splitlines()]
    assert first == {
        "ts_ns": NOW,
        "event": "gap",
        "product": "BTC-USD",
        "expected": 5,
        "got": 7,
    }
    assert second["reason"] == "gap"


def test_log_rotates_at_midnight_utc(tmp_path):
    clock = {"now": NOW}
    log = HealthLog(tmp_path, now_ns=lambda: clock["now"])
    log.emit("a")
    clock["now"] = NOW + 86_400 * SECOND
    log.emit("b")
    log.close()
    assert sorted(p.name for p in (tmp_path / "health").iterdir()) == [
        "2026-11-04.jsonl",
        "2026-11-05.jsonl",
    ]
    assert [r["event"] for r in read_health(tmp_path)] == ["a", "b"]


def test_log_can_be_reopened_and_keeps_earlier_records(tmp_path):
    for event in ("a", "b"):
        log = HealthLog(tmp_path, now_ns=lambda: NOW)
        log.emit(event)
        log.close()
    assert [r["event"] for r in read_health(tmp_path)] == ["a", "b"]


def test_fresh_status_is_healthy(tmp_path):
    write_status(tmp_path, status())
    assert check_status(tmp_path, max_age_s=60, now_ns=NOW + 30 * SECOND) == []


def test_missing_status_is_unhealthy(tmp_path):
    assert "does not exist" in check_status(tmp_path, max_age_s=60, now_ns=NOW)[0]


def test_corrupt_status_is_unhealthy(tmp_path):
    (tmp_path / "status.json").write_text("{")
    assert "not valid JSON" in check_status(tmp_path, max_age_s=60, now_ns=NOW)[0]


def test_stale_status_is_unhealthy(tmp_path):
    write_status(tmp_path, status())
    problems = check_status(tmp_path, max_age_s=60, now_ns=NOW + 61 * SECOND)
    assert any("status last updated" in p for p in problems)


def test_silent_product_is_unhealthy_even_if_status_is_fresh(tmp_path):
    late = NOW + 300 * SECOND
    write_status(tmp_path, status(updated=late, last_message=NOW, state="resyncing"))
    [problem] = check_status(tmp_path, max_age_s=60, now_ns=late)
    assert "BTC-USD" in problem
    assert "resyncing" in problem


def test_product_with_no_messages_yet_says_so(tmp_path):
    write_status(tmp_path, status(last_message=0, state="connecting"))
    [problem] = check_status(tmp_path, max_age_s=60, now_ns=NOW)
    assert "no message received yet" in problem
