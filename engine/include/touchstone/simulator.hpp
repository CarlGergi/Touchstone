#pragma once

// A market maker's own orders replayed against a recorded book, with queue position and latency.
//
// Times are nanoseconds on the exchange's clock. The recorded book is applied message by message
// at each message's exchange time, and trades at theirs. The strategy decides on what it had
// received, at each message's "seen" time, and its orders and cancels reach the exchange after a
// delay. The book does not contain our orders, so our queue position is an estimate:
//
// * An order joins the back of its level: everything resting there when it arrives is ahead.
// * A trade at our price uses up the queue ahead first, then fills us, possibly partially.
// * A trade at a worse price means the level was swept, so we are filled completely.
// * A fall in the level's size not explained by trades is cancellations, and a configurable share
//   of them is assumed to come from ahead of us. The queue ahead never exceeds the level's size.
//
// The naive model ignores all of that: any trade at or through our price fills us completely.

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <queue>
#include <random>
#include <stdexcept>
#include <vector>

#include "touchstone/order_book.hpp"

namespace touchstone {

struct SimConfig {
  bool naive = false;
  double cancel_share = 0.5;  // Share of unexplained level decreases assumed to be ahead of us.
  std::int64_t order_size = 0;  // Scaled by 1e8, as recorded sizes are.
  std::int64_t max_inventory_lots = 10;
  std::int64_t tick = 1'000'000;  // 0.01 in price units scaled by 1e8.
  int strategy = 0;  // 0: join the best bid and ask. 1: Avellaneda-Stoikov.
  double gamma = 0.1;  // Risk aversion, per lot.
  double kappa = 1.0;  // Order-arrival decay, per tick of distance.
  double sigma_ticks = 1.0;  // Mid volatility in ticks per square root of a second.
  double horizon_s = 60.0;  // The constant horizon of the infinite-horizon variant.
  double skew_ticks_per_unit = 0.0;  // Reservation price shift per unit of signal.
  std::uint64_t seed = 1;
};

struct Fill {
  std::int64_t ts;
  std::int64_t price;
  std::int64_t qty;  // Positive for a buy, negative for a sell.
};

struct SimResult {
  std::vector<Fill> fills;
  std::int64_t orders_sent = 0;
  std::int64_t cancels_sent = 0;
  std::int64_t cancelled_by_outage = 0;
  std::int64_t final_inventory = 0;
};

namespace detail {

struct Order {
  Side side;
  std::int64_t price;
  std::int64_t remaining;
  std::int64_t live_at;  // When it reaches the exchange.
  std::int64_t cancel_at = std::numeric_limits<std::int64_t>::max();
  double queue_ahead = 0;
  std::int64_t traded_here = 0;  // Volume traded at our price since the last book message.
  bool joined = false;  // Whether it has reached the exchange and taken its place in the queue.
  bool done = false;
};

inline bool is_live(const Order& o, std::int64_t now) { return !o.done && o.joined && now < o.cancel_at; }

}  // namespace detail

class Simulator {
 public:
  // Book stream: rows with event 0 (snapshot) or 1 (update), side, price, size, seq, session and
  // exchange time. Per message, in order: the time the strategy saw it, and a signal value.
  // Trades: exchange time, maker side, price, size, sorted by time.
  SimResult run(const std::uint8_t* event, const std::uint8_t* side, const std::int64_t* price,
                const std::int64_t* size, const std::int64_t* seq, const std::int64_t* session,
                const std::int64_t* ts, std::size_t rows, const std::int64_t* seen, const double* signal,
                std::size_t messages, const std::int64_t* trade_ts, const std::uint8_t* trade_side,
                const std::int64_t* trade_price, const std::int64_t* trade_size, std::size_t trades,
                const std::int64_t* delays, std::size_t n_delays, const SimConfig& config) {
    if (n_delays == 0) throw std::invalid_argument("at least one delay is needed");
    config_ = config;
    rng_.seed(config.seed);
    pick_ = std::uniform_int_distribution<std::size_t>(0, n_delays - 1);
    delays_ = delays;
    result_ = SimResult{};
    orders_.clear();
    book_.clear();
    inventory_ = 0;

    // Decisions wait in a queue until their time, so that latency can reorder them with the market.
    using Decision = std::pair<std::int64_t, std::size_t>;  // (seen time, message index)
    std::priority_queue<Decision, std::vector<Decision>, std::greater<>> decisions;
    struct Top {
      std::int64_t bid, ask;
    };
    std::vector<Top> tops;
    tops.reserve(messages);

    std::size_t row = 0, trade = 0, message = 0;
    std::int64_t current_session = std::numeric_limits<std::int64_t>::min();
    while (row < rows || trade < trades || !decisions.empty()) {
      const std::int64_t next_book = row < rows ? ts[row] : std::numeric_limits<std::int64_t>::max();
      const std::int64_t next_trade = trade < trades ? trade_ts[trade] : std::numeric_limits<std::int64_t>::max();
      const std::int64_t next_decision =
          decisions.empty() ? std::numeric_limits<std::int64_t>::max() : decisions.top().first;
      const std::int64_t market = std::min(next_book, next_trade);

      if (next_decision < market) {
        const auto [at, index] = decisions.top();
        decisions.pop();
        settle_arrivals(at);
        decide(at, tops[index], signal[index]);
        continue;
      }
      settle_arrivals(market);
      if (next_trade <= next_book) {
        on_trade(trade_ts[trade], static_cast<Side>(trade_side[trade]), trade_price[trade], trade_size[trade]);
        ++trade;
        continue;
      }

      // A book message: every row with this session and sequence number.
      if (session[row] != current_session) {
        cancel_all_for_outage();
        current_session = session[row];
      }
      const auto msg_seq = seq[row];
      const auto msg_session = session[row];
      const bool snapshot = event[row] == 0;
      std::vector<std::int64_t> before;
      before.reserve(orders_.size());
      for (const auto& o : orders_) before.push_back(book_.size_at(o.side, o.price));
      if (snapshot) book_.clear();
      for (; row < rows && seq[row] == msg_seq && session[row] == msg_session; ++row) {
        book_.set(static_cast<Side>(side[row]), price[row], size[row]);
      }
      on_book_change(before, next_book);
      const auto b = book_.best(Side::Bid);
      const auto a = book_.best(Side::Ask);
      tops.push_back({b ? b->first : 0, a ? a->first : 0});
      if (message >= messages) throw std::invalid_argument("more book messages than seen times");
      decisions.emplace(seen[message], message);
      ++message;
    }
    result_.final_inventory = inventory_;
    return result_;
  }

