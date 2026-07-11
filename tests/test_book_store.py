from datetime import datetime
from types import SimpleNamespace
import unittest

from river_arb_monitor.book_store import BookStore, StoreResult
from rivermarkets.realtime.messages import parse_message


def frame(*, river_id=123, frame_type="snapshot", is_valid=True, bid=0.42, ask=0.43):
    return SimpleNamespace(
        type=frame_type,
        river_id=river_id,
        data={
            "river_id": river_id,
            "bids": [{"price": bid, "qty": 1500}],
            "asks": [{"price": ask, "qty": 2200}],
            "best_bid_price": bid,
            "best_ask_price": ask,
            "exchange_timestamp": "2026-04-19T15:22:01.123456+00:00",
            "is_valid": is_valid,
        },
    )


class BookStoreTests(unittest.TestCase):
    def test_stores_snapshot_by_river_id(self):
        store = BookStore()

        result = store.apply_message(frame())

        self.assertEqual(result, StoreResult.STORED)
        book = store.get(123)
        self.assertIsNotNone(book)
        self.assertEqual(book.best_bid_price, 0.42)
        self.assertEqual(book.best_ask_price, 0.43)
        self.assertEqual(book.bids[0].qty, 1500)
        self.assertIsInstance(book.exchange_timestamp, datetime)

    def test_accepts_the_sdk_generic_websocket_message(self):
        store = BookStore()
        sdk_message = parse_message(
            {
                "type": "snapshot",
                "river_id": 123,
                "data": frame().data,
            }
        )

        result = store.apply_message(sdk_message)

        self.assertEqual(result, StoreResult.STORED)
        self.assertEqual(store.get(123).best_bid_price, 0.42)

    def test_valid_update_replaces_full_book(self):
        store = BookStore()
        store.apply_message(frame())

        store.apply_message(frame(frame_type="update", bid=0.44, ask=0.45))

        self.assertEqual(store.get(123).best_bid_price, 0.44)
        self.assertEqual(store.get(123).best_ask_price, 0.45)

    def test_invalid_update_is_counted_and_last_valid_book_is_retained(self):
        store = BookStore()
        store.apply_message(frame())

        result = store.apply_message(
            frame(frame_type="update", is_valid=False, bid=0.50, ask=0.49)
        )

        self.assertEqual(result, StoreResult.DROPPED_INVALID)
        self.assertEqual(store.invalid_frame_count(123), 1)
        self.assertIsNotNone(store.last_invalid_at(123))
        self.assertEqual(store.get(123).best_bid_price, 0.42)
        self.assertEqual(store.get(123).best_ask_price, 0.43)

    def test_non_book_frame_is_ignored(self):
        store = BookStore()

        result = store.apply_message(SimpleNamespace(type="pending", river_id=123))

        self.assertEqual(result, StoreResult.IGNORED)
        self.assertIsNone(store.get(123))

    def test_mismatched_payload_river_id_is_rejected(self):
        store = BookStore()
        message = frame()
        message.data["river_id"] = 999

        with self.assertRaises(ValueError):
            store.apply_message(message)


if __name__ == "__main__":
    unittest.main()
