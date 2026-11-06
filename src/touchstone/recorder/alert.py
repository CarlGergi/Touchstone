"""Pushes phone notifications through ntfy: an alert when data stops, and a daily summary.

This runs in AWS Lambda, not on the recorder's server. The server only reports when it last wrote
each product's data. Deciding out here means a dead server raises the same alert as a stalled
recorder, because its report simply stops changing.

The daily push is the check on everything else. It crosses the server's daily job, S3, this
function and ntfy, so its absence means one of them is broken, including the alert path itself.

It uses only the standard library, plus boto3 from the Lambda runtime, so it deploys as one file.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any, NamedTuple

STALL_AFTER_S = 600
REPEAT_EVERY_S = 3600
HEARTBEAT_KEY = "ops/heartbeat.json"
STATE_KEY = "ops/alert-state.json"
# The server's daily job runs at 00:30 UTC.
DIGEST_HOUR_UTC = 1
SECONDS_PER_DAY = 86_400
# Routine reconnects take a second or two, and the trade snapshot fills them.
LONG_OUTAGE_MS = 60_000


class Notice(NamedTuple):
    title: str
    message: str
    priority: str
    tags: str = "rotating_light"


def _minutes(seconds: float) -> int:
    return int(seconds // 60)


def decide(
    heartbeat: dict[str, Any] | None,
    state: dict[str, Any],
    products: list[str],
    now: float,
) -> tuple[list[Notice], dict[str, Any]]:
    """Returns the notices to send and the new state, which maps a product to its alert times."""
    last_writes = (heartbeat or {}).get("products", {})
    notices: list[Notice] = []
    new_state: dict[str, Any] = {}

    for product in products:
        last = last_writes.get(product) or 0
        alert = state.get(product)
        if now - last >= STALL_AFTER_S:
            silent = "no record of any writes" if not last else f"{_minutes(now - last)} minutes"
            if alert is None:
                alert = {"first": now, "last": now}
                notices.append(
                    Notice("Recorder stopped", f"{product}: no data written ({silent}).", "high")
                )
            elif now - alert["last"] >= REPEAT_EVERY_S:
                alert = {**alert, "last": now}
                notices.append(
                    Notice("Recorder still down", f"{product}: no data written ({silent}).", "high")
                )
            new_state[product] = alert
        elif alert is not None:
            down = _minutes(now - alert["first"])
            notices.append(
                Notice(
                    "Recorder recovered",
                    f"{product}: writing again after about {down} minutes since the alert.",
                    "default",
                    "white_check_mark",
                )
            )
    return notices, new_state


def _day_of(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(timestamp))


def digest_due(reported_through: str | None, now: float) -> tuple[str | None, str]:
    """Returns the day to report, if one is due, and the new last-reported day.

    The first run only records where to start, so a new deployment does not report on a day from
    before it existed.
    """
    yesterday = _day_of(now - SECONDS_PER_DAY)
    if reported_through is None:
        return None, yesterday
    if reported_through < yesterday and time.gmtime(now).tm_hour >= DIGEST_HOUR_UTC:
        return yesterday, yesterday
    return None, reported_through


def _count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)


def _spend_line(spend: float | None) -> str:
    if spend is None:
        return "AWS spend could not be read."
    return f"AWS usage so far this year: ${spend:.2f} of the $100 budget, before credits."


def format_digest(day: str, summary: dict[str, Any] | None, spend: float | None = None) -> Notice:
    """Writes the daily push. A missing summary means the server's daily job did not finish."""
    if summary is None:
        return Notice(
            f"Recorder needs a look: {day}",
            f"No summary for {day} reached S3: the server's daily sync or summary did not finish.\n"
            + _spend_line(spend),
            "high",
        )

    lines = []
    gaps = 0
    for product, stats in summary["products"].items():
        gaps += stats["gaps"]
        rows = stats["rows"]
        trades = rows.get("trade", 0) + rows.get("trade_snapshot", 0)
        line = (
            f"{product}: {_count(rows.get('update', 0))} updates, {_count(trades)} trades, "
            f"{stats['gaps']} gaps, {stats['reconnects']} reconnects"
        )
        if stats["longest_outage_ms"]:
            line += f", longest outage {stats['longest_outage_ms'] / 1000:.1f}s"
        if stats["missing_trade_ids"]:
            line += f", {_count(stats['missing_trade_ids'])} trades missed"
        lines.append(line)
    if summary["process_starts"]:
        lines.append(f"Recorder started {summary['process_starts']} time(s).")
    lines += summary["problems"]
    lines.append(_spend_line(spend))

    body = "\n".join(lines)
    if summary["problems"]:
        return Notice(f"Recorder needs a look: {day}", body, "high")
    if gaps:
        return Notice(f"Recorder: {gaps} gaps on {day}", body, "default", "warning")
    interrupted = summary["process_starts"] > 0 or any(
        stats["missing_trade_ids"] or (stats["longest_outage_ms"] or 0) > LONG_OUTAGE_MS
        for stats in summary["products"].values()
    )
    if interrupted:
        return Notice(f"Recorder had outages: {day}", body, "default", "warning")
    return Notice(f"Recorder all good: {day}", body, "default", "white_check_mark")


