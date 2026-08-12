from datetime import datetime, timedelta, timezone
import unittest

from cross_venue_arb.book_store import Book
from cross_venue_arb.gate5 import (
    OpportunityStatus,
    PersistenceTracker,
    TwoTierStaleness,
)


BASE = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


def book(market_id: int, received_at: datetime, *, bid: float = 0.50, ask: float = 0.51) -> Book:
    return Book(
        market_id=market_id,
        bids=(),
        asks=(),
        best_bid_price=bid,
        best_ask_price=ask,
        exchange_timestamp=received_at,
        received_at=received_at,
    )


def seed_cadence(det: TwoTierStaleness, market_id: int, gap: float, count: int, *, mid=0.505, start=None):
    """Record ``count`` updates ``gap`` seconds apart to establish a cadence."""
    start = start or (BASE - timedelta(seconds=gap * count + 5))
    for i in range(count):
        det.record(market_id, mid, start + timedelta(seconds=gap * i))
    return start + timedelta(seconds=gap * (count - 1))  # last update time


class StalenessTests(unittest.TestCase):
    def test_missing_book_counts_as_infinitely_stale(self):
        det = TwoTierStaleness()
        result = det.assess(book(1, BASE), None, BASE)
        self.assertTrue(result.stale)
        self.assertTrue(result.polymarket_stale)
        self.assertEqual(result.polymarket_age_seconds, float("inf"))

    def test_fresh_within_warmup_is_not_stale(self):
        det = TwoTierStaleness()  # no measured cadence yet -> warm-up window
        result = det.assess(
            book(1, BASE - timedelta(seconds=1)),
            book(2, BASE - timedelta(seconds=2)),
            BASE,
        )
        self.assertFalse(result.stale)

    def test_tier1_adaptive_baseline_tolerates_a_thin_markets_normal_silence(self):
        det = TwoTierStaleness()
        # Both venues genuinely update ~every 6s (a thin market, ~the p90 of the
        # measured distribution).
        last = seed_cadence(det, 1, gap=6.0, count=6)
        seed_cadence(det, 2, gap=6.0, count=6, start=BASE - timedelta(seconds=41))
        self.assertAlmostEqual(det.median_gap(1), 6.0, places=3)
        threshold = det.baseline_threshold(1)
        self.assertAlmostEqual(threshold, 24.0, places=3)  # clamp(4*6, 3, 30)

        # 12s of silence: the OLD flat 3s rule would have flagged STALE...
        now = last + timedelta(seconds=12)
        result = det.assess(book(1, last), book(2, last), now)
        self.assertGreater(result.kalshi_age_seconds, 3.0)   # old rule -> STALE
        self.assertFalse(result.stale)                       # new rule -> fine
        self.assertEqual(result.reason, "")

    def test_tier1_still_flags_once_past_the_adaptive_window(self):
        det = TwoTierStaleness()
        last = seed_cadence(det, 1, gap=6.0, count=6)
        seed_cadence(det, 2, gap=6.0, count=6, start=BASE - timedelta(seconds=41))
        result = det.assess(book(1, last), book(2, last), last + timedelta(seconds=40))
        self.assertTrue(result.stale)
        self.assertEqual(result.reason, "baseline")

    def test_tier2_price_jump_flags_immediately_despite_long_baseline(self):
        det = TwoTierStaleness()
        # Thin cadence on both -> long ~24s baseline windows.
        last = seed_cadence(det, 1, gap=6.0, count=6, mid=0.50)
        seed_cadence(det, 2, gap=6.0, count=6, mid=0.50, start=BASE - timedelta(seconds=41))

        # Polymarket sits still at ~0.50; Kalshi posts a fresh +5c jump 5s later.
        poly = book(2, last, bid=0.495, ask=0.505)          # mid 0.50, silent
        jump_at = last + timedelta(seconds=5)
        kalshi = book(1, jump_at, bid=0.545, ask=0.555)     # mid 0.55, a real move
        result = det.assess(kalshi, poly, jump_at)

        # Baseline alone would NOT flag: poly's 5s silence is well inside 24s.
        self.assertLess(
            result.polymarket_age_seconds, result.polymarket_threshold_seconds
        )
        # But Tier 2 fires immediately on the move-against-a-still-quote.
        self.assertTrue(result.stale)
        self.assertEqual(result.reason, "price_move")
        self.assertTrue(result.polymarket_stale)   # the lagging side is stale
        self.assertFalse(result.kalshi_stale)

    def test_tier2_ignores_a_small_move(self):
        det = TwoTierStaleness()
        last = seed_cadence(det, 1, gap=6.0, count=6, mid=0.50)
        seed_cadence(det, 2, gap=6.0, count=6, mid=0.50, start=BASE - timedelta(seconds=41))
        poly = book(2, last, bid=0.495, ask=0.505)
        jump_at = last + timedelta(seconds=5)
        kalshi = book(1, jump_at, bid=0.505, ask=0.515)     # mid 0.51, only +1c
        result = det.assess(kalshi, poly, jump_at)
        self.assertFalse(result.stale)

    def test_tier2_requires_the_other_side_to_be_lagging(self):
        det = TwoTierStaleness()
        last = seed_cadence(det, 1, gap=6.0, count=6, mid=0.50)
        seed_cadence(det, 2, gap=6.0, count=6, mid=0.50, start=BASE - timedelta(seconds=41))
        now = last + timedelta(seconds=5)
        # Big Kalshi move, but Polymarket is also current (updated 0.5s ago).
        poly = book(2, now - timedelta(seconds=0.5), bid=0.495, ask=0.505)
        kalshi = book(1, now, bid=0.545, ask=0.555)
        result = det.assess(kalshi, poly, now)
        self.assertFalse(result.stale)  # both sides current -> no risk, no flag

    def test_naive_timestamp_is_treated_as_utc(self):
        det = TwoTierStaleness()
        naive_now = BASE.replace(tzinfo=None)
        old = book(1, received_at=naive_now - timedelta(seconds=20))
        result = det.assess(old, old, naive_now)
        self.assertTrue(result.stale)


