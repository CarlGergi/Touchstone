"""Which recorded days an experiment may use."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from touchstone.data import load
from touchstone.recorder.storage import recorded_days

# Experiments for the first results use development data received before this date only. The
# held-out week starting 2026-12-01 is out of reach anyway: the loader refuses it.
RESEARCH_END = "2026-11-15"


def research_days(root: Path, product: str, *, first: str = "2026-11-06") -> list[str]:
    """Whole recorded days from the server's first full day up to the research cutoff.

    2026-11-06 is the first day recorded on the always-on server. Earlier days came from a laptop
    that slept, and lost up to a third of their trades.
    """
    return [d for d in recorded_days(root, product) if first <= d < RESEARCH_END]


def day_frames(root: Path, product: str, days: list[str]) -> Iterator[tuple[str, pl.DataFrame]]:
    for day in days:
        end = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        yield day, load(root, product, start=day, end=end).collect()
