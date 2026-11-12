// Python bindings for the order book, the replay pass, and the ring-buffer pipeline benchmark.

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <stdexcept>
#include <thread>
#include <vector>

#if defined(__APPLE__)
#include <pthread/qos.h>
#endif

#include "touchstone/flat_book.hpp"
#include "touchstone/order_book.hpp"
#include "touchstone/ring_buffer.hpp"
#include "touchstone/simulator.hpp"

namespace py = pybind11;
using touchstone::FlatBook;
using touchstone::OrderBook;
using touchstone::Side;

namespace {

// Ticks each side of a flat book keeps in its array unless told otherwise: about 1 million.
constexpr std::size_t kWindow = std::size_t{1} << 20;

// Codes of the recorded event column, in the order of its enum.
constexpr std::uint8_t kSnapshot = 0;
constexpr std::uint8_t kUpdate = 1;

template <typename T>
using Array = py::array_t<T, py::array::c_style | py::array::forcecast>;

// One replayable stream: book rows only, in arrival order. A message is a run of rows with the same
// session and sequence number; a message whose first row is a snapshot replaces the book.
struct Stream {
  const std::uint8_t* event;
  const std::uint8_t* side;
  const std::int64_t* price;
  const std::int64_t* size;
  const std::int64_t* seq;
  const std::int64_t* session;
  const std::int64_t* ts;
  std::size_t n;
};

Stream make_stream(const Array<std::uint8_t>& event, const Array<std::uint8_t>& side,
                   const Array<std::int64_t>& price, const Array<std::int64_t>& size,
                   const Array<std::int64_t>& seq, const Array<std::int64_t>& session,
                   const Array<std::int64_t>& ts) {
  const auto n = static_cast<std::size_t>(event.size());
  for (const auto size_of : {side.size(), price.size(), size.size(), seq.size(), session.size(), ts.size()}) {
    if (static_cast<std::size_t>(size_of) != n) throw std::invalid_argument("columns differ in length");
  }
  return {event.data(), side.data(), price.data(), size.data(), seq.data(), session.data(), ts.data(), n};
}

// Runs f with the book a replay asked for: the flat book on a tick grid when tick is positive,
// otherwise the std::map book.
template <typename F>
decltype(auto) with_book(std::int64_t tick, std::size_t window, F&& f) {
  if (tick > 0) {
    FlatBook book(tick, window);
    return f(book);
  }
  OrderBook book;
  return f(book);
}

// Applies one message starting at row i and returns the row after it.
template <typename Book>
std::size_t apply_message(Book& book, const Stream& s, std::size_t i) {
  const auto seq = s.seq[i];
  const auto session = s.session[i];
  if (s.event[i] == kSnapshot) book.clear();
  for (; i < s.n && s.seq[i] == seq && s.session[i] == session; ++i) {
    if (s.event[i] != kSnapshot && s.event[i] != kUpdate) throw std::invalid_argument("non-book row in stream");
    book.set(static_cast<Side>(s.side[i]), s.price[i], s.size[i]);
  }
  return i;
}

// Replays a stream and returns the top of the book after every message.
py::dict replay_top(const Array<std::uint8_t>& event, const Array<std::uint8_t>& side,
                    const Array<std::int64_t>& price, const Array<std::int64_t>& size,
                    const Array<std::int64_t>& seq, const Array<std::int64_t>& session,
                    const Array<std::int64_t>& ts, std::int64_t tick, std::size_t window) {
  const Stream s = make_stream(event, side, price, size, seq, session, ts);
  std::vector<std::int64_t> out_ts, bid, bid_size, ask, ask_size, out_session;
  {
    py::gil_scoped_release release;
    with_book(tick, window, [&](auto& book) {
    for (std::size_t i = 0; i < s.n;) {
      const auto start = i;
      i = apply_message(book, s, i);
      const auto b = book.best(Side::Bid);
      const auto a = book.best(Side::Ask);
      out_ts.push_back(s.ts[start]);
      out_session.push_back(s.session[start]);
      bid.push_back(b ? b->first : 0);
      bid_size.push_back(b ? b->second : 0);
      ask.push_back(a ? a->first : 0);
      ask_size.push_back(a ? a->second : 0);
    }
    });
  }
  py::dict result;
  result["exchange_ts_ns"] = py::array(py::cast(out_ts));
  result["session"] = py::array(py::cast(out_session));
  result["bid"] = py::array(py::cast(bid));
  result["bid_size"] = py::array(py::cast(bid_size));
  result["ask"] = py::array(py::cast(ask));
  result["ask_size"] = py::array(py::cast(ask_size));
  return result;
}

// Replays a stream, reading the top of the book after every message as replay_top does, and
// returns rows per second. This is the workload the benchmark compares against Python.
double replay_rate(const Array<std::uint8_t>& event, const Array<std::uint8_t>& side,
                   const Array<std::int64_t>& price, const Array<std::int64_t>& size,
                   const Array<std::int64_t>& seq, const Array<std::int64_t>& session,
                   const Array<std::int64_t>& ts, std::int64_t tick, std::size_t window) {
  const Stream s = make_stream(event, side, price, size, seq, session, ts);
  py::gil_scoped_release release;
  return with_book(tick, window, [&](auto& book) {
    // The book is built before the clock starts, so the flat book's array allocation is not timed.
    const auto start = std::chrono::steady_clock::now();
    std::int64_t sink = 0;
    for (std::size_t i = 0; i < s.n;) {
      i = apply_message(book, s, i);
      const auto b = book.best(Side::Bid);
      const auto a = book.best(Side::Ask);
      sink += (b ? b->first : 0) - (a ? a->first : 0);
    }
    const std::chrono::duration<double> elapsed = std::chrono::steady_clock::now() - start;
    // Using the sum keeps the compiler from dropping the reads.
    return static_cast<double>(s.n) / elapsed.count() + static_cast<double>(sink == 42) * 0.0;
  });
}

// Replays a stream and captures the book just before each checkpoint time: every message whose
// exchange time is at or before the checkpoint is applied, and the levels touched by the next
// message are returned too, because a snapshot taken between two messages may already hold part
// of the later one. A checkpoint is skipped when no session covers it on both sides.
py::list replay_checkpoints(const Array<std::uint8_t>& event, const Array<std::uint8_t>& side,
                            const Array<std::int64_t>& price, const Array<std::int64_t>& size,
                            const Array<std::int64_t>& seq, const Array<std::int64_t>& session,
                            const Array<std::int64_t>& ts, const Array<std::int64_t>& checkpoints) {
  const Stream s = make_stream(event, side, price, size, seq, session, ts);
  py::list out;
  OrderBook book;
  std::size_t i = 0;
  bool live = false;
  for (py::ssize_t c = 0; c < checkpoints.size(); ++c) {
    const auto at = checkpoints.data()[c];
    while (i < s.n && s.ts[i] <= at) {
      if (s.event[i] == kSnapshot) live = true;
      if (live && i > 0 && s.session[i] != s.session[i - 1] && s.event[i] != kSnapshot) live = false;
      i = apply_message(book, s, i);
    }
    // The next message must continue the same session, or the book cannot be trusted up to `at`.
    if (!live || i == 0 || i >= s.n || s.session[i] != s.session[i - 1] || s.event[i] == kSnapshot) {
      out.append(py::none());
      continue;
    }
    py::list touched;
    for (std::size_t j = i; j < s.n && s.seq[j] == s.seq[i] && s.session[j] == s.session[i]; ++j) {
      touched.append(py::make_tuple(s.side[j], s.price[j]));
    }
    py::dict state;
    state["bids"] = book.levels(Side::Bid);
    state["asks"] = book.levels(Side::Ask);
    state["touched"] = touched;
    state["last_ts"] = s.ts[i - 1];
    out.append(state);
  }
  return out;
}

struct Item {
  std::int64_t price;
  std::int64_t size;
  std::int64_t pushed_ns;
  std::uint8_t side;
  std::uint8_t snapshot_start;
};

// Asks the scheduler to keep the calling thread on a fast core, as a latency-sensitive thread
// would be. Linux has no equivalent without privileges, so it is a no-op there.
void prefer_fast_core() {
#if defined(__APPLE__)
  pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
#endif
}

std::int64_t now_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

// Runs the network-thread to engine handoff: a producer thread pushes recorded rows into the ring
// at a fixed rate, and a consumer thread applies them to a book, recording how long each row waited
// between push and applied. Pacing below capacity measures handoff latency rather than queueing.
py::dict pipeline(const Array<std::uint8_t>& event, const Array<std::uint8_t>& side,
                  const Array<std::int64_t>& price, const Array<std::int64_t>& size,
                  const Array<std::int64_t>& seq, const Array<std::int64_t>& session,
                  const Array<std::int64_t>& ts, double rate_per_s, std::int64_t tick, std::size_t window) {
  const Stream s = make_stream(event, side, price, size, seq, session, ts);
  auto ring = std::make_unique<touchstone::SpscRing<Item, 1 << 16>>();
  std::vector<std::uint32_t> latency(s.n);
  std::uint64_t checksum = 0;
  double seconds = 0;
  std::int64_t best_bid = 0, best_ask = 0;
  {
    py::gil_scoped_release release;
    with_book(tick, window, [&](auto& book) {
    const auto interval_ns = rate_per_s > 0 ? static_cast<std::int64_t>(1e9 / rate_per_s) : 0;
    const auto start = now_ns();
    prefer_fast_core();
    std::thread consumer([&] {
      prefer_fast_core();
      Item item{};
      for (std::size_t done = 0; done < s.n;) {
        if (!ring->try_pop(item)) continue;
        if (item.snapshot_start) book.clear();
        book.set(static_cast<Side>(item.side), item.price, item.size);
        checksum = checksum * 1099511628211ULL + static_cast<std::uint64_t>(item.price ^ item.size);
        latency[done++] = static_cast<std::uint32_t>(std::min<std::int64_t>(now_ns() - item.pushed_ns, UINT32_MAX));
      }
    });
    for (std::size_t i = 0; i < s.n; ++i) {
      if (interval_ns > 0) {
        const auto due = start + static_cast<std::int64_t>(i) * interval_ns;
        while (now_ns() < due) {
        }
      }
      const bool snapshot_start =
          s.event[i] == kSnapshot && (i == 0 || s.seq[i] != s.seq[i - 1] || s.session[i] != s.session[i - 1]);
      Item item{s.price[i], s.size[i], now_ns(), s.side[i], static_cast<std::uint8_t>(snapshot_start)};
      while (!ring->try_push(item)) {
        // The ring is full: spin until the consumer frees a slot.
      }
    }
    consumer.join();
    seconds = static_cast<double>(now_ns() - start) / 1e9;
    const auto b = book.best(Side::Bid);
    const auto a = book.best(Side::Ask);
    best_bid = b ? b->first : 0;
    best_ask = a ? a->first : 0;
    });
  }
  py::dict result;
  result["latency_ns"] = py::array(py::cast(latency));
  result["seconds"] = seconds;
  result["checksum"] = checksum;
  result["bid"] = best_bid;
  result["ask"] = best_ask;
  return result;
}

// Runs the fill simulator over one stream. See simulator.hpp for the model.
py::dict simulate(const Array<std::uint8_t>& event, const Array<std::uint8_t>& side,
                  const Array<std::int64_t>& price, const Array<std::int64_t>& size,
                  const Array<std::int64_t>& seq, const Array<std::int64_t>& session,
                  const Array<std::int64_t>& ts, const Array<std::int64_t>& seen,
                  const Array<double>& signal, const Array<std::int64_t>& trade_ts,
                  const Array<std::uint8_t>& trade_side, const Array<std::int64_t>& trade_price,
                  const Array<std::int64_t>& trade_size, const Array<std::int64_t>& delays,
                  const touchstone::SimConfig& config) {
  const Stream s = make_stream(event, side, price, size, seq, session, ts);
  if (seen.size() != signal.size()) throw std::invalid_argument("seen and signal differ in length");
  const auto n_trades = static_cast<std::size_t>(trade_ts.size());
  if (static_cast<std::size_t>(trade_side.size()) != n_trades || static_cast<std::size_t>(trade_price.size()) != n_trades ||
      static_cast<std::size_t>(trade_size.size()) != n_trades) {
    throw std::invalid_argument("trade columns differ in length");
  }
  touchstone::SimResult result;
  {
    py::gil_scoped_release release;
    touchstone::Simulator simulator;
    result = simulator.run(s.event, s.side, s.price, s.size, s.seq, s.session, s.ts, s.n, seen.data(),
                           signal.data(), static_cast<std::size_t>(seen.size()), trade_ts.data(),
                           trade_side.data(), trade_price.data(), trade_size.data(), n_trades,
                           delays.data(), static_cast<std::size_t>(delays.size()), config);
  }
  std::vector<std::int64_t> fill_ts, fill_price, fill_qty;
  for (const auto& f : result.fills) {
    fill_ts.push_back(f.ts);
    fill_price.push_back(f.price);
    fill_qty.push_back(f.qty);
  }
  py::dict out;
  out["fill_ts"] = py::array(py::cast(fill_ts));
  out["fill_price"] = py::array(py::cast(fill_price));
  out["fill_qty"] = py::array(py::cast(fill_qty));
  out["orders_sent"] = result.orders_sent;
  out["cancels_sent"] = result.cancels_sent;
  out["cancelled_by_outage"] = result.cancelled_by_outage;
  out["final_inventory"] = result.final_inventory;
  return out;
}

}  // namespace