class PersistenceTests(unittest.TestCase):
    def test_one_tick_flicker_never_confirms(self):
        tracker = PersistenceTracker(min_updates=3)

        seen = tracker.observe("k", present=True, now=BASE)
        self.assertEqual(seen.status, OpportunityStatus.PENDING)
        self.assertEqual(seen.observations, 1)

        # Next update the edge is gone: it evaporates without ever confirming.
        gone = tracker.observe("k", present=False, now=BASE + timedelta(seconds=1))
        self.assertEqual(gone.status, OpportunityStatus.EVAPORATED)
        self.assertNotEqual(gone.status, OpportunityStatus.CONFIRMED)
        self.assertIsNone(tracker.get("k"))
        self.assertEqual(tracker.confirmed_keys, frozenset())

    def test_two_ticks_then_gone_still_below_threshold(self):
        tracker = PersistenceTracker(min_updates=3)
        tracker.observe("k", True, BASE)
        second = tracker.observe("k", True, BASE + timedelta(seconds=1))
        self.assertEqual(second.status, OpportunityStatus.PENDING)
        self.assertEqual(second.observations, 2)
        gone = tracker.observe("k", False, BASE + timedelta(seconds=2))
        self.assertEqual(gone.status, OpportunityStatus.EVAPORATED)

    def test_edge_surviving_min_updates_is_confirmed(self):
        tracker = PersistenceTracker(min_updates=3)
        tracker.observe("k", True, BASE)
        tracker.observe("k", True, BASE + timedelta(seconds=1))
        third = tracker.observe("k", True, BASE + timedelta(seconds=2))

        self.assertEqual(third.status, OpportunityStatus.CONFIRMED)
        self.assertEqual(third.observations, 3)
        self.assertEqual(third.first_seen, BASE)
        self.assertEqual(third.last_seen, BASE + timedelta(seconds=2))
        self.assertEqual(third.confirmed_at, BASE + timedelta(seconds=2))
        self.assertAlmostEqual(third.lifetime_seconds, 2.0, places=3)
        self.assertEqual(tracker.confirmed_keys, frozenset({"k"}))

    def test_repeated_same_data_version_does_not_count_toward_confirmation(self):
        tracker = PersistenceTracker(min_updates=3)
        # Same market snapshot (data_version "v1") re-observed on a timer holds
        # at one observation — it must not ratchet toward CONFIRMED.
        for i in range(5):
            state = tracker.observe(
                "k", True, BASE + timedelta(seconds=i), data_version="v1"
            )
        self.assertEqual(state.status, OpportunityStatus.PENDING)
        self.assertEqual(state.observations, 1)

        # Distinct data_versions (genuine new frames) do count.
        tracker.observe("k", True, BASE + timedelta(seconds=5), data_version="v2")
        third = tracker.observe("k", True, BASE + timedelta(seconds=6), data_version="v3")
        self.assertEqual(third.observations, 3)
        self.assertEqual(third.status, OpportunityStatus.CONFIRMED)

    def test_absence_resets_even_with_unchanged_data_version(self):
        # A vanished edge evaporates immediately regardless of data_version.
        tracker = PersistenceTracker(min_updates=3)
        tracker.observe("k", True, BASE, data_version="v1")
        gone = tracker.observe("k", False, BASE + timedelta(seconds=1), data_version="v1")
        self.assertEqual(gone.status, OpportunityStatus.EVAPORATED)
        self.assertIsNone(tracker.get("k"))

    def test_confirmed_at_is_pinned_to_first_crossing(self):
        tracker = PersistenceTracker(min_updates=2)
        tracker.observe("k", True, BASE)
        tracker.observe("k", True, BASE + timedelta(seconds=1))
        later = tracker.observe("k", True, BASE + timedelta(seconds=5))
        self.assertEqual(later.status, OpportunityStatus.CONFIRMED)
        # confirmed_at stays at the moment it first crossed, not the latest tick.
        self.assertEqual(later.confirmed_at, BASE + timedelta(seconds=1))
        self.assertEqual(later.last_seen, BASE + timedelta(seconds=5))

    def test_duration_threshold_confirms_independently_of_count(self):
        tracker = PersistenceTracker(min_updates=None, min_duration_seconds=2.0)
        first = tracker.observe("k", True, BASE)
        self.assertEqual(first.status, OpportunityStatus.PENDING)
        mid = tracker.observe("k", True, BASE + timedelta(seconds=1))
        self.assertEqual(mid.status, OpportunityStatus.PENDING)
        after = tracker.observe("k", True, BASE + timedelta(seconds=2))
        self.assertEqual(after.status, OpportunityStatus.CONFIRMED)
        self.assertEqual(after.observations, 3)

    def test_reset_starts_a_fresh_lifetime(self):
        tracker = PersistenceTracker(min_updates=3)
        tracker.observe("k", True, BASE)
        tracker.observe("k", True, BASE + timedelta(seconds=1))
        tracker.observe("k", False, BASE + timedelta(seconds=2))  # evaporates

        # Re-appearing later is a brand new opportunity, not a resumption.
        reborn = tracker.observe("k", True, BASE + timedelta(seconds=10))
        self.assertEqual(reborn.status, OpportunityStatus.PENDING)
        self.assertEqual(reborn.observations, 1)
        self.assertEqual(reborn.first_seen, BASE + timedelta(seconds=10))

    def test_absent_unknown_key_returns_none(self):
        tracker = PersistenceTracker(min_updates=3)
        self.assertIsNone(tracker.observe("k", present=False, now=BASE))

    def test_requires_a_configured_threshold(self):
        with self.assertRaises(ValueError):
            PersistenceTracker(min_updates=None, min_duration_seconds=None)

    def test_independent_keys_track_separately(self):
        tracker = PersistenceTracker(min_updates=2)
        tracker.observe("a", True, BASE)
        tracker.observe("b", True, BASE)
        a2 = tracker.observe("a", True, BASE + timedelta(seconds=1))
        self.assertEqual(a2.status, OpportunityStatus.CONFIRMED)
        # b has only been seen once and stays pending.
        self.assertEqual(tracker.get("b").status, OpportunityStatus.PENDING)


if __name__ == "__main__":
    unittest.main()
