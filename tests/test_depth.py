from datetime import datetime, timezone
from decimal import Decimal
import unittest

from cross_venue_arb.book_store import Book, PriceLevel
from cross_venue_arb.depth import depth_walk
from cross_venue_arb.edge import top_of_book_edges


def _levels(*pairs: tuple[float, float]) -> tuple[PriceLevel, ...]:
    return tuple(PriceLevel(price=price, qty=qty) for price, qty in pairs)


def book(
    market_id: int,
    *,
    asks: tuple[PriceLevel, ...] = (),
    bids: tuple[PriceLevel, ...] = (),
) -> Book:
    best_bid = max((level.price for level in bids), default=None)
    best_ask = min((level.price for level in asks), default=None)
    return Book(
        market_id=market_id,
        bids=bids,
        asks=asks,
        best_bid_price=best_bid,
        best_ask_price=best_ask,
        exchange_timestamp=None,
        received_at=datetime.now(timezone.utc),
    )


class DepthWalkTests(unittest.TestCase):
    def test_real_edge_survives_full_depth(self):
        # Buy YES on Kalshi (lift its ask ladder) against Polymarket's YES bids.
        # Deliberately supplied out of price order to prove the walk sorts.
        kalshi = book(1, asks=_levels((0.42, 100), (0.40, 100), (0.41, 100)))
        polymarket = book(2, bids=_levels((0.48, 100), (0.50, 100), (0.49, 100)))

        depth = depth_walk(kalshi, polymarket)
        result = depth.buy_yes_kalshi

        self.assertEqual(result.buy_yes_venue, "KALSHI")
        self.assertEqual(result.buy_no_venue, "POLYMARKET")
        self.assertEqual(result.capturable_size, 300)
        self.assertEqual(result.levels_consumed, 3)
        self.assertEqual(result.locked_profit, Decimal("15.9100"))
        # Upfront cost at actual fills: (0.40+0.50)*100 + (0.41+0.51)*100
        # + (0.42+0.52)*100 = 90 + 92 + 94.
        self.assertEqual(result.capital_locked, Decimal("276.00"))
        self.assertTrue(result.is_capturable)
        # Nothing on the mirror side, so that direction captures nothing.
        self.assertEqual(depth.buy_yes_polymarket.capturable_size, 0)
        self.assertIs(depth.best, result)

    def test_edge_real_at_top_but_vanishes_a_few_levels_down(self):
        # First three levels clear fees; the fourth (asks jump, bids drop) goes
        # net-negative, so the walk stops there.
        # Opposing top levels so top_of_book_edges has all four prices; they do
        # not touch the buy-YES-on-Kalshi walk (kalshi asks + polymarket bids).
        kalshi = book(
            1,
            asks=_levels((0.40, 40), (0.42, 40), (0.44, 40), (0.50, 40)),
            bids=_levels((0.39, 40),),
        )
        polymarket = book(
            2,
            asks=_levels((0.51, 40),),
            bids=_levels((0.50, 40), (0.49, 40), (0.48, 40), (0.46, 40)),
        )

        result = depth_walk(kalshi, polymarket).buy_yes_kalshi

        # Top-of-book alone looked like a real edge...
        self.assertGreater(top_of_book_edges(kalshi, polymarket).buy_yes_kalshi.net, 0)
        # ...but capturable size stops well before the ladder is exhausted.
        self.assertEqual(result.capturable_size, 120)
        self.assertEqual(result.levels_consumed, 3)
        self.assertEqual(result.locked_profit, Decimal("5.1520"))

    def test_asymmetric_quantities_walk_by_smaller_remaining_side(self):
        # 30 pairs at the top ask, then the thin ask rolls to the next price
        # while the deep bid still has size left.
        kalshi = book(1, asks=_levels((0.40, 30), (0.41, 100)))
        polymarket = book(2, bids=_levels((0.50, 120)))

        result = depth_walk(kalshi, polymarket).buy_yes_kalshi

        self.assertEqual(result.capturable_size, 120)
        self.assertEqual(result.levels_consumed, 2)
        self.assertEqual(result.locked_profit, Decimal("7.8660"))

    def test_single_level_matches_top_of_book_edge_math(self):
        # One contract each side: locked profit must equal the top-of-book net,
        # proving the walk's fee assignment mirrors edge.top_of_book_edges. Only
        # one mirror direction can be positive at a time, so each is checked on
        # the book pair where it is the winner.
        buy_kalshi_k = book(1, asks=_levels((0.40, 1)), bids=_levels((0.39, 1)))
        buy_kalshi_p = book(2, asks=_levels((0.61, 1)), bids=_levels((0.50, 1)))
        depth = depth_walk(buy_kalshi_k, buy_kalshi_p)
        edges = top_of_book_edges(buy_kalshi_k, buy_kalshi_p)
        self.assertEqual(depth.buy_yes_kalshi.capturable_size, 1)
        self.assertEqual(depth.buy_yes_kalshi.locked_profit, edges.buy_yes_kalshi.net)
        self.assertEqual(depth.buy_yes_polymarket.capturable_size, 0)

        # Mirror the setup so buying YES on Polymarket is the profitable leg.
        buy_poly_k = book(1, asks=_levels((0.51, 1)), bids=_levels((0.50, 1)))
        buy_poly_p = book(2, asks=_levels((0.40, 1)), bids=_levels((0.39, 1)))
        depth = depth_walk(buy_poly_k, buy_poly_p)
        edges = top_of_book_edges(buy_poly_k, buy_poly_p)
        self.assertEqual(depth.buy_yes_polymarket.capturable_size, 1)
        self.assertEqual(
            depth.buy_yes_polymarket.locked_profit, edges.buy_yes_polymarket.net
        )
        self.assertEqual(depth.buy_yes_kalshi.capturable_size, 0)

    def test_no_edge_returns_zero_capturable(self):
        kalshi = book(1, asks=_levels((0.60, 100)))
        polymarket = book(2, bids=_levels((0.50, 100)))

        result = depth_walk(kalshi, polymarket).buy_yes_kalshi

        self.assertEqual(result.capturable_size, 0)
        self.assertEqual(result.locked_profit, Decimal("0"))
        self.assertEqual(result.levels_consumed, 0)
        self.assertFalse(result.is_capturable)

    def test_partial_polymarket_share_floors_to_whole_pairs(self):
        # Polymarket permits fractional shares, but a pair needs one whole Kalshi
        # contract, so a 30.5-share bid yields 30 pairs, not 30.5. The stranded
        # 0.5 share cannot complete a 1:1 cross-venue pair.
        kalshi = book(1, asks=_levels((0.40, 40)))
        polymarket = book(2, bids=_levels((0.50, 30.5)))

        result = depth_walk(kalshi, polymarket).buy_yes_kalshi

        self.assertEqual(result.capturable_size, 30)
        # 30 * 0.0632, not the old fractional 30.5 * 0.0632 = 1.9276.
        self.assertEqual(result.locked_profit, Decimal("2.1960"))
        # Capital covers the 30 whole pairs only: (0.40 + 0.50) * 30.
        self.assertEqual(result.capital_locked, Decimal("27.000"))
        self.assertIsInstance(result.capturable_size, int)


if __name__ == "__main__":
    unittest.main()
