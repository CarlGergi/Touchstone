"""The alert function, which runs off the server so that a dead server still raises an alert."""

import ast
import io
import json
import pathlib
import subprocess
import sys

import pytest

from touchstone.recorder import alert
from touchstone.recorder.alert import decide, handler

NOW = 1_800_000_000.0
PRODUCTS = ["BTC-USD", "ETH-USD"]


def beat(btc=NOW, eth=NOW):
    return {"written_at": NOW, "products": {"BTC-USD": btc, "ETH-USD": eth}}


def test_healthy_recorder_sends_nothing():
    assert decide(beat(), {}, PRODUCTS, NOW) == ([], {})


def test_nine_minutes_fifty_nine_is_not_yet_an_alert():
    assert decide(beat(btc=NOW - 599), {}, PRODUCTS, NOW) == ([], {})


def test_ten_minutes_without_a_write_alerts():
    notices, state = decide(beat(btc=NOW - 600), {}, PRODUCTS, NOW)
    [notice] = notices
    assert notice.priority == "high"
    assert "BTC-USD" in notice.message
    assert "10 minutes" in notice.message
    assert state == {"BTC-USD": {"first": NOW, "last": NOW}}


def test_products_are_judged_separately():
    notices, state = decide(beat(eth=NOW - 900), {}, PRODUCTS, NOW)
    assert ["ETH-USD" in n.message for n in notices] == [True]
    assert list(state) == ["ETH-USD"]


def test_dead_server_alerts_because_its_report_stops_changing():
    # The server wrote this report and then died: nothing changed but the clock.
    report = beat(btc=NOW - 3000, eth=NOW - 3000)
    report["written_at"] = NOW - 3000
    notices, _ = decide(report, {}, PRODUCTS, NOW)
    assert len(notices) == 2


def test_missing_report_alerts_for_every_product():
    notices, _ = decide(None, {}, PRODUCTS, NOW)
    assert len(notices) == 2
    assert all("no record" in n.message for n in notices)


def test_product_absent_from_the_report_alerts():
    report = {"products": {"BTC-USD": NOW}}
    notices, _ = decide(report, {}, PRODUCTS, NOW)
    assert ["ETH-USD" in n.message for n in notices] == [True]


def test_a_stall_is_not_repeated_every_minute():
    _, state = decide(beat(btc=NOW - 600), {}, PRODUCTS, NOW)
    notices, later = decide(beat(btc=NOW - 600), state, PRODUCTS, NOW + 60)
    assert notices == []
    assert later == state


def test_a_stall_is_repeated_hourly():
    _, state = decide(beat(btc=NOW - 600), {}, PRODUCTS, NOW)
    notices, later = decide(beat(btc=NOW - 600, eth=NOW + 3600), state, PRODUCTS, NOW + 3600)
    assert [n.title for n in notices] == ["Recorder still down"]
    assert later["BTC-USD"] == {"first": NOW, "last": NOW + 3600}


def test_recovery_is_announced_once():
    _, state = decide(beat(btc=NOW - 600), {}, PRODUCTS, NOW)
    notices, state = decide(beat(btc=NOW + 900, eth=NOW + 900), state, PRODUCTS, NOW + 900)
    assert [n.title for n in notices] == ["Recorder recovered"]
    assert state == {}
    assert decide(beat(btc=NOW + 960, eth=NOW + 960), state, PRODUCTS, NOW + 960) == ([], {})


class FakeS3:
    class exceptions:  # noqa: N801 - mirrors the boto3 client attribute
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects = {}
        self.puts = []

    def get_object(self, Bucket, Key):  # noqa: N803 - mirrors boto3
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}

    def put_object(self, Bucket, Key, Body):  # noqa: N803 - mirrors boto3
        self.puts.append(Key)
        self.objects[Key] = json.loads(Body)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("BUCKET", "bucket")
    monkeypatch.setenv("NTFY_TOPIC", "topic")
    monkeypatch.setenv("PRODUCTS", "BTC-USD,ETH-USD")
    monkeypatch.setenv("ACCOUNT_ID", "123")
    monkeypatch.setenv("BUDGET_NAME", "touchstone")