PYBIND11_MODULE(touchstone_engine, m) {
  m.doc() = "C++ order book, replay engine and single-producer single-consumer ring buffer";

  py::enum_<Side>(m, "Side").value("BID", Side::Bid).value("ASK", Side::Ask);

  py::class_<OrderBook>(m, "OrderBook")
      .def(py::init<>())
      .def("clear", &OrderBook::clear)
      .def("set", &OrderBook::set, py::arg("side"), py::arg("price"), py::arg("size"))
      .def("best", &OrderBook::best, py::arg("side"))
      .def("size_at", &OrderBook::size_at, py::arg("side"), py::arg("price"))
      .def("depth", &OrderBook::depth, py::arg("side"))
      .def("levels", &OrderBook::levels, py::arg("side"), py::arg("limit") = 0);

  py::class_<FlatBook>(m, "FlatBook")
      .def(py::init<std::int64_t, std::size_t>(), py::arg("tick"), py::arg("window") = kWindow)
      .def("clear", &FlatBook::clear)
      .def("set", &FlatBook::set, py::arg("side"), py::arg("price"), py::arg("size"))
      .def("best", &FlatBook::best, py::arg("side"))
      .def("size_at", &FlatBook::size_at, py::arg("side"), py::arg("price"))
      .def("depth", &FlatBook::depth, py::arg("side"))
      .def("levels", &FlatBook::levels, py::arg("side"), py::arg("limit") = 0)
      .def_property_readonly("recenters", &FlatBook::recenters);

  py::class_<touchstone::SimConfig>(m, "SimConfig")
      .def(py::init<>())
      .def_readwrite("naive", &touchstone::SimConfig::naive)
      .def_readwrite("cancel_share", &touchstone::SimConfig::cancel_share)
      .def_readwrite("order_size", &touchstone::SimConfig::order_size)
      .def_readwrite("max_inventory_lots", &touchstone::SimConfig::max_inventory_lots)
      .def_readwrite("tick", &touchstone::SimConfig::tick)
      .def_readwrite("strategy", &touchstone::SimConfig::strategy)
      .def_readwrite("gamma", &touchstone::SimConfig::gamma)
      .def_readwrite("kappa", &touchstone::SimConfig::kappa)
      .def_readwrite("sigma_ticks", &touchstone::SimConfig::sigma_ticks)
      .def_readwrite("horizon_s", &touchstone::SimConfig::horizon_s)
      .def_readwrite("skew_ticks_per_unit", &touchstone::SimConfig::skew_ticks_per_unit)
      .def_readwrite("seed", &touchstone::SimConfig::seed);

  m.def("simulate", &simulate, "Our orders against a recorded book, with queue position and latency");
  m.def("replay_top", &replay_top, "Top of the book after every message", py::arg("event"), py::arg("side"), py::arg("price"), py::arg("size"), py::arg("seq"), py::arg("session"), py::arg("ts"),
        py::arg("tick") = 0, py::arg("window") = kWindow);
  m.def("replay_rate", &replay_rate, "Rows replayed per second", py::arg("event"), py::arg("side"), py::arg("price"), py::arg("size"), py::arg("seq"), py::arg("session"), py::arg("ts"), py::arg("tick") = 0,
        py::arg("window") = kWindow);
  m.def("replay_checkpoints", &replay_checkpoints, "Book state just before each checkpoint time");
  m.def("pipeline", &pipeline, "Producer to consumer handoff through the ring buffer", py::arg("event"),
        py::arg("side"), py::arg("price"), py::arg("size"), py::arg("seq"), py::arg("session"), py::arg("ts"),
        py::arg("rate_per_s"), py::arg("tick") = 0, py::arg("window") = kWindow);
}
