"""Sequence-gap detection for one connection."""

from __future__ import annotations

from enum import Enum, auto


class SequenceStatus(Enum):
    IN_ORDER = auto()
    GAP = auto()
    STALE = auto()


class SequenceTracker:
    """Follows ``sequence_num`` across every channel on a single connection.

    Any jump forward is treated as a loss straight away. TCP delivers in order, so a late arrival
    could only come from the server's side, and resyncing costs far less than recording a book
    that has silently diverged.
    """

    def __init__(self) -> None:
        self.expected: int | None = None

    def observe(self, seq: int) -> SequenceStatus:
        if self.expected is None or seq == self.expected:
            self.expected = seq + 1
            return SequenceStatus.IN_ORDER
        if seq > self.expected:
            return SequenceStatus.GAP
        return SequenceStatus.STALE
