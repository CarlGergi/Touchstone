"""The recorder's health log and the status file used by the container health check."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

logger = logging.getLogger("touchstone.recorder")

STATUS_FILE = "status.json"
_QUIET_EVENTS = {"rtt", "stats"}


def _utc_day(ts_ns: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts_ns // 10**9))


class HealthLog:
    """Appends one JSON object per line to ``health/YYYY-MM-DD.jsonl``."""

    def __init__(self, root: Path, now_ns: Callable[[], int] = time.time_ns) -> None:
        self.directory = root / "health"
        self.now_ns = now_ns
        self._day: str | None = None
        self._file: TextIO | None = None

    def emit(self, event: str, **fields: Any) -> None:
        ts_ns = self.now_ns()
        record = {"ts_ns": ts_ns, "event": event, **fields}
        line = json.dumps(record, separators=(",", ":"), default=str)
        file = self._file_for(_utc_day(ts_ns))
        file.write(line + "\n")
        file.flush()
        if event not in _QUIET_EVENTS:
            logger.info("%s %s", event, json.dumps(fields, default=str))

    def _file_for(self, day: str) -> TextIO:
        if day != self._day or self._file is None:
            self.close()
            self.directory.mkdir(parents=True, exist_ok=True)
            self._file = (self.directory / f"{day}.jsonl").open("a", encoding="utf-8")
            self._day = day
        return self._file

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def read_health(root: Path, day: str | None = None) -> list[dict[str, Any]]:
    """Loads health records under ``root``, oldest day first, optionally for one UTC day only."""
    records: list[dict[str, Any]] = []
    for path in sorted((root / "health").glob(f"{day or '*'}.jsonl")):
        with path.open(encoding="utf-8") as lines:
            records += [json.loads(line) for line in lines]
    return records


def write_status(root: Path, status: dict[str, Any]) -> None:
    tmp = root / (STATUS_FILE + ".tmp")
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    os.replace(tmp, root / STATUS_FILE)


def check_status(root: Path, *, max_age_s: float, now_ns: int | None = None) -> list[str]:
    """Returns the reasons the recorder looks unhealthy. An empty list means it looks healthy."""
    now_ns = time.time_ns() if now_ns is None else now_ns
    path = root / STATUS_FILE
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{path} does not exist"]
    except ValueError as exc:
        return [f"{path} is not valid JSON: {exc}"]

    limit_ns = max_age_s * 1e9
    problems = []
    if now_ns - status["updated_ns"] > limit_ns:
        problems.append(f"status last updated {(now_ns - status['updated_ns']) / 1e9:.0f}s ago")
    for product, feed in status["products"].items():
        if not feed["last_message_ns"]:
            problems.append(f"{product}: no message received yet (state {feed['state']})")
            continue
        age_ns = now_ns - feed["last_message_ns"]
        if age_ns > limit_ns:
            problems.append(
                f"{product}: last message {age_ns / 1e9:.0f}s ago (state {feed['state']})"
            )
    return problems