 private:
  std::int64_t delay() { return delays_[pick_(rng_)]; }

  // Orders whose time has come take their place at the back of their level's queue.
  void settle_arrivals(std::int64_t now) {
    for (auto& o : orders_) {
      if (o.done || o.joined || o.live_at > now) continue;
      if (o.cancel_at <= o.live_at) {
        o.done = true;  // Cancelled before it ever arrived.
        continue;
      }
      // A price that would cross the book is rejected, as a post-only order is.
      const auto opposite = book_.best(o.side == Side::Bid ? Side::Ask : Side::Bid);
      if (opposite && ((o.side == Side::Bid && o.price >= opposite->first) ||
                       (o.side == Side::Ask && o.price <= opposite->first))) {
        o.done = true;
        continue;
      }
      o.joined = true;
      o.queue_ahead = config_.naive ? 0.0 : static_cast<double>(book_.size_at(o.side, o.price));
    }
    for (auto& o : orders_) {
      if (!o.done && o.cancel_at <= now) o.done = true;
    }
    std::erase_if(orders_, [](const detail::Order& o) { return o.done; });
  }

  void fill(detail::Order& o, std::int64_t qty, std::int64_t at) {
    qty = std::min(qty, o.remaining);
    if (qty <= 0) return;
    o.remaining -= qty;
    if (o.remaining == 0) o.done = true;
    const auto signed_qty = o.side == Side::Bid ? qty : -qty;
    inventory_ += signed_qty;
    result_.fills.push_back({at, o.price, signed_qty});
  }

