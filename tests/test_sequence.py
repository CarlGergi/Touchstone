from touchstone.recorder.sequence import SequenceStatus, SequenceTracker


def test_consecutive_numbers_are_in_order():
    tracker = SequenceTracker()
    assert [tracker.observe(n) for n in (0, 1, 2)] == [SequenceStatus.IN_ORDER] * 3
    assert tracker.expected == 3


def test_first_number_sets_the_baseline():
    tracker = SequenceTracker()
    assert tracker.observe(41) is SequenceStatus.IN_ORDER
    assert tracker.observe(42) is SequenceStatus.IN_ORDER


def test_any_jump_forward_is_a_gap():
    tracker = SequenceTracker()
    tracker.observe(0)
    assert tracker.observe(2) is SequenceStatus.GAP


def test_gap_leaves_the_expected_number_alone():
    tracker = SequenceTracker()
    tracker.observe(0)
    tracker.observe(5)
    assert tracker.expected == 1


def test_earlier_numbers_are_stale_and_do_not_move_the_tracker():
    tracker = SequenceTracker()
    for n in (0, 1, 2):
        tracker.observe(n)
    assert tracker.observe(1) is SequenceStatus.STALE
    assert tracker.observe(2) is SequenceStatus.STALE
    assert tracker.expected == 3
    assert tracker.observe(3) is SequenceStatus.IN_ORDER
