#pragma once

#include <algorithm>
#include <bit>
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <stdexcept>
#include <utility>
#include <vector>

#include "touchstone/order_book.hpp"

namespace touchstone {

// A set of indices in [0, n) that finds its largest and smallest member in three word reads, with
// one bit per index and two summary levels above it (a bit is set when its 64 children have any).
class LevelBitmap {
 public:
  explicit LevelBitmap(std::size_t n)
      : l0_((n + 63) / 64), l1_((l0_.size() + 63) / 64), l2_((l1_.size() + 63) / 64) {}

  void set(std::size_t i) {
    l0_[i >> 6] |= bit(i);
    l1_[i >> 12] |= bit(i >> 6);
    l2_[i >> 18] |= bit(i >> 12);
  }

  void reset(std::size_t i) {
    if ((l0_[i >> 6] &= ~bit(i)) != 0) return;
    if ((l1_[i >> 12] &= ~bit(i >> 6)) != 0) return;
    l2_[i >> 18] &= ~bit(i >> 12);
  }

  std::optional<std::size_t> max() const {
    for (std::size_t w = l2_.size(); w-- > 0;) {
      if (l2_[w] == 0) continue;
      const std::size_t j1 = (w << 6) | high(l2_[w]);
      const std::size_t j0 = (j1 << 6) | high(l1_[j1]);
      return (j0 << 6) | high(l0_[j0]);
    }
    return std::nullopt;
  }

  std::optional<std::size_t> min() const {
    for (std::size_t w = 0; w < l2_.size(); ++w) {
      if (l2_[w] == 0) continue;
      const std::size_t j1 = (w << 6) | low(l2_[w]);
      const std::size_t j0 = (j1 << 6) | low(l1_[j1]);
      return (j0 << 6) | low(l0_[j0]);
    }
    return std::nullopt;
  }

  // Calls f(i) for every member, in ascending order, skipping empty words.
  template <typename F>
  void for_each(F&& f) const {
    for (std::size_t w = 0; w < l0_.size(); ++w) {
      for (auto word = l0_[w]; word != 0; word &= word - 1) f((w << 6) | low(word));
    }
  }

  void clear() {
    std::fill(l0_.begin(), l0_.end(), 0);
    std::fill(l1_.begin(), l1_.end(), 0);
    std::fill(l2_.begin(), l2_.end(), 0);
  }

 private:
  static std::uint64_t bit(std::size_t i) { return std::uint64_t{1} << (i & 63); }
  static std::size_t high(std::uint64_t w) { return 63 - static_cast<std::size_t>(std::countl_zero(w)); }
  static std::size_t low(std::uint64_t w) { return static_cast<std::size_t>(std::countr_zero(w)); }

  std::vector<std::uint64_t> l0_, l1_, l2_;
};

// One side of a FlatBook. Levels on the tick grid within a window around the best price live in a
// flat array indexed by tick, so an update is an array write and the best price is a bitmap scan.
// Everything else, far from the touch or off the grid, lives in an ordered map. When the best price
// leaves the window, the window is moved to centre on it.
template <bool IsBid>
class Ladder {
 public:
  using Level = std::pair<std::int64_t, std::int64_t>;

  Ladder(std::int64_t tick, std::size_t window) : tick_(tick), window_(window), sizes_(window, 0), bits_(window) {
    if (tick <= 0) throw std::invalid_argument("tick must be positive");
    if (window < 2) throw std::invalid_argument("window must hold at least two ticks");
  }

  void clear() {
    bits_.for_each([&](std::size_t i) { sizes_[i] = 0; });
    bits_.clear();
    far_.clear();
    in_window_ = 0;
    anchored_ = false;
  }

  void set(std::int64_t price, std::int64_t size) {
    std::size_t i;
    if (!slot(price, i)) {
      if (size == 0) {
        far_.erase(price);
      } else {
        far_.insert_or_assign(price, size);
      }
      return;
    }
    if (size == 0) {
      if (sizes_[i] != 0) {
        sizes_[i] = 0;
        bits_.reset(i);
        --in_window_;
      }
      return;
    }
    if (sizes_[i] == 0) {
      bits_.set(i);
      ++in_window_;
    }
    sizes_[i] = size;
  }

  std::optional<Level> best() {
    auto inside = window_best();
    if (!far_.empty() && (!inside || better(far_.begin()->first, inside->first))) {
      recenter(far_.begin()->first);
      inside = window_best();
      // A level off the tick grid cannot enter the window, so it may still be the best.
      if (!far_.empty() && (!inside || better(far_.begin()->first, inside->first))) return *far_.begin();
    }
    return inside;
  }

  std::int64_t size_at(std::int64_t price) const {
    std::size_t i;
    if (slot(price, i)) return sizes_[i];
    auto it = far_.find(price);
    return it == far_.end() ? 0 : it->second;
  }

