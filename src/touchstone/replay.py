"""Event-by-event replay of a recorded book, on the C++ engine, plus a pure Python reference.

The engine takes the book rows of one stream in arrival order. A message is a run of rows sharing a
session and sequence number, and a message that starts with a snapshot replaces the book.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import polars as pl
import touchstone_engine as engine
from sortedcontainers import SortedDict

_BOOK = ("snapshot", "update")


def stream_arrays(frame: pl.DataFrame) -> dict[str, np.ndarray]:
    """The columns the engine reads, for the book rows only, in arrival order."""
    book = frame.filter(pl.col("event").is_in(_BOOK))
    return {
        "event": book["event"].to_physical().cast(pl.UInt8).to_numpy(),
        "side": book["side"].to_physical().cast(pl.UInt8).to_numpy(),
        "price": book["price_e8"].to_numpy(),
        "size": book["size_e8"].to_numpy(),
        "seq": book["seq"].to_numpy(),
        "session": book["session"].to_numpy(),
        "ts": book["exchange_ts_ns"].to_numpy(),
    }


def replay_top(arrays: dict[str, np.ndarray]) -> pl.DataFrame:
    """Top of the book after every message."""
    return pl.DataFrame(engine.replay_top(*arrays.values()))


def python_book_rate(arrays: dict[str, np.ndarray]) -> float:
    """Rows per second for the same replay in plain Python, the benchmark's baseline.

    It does the engine's work: apply each message, then read the best bid and ask. The book is kept
    sorted with ``SortedDict``, the usual choice in Python, so the comparison is like for like.
    """
    event, side, price, size, seq, session = (arrays[k].tolist() for k in list(arrays)[:6])
    bids, asks = SortedDict(), SortedDict()
    books = (bids, asks)
    sink = 0
    start = time.perf_counter()
    i, n = 0, len(event)
    while i < n:
        key = (session[i], seq[i])
        if event[i] == 0:
            bids.clear()
            asks.clear()
        while i < n and (session[i], seq[i]) == key:
            levels = books[side[i]]
            if size[i]:
                levels[price[i]] = size[i]
            else:
                levels.pop(price[i], None)
            i += 1
        best_bid = bids.peekitem(-1)[0] if bids else 0
        best_ask = asks.peekitem(0)[0] if asks else 0
        sink += best_bid - best_ask
    return n / (time.perf_counter() - start)


@dataclass(frozen=True, slots=True)
class AuditCheck:
    """How the rebuilt book compares with the independent audit snapshots."""

    audits: int
    checked: int
    exact: int
    consistent: int
    unexplained_levels: int

    @property
    def passed(self) -> bool:
        return self.checked > 0 and self.consistent == self.checked


def check_against_audits(frame: pl.DataFrame) -> AuditCheck:
    """Compares the replayed book with every audit snapshot in the frame.

    An audit taken between two stream messages may already hold part of the later one, whose rows
    all carry one later time. So the book must match exactly except at levels that later message
    touches. A level outside it that differs is unexplained, and means the replay is wrong.
    """
    audits = []
    for _, rows in frame.filter(pl.col("event") == "audit_snapshot").group_by(
        "session", maintain_order=True
    ):
        levels = {
            (0 if side == "bid" else 1, price): size
            for side, price, size in rows.select("side", "price_e8", "size_e8").iter_rows()
            if size
        }
        audits.append((int(rows["exchange_ts_ns"].min()), levels))
    audits.sort(key=lambda audit: audit[0])
    states = engine.replay_checkpoints(
        *stream_arrays(frame).values(), np.array([at for at, _ in audits], dtype=np.int64)
    )

    checked = exact = consistent = unexplained = 0
    for (_, snapshot), state in zip(audits, states, strict=True):
        if state is None:
            continue
        book = {(0, price): size for price, size in state["bids"]}
        book |= {(1, price): size for price, size in state["asks"]}
        touched = {(int(side), price) for side, price in state["touched"]}
        differing = {
            key for key in book.keys() | snapshot.keys() if book.get(key) != snapshot.get(key)
        }
        checked += 1
        exact += not differing
        consistent += differing <= touched
        unexplained += len(differing - touched)
    return AuditCheck(len(audits), checked, exact, consistent, unexplained)
