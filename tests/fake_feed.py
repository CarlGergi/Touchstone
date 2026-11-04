"""An in-process stand-in for Coinbase's Advanced Trade market-data endpoint.

It serves the real protocol over a real WebSocket, including the property that matters most:
``sequence_num`` is per connection and rises across every channel, so a dropped message of any
kind leaves a hole. Each market keeps its true book after every step, so tests can check what was
recorded against what actually happened rather than only checking that a gap was logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import http
import itertools
import json
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

E8 = 10**8
TICK_E8 = 1_000_000  # 0.01
START_NS = 1_793_750_000_000_000_000
STEP_NS = 50_000_000

Book = dict[tuple[str, int], int]


def format_e8(value: int) -> str:
    whole, fraction = divmod(value, E8)
    return f"{whole}.{fraction:08d}".rstrip("0").rstrip(".") if fraction else str(whole)


def format_ts(ts_ns: int, digits: int = 6) -> str:
    seconds, nanos = divmod(ts_ns, 10**9)
    prefix = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{prefix}.{nanos:09d}"[: 20 + digits] + "Z"


class FakeMarket:
    """Deterministic ground truth for one product: its book after every step, and its trades."""

    def __init__(self, product: str, seed: int = 0, extra_levels: int = 0) -> None:
        self.product = product
        self.rng = random.Random(f"{product}:{seed}")
        self.now_ns = START_NS
        self.mid_ticks = 7_724_489
        self.book: Book = {}
        for offset in range(1, 11 + extra_levels):
            size = self.rng.randint(1, 10**9)
            self.book["bid", (self.mid_ticks - offset) * TICK_E8] = size
            self.book["ask", (self.mid_ticks + offset) * TICK_E8] = size
        self.history: dict[int, Book] = {self.now_ns: dict(self.book)}
        self.tape: list[dict[str, str]] = []
        self.trade_ids = itertools.count(1_114_630_000)

    def step(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        self.now_ns += STEP_NS
        updates = [self._change_level() for _ in range(self.rng.randint(1, 3))]
        trades = [self._trade()] if self.rng.random() < 0.3 else []
        self.history[self.now_ns] = dict(self.book)
        return updates, trades

    def _change_level(self) -> dict[str, str]:
        side = self.rng.choice(("bid", "ask"))
        offset = self.rng.randint(1, 12)
        ticks = self.mid_ticks - offset if side == "bid" else self.mid_ticks + offset
        price = ticks * TICK_E8
        size = 0 if self.rng.random() < 0.25 else self.rng.randint(1, 10**9)
        if size:
            self.book[side, price] = size
        else:
            self.book.pop((side, price), None)
        return {
            "side": "bid" if side == "bid" else "offer",
            "event_time": format_ts(self.now_ns),
            "price_level": format_e8(price),
            "new_quantity": format_e8(size),
        }

    def _trade(self) -> dict[str, str]:
        maker_buys = self.rng.random() < 0.5
        ticks = self.mid_ticks - 1 if maker_buys else self.mid_ticks + 1
        trade = {
            "product_id": self.product,
            "trade_id": str(next(self.trade_ids)),
            "price": format_e8(ticks * TICK_E8),
            "size": format_e8(self.rng.randint(1, 10**8)),
            "time": format_ts(self.now_ns),
            "side": "BUY" if maker_buys else "SELL",
        }
        self.tape.append(trade)
        return trade

    def snapshot_updates(self) -> list[dict[str, str]]:
        return [
            {
                "side": "bid" if side == "bid" else "offer",
                "event_time": format_ts(self.now_ns),
                "price_level": format_e8(price),
                "new_quantity": format_e8(size),
            }
            for (side, price), size in sorted(self.book.items())
        ]


@dataclass
class _Connection:
    ws: ServerConnection
    next_seq: int = 0
    channels: dict[str, set[str]] = field(default_factory=dict)
    frozen: bool = False
    last_book_message: str = ""

    def wants(self, channel: str, product: str) -> bool:
        return product in self.channels.get(channel, ())


@dataclass
class _Drop:
    channel: str
    remaining: int


class FakeFeed:
    def __init__(
        self, products: tuple[str, ...] = ("BTC-USD",), seed: int = 0, extra_levels: int = 0
    ) -> None:
        self.markets = {p: FakeMarket(p, seed, extra_levels) for p in products}
        self.connections: list[_Connection] = []
        self.total_connections = 0
        self.dropped: list[dict[str, Any]] = []
        self.withhold_snapshots = False
        self._drops: list[_Drop] = []
        self._refuse = 0
        self._server: Any = None
        self.url = ""

    async def __aenter__(self) -> FakeFeed:
        self._server = await serve(
            self._serve, "127.0.0.1", 0, process_request=self._process_request, max_size=None
        )
        port = next(iter(self._server.sockets)).getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    def _process_request(self, connection: ServerConnection, request: Any) -> Any:
        if self._refuse > 0:
            self._refuse -= 1
            return connection.respond(http.HTTPStatus.SERVICE_UNAVAILABLE, "try later\n")
        return None

    async def _serve(self, ws: ServerConnection) -> None:
        connection = _Connection(ws)
        self.connections.append(connection)
        self.total_connections += 1
        try:
            async for raw in ws:
                message = json.loads(raw)
                if message["type"] == "subscribe":
                    await self._subscribe(connection, message["channel"], message["product_ids"])
        except websockets.ConnectionClosed:
            pass
        finally:
            self.connections.remove(connection)

    async def _subscribe(self, connection: _Connection, channel: str, products: list[str]) -> None:
        connection.channels.setdefault(channel, set()).update(products)
        for product in products:
            market = self.markets[product]
            if channel == "level2" and not self.withhold_snapshots:
                event = {
                    "type": "snapshot",
                    "product_id": product,
                    "updates": market.snapshot_updates(),
                }
                await self._send(connection, "l2_data", [event])
            elif channel == "market_trades":
                event = {"type": "snapshot", "trades": list(reversed(market.tape[-100:]))}
                await self._send(connection, "market_trades", [event])
        await self._send(connection, "subscriptions", [{"subscriptions": {channel: products}}])

    async def _send(self, connection: _Connection, channel: str, events: list[Any]) -> None:
        seq = connection.next_seq
        connection.next_seq += 1
        if connection.frozen:
            return
        for rule in self._drops:
            if rule.channel == channel and rule.remaining > 0:
                rule.remaining -= 1
                self.dropped.append({"channel": channel, "seq": seq})
                return
        now_ns = max(market.now_ns for market in self.markets.values())
        envelope = {
            "channel": channel,
            "client_id": "",
            "timestamp": format_ts(now_ns + 1_000_000, digits=9),
            "sequence_num": seq,
            "events": events,
        }
        text = json.dumps(envelope)
        if channel == "l2_data":
            connection.last_book_message = text
        await self._send_text(connection, text)

    async def _send_text(self, connection: _Connection, text: str) -> None:
        # A connection the test has just aborted may not have been removed from the list yet.
        with contextlib.suppress(websockets.ConnectionClosed):
            await connection.ws.send(text)

    async def step(self, count: int = 1) -> None:
        """Advances every market and sends the changes to whoever is subscribed."""
        for _ in range(count):
            for product, market in self.markets.items():
                updates, trades = market.step()
                for connection in list(self.connections):
                    if connection.wants("level2", product):
                        event = {"type": "update", "product_id": product, "updates": updates}
                        await self._send(connection, "l2_data", [event])
                    if trades and connection.wants("market_trades", product):
                        await self._send(
                            connection, "market_trades", [{"type": "update", "trades": trades}]
                        )

    async def heartbeat(self) -> None:
        for connection in list(self.connections):
            if "heartbeats" in connection.channels:
                event = {"current_time": "fake", "heartbeat_counter": connection.next_seq}
                await self._send(connection, "heartbeats", [event])

    def drop_next(self, channel: str, count: int = 1) -> None:
        """Swallows the next messages on a channel while their sequence numbers are still used."""
        self._drops.append(_Drop(channel, count))

    def refuse_next_connections(self, count: int) -> None:
        self._refuse = count

    def stream_connections(self, product: str | None = None) -> list[_Connection]:
        """Connections subscribed to the heartbeats channel, which audit connections are not."""
        return [
            c
            for c in self.connections
            if "heartbeats" in c.channels and (product is None or c.wants("level2", product))
        ]

    async def abort_connections(self, product: str | None = None) -> None:
        """Kills connections with no close handshake, as a network failure would."""
        for connection in self.stream_connections(product):
            connection.ws.transport.abort()

    def freeze_connections(self, product: str | None = None) -> None:
        """Stops sending on connections while leaving the sockets open."""
        for connection in self.stream_connections(product):
            connection.frozen = True

    async def send_raw(self, text: str, product: str | None = None) -> None:
        for connection in self.stream_connections(product):
            await self._send_text(connection, text)

    async def send_with_seq(self, seq_offset: int, product: str | None = None) -> None:
        """Sends a heartbeat with its sequence number off by ``seq_offset``."""
        for connection in self.stream_connections(product):
            envelope = {
                "channel": "heartbeats",
                "timestamp": format_ts(START_NS, digits=9),
                "sequence_num": connection.next_seq + seq_offset,
                "events": [{"current_time": "fake", "heartbeat_counter": 0}],
            }
            await self._send_text(connection, json.dumps(envelope))

    async def send_live_trades_labelled_snapshot(self, product: str) -> list[str]:
        """Sends new trades in a ``snapshot`` event, as the live feed occasionally does."""
        market = self.markets[product]
        before = len(market.tape)
        while len(market.tape) == before:
            market.step()
        fresh = market.tape[before:]
        for connection in self.stream_connections(product):
            event = {"type": "snapshot", "trades": list(reversed(fresh))}
            await self._send(connection, "market_trades", [event])
        return [trade["trade_id"] for trade in fresh]

    async def send_book_snapshot(self, product: str) -> None:
        """Sends a full book in the middle of a session."""
        market = self.markets[product]
        for connection in self.stream_connections(product):
            event = {
                "type": "snapshot",
                "product_id": product,
                "updates": market.snapshot_updates(),
            }
            await self._send(connection, "l2_data", [event])

    async def resend_last_book_message(self, product: str | None = None) -> None:
        """Sends the latest book message again, with its original, now old, sequence number."""
        for connection in self.stream_connections(product):
            await self._send_text(connection, connection.last_book_message)

    async def wait_for_streams(self, count: int = 1, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while len(self.stream_connections()) < count:
            if time.monotonic() > deadline:
                raise TimeoutError(f"expected {count} stream connection(s)")
            await asyncio.sleep(0.002)