  std::size_t depth() const { return in_window_ + far_.size(); }

  std::vector<Level> levels(std::size_t limit) const {
    std::vector<Level> out(far_.begin(), far_.end());
    bits_.for_each([&](std::size_t i) { out.emplace_back(price_of(i), sizes_[i]); });
    std::sort(out.begin(), out.end(), [](const Level& a, const Level& b) { return better(a.first, b.first); });
    if (limit != 0 && out.size() > limit) out.resize(limit);
    return out;
  }

  std::size_t recenters() const { return recenters_; }

 private:
  static bool better(std::int64_t a, std::int64_t b) { return IsBid ? a > b : a < b; }

  std::int64_t floor_to_tick(std::int64_t price) const {
    const std::int64_t q = price / tick_;
    return (q - ((price % tick_) < 0 ? 1 : 0)) * tick_;
  }

  std::int64_t price_of(std::size_t i) const { return base_ + static_cast<std::int64_t>(i) * tick_; }

  bool slot(std::int64_t price, std::size_t& i) const {
    if (!anchored_) return false;
    const std::int64_t offset = price - base_;
    if (offset < 0 || offset % tick_ != 0) return false;
    const auto q = static_cast<std::uint64_t>(offset / tick_);
    if (q >= window_) return false;
    i = static_cast<std::size_t>(q);
    return true;
  }

  std::optional<Level> window_best() const {
    const auto i = IsBid ? bits_.max() : bits_.min();
    if (!i) return std::nullopt;
    return Level{price_of(*i), sizes_[*i]};
  }

  // Moves the window so that `centre` sits in its middle. The window stays on the tick grid, so a
  // price off the grid stays in the map, and re-centring on one twice does nothing the second time.
  void recenter(std::int64_t centre) {
    const std::int64_t base = floor_to_tick(centre) - static_cast<std::int64_t>(window_ / 2) * tick_;
    if (anchored_ && base == base_) return;
    ++recenters_;
    bits_.for_each([&](std::size_t i) {
      far_.insert_or_assign(price_of(i), sizes_[i]);
      sizes_[i] = 0;
    });
    bits_.clear();
    in_window_ = 0;
    base_ = base;
    anchored_ = true;
    const std::int64_t lo = base_;
    const std::int64_t hi = price_of(window_ - 1);
    // The map is ordered best first, so for bids the window runs from hi down to lo.
    auto it = far_.lower_bound(IsBid ? hi : lo);
    const auto end = far_.upper_bound(IsBid ? lo : hi);
    while (it != end) {
      std::size_t i;
      if (slot(it->first, i)) {
        sizes_[i] = it->second;
        bits_.set(i);
        ++in_window_;
        it = far_.erase(it);
      } else {
        ++it;
      }
    }
  }

  using Order = std::conditional_t<IsBid, std::greater<>, std::less<>>;

  std::int64_t tick_;
  std::size_t window_;
  std::vector<std::int64_t> sizes_;
  LevelBitmap bits_;
  std::map<std::int64_t, std::int64_t, Order> far_;
  std::int64_t base_ = 0;
  bool anchored_ = false;
  std::size_t in_window_ = 0;
  std::size_t recenters_ = 0;
};

// The same book as OrderBook, with the same results for any input, laid out for speed: the levels
// near the touch are a flat array indexed by price tick. `tick` is the product's price increment,
// scaled by 1e8 like prices, and `window` is how many ticks each side keeps in the array.
class FlatBook {
 public:
  explicit FlatBook(std::int64_t tick = 1, std::size_t window = std::size_t{1} << 20)
      : bids_(tick, window), asks_(tick, window) {}

  void clear() {
    bids_.clear();
    asks_.clear();
  }

  void set(Side side, std::int64_t price, std::int64_t size) {
    if (side == Side::Bid) {
      bids_.set(price, size);
    } else {
      asks_.set(price, size);
    }
  }

  std::optional<std::pair<std::int64_t, std::int64_t>> best(Side side) {
    return side == Side::Bid ? bids_.best() : asks_.best();
  }

  std::int64_t size_at(Side side, std::int64_t price) const {
    return side == Side::Bid ? bids_.size_at(price) : asks_.size_at(price);
  }

  std::size_t depth(Side side) const { return side == Side::Bid ? bids_.depth() : asks_.depth(); }

  std::vector<std::pair<std::int64_t, std::int64_t>> levels(Side side, std::size_t limit = 0) const {
    return side == Side::Bid ? bids_.levels(limit) : asks_.levels(limit);
  }

  std::size_t recenters() const { return bids_.recenters() + asks_.recenters(); }

 private:
  Ladder<true> bids_;
  Ladder<false> asks_;
};

}  // namespace touchstone