class FakeBudgets:
    def __init__(self, amount="7.5", fail=False):
        self.amount, self.fail = amount, fail

    def describe_budget(self, AccountId, BudgetName):  # noqa: N803 - mirrors boto3
        if self.fail:
            raise RuntimeError("access denied")
        assert (AccountId, BudgetName) == ("123", "touchstone")
        return {
            "Budget": {"CalculatedSpend": {"ActualSpend": {"Amount": self.amount, "Unit": "USD"}}}
        }


def run(s3, posts, now=NOW, budgets=None):
    return handler(
        {},
        None,
        s3=s3,
        budgets=budgets or FakeBudgets(),
        post=lambda topic, n: posts.append((topic, n)),
        now=lambda: now,
    )


def test_handler_alerts_and_saves_state():
    s3 = FakeS3()
    s3.objects = {alert.HEARTBEAT_KEY: beat(btc=NOW - 700)}
    posts = []
    assert run(s3, posts) == {"notices": 1}
    assert posts[0][0] == "topic"
    assert list(s3.objects[alert.STATE_KEY]["alerts"]) == ["BTC-USD"]
    assert run(s3, posts, NOW + 60) == {"notices": 0}
    assert len(posts) == 1


def test_handler_treats_a_missing_report_as_a_stall():
    s3 = FakeS3()
    posts = []
    assert run(s3, posts) == {"notices": 2}


def test_handler_does_not_write_when_nothing_changed():
    s3 = FakeS3()
    s3.objects = {alert.HEARTBEAT_KEY: beat()}
    run(s3, [])
    assert s3.puts == [alert.STATE_KEY], "the first run records where the digest starts"
    run(s3, [])
    assert s3.puts == [alert.STATE_KEY]


def test_failed_push_is_retried_on_the_next_run():
    s3 = FakeS3()
    s3.objects = {alert.HEARTBEAT_KEY: beat(btc=NOW - 700)}

    def fail(_topic, _notice):
        raise OSError("ntfy is down")

    with pytest.raises(OSError, match="ntfy"):
        handler({}, None, s3=s3, budgets=FakeBudgets(), post=fail, now=lambda: NOW)
    assert alert.STATE_KEY not in s3.objects

    posts = []
    assert run(s3, posts, NOW + 60) == {"notices": 1}


def test_test_event_pushes_without_touching_state():
    s3 = FakeS3()
    posts = []
    result = handler(
        {"test": True}, None, s3=s3, post=lambda t, n: posts.append(n), now=lambda: NOW
    )
    assert result == {"notices": 1}
    assert posts[0].title == "Recorder alert test"
    assert s3.puts == []


# 2026-11-06 01:00:00 UTC.
DIGEST_TIME = 1_793_926_800.0
GOOD_DAY = {
    "day": "2026-11-05",
    "process_starts": 0,
    "problems": [],
    "products": {
        "BTC-USD": {
            "rows": {
                "snapshot": 40_000,
                "update": 48_123_456,
                "trade": 612_345,
                "trade_snapshot": 100,
            },
            "gaps": 0,
            "lost_messages": 0,
            "reconnects": 3,
            "longest_outage_ms": 1348.0,
            "missing_trade_ids": 0,
            "audit_failures": 0,
        },
        "ETH-USD": {
            "rows": {"update": 950, "trade": 12},
            "gaps": 0,
            "lost_messages": 0,
            "reconnects": 0,
            "longest_outage_ms": None,
            "missing_trade_ids": 0,
            "audit_failures": 0,
        },
    },
}


def test_clock_constant_is_the_time_it_says():
    assert alert._day_of(DIGEST_TIME) == "2026-11-06"
    assert alert.time.gmtime(DIGEST_TIME).tm_hour == 1
    assert alert.time.gmtime(DIGEST_TIME).tm_min == 0