def _post(topic: str, notice: Notice) -> None:
    request = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=notice.message.encode(),
        headers={"Title": notice.title, "Priority": notice.priority, "Tags": notice.tags},
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


def _read_json(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def _daily_key(day: str) -> str:
    return f"ops/daily/{day}.json"


def _spend(budgets: Any) -> float | None:
    """Reads spend so far from the budget. A failure only costs the spend line, never the push."""
    try:
        if budgets is None:
            import boto3

            # Budgets is a global service, answered from us-east-1.
            budgets = boto3.client("budgets", region_name="us-east-1")
        budget = budgets.describe_budget(
            AccountId=os.environ["ACCOUNT_ID"], BudgetName=os.environ["BUDGET_NAME"]
        )["Budget"]
        return float(budget["CalculatedSpend"]["ActualSpend"]["Amount"])
    except Exception:
        return None


def handler(
    event: Any,
    context: Any,
    *,
    s3: Any = None,
    budgets: Any = None,
    post: Any = _post,
    now: Any = time.time,
):
    """Lambda entry point. Runs every minute, and can be invoked by hand to test the push."""
    if s3 is None:
        import boto3

        s3 = boto3.client("s3")
    bucket = os.environ["BUCKET"]
    topic = os.environ["NTFY_TOPIC"]
    products = os.environ["PRODUCTS"].split(",")

    if isinstance(event, dict) and event.get("test"):
        message = "Alerts from the recorder reach this phone."
        post(topic, Notice("Recorder alert test", message, "default", "white_check_mark"))
        return {"notices": 1}
    if isinstance(event, dict) and event.get("digest"):
        day = event["digest"]
        post(topic, format_digest(day, _read_json(s3, bucket, _daily_key(day)), _spend(budgets)))
        return {"notices": 1}

    clock = now()
    stored = _read_json(s3, bucket, STATE_KEY) or {}
    heartbeat = _read_json(s3, bucket, HEARTBEAT_KEY)
    notices, alerts = decide(heartbeat, stored.get("alerts", {}), products, clock)
    day, reported_through = digest_due(stored.get("digest_day"), clock)
    if day is not None:
        notices.append(format_digest(day, _read_json(s3, bucket, _daily_key(day)), _spend(budgets)))

    # A failed post raises before the state is saved, so the next run tries again.
    for notice in notices:
        post(topic, notice)
    new_state = {"alerts": alerts, "digest_day": reported_through}
    if new_state != stored:
        s3.put_object(Bucket=bucket, Key=STATE_KEY, Body=json.dumps(new_state).encode())
    return {"notices": len(notices)}
