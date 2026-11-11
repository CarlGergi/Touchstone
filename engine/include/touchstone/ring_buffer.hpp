#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <new>

namespace touchstone {

// A lock-free ring buffer for exactly one producer thread and one consumer thread.
//
// Each side owns one index and only reads the other's. The producer publishes a slot with a
// release store of head, and the consumer sees it with an acquire load, so the slot's contents are
// visible before the index that announces them. Each side caches the other's index and reloads it
// only when the buffer looks full or empty, which keeps the two cache lines from bouncing between
// cores on every operation.
template <typename T, std::size_t Capacity>
class SpscRing {
  static_assert(Capacity >= 2 && (Capacity & (Capacity - 1)) == 0, "capacity must be a power of two");

 public:
  bool try_push(const T& item) {
    const std::size_t head = head_.load(std::memory_order_relaxed);
    if (head - cached_tail_ == Capacity) {
      cached_tail_ = tail_.load(std::memory_order_acquire);
      if (head - cached_tail_ == Capacity) return false;
    }
    slots_[head & (Capacity - 1)] = item;
    head_.store(head + 1, std::memory_order_release);
    return true;
  }

  bool try_pop(T& item) {
    const std::size_t tail = tail_.load(std::memory_order_relaxed);
    if (tail == cached_head_) {
      cached_head_ = head_.load(std::memory_order_acquire);
      if (tail == cached_head_) return false;
    }
    item = slots_[tail & (Capacity - 1)];
    tail_.store(tail + 1, std::memory_order_release);
    return true;
  }

 private:
  static constexpr std::size_t kLine = 64;
  alignas(kLine) std::atomic<std::size_t> head_{0};
  alignas(kLine) std::size_t cached_tail_ = 0;  // Producer's copy of tail_.
  alignas(kLine) std::atomic<std::size_t> tail_{0};
  alignas(kLine) std::size_t cached_head_ = 0;  // Consumer's copy of head_.
  alignas(kLine) std::array<T, Capacity> slots_{};
};

}  // namespace touchstone
