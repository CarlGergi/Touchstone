#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <utility>
#include <vector>

namespace touchstone {

enum class Side : std::uint8_t { Bid = 0, Ask = 1 };

// A level-2 book: price to total size on each side. Prices and sizes are integers scaled by 1e8,
// as recorded, so levels compare exactly. An update carries the level's new absolute size, and a
// size of zero removes the level.
class OrderBook {
 public:
  void clear() {
    bids_.clear();
    asks_.clear();
  }

  void set(Side side, std::int64_t price, std::int64_t size) {
    if (side == Side::Bid) {
      apply(bids_, price, size);
    } else {
      apply(asks_, price, size);
    }
  }

  std::optional<std::pair<std::int64_t, std::int64_t>> best(Side side) const {
    if (side == Side::Bid) {
      if (bids_.empty()) return std::nullopt;
      return *bids_.begin();
    }
    if (asks_.empty()) return std::nullopt;
    return *asks_.begin();
  }

  std::int64_t size_at(Side side, std::int64_t price) const {
    if (side == Side::Bid) {
      auto it = bids_.find(price);
      return it == bids_.end() ? 0 : it->second;
    }
    auto it = asks_.find(price);
    return it == asks_.end() ? 0 : it->second;
  }

  std::size_t depth(Side side) const { return side == Side::Bid ? bids_.size() : asks_.size(); }

  // Levels from the best price outward. A limit of zero means all of them.
  std::vector<std::pair<std::int64_t, std::int64_t>> levels(Side side, std::size_t limit = 0) const {
    std::vector<std::pair<std::int64_t, std::int64_t>> out;
    auto copy = [&](const auto& levels) {
      for (const auto& level : levels) {
        if (limit != 0 && out.size() == limit) break;
        out.push_back(level);
      }
    };
    if (side == Side::Bid) {
      copy(bids_);
    } else {
      copy(asks_);
    }
    return out;
  }

 private:
  template <typename Levels>
  static void apply(Levels& levels, std::int64_t price, std::int64_t size) {
    if (size == 0) {
      levels.erase(price);
    } else {
      levels.insert_or_assign(price, size);
    }
  }

  std::map<std::int64_t, std::int64_t, std::greater<>> bids_;
  std::map<std::int64_t, std::int64_t> asks_;
};

}  // namespace touchstone
