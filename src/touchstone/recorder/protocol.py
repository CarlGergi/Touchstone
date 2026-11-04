"""Decodes Coinbase Advanced Trade market-data messages into recorder rows.

Message shapes were checked against the live feed, not only the documentation:

* Subscribing to ``level2`` yields messages on the channel ``l2_data``.
* ``sequence_num`` starts at 0 for each connection and rises by exactly one across every channel
  and product on that connection. The API reference says per connection; the overview page says
  per product, which the live feed contradicts.
* ``new_quantity`` is the absolute size at a level, not a delta. Zero removes the level.
* A trade's ``side`` is the resting (maker) order's side, so ``BUY`` means the trade hit the bid.
  On the live feed, a trade's level existed on the recorded side and not the other in 99.6% of
  the cases where it existed on exactly one side.
* Coinbase occasionally labels a batch of live trades ``snapshot`` in the middle of a session, so
  the label does not mean a trade is old. Those trades appear nowhere else.

Prices and sizes are stored as integers scaled by 1e8. That keeps them exact, which matters
because prices are book keys.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

SCALE_DIGITS = 8
SCALE = 10**SCALE_DIGITS
_INT64_MAX = 2**63 - 1

BOOK_CHANNEL = "l2_data"
TRADES_CHANNEL = "market_trades"
HEARTBEAT_CHANNEL = "heartbeats"
_RECORDED_CHANNELS = {BOOK_CHANNEL, TRADES_CHANNEL}

_BOOK_SIDES = {"bid": "bid", "offer": "ask"}
_MAKER_SIDES = {"BUY": "bid", "SELL": "ask"}

# Column order matches storage.SCHEMA:
# local_ts_ns, server_ts_ns, exchange_ts_ns, session, seq, event, side, price_e8, size_e8, trade_id
Row = tuple[int, int, int, int, int, str, str, int, int, int | None]


class ProtocolError(ValueError):
    """A message could not be decoded without guessing at its meaning."""


class ServerError(RuntimeError):
    """The server reported an error on this connection."""


@dataclass(frozen=True, slots=True)
class Message:
    channel: str
    seq: int
    server_ts_ns: int
    events: list[dict[str, Any]]


def parse_fixed(text: str) -> int:
    """Converts a non-negative decimal string to an integer scaled by 1e8, exactly."""
    whole, dot, fraction = text.partition(".")
    if not (whole.isascii() and whole.isdigit()):
        raise ProtocolError(f"not a decimal: {text!r}")
    if dot and not (fraction.isascii() and fraction.isdigit()):
        raise ProtocolError(f"not a decimal: {text!r}")
    fraction = fraction.rstrip("0")
    if len(fraction) > SCALE_DIGITS:
        raise ProtocolError(f"more than {SCALE_DIGITS} decimal places: {text!r}")
    value = int(whole) * SCALE + (int(fraction.ljust(SCALE_DIGITS, "0")) if fraction else 0)
    if value > _INT64_MAX:
        raise ProtocolError(f"too large for int64: {text!r}")
    return value


@lru_cache(maxsize=8)
def _epoch_seconds(prefix: str) -> int:
    parsed = datetime.strptime(prefix, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    return int(parsed.timestamp())


def parse_timestamp_ns(text: str) -> int:
    """Converts an RFC 3339 UTC timestamp with up to nine fractional digits to epoch nanoseconds."""
    if not text.endswith("Z"):
        raise ProtocolError(f"not a UTC timestamp: {text!r}")
    prefix, dot, fraction = text[:-1].partition(".")
    if dot and not (fraction.isascii() and fraction.isdigit() and len(fraction) <= 9):
        raise ProtocolError(f"bad fractional seconds: {text!r}")
    try:
        seconds = _epoch_seconds(prefix)
    except ValueError as exc:
        raise ProtocolError(f"bad timestamp: {text!r}") from exc
    return seconds * 10**9 + (int(fraction.ljust(9, "0")) if fraction else 0)


def decode(raw: str | bytes) -> Message | None:
    """Decodes a message envelope, or returns None for a kind of message that should be ignored."""
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError(f"expected a JSON object, got {type(payload).__name__}")
    if payload.get("type") == "error":
        raise ServerError(str(payload.get("message", payload)))

    channel = payload.get("channel")
    seq = payload.get("sequence_num")
    if not isinstance(channel, str) or seq is None:
        if channel in _RECORDED_CHANNELS:
            raise ProtocolError(f"{channel} message without sequence_num")
        return None
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise ProtocolError(f"sequence_num is not an integer: {seq!r}")

    events = payload.get("events")
    if not isinstance(events, list):
        raise ProtocolError(f"{channel} message without events")
    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, str):
        raise ProtocolError(f"{channel} message without timestamp")
    return Message(channel, seq, parse_timestamp_ns(timestamp), events)


def has_book_snapshot(message: Message) -> bool:
    return any(event.get("type") == "snapshot" for event in message.events)


def book_rows(
    message: Message,
    *,
    product: str,
    local_ts_ns: int,
    session: int,
    snapshot_label: str = "snapshot",
) -> list[Row]:
    """Flattens a level-2 message into one row per changed level."""
    rows: list[Row] = []
    try:
        for event in message.events:
            if event["product_id"] != product:
                raise ProtocolError(f"unexpected product {event['product_id']!r}")
            label = _event_label(event["type"], snapshot_label, "update")
            for level in event["updates"]:
                rows.append(
                    (
                        local_ts_ns,
                        message.server_ts_ns,
                        parse_timestamp_ns(level["event_time"]),
                        session,
                        message.seq,
                        label,
                        _BOOK_SIDES[level["side"]],
                        parse_fixed(level["price_level"]),
                        parse_fixed(level["new_quantity"]),
                        None,
                    )
                )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ProtocolError(f"malformed {message.channel} event: {exc!r}") from exc
    return rows


def trade_rows(message: Message, *, product: str, local_ts_ns: int, session: int) -> list[Row]:
    """Flattens a trades message into one row per trade, with ``side`` set to the maker's side."""
    rows: list[Row] = []
    try:
        for event in message.events:
            label = _event_label(event["type"], "trade_snapshot", "trade")
            for trade in event["trades"]:
                if trade["product_id"] != product:
                    raise ProtocolError(f"unexpected product {trade['product_id']!r}")
                rows.append(
                    (
                        local_ts_ns,
                        message.server_ts_ns,
                        parse_timestamp_ns(trade["time"]),
                        session,
                        message.seq,
                        label,
                        _MAKER_SIDES[trade["side"]],
                        parse_fixed(trade["price"]),
                        parse_fixed(trade["size"]),
                        int(trade["trade_id"]),
                    )
                )
    except ProtocolError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ProtocolError(f"malformed {message.channel} event: {exc!r}") from exc
    return rows


def _event_label(kind: str, snapshot_label: str, update_label: str) -> str:
    if kind == "snapshot":
        return snapshot_label
    if kind == "update":
        return update_label
    raise ProtocolError(f"unknown event type {kind!r}")