def test_first_run_starts_from_yesterday_without_reporting_on_it():
    assert alert.digest_due(None, DIGEST_TIME) == (None, "2026-11-05")


def test_digest_is_due_at_one_utc_for_yesterday():
    assert alert.digest_due("2026-11-04", DIGEST_TIME) == ("2026-11-05", "2026-11-05")


def test_digest_waits_until_the_servers_daily_job_has_had_time_to_run():
    assert alert.digest_due("2026-11-04", DIGEST_TIME - 1) == (None, "2026-11-04")


def test_digest_is_only_sent_once_a_day():
    assert alert.digest_due("2026-11-05", DIGEST_TIME + 3600) == (None, "2026-11-05")


def test_good_day_reads_all_good_with_counts_and_gaps():
    notice = alert.format_digest("2026-11-05", GOOD_DAY, 12.3456)
    assert notice.title == "Recorder all good: 2026-11-05"
    assert notice.priority == "default"
    btc, eth, spend = notice.message.splitlines()
    assert spend == "AWS usage so far this year: $12.35 of the $100 budget, before credits."
    assert btc == ("BTC-USD: 48.1M updates, 612k trades, 0 gaps, 3 reconnects, longest outage 1.3s")
    assert eth == "ETH-USD: 950 updates, 12 trades, 0 gaps, 0 reconnects"


def _changed(**changes):
    summary = json.loads(json.dumps(GOOD_DAY))
    summary["products"]["BTC-USD"].update(changes)
    return summary


def test_gaps_change_the_title_and_say_how_many():
    notice = alert.format_digest("2026-11-05", _changed(gaps=2))
    assert notice.title == "Recorder: 2 gaps on 2026-11-05"
    assert "2 gaps" in notice.message


def test_missed_trades_are_mentioned():
    assert (
        "2k trades missed"
        in alert.format_digest("2026-11-05", _changed(missing_trade_ids=2428)).message
    )


def test_restarts_are_mentioned():
    summary = {**GOOD_DAY, "process_starts": 2}
    assert "started 2 time(s)" in alert.format_digest("2026-11-05", summary).message


@pytest.mark.parametrize(
    "summary",
    [
        _changed(missing_trade_ids=1),
        _changed(longest_outage_ms=60_001.0),
        {**GOOD_DAY, "process_starts": 1},
    ],
    ids=["missed_a_trade", "long_outage", "restarted"],
)
def test_all_good_is_never_said_about_an_interrupted_day(summary):
    # A real day on a sleeping machine lost 29% of its trades and had to not read "all good".
    notice = alert.format_digest("2026-11-05", summary)
    assert notice.title == "Recorder had outages: 2026-11-05"
    assert "white_check_mark" not in notice.tags


def test_a_routine_reconnect_is_still_all_good():
    notice = alert.format_digest("2026-11-05", _changed(reconnects=9, longest_outage_ms=60_000.0))
    assert notice.title == "Recorder all good: 2026-11-05"
    assert notice.tags == "white_check_mark"


def test_problems_make_it_urgent():
    summary = {**GOOD_DAY, "problems": ["BTC-USD: no merged file for 2026-11-05."]}
    notice = alert.format_digest("2026-11-05", summary)
    assert notice.title == "Recorder needs a look: 2026-11-05"
    assert notice.priority == "high"
    assert "no merged file" in notice.message


def test_missing_summary_means_the_daily_job_did_not_finish():
    notice = alert.format_digest("2026-11-05", None)
    assert notice.title == "Recorder needs a look: 2026-11-05"
    assert notice.priority == "high"
    assert "did not finish" in notice.message


def digest_store(reported_through="2026-11-04", summary=GOOD_DAY):
    s3 = FakeS3()
    s3.objects = {
        alert.HEARTBEAT_KEY: beat(btc=DIGEST_TIME, eth=DIGEST_TIME),
        alert.STATE_KEY: {"alerts": {}, "digest_day": reported_through},
    }
    if summary is not None:
        s3.objects["ops/daily/2026-11-05.json"] = summary
    return s3