  void on_trade(std::int64_t at, Side maker, std::int64_t trade_price, std::int64_t trade_size) {
    for (auto& o : orders_) {
      if (o.side != maker || !is_live(o, at)) continue;
      const bool through = o.side == Side::Bid ? trade_price < o.price : trade_price > o.price;
      const bool at_price = trade_price == o.price;
      if (config_.naive) {
        if (through || at_price) fill(o, o.remaining, at);
        continue;
      }
      if (through) {
        fill(o, o.remaining, at);
      } else if (at_price) {
        o.traded_here += trade_size;
        const double left = static_cast<double>(trade_size) - o.queue_ahead;
        o.queue_ahead = std::max(0.0, o.queue_ahead - static_cast<double>(trade_size));
        if (left > 0) fill(o, static_cast<std::int64_t>(left), at);
      }
    }
  }

  void on_book_change(const std::vector<std::int64_t>& before, std::int64_t now) {
    if (config_.naive) return;
    for (std::size_t i = 0; i < orders_.size() && i < before.size(); ++i) {
      auto& o = orders_[i];
      if (!is_live(o, now)) continue;
      const auto after = book_.size_at(o.side, o.price);
      // Trades already moved us forward, so only the rest of the fall can be cancellations.
      const auto unexplained = (before[i] - after) - o.traded_here;
      if (unexplained > 0) o.queue_ahead -= config_.cancel_share * static_cast<double>(unexplained);
      o.queue_ahead = std::clamp(o.queue_ahead, 0.0, static_cast<double>(after));
      o.traded_here = 0;
    }
  }

  // The recording lost the stream, and Coinbase cancels a disconnected client's orders.
  void cancel_all_for_outage() {
    for (auto& o : orders_) {
      if (!o.done) ++result_.cancelled_by_outage;
    }
    orders_.clear();
  }

  // The order still working on a side: the newest one not already being cancelled.
  detail::Order* working(Side s) {
    for (auto it = orders_.rbegin(); it != orders_.rend(); ++it) {
      if (it->side == s && !it->done && it->cancel_at == std::numeric_limits<std::int64_t>::max()) return &*it;
    }
    return nullptr;
  }

  void decide(std::int64_t now, const auto& top, double signal) {
    if (top.bid == 0 || top.ask == 0) return;
    const auto tick = config_.tick;
    std::int64_t bid = top.bid, ask = top.ask;
    if (config_.strategy == 1) {
      const double lots = static_cast<double>(inventory_) / static_cast<double>(config_.order_size);
      const double mid_ticks = (static_cast<double>(top.bid) + static_cast<double>(top.ask)) / 2.0 / tick;
      const double variance = config_.sigma_ticks * config_.sigma_ticks * config_.horizon_s;
      const double reservation =
          mid_ticks - lots * config_.gamma * variance + config_.skew_ticks_per_unit * signal;
      const double half_spread =
          (config_.gamma * variance + (2.0 / config_.gamma) * std::log1p(config_.gamma / config_.kappa)) / 2.0;
      bid = static_cast<std::int64_t>(std::floor(reservation - half_spread)) * tick;
      ask = static_cast<std::int64_t>(std::ceil(reservation + half_spread)) * tick;
      // Never cross the visible book: at most one tick inside the opposite best.
      bid = std::min(bid, top.ask - tick);
      ask = std::max(ask, top.bid + tick);
    }
    const auto limit = config_.max_inventory_lots * config_.order_size;
    requote(Side::Bid, inventory_ + config_.order_size <= limit ? bid : 0, now);
    requote(Side::Ask, inventory_ - config_.order_size >= -limit ? ask : 0, now);
  }

  void requote(Side s, std::int64_t desired, std::int64_t now) {
    auto* current = working(s);
    if (current && current->price == desired) return;
    const auto d = config_.naive ? 0 : delay();
    if (current) {
      current->cancel_at = now + d;
      ++result_.cancels_sent;
    }
    if (desired > 0) {
      orders_.push_back({s, desired, config_.order_size, now + (config_.naive ? 0 : delay())});
      ++result_.orders_sent;
    }
  }

  SimConfig config_;
  std::mt19937_64 rng_;
  std::uniform_int_distribution<std::size_t> pick_;
  const std::int64_t* delays_ = nullptr;
  SimResult result_;
  std::vector<detail::Order> orders_;
  OrderBook book_;
  std::int64_t inventory_ = 0;
};

}  // namespace touchstone
