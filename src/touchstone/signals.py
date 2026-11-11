"""Order flow imbalance and queue imbalance, and how well they predict the mid.

Everything is timed by when the data was received (local time), never by the exchange's clock, so
a signal at time t uses only what had arrived by t. Book updates arrive about 30 ms after their
exchange time, and trades up to 250 ms after, so using exchange time would look into the future.

* Order flow imbalance (Cont, Kukanov and Stoikov, 2014) adds up the net pressure at the best bid
  and ask, message by message, over a trailing window.
* Queue imbalance is (bid size - ask size) / (bid size + ask size) at the best prices.

The target is the change in mid, in ticks, from t to t + horizon.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

HORIZONS_S = (0.1, 1.0, 10.0)
OFI_WINDOWS_S = (0.1, 1.0, 10.0)


@dataclass(frozen=True)
class Book:
    """The top of the book after each message, by the time it was received."""

    seen_ns: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    bid_size: np.ndarray
    ask_size: np.ndarray

    @property
    def mid(self) -> np.ndarray:
        return (self.bid + self.ask) / 2


def order_flow(book: Book) -> np.ndarray:
    """Each message's contribution to order flow imbalance, in units of size."""
    bid, ask = book.bid.astype(float), book.ask.astype(float)
    qb, qa = book.bid_size.astype(float), book.ask_size.astype(float)
    e = np.zeros(len(bid))
    e[1:] = (
        (bid[1:] >= bid[:-1]) * qb[1:]
        - (bid[1:] <= bid[:-1]) * qb[:-1]
        - (ask[1:] <= ask[:-1]) * qa[1:]
        + (ask[1:] >= ask[:-1]) * qa[:-1]
    )
    return e


def trailing_sum(times: np.ndarray, values: np.ndarray, window_ns: int) -> np.ndarray:
    """For each message, the sum of values over messages received within the window up to it."""
    total = np.concatenate([[0.0], np.cumsum(values)])
    start = np.searchsorted(times, times - window_ns, side="right")
    return total[1:] - total[start]


def features(book: Book) -> dict[str, np.ndarray]:
    """Every signal variant, valued after each message. These are all the variants tried."""
    flow = order_flow(book)
    depth = (book.bid_size + book.ask_size).astype(float)
    typical_depth = float(np.median(depth[depth > 0])) if (depth > 0).any() else 1.0
    out: dict[str, np.ndarray] = {}
    for window in OFI_WINDOWS_S:
        ofi = trailing_sum(book.seen_ns, flow, int(window * 1e9))
        out[f"ofi_{window:g}s"] = ofi
        # Scaled by the day's typical depth, so a value means the same on quiet and busy days.
        out[f"ofi_{window:g}s_scaled"] = ofi / typical_depth
    with np.errstate(invalid="ignore", divide="ignore"):
        qi = np.where(depth > 0, (book.bid_size - book.ask_size) / depth, 0.0)
    out["queue_imbalance"] = qi
    return out


def sample_times(book: Book, step_s: float, horizon_s: float) -> np.ndarray:
    """A regular grid of times, leaving room for the longest horizon at the end of the day."""
    start, end = book.seen_ns[0], book.seen_ns[-1] - int(horizon_s * 1e9)
    return np.arange(start, end, int(step_s * 1e9), dtype=np.int64)


def value_at(book: Book, values: np.ndarray, times: np.ndarray) -> np.ndarray:
    """Each value as last known at each time."""
    index = np.searchsorted(book.seen_ns, times, side="right") - 1
    return values[np.clip(index, 0, len(values) - 1)]


def forward_change(book: Book, times: np.ndarray, horizon_s: float, tick: float) -> np.ndarray:
    """Mid change in ticks from each time to the horizon after it."""
    later = times + int(horizon_s * 1e9)
    return (value_at(book, book.mid, later) - value_at(book, book.mid, times)) / tick


def rank_ic(signal: np.ndarray, target: np.ndarray) -> float:
    """Spearman rank correlation, the information coefficient."""
    keep = np.isfinite(signal) & np.isfinite(target)
    if keep.sum() < 3:
        return float("nan")
    a = _ranks(signal[keep])
    b = _ranks(target[keep])
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _ranks(values: np.ndarray) -> np.ndarray:
    """Average ranks, so ties, which are common in mid changes, are handled correctly."""
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    sorted_values = values[order]
    boundaries = np.flatnonzero(np.diff(sorted_values)) + 1
    starts = np.concatenate([[0], boundaries])
    ends = np.concatenate([boundaries, [len(values)]])
    for start, end in zip(starts, ends, strict=True):
        ranks[order[start:end]] = (start + end - 1) / 2
    return ranks
