"""One product's connection to the market-data feed.

Each product gets its own connection because ``sequence_num`` is per connection. A gap says that
something on the connection was lost but not what, so the only safe response is to discard the
session and start again from a snapshot. Separate connections keep a gap in one product from
disturbing the other.
"""

from __future__ import annotations

import asyncio
import json
import random
import ssl
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import certifi
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from touchstone.recorder import protocol
from touchstone.recorder.config import RecorderConfig
from touchstone.recorder.health import HealthLog
from touchstone.recorder.protocol import ProtocolError, ServerError
from touchstone.recorder.sequence import SequenceStatus, SequenceTracker
from touchstone.recorder.storage import DayStore, utc_day

_CHANNELS = ("level2", "market_trades", "heartbeats")
_CONNECTION_ERRORS = (OSError, TimeoutError, EOFError, WebSocketException)


class Resync(Exception):
    """The current session can no longer be trusted, so a fresh one must be started."""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


class Pacer:
    """Spaces outbound connections and messages to stay inside Coinbase's per-IP rate limits."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second
        self._next = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        slot = max(now, self._next)
        self._next = slot + self._interval
        if slot > now:
            await asyncio.sleep(slot - now)


@dataclass(slots=True)
class FeedStatus:
    state: str = "connecting"
    session: int = 0
    last_message_ns: int = 0
    last_exchange_ts_ns: int = 0
    messages: int = 0
    rows: int = 0
    gaps: int = 0
    resyncs: int = 0
    out_of_order: int = 0
    ignored: int = 0
    audits: int = 0
    audit_failures: int = 0


@dataclass(slots=True)
class _Session:
    id: int
    day: str
    tracker: SequenceTracker
    sync_deadline: float
    synced: bool = False
    last_book_update: float = 0.0


class ProductFeed:
    def __init__(
        self,
        product: str,
        config: RecorderConfig,
        store: DayStore,
        health: HealthLog,
        pacer: Pacer,
        now_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.product = product
        self.config = config
        self.store = store
        self.health = health
        self.pacer = pacer
        self.now_ns = now_ns
        self.status = FeedStatus()
        self._last_session_id = 0
        self._outage_started_ns: int | None = None
        self._ssl = (
            ssl.create_default_context(cafile=certifi.where())
            if config.url.startswith("wss://")
            else None
        )

    def status_dict(self) -> dict[str, Any]:
        return asdict(self.status)

    async def run(self) -> None:
        """Keeps a session alive forever, restarting from a snapshot whenever one is lost."""
        failures = 0
        while True:
            started = time.monotonic()
            reason, details = await self._attempt_session()
            lived_s = time.monotonic() - started
            self.status.state = "resyncing"
            self.status.resyncs += 1
            self._outage_started_ns = self.now_ns()
            self.health.emit(
                "resync",
                product=self.product,
                session=self.status.session,
                reason=reason,
                lived_s=round(lived_s, 3),
                **details,
            )
            failures = 0 if lived_s >= self.config.stable_after_s else failures + 1
            if reason != "day_rollover":
                await asyncio.sleep(self._backoff_s(failures))

    def _backoff_s(self, failures: int) -> float:
        ceiling = min(self.config.backoff_cap_s, self.config.backoff_base_s * 2**failures)
        return random.uniform(ceiling / 2, ceiling)

    async def _attempt_session(self) -> tuple[str, dict[str, Any]]:
        try:
            await self._run_session()
        except Resync as exc:
            return exc.reason, exc.details
        except _CONNECTION_ERRORS as exc:
            return "connection_lost", {"error": repr(exc)}
        raise AssertionError("a session can only end by raising")

    def _new_session_id(self) -> int:
        self._last_session_id = max(self.now_ns(), self._last_session_id + 1)
        return self._last_session_id

    def _connect(self) -> connect:
        return connect(
            self.config.url,
            ssl=self._ssl,
            max_size=self.config.max_message_bytes,
            open_timeout=self.config.open_timeout_s,
            close_timeout=1,
        )

    async def _subscribe(self, ws: ClientConnection, channel: str) -> None:
        await self.pacer.wait()
        message = {"type": "subscribe", "channel": channel, "product_ids": [self.product]}
        await ws.send(json.dumps(message))

    async def _run_session(self) -> None:
        self.status.state = "connecting"
        await self.pacer.wait()
        async with self._connect() as ws:
            now = time.monotonic()
            session = _Session(
                id=self._new_session_id(),
                day=utc_day(self.now_ns()),
                tracker=SequenceTracker(),
                sync_deadline=now + self.config.snapshot_timeout_s,
                last_book_update=now,
            )
            self.status.session = session.id
            self.status.state = "syncing"
            self.health.emit("connected", product=self.product, session=session.id)
            for channel in _CHANNELS:
                await self._subscribe(ws, channel)
            rtt_task = asyncio.create_task(self._sample_rtt(ws, session.id))
            try:
                await self._read_loop(ws, session)
            finally:
                rtt_task.cancel()

    async def _read_loop(self, ws: ClientConnection, session: _Session) -> None:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), self.config.stale_after_s)
            except TimeoutError:
                raise Resync("stale", silent_s=self.config.stale_after_s) from None
            self._handle(session, raw, self.now_ns())
            self._check_watchdogs(session)

    def _check_watchdogs(self, session: _Session) -> None:
        now = time.monotonic()
        if not session.synced:
            if now > session.sync_deadline:
                raise Resync("snapshot_timeout", waited_s=self.config.snapshot_timeout_s)
        elif now - session.last_book_update > self.config.book_stale_after_s:
            raise Resync("book_stale", silent_s=self.config.book_stale_after_s)

    def _handle(self, session: _Session, raw: str | bytes, local_ts_ns: int) -> None:
        self.status.last_message_ns = local_ts_ns
        self.status.messages += 1
        try:
            message = protocol.decode(raw)
            if message is None:
                self.status.ignored += 1
                return
            if self._in_order(session, message.seq):
                self._dispatch(session, message, local_ts_ns)
        except ServerError as exc:
            raise Resync("server_error", error=str(exc)) from exc
        except ProtocolError as exc:
            # An unreadable message may have changed the book, so it counts as a lost one.
            raise Resync("malformed", error=str(exc)) from exc

    def _in_order(self, session: _Session, seq: int) -> bool:
        tracker = session.tracker
        if tracker.expected is None and seq != 0:
            self.health.emit(
                "unexpected_first_seq", product=self.product, session=session.id, seq=seq
            )
        expected = tracker.expected
        status = tracker.observe(seq)
        if status is SequenceStatus.GAP:
            self.status.gaps += 1
            lost = seq - expected
            self.health.emit(
                "gap",
                product=self.product,
                session=session.id,
                expected=expected,
                got=seq,
                lost=lost,
            )
            raise Resync("gap", expected=expected, got=seq, lost=lost)
        if status is SequenceStatus.STALE:
            self.status.out_of_order += 1
            self.health.emit(
                "out_of_order", product=self.product, session=session.id, expected=expected, got=seq
            )
            return False
        return True

    def _dispatch(self, session: _Session, message: protocol.Message, local_ts_ns: int) -> None:
        # The first message of a new UTC day ends the session, so that every daily file starts
        # with a real exchange snapshot and can be replayed by itself.
        day = utc_day(local_ts_ns)
        if day != session.day:
            raise Resync("day_rollover", previous_day=session.day, day=day)

        if message.channel == protocol.BOOK_CHANNEL:
            self._on_book(session, message, local_ts_ns, day)
        elif message.channel == protocol.TRADES_CHANNEL:
            rows = protocol.trade_rows(
                message, product=self.product, local_ts_ns=local_ts_ns, session=session.id
            )
            self.store.append(day, rows)
            self.status.rows += len(rows)
        elif message.channel not in (protocol.HEARTBEAT_CHANNEL, "subscriptions"):
            self.status.ignored += 1

    def _on_book(
        self, session: _Session, message: protocol.Message, local_ts_ns: int, day: str
    ) -> None:
        is_snapshot = protocol.has_book_snapshot(message)
        if not session.synced and not is_snapshot:
            raise Resync("update_before_snapshot", seq=message.seq)
        rows = protocol.book_rows(
            message, product=self.product, local_ts_ns=local_ts_ns, session=session.id
        )
        self.store.append(day, rows)
        self.status.rows += len(rows)
        session.last_book_update = time.monotonic()
        if rows:
            self.status.last_exchange_ts_ns = rows[-1][2]
        if is_snapshot and not session.synced:
            session.synced = True
            self.status.state = "live"
            outage_ms = None
            if self._outage_started_ns is not None:
                outage_ms = round((local_ts_ns - self._outage_started_ns) / 1e6, 3)
                self._outage_started_ns = None
            self.health.emit(
                "synced",
                product=self.product,
                session=session.id,
                levels=len(rows),
                outage_ms=outage_ms,
            )

    async def _sample_rtt(self, ws: ClientConnection, session: int) -> None:
        """Records WebSocket ping round trips, which the latency model samples from."""
        interval = self.config.rtt_interval_s
        if interval <= 0:
            return
        try:
            while True:
                await asyncio.sleep(interval)
                pong = await ws.ping()
                try:
                    rtt_s = await asyncio.wait_for(pong, interval)
                except TimeoutError:
                    self.health.emit("rtt_timeout", product=self.product, session=session)
                    continue
                self.health.emit(
                    "rtt", product=self.product, session=session, rtt_ms=round(rtt_s * 1e3, 3)
                )
        except ConnectionClosed:
            return

    async def run_audits(self) -> None:
        """Periodically records an independent snapshot to check the replayed book against."""
        interval = self.config.audit_interval_s
        if interval <= 0:
            return
        while True:
            await asyncio.sleep(interval)
            try:
                async with asyncio.timeout(self.config.snapshot_timeout_s):
                    levels = await self._audit_once()
            except (*_CONNECTION_ERRORS, ProtocolError, ServerError) as exc:
                self.status.audit_failures += 1
                self.health.emit("audit_failed", product=self.product, error=repr(exc))
            else:
                self.status.audits += 1
                self.health.emit("audit", product=self.product, levels=levels)

    async def _audit_once(self) -> int:
        await self.pacer.wait()
        async with self._connect() as ws:
            session = self._new_session_id()
            await self._subscribe(ws, "level2")
            while True:
                raw = await ws.recv()
                local_ts_ns = self.now_ns()
                message = protocol.decode(raw)
                if (
                    message is None
                    or message.channel != protocol.BOOK_CHANNEL
                    or not protocol.has_book_snapshot(message)
                ):
                    continue
                rows = protocol.book_rows(
                    message,
                    product=self.product,
                    local_ts_ns=local_ts_ns,
                    session=session,
                    snapshot_label="audit_snapshot",
                )
                self.store.append(utc_day(local_ts_ns), rows)
                self.status.rows += len(rows)
                return len(rows)
