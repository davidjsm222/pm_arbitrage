from datetime import datetime, timezone
from decimal import Decimal
import unittest

from cross_venue_arb.book_store import Book
from cross_venue_arb.edge import (
    kalshi_taker_fee,
    polymarket_us_taker_fee,
    top_of_book_edges,
)


def book(market_id: int, bid: float, ask: float) -> Book:
    return Book(
        market_id=market_id,
        bids=(),
        asks=(),
        best_bid_price=bid,
        best_ask_price=ask,
        exchange_timestamp=None,
        received_at=datetime.now(timezone.utc),
    )


class FeeTests(unittest.TestCase):
    def test_kalshi_current_general_curve_and_centicent_rounding(self):
        self.assertEqual(
            kalshi_taker_fee("0.50", contracts=100), Decimal("1.7500")
        )
        self.assertEqual(kalshi_taker_fee("0.50"), Decimal("0.0175"))

    def test_polymarket_current_curve_and_bankers_rounding(self):
        self.assertEqual(
            polymarket_us_taker_fee("0.50", contracts=100), Decimal("1.25")
        )
        self.assertEqual(polymarket_us_taker_fee("0.50"), Decimal("0.01"))
        # Raw fee is $0.0375; nearest-cent rounding produces $0.04.
        self.assertEqual(
            polymarket_us_taker_fee("0.50", contracts=3), Decimal("0.04")
        )
        self.assertEqual(polymarket_us_taker_fee("0.06"), Decimal("0.00"))


class EdgeTests(unittest.TestCase):
    def test_both_directions_use_executable_bid_and_ask(self):
        edges = top_of_book_edges(
            book(1383, bid=0.063, ask=0.065),
            book(691214, bid=0.059, ask=0.060),
        )

        self.assertEqual(edges.buy_yes_kalshi.gross, Decimal("-0.006"))
        self.assertEqual(edges.buy_yes_kalshi.kalshi_fee, Decimal("0.0043"))
        self.assertEqual(edges.buy_yes_kalshi.polymarket_fee, Decimal("0.00"))
        self.assertEqual(edges.buy_yes_kalshi.net, Decimal("-0.0103"))

        self.assertEqual(edges.buy_yes_polymarket.gross, Decimal("0.003"))
        self.assertEqual(edges.buy_yes_polymarket.kalshi_fee, Decimal("0.0042"))
        self.assertEqual(edges.buy_yes_polymarket.polymarket_fee, Decimal("0.00"))
        self.assertEqual(edges.buy_yes_polymarket.net, Decimal("-0.0012"))


if __name__ == "__main__":
    unittest.main()
