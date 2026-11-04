"""Recorder settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_URL = "wss://advanced-trade-ws.coinbase.com"
DEFAULT_PRODUCTS = ("BTC-USD", "ETH-USD")


@dataclass(frozen=True, slots=True)
class RecorderConfig:
    data_dir: Path
    products: tuple[str, ...] = DEFAULT_PRODUCTS
    url: str = DEFAULT_URL

    # A full BTC-USD snapshot is about 4 MB, well past the WebSocket library's 1 MiB default.
    max_message_bytes: int = 64 * 1024 * 1024
    open_timeout_s: float = 10.0

    # The heartbeats channel sends one message a second, so any silence means a dead connection.
    stale_after_s: float = 15.0
    # Heartbeats alone do not prove the book is flowing.
    book_stale_after_s: float = 60.0
    snapshot_timeout_s: float = 30.0

    backoff_base_s: float = 0.25
    backoff_cap_s: float = 30.0
    # A session that lived this long clears the failure count used for backoff.
    stable_after_s: float = 60.0
    # Coinbase allows 8 connections and 8 unauthenticated messages per second per IP.
    outbound_per_s: float = 5.0

    flush_interval_s: float = 30.0
    status_interval_s: float = 10.0
    stats_every_status: int = 6
    min_free_disk_bytes: int = 5 * 1024**3

    # How often an independent connection fetches a snapshot to check the replayed book against.
    # Zero disables audits.
    audit_interval_s: float = 900.0
    # WebSocket ping round trips, kept for the latency model. Zero disables sampling.
    rtt_interval_s: float = 10.0