def test_handler_sends_the_digest_once():
    s3 = digest_store()
    posts = []
    assert run(s3, posts, DIGEST_TIME) == {"notices": 1}
    assert posts[0][1].title == "Recorder all good: 2026-11-05"
    assert s3.objects[alert.STATE_KEY]["digest_day"] == "2026-11-05"
    assert run(s3, posts, DIGEST_TIME + 60) == {"notices": 0}
    assert len(posts) == 1


def test_daily_push_carries_the_spend_so_far():
    s3 = digest_store()
    posts = []
    run(s3, posts, DIGEST_TIME, FakeBudgets("41.2"))
    assert "$41.20 of the $100 budget" in posts[0][1].message


def test_unreadable_spend_still_sends_the_daily_push():
    s3 = digest_store()
    posts = []
    assert run(s3, posts, DIGEST_TIME, FakeBudgets(fail=True)) == {"notices": 1}
    assert "AWS spend could not be read." in posts[0][1].message
    assert posts[0][1].title == "Recorder all good: 2026-11-05"


def test_handler_reports_a_missing_summary_instead_of_staying_silent():
    s3 = digest_store(summary=None)
    posts = []
    assert run(s3, posts, DIGEST_TIME) == {"notices": 1}
    assert "needs a look" in posts[0][1].title


def test_failed_digest_push_is_retried():
    s3 = digest_store()

    def fail(_topic, _notice):
        raise OSError("ntfy is down")

    with pytest.raises(OSError, match="ntfy"):
        handler({}, None, s3=s3, budgets=FakeBudgets(), post=fail, now=lambda: DIGEST_TIME)
    assert s3.objects[alert.STATE_KEY]["digest_day"] == "2026-11-04"
    posts = []
    assert run(s3, posts, DIGEST_TIME + 60) == {"notices": 1}


def test_an_alert_and_the_digest_can_go_out_together():
    s3 = digest_store()
    s3.objects[alert.HEARTBEAT_KEY] = beat(btc=DIGEST_TIME - 700, eth=DIGEST_TIME)
    posts = []
    assert run(s3, posts, DIGEST_TIME) == {"notices": 2}
    assert {n.title.split(":")[0] for _, n in posts} == {"Recorder stopped", "Recorder all good"}


def test_digest_can_be_requested_by_hand_without_touching_state():
    s3 = digest_store()
    before = json.dumps(s3.objects[alert.STATE_KEY])
    posts = []
    handler(
        {"digest": "2026-11-05"},
        None,
        s3=s3,
        post=lambda t, n: posts.append(n),
        now=lambda: DIGEST_TIME,
    )
    assert posts[0].title == "Recorder all good: 2026-11-05"
    assert json.dumps(s3.objects[alert.STATE_KEY]) == before


def test_push_goes_to_the_topic_on_ntfy(monkeypatch):
    sent = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(request, timeout):
        sent.append(request)
        return Response()

    monkeypatch.setattr(alert.urllib.request, "urlopen", urlopen)
    alert._post("my-topic", alert.Notice("Title here", "Body here", "high"))
    [request] = sent
    assert request.full_url == "https://ntfy.sh/my-topic"
    assert request.data == b"Body here"
    assert request.get_header("Title") == "Title here"
    assert request.get_header("Priority") == "high"


def test_alert_runs_from_a_single_file_with_only_the_standard_library():
    path = pathlib.Path(alert.__file__)
    imported = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Import):
            imported |= {name.name.split(".")[0] for name in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names)

    # -I and -S leave out PYTHONPATH and site-packages, as the bare Lambda package does.
    directory = str(path.parent)
    code = (
        f"import sys; sys.path.insert(0, {directory!r}); import alert; print(alert.STALL_AFTER_S)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True
    )
    assert result.stdout.strip() == "600", result.stderr
