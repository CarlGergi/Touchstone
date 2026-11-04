"""Message decoding. The sample payloads are trimmed captures from the live feed."""

import json

import pytest

from touchstone.recorder import protocol
from touchstone.recorder.protocol import ProtocolError, ServerError

LIVE_BOOK_SNAPSHOT = {
    "channel": "l2_data",
    "client_id": "",
    "timestamp": "2026-11-04T00:14:34.303727595Z",
    "sequence_num": 0,
    "events": [
        {
            "type": "snapshot",
            "product_id": "BTC-USD",
            "updates": [
                {
                    "side": "bid",
                    "event_time": "2026-11-04T00:14:34.025926Z",
                    "price_level": "77244.89",
                    "new_quantity": "0.12622162",
                },
                {
                    "side": "offer",
                    "event_time": "2026-11-04T00:14:34.025926Z",
                    "price_level": "77244.9",
                    "new_quantity": "0",
                },
            ],
        }
    ],
}

LIVE_TRADES = {
    "channel": "market_trades",
    "client_id": "",
    "timestamp": "2026-11-04T00:14:34.74441651Z",
    "sequence_num": 28,
    "events": [
        {
            "type": "update",
            "trades": [
                {
                    "product_id": "BTC-USD",
                    "trade_id": "1114630210",
                    "price": "77238.12",
                    "size": "0.00000004",
                    "time": "2026-11-04T00:14:34.68375Z",
                    "side": "BUY",
                }
            ],
        }
    ],
}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0),
        ("1", 100_000_000),
        ("77234.8", 7_723_480_000_000),
        ("0.00000007", 7),
        ("0.12622162", 12_622_162),
        ("1.500000000000", 150_000_000),
        ("92233720368.54775807", 2**63 - 1),
    ],
)
def test_parse_fixed_is_exact(text, expected):
    assert protocol.parse_fixed(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        ".5",
        "5.",
        "-1",
        "1e-7",
        "1_000",
        "٣",
        "abc",
        "0.123456789",
        "92233720368.54775808",
    ],
)
def test_parse_fixed_refuses_anything_it_cannot_represent_exactly(text):
    with pytest.raises(ProtocolError):
        protocol.parse_fixed(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-04T00:14:35Z", 1_793_751_275_000_000_000),
        ("2026-11-04T00:14:34.68375Z", 1_793_751_274_683_750_000),
        ("2026-11-04T00:14:34.303727595Z", 1_793_751_274_303_727_595),
    ],
)
def test_parse_timestamp_keeps_nanoseconds(text, expected):
    assert protocol.parse_timestamp_ns(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "2026-11-04T00:14:34",
        "2026-11-04T00:14:34+00:00",
        "2026-11-04T00:14:34.Z",
        "2026-11-04T00:14:34.1234567890Z",
        "2026-13-04T00:14:34Z",
    ],
)
def test_parse_timestamp_refuses_other_formats(text):
    with pytest.raises(ProtocolError):
        protocol.parse_timestamp_ns(text)


def test_book_snapshot_rows():
    message = protocol.decode(json.dumps(LIVE_BOOK_SNAPSHOT))
    assert protocol.has_book_snapshot(message)
    rows = protocol.book_rows(message, product="BTC-USD", local_ts_ns=7, session=3)
    assert rows == [
        (
            7,
            1_793_751_274_303_727_595,
            1_793_751_274_025_926_000,
            3,
            0,
            "snapshot",
            "bid",
            7_724_489_000_000,
            12_622_162,
            None,
        ),
        (
            7,
            1_793_751_274_303_727_595,
            1_793_751_274_025_926_000,
            3,
            0,
            "snapshot",
            "ask",
            7_724_490_000_000,
            0,
            None,
        ),
    ]


def test_book_snapshot_can_be_labelled_as_an_audit():
    message = protocol.decode(json.dumps(LIVE_BOOK_SNAPSHOT))
    rows = protocol.book_rows(
        message, product="BTC-USD", local_ts_ns=1, session=1, snapshot_label="audit_snapshot"
    )
    assert {row[5] for row in rows} == {"audit_snapshot"}


def test_trade_side_is_recorded_as_the_makers_book_side():
    message = protocol.decode(json.dumps(LIVE_TRADES))
    [row] = protocol.trade_rows(message, product="BTC-USD", local_ts_ns=9, session=2)
    assert row == (
        9,
        1_793_751_274_744_416_510,
        1_793_751_274_683_750_000,
        2,
        28,
        "trade",
        "bid",
        7_723_812_000_000,
        4,
        1_114_630_210,
    )


def test_trade_snapshot_is_labelled_separately():
    payload = json.loads(json.dumps(LIVE_TRADES))
    payload["events"][0]["type"] = "snapshot"
    rows = protocol.trade_rows(
        protocol.decode(json.dumps(payload)), product="BTC-USD", local_ts_ns=9, session=2
    )
    assert rows[0][5] == "trade_snapshot"


def test_invalid_json_is_a_protocol_error():
    with pytest.raises(ProtocolError):
        protocol.decode("{not json")
    with pytest.raises(ProtocolError):
        protocol.decode("[1, 2]")


def test_server_error_is_surfaced():
    with pytest.raises(ServerError, match="rate limit"):
        protocol.decode(json.dumps({"type": "error", "message": "rate limit exceeded"}))


def test_unknown_message_kinds_are_ignored():
    assert protocol.decode(json.dumps({"type": "something_new", "x": 1})) is None


@pytest.mark.parametrize("channel", ["l2_data", "market_trades"])
def test_recorded_channels_must_carry_a_sequence_number(channel):
    with pytest.raises(ProtocolError):
        protocol.decode(json.dumps({"channel": channel, "events": []}))


@pytest.mark.parametrize("seq", ["1", 1.5, True])
def test_sequence_number_must_be_an_integer(seq):
    with pytest.raises(ProtocolError):
        protocol.decode(json.dumps({**LIVE_TRADES, "sequence_num": seq}))


def _mutated_book(**level_changes):
    payload = json.loads(json.dumps(LIVE_BOOK_SNAPSHOT))
    payload["events"][0]["updates"][0].update(level_changes)
    return protocol.decode(json.dumps(payload))


@pytest.mark.parametrize(
    "changes",
    [
        {"side": "sideways"},
        {"price_level": "abc"},
        {"new_quantity": None},
        {"event_time": 12345},
    ],
)
def test_malformed_levels_are_protocol_errors(changes):
    with pytest.raises(ProtocolError):
        protocol.book_rows(_mutated_book(**changes), product="BTC-USD", local_ts_ns=1, session=1)


def test_book_message_for_another_product_is_refused():
    message = protocol.decode(json.dumps(LIVE_BOOK_SNAPSHOT))
    with pytest.raises(ProtocolError, match="unexpected product"):
        protocol.book_rows(message, product="ETH-USD", local_ts_ns=1, session=1)


@pytest.mark.parametrize(
    "changes",
    [{"side": "UNKNOWN"}, {"trade_id": "not-a-number"}, {"price": "x"}, {"product_id": "ETH-USD"}],
)
def test_malformed_trades_are_protocol_errors(changes):
    payload = json.loads(json.dumps(LIVE_TRADES))
    payload["events"][0]["trades"][0].update(changes)
    with pytest.raises(ProtocolError):
        protocol.trade_rows(
            protocol.decode(json.dumps(payload)), product="BTC-USD", local_ts_ns=1, session=1
        )
