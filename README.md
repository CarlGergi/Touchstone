# Touchstone

A queue-aware market-making simulator. Touchstone records Coinbase's public order book, replays it event by event, and tests a market maker against it with realistic fills. The headline result is how much a naive backtest, which assumes a quote fills as soon as the price touches it, overstates profit compared with one that models queue position and latency.

| Milestone | Status |
| --- | --- |
| 1. Recorder | Done |
| 2. C++ order book, replay engine, ring buffer | Not started |
| 3. Queue estimator, latency model, fill simulator | Not started |
| 4. Signal study | Not started |
| 5. Strategy ladder, P&L attribution, held-out evaluation | Not started |

## Quick start

Requires [uv](https://docs.astral.sh/uv/), and Docker to run the recorder as a service.

```bash
make test        # run the test suite
make reproduce   # rebuild every result in the repository
make record      # record to ./data in the foreground
make docker-up   # record to ./data in a container that restarts on failure
make docker-logs
make docker-down
```

## Recorder

The recorder subscribes to the `level2`, `market_trades`, and `heartbeats` channels of Coinbase's [Advanced Trade WebSocket feed](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-overview), which is public and needs no credentials. It writes one Parquet file per product per day.

### Keeping the book correct

The output's central guarantee is that replaying any recorded session reproduces the exchange's book exactly. A lost message that went unnoticed would corrupt every later price, so:

- **Gap detection.** `sequence_num` is per connection and rises by one across all channels, so each product gets its own connection and any jump counts as a loss.
- **Resync from snapshots.** A gap only says that something was lost, not what. The recorder discards the session without writing the message that exposed the gap, reconnects, and starts again from a fresh snapshot.
- **Treating doubt as loss.** An unreadable message, a server error, a silent socket, a book that stops updating while heartbeats continue, or a missing snapshot all force a resync.
- **Self-contained days.** The session is restarted at midnight UTC, so each daily file starts with a real exchange snapshot.

### Audit snapshots

Every 15 minutes a second connection fetches a snapshot and records it as `audit_snapshot` without disturbing the main session. These give the replay engine independent checkpoints to verify the rebuilt book against.

An audit's time often falls between two stream batches, and the snapshot may already include part of the later batch. So a rebuilt book is compared with an audit like this: applying every batch up to the audit's time must match it exactly, except at levels touched by the next batch. On the live feed, all 22 audits checked held to this rule, at about 40,000 levels for BTC-USD and 19,000 for ETH-USD, and the 11 landing exactly on a batch time matched on every level.

### Files

```text
data/
  BTC-USD/2026-11-04.parquet                    one file per product per finished day
  .spool/BTC-USD/2026-11-05/part-00000001.parquet   rows for the current day
  health/2026-11-05.jsonl                       health log
  status.json                                   current state, read by the container health check
```

A Parquet file is unreadable until its footer is written, so holding a daily file open would risk the whole day on a crash. Rows are written instead as complete part files every 30 seconds and merged once the day is over, which limits a crash to the last 30 seconds of buffered rows. The merge is atomic, and recovery on restart handles a crash at any step. `touchstone.recorder.storage.read_day` reads a day whether or not it has been merged.

### Schema

Each row is one changed book level or one trade.

| Column | Type | Meaning |
| --- | --- | --- |
| `local_ts_ns` | Int64 | Local wall clock when the message was received |
| `server_ts_ns` | Int64 | When Coinbase's WebSocket server sent the message |
| `exchange_ts_ns` | Int64 | The matching engine's time for this level change or trade |
| `session` | Int64 | Identifies one connection; its value is the time the connection opened |
| `seq` | Int64 | `sequence_num` of the message, unique within a session |
| `event` | Enum | See below |
| `side` | Enum | `bid` or `ask`. For trades, the side of the resting (maker) order |
| `price_e8` | Int64 | Price multiplied by 1e8, which keeps it exact |
| `size_e8` | Int64 | New absolute size at the level, or traded size, multiplied by 1e8. Zero removes a level |
| `trade_id` | Int64 | Trades only |

| `event` | Meaning |
| --- | --- |
| `snapshot` | The full book at the start of a session. Replay resets its book here |
| `update` | A change to one level |
| `trade` | A trade Coinbase labelled as an update |
| `trade_snapshot` | A trade Coinbase labelled as a snapshot. These are the last 100 trades on subscribing, which can predate the session, but also occasional batches of new trades in mid-session that are delivered nowhere else |
| `audit_snapshot` | An independent book snapshot from a separate connection. Not part of the replay stream |

Rows are in arrival order. To replay, take one `session` at a time and read `snapshot` and `update` rows in file order.

### Things to know when reading the data

- **Use both trade labels.** Filtering on `event == "trade"` silently loses real trades. Take `trade` and `trade_snapshot` together. Repeats are already removed, and trade ids rise by one per product, so `verify` can count any that are missing.
- **Sort trades by `exchange_ts_ns`.** Coinbase lists each batch newest first.
- **Keep what was known separate from what happened.** Trades are batched over 250 ms, so a trade's `local_ts_ns` can trail its `exchange_ts_ns` by that much. Anything simulating what a strategy could have known at a given moment has to use the local time, or it will see trades before they could have arrived.
- **The book is conflated.** Book changes arrive every 50 ms, with one row per level holding its final size and a single event time. Changes inside a batch are invisible: an order added and filled within 50 ms never appears, and a drop in size cannot be split into fills and cancellations without the trades.

### Health log

`health/YYYY-MM-DD.jsonl` holds one JSON object per line. The events are `start`, `stop`, `fatal`, `connected`, `synced`, `gap`, `out_of_order`, `resync` (with its reason), `audit`, `audit_failed`, `compacted`, `compaction_failed`, `stats`, `low_disk`, and `rtt`. `rtt` records WebSocket ping round trips to the exchange, which the latency model samples from.

### Commands

```bash
uv run python -m touchstone.recorder run --data-dir data
uv run python -m touchstone.recorder verify BTC-USD 2026-11-04 --data-dir data
uv run python -m touchstone.recorder healthcheck --data-dir data
uv run python -m touchstone.recorder compact --data-dir data
```

`verify` checks a recorded day for the invariants replay depends on and exits non-zero if any fail.

### Testing

The tests run the real recorder over a real WebSocket against `tests/fake_feed.py`, which speaks the same protocol, misbehaves on demand, and keeps the true book after every step. Its faults include dropped messages on any channel, aborted and silent connections, garbage, replayed messages, refused connections, and oversized snapshots. The main check replays every recorded session and compares the book with the truth after every message, including under a seeded storm of random faults. Other tests kill the recorder with `SIGKILL` and crash the daily merge at each step.
