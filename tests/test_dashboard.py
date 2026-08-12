from __future__ import annotations

import sqlite3
import tempfile
import unittest
import unittest.mock
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from rich.style import Style
from rich.text import Text
from textual.screen import Screen
from textual.widgets import DataTable, Label, Static

from cross_venue_arb.book_store import Book, BookStore, PriceLevel
from cross_venue_arb.dashboard import (
    TABLE_TITLE,
    ArbDashboard,
    DashboardPair,
    ExclusionsScreen,
    MarketDetail,
    PairDetailScreen,
    _contract_url,
    _market_detail_text,
    _resolution_text,
    annualized_rolc,
    build_pair_view,
    fetch_pair_metadata,
    load_dashboard_pairs,
    resolve_pair_expiration,
)
from cross_venue_arb.matcher import (
    flag_false_pair,
    initialize_cache,
    list_false_pair_exclusions,
)


def _pair() -> DashboardPair:
    return DashboardPair(
        kalshi_market_id=1,
        polymarket_market_id=2,
        kalshi_name="Will Example happen?",
        polymarket_name="Will Example happen in 2026?",
        confidence=0.88,
        source="independent",
        phrase_similarity=0.77,
        entity_similarity=0.91,
    )


def _book_message(market_id: int, bid: float, ask: float) -> dict[str, object]:
    return {
        "type": "snapshot",
        "market_id": market_id,
        "data": {
            "market_id": market_id,
            "is_valid": True,
            "bids": [{"price": bid, "qty": 10}],
            "asks": [{"price": ask, "qty": 10}],
            "best_bid_price": bid,
            "best_ask_price": ask,
            "exchange_timestamp": "2026-07-13T12:00:00Z",
        },
    }


def _gate_book(
    market_id,
    *,
    best_bid=None,
    best_ask=None,
    bids=(),
    asks=(),
    received_at=None,
):
    return Book(
        market_id=market_id,
        bids=tuple(PriceLevel(price=p, qty=q) for p, q in bids),
        asks=tuple(PriceLevel(price=p, qty=q) for p, q in asks),
        best_bid_price=best_bid,
        best_ask_price=best_ask,
        exchange_timestamp=received_at,
        received_at=received_at or datetime.now(timezone.utc),
    )


class GatePipelineTests(unittest.TestCase):
    """Gate 4 (depth) + Gate 5 (staleness/persistence) wired into the app.

    These exercise the evaluation pipeline directly (no rendering needed), so
    ``now`` and book timestamps are controlled explicitly.
    """

    NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

    def _app(self, *pairs, **kwargs):
        # Large refresh so no timer fires; we drive _evaluate_gates by hand.
        return ArbDashboard(pairs, connect_feed=False, refresh_seconds=1000, **kwargs)

    def test_depth_gate_caps_a_fat_top_of_book_edge_to_meaningless_size(self):
        pair = _pair()  # market ids 1 (kalshi), 2 (polymarket)
        app = self._app(pair)
        now = self.NOW
        # Fat top-of-book edge, but the real ladder is only 2 contracts deep
        # before it turns negative — the classic "not worth a trade" mirage.
        app.store._books[1] = _gate_book(
            1, best_bid=0.39, best_ask=0.40,
            asks=[(0.40, 2), (0.60, 100)], bids=[(0.39, 100)], received_at=now,
        )
        app.store._books[2] = _gate_book(
            2, best_bid=0.55, best_ask=0.56,
            asks=[(0.56, 100)], bids=[(0.55, 2), (0.30, 100)], received_at=now,
        )
        # The raw top-of-book Net column still shows a healthy positive edge...
        view = build_pair_view(pair, app.store)
        self.assertGreater(view.net, Decimal("0.1"))
        # ...but even after enough survivals to confirm, depth caps the size at
        # the 2 real contracts — below the buildsheet's "three contracts" floor.
        for i in range(5):
            app._evaluate_gates(now + timedelta(seconds=i))
        gate = app._gate_state[pair.key]
        self.assertEqual(gate.capturable_size, 2)
        self.assertLessEqual(gate.capturable_size, 3)

    def test_stale_side_flags_regardless_of_apparent_edge(self):
        pair = _pair()
        app = self._app(pair)
        now = self.NOW
        # A big, deep, genuine edge on both books...
        app.store._books[1] = _gate_book(
            1, best_bid=0.39, best_ask=0.40,
            asks=[(0.40, 100)], bids=[(0.39, 100)],
            received_at=now - timedelta(seconds=30),  # Kalshi leg is stale
        )
        app.store._books[2] = _gate_book(
            2, best_bid=0.55, best_ask=0.56,
            asks=[(0.56, 100)], bids=[(0.55, 100)], received_at=now,
        )
        # Drive several ticks; staleness must dominate and block confirmation.
        for i in range(5):
            app._evaluate_gates(now + timedelta(seconds=i))
        gate = app._gate_state[pair.key]
        self.assertTrue(gate.stale)
        self.assertEqual(gate.status, "STALE")
        self.assertNotEqual(gate.status, "CONFIRMED")

    def test_single_tick_flicker_never_reaches_confirmed(self):
        pair = _pair()
        app = self._app(pair)  # default min_updates = 3
        now = self.NOW
        deep = dict(asks=[(0.40, 100)], bids=[(0.39, 100)])
        app.store._books[1] = _gate_book(1, best_bid=0.39, best_ask=0.40, received_at=now, **deep)
        app.store._books[2] = _gate_book(
            2, best_bid=0.55, best_ask=0.56, asks=[(0.56, 100)], bids=[(0.55, 100)], received_at=now
        )
        app._evaluate_gates(now)
        self.assertEqual(app._gate_state[pair.key].status, "PENDING")

        # Next tick the edge is gone (prices cross the wrong way) -> evaporate.
        later = now + timedelta(seconds=0.5)
        app.store._books[1] = _gate_book(1, best_bid=0.39, best_ask=0.60, asks=[(0.60, 100)], bids=[(0.39, 100)], received_at=later)
        app.store._books[2] = _gate_book(2, best_bid=0.30, best_ask=0.56, asks=[(0.56, 100)], bids=[(0.30, 100)], received_at=later)
        app._evaluate_gates(later)
        self.assertEqual(app._gate_state[pair.key].status, "EVAPORATED")

        # It flickered once and never reached CONFIRMED; EVAPORATED lingers on
        # subsequent ticks (display stickiness) so the disappearance is
        # catchable, then clears after the linger window. Books are re-stamped
        # fresh at each tick so staleness (which outranks the linger) stays out
        # of the picture.
        def tick(at):
            for rid in (1, 2):
                app.store._books[rid] = replace(app.store.get(rid), received_at=at)
            app._evaluate_gates(at)

        tick(now + timedelta(seconds=1.0))
        self.assertEqual(app._gate_state[pair.key].status, "EVAPORATED")
        tick(now + timedelta(seconds=30))
        self.assertEqual(app._gate_state[pair.key].status, "EVAPORATED")
        tick(now + timedelta(seconds=0.5 + 61))
        self.assertEqual(app._gate_state[pair.key].status, "")

    def test_reappearing_edge_clears_the_evaporated_linger(self):
        pair = _pair()
        app = self._app(pair)
        now = self.NOW
        deep = dict(asks=[(0.40, 100)], bids=[(0.39, 100)])
        good_poly = dict(asks=[(0.56, 100)], bids=[(0.55, 100)])
        app.store._books[1] = _gate_book(1, best_bid=0.39, best_ask=0.40, received_at=now, **deep)
        app.store._books[2] = _gate_book(2, best_bid=0.55, best_ask=0.56, received_at=now, **good_poly)
        app._evaluate_gates(now)
        # Edge dies -> evaporates and lingers.
        gone = now + timedelta(seconds=1)
        app.store._books[2] = _gate_book(2, best_bid=0.30, best_ask=0.56, asks=[(0.56, 100)], bids=[(0.30, 100)], received_at=gone)
        app._evaluate_gates(gone)
        self.assertEqual(app._gate_state[pair.key].status, "EVAPORATED")
        # Edge comes back -> PENDING immediately, linger memo cleared.
        back = now + timedelta(seconds=2)
        app.store._books[2] = _gate_book(2, best_bid=0.55, best_ask=0.56, received_at=back, **good_poly)
        app._evaluate_gates(back)
        self.assertEqual(app._gate_state[pair.key].status, "PENDING")
        self.assertNotIn(pair.key, app._evaporated_at)

    def test_confirmed_depth_pair_outranks_a_bigger_raw_net_mirage(self):
        deep = DashboardPair(1, 2, "deep", "deep", 0.9, "independent")
        mirage = DashboardPair(5, 6, "mirage", "mirage", 0.9, "independent")
        app = self._app(deep, mirage)
        now = self.NOW
        # Deep pair: modest raw net, but real depth behind it.
        app.store._books[1] = _gate_book(1, best_bid=0.39, best_ask=0.40, asks=[(0.40, 100)], bids=[(0.39, 100)], received_at=now)
        app.store._books[2] = _gate_book(2, best_bid=0.50, best_ask=0.51, asks=[(0.51, 100)], bids=[(0.50, 100)], received_at=now)
        # Mirage pair: much bigger raw top-of-book net, but no capturable depth.
        app.store._books[5] = _gate_book(5, best_bid=0.39, best_ask=0.40, received_at=now)
        app.store._books[6] = _gate_book(6, best_bid=0.70, best_ask=0.71, received_at=now)

        # Each sweep re-stamps received_at (a genuine market update on both
        # sides), so persistence counts real updates rather than timer ticks.
        for i in range(3):
            tick = now + timedelta(seconds=i)
            for rid in (1, 2, 5, 6):
                app.store._books[rid] = replace(app.store.get(rid), received_at=tick)
            app._evaluate_gates(tick)
        self.assertEqual(app._gate_state["1:2"].status, "CONFIRMED")
        self.assertNotEqual(app._gate_state["5:6"].status, "CONFIRMED")

        views = [
            build_pair_view(p, app.store)
            for p in (deep, mirage)
        ]
        net = {v.pair.key: v.net for v in views}
        self.assertGreater(net["5:6"], net["1:2"])  # mirage has the bigger raw net
        ranked = sorted(views, key=app._rank_key, reverse=True)
        self.assertEqual(ranked[0].pair.key, "1:2")  # but the confirmed one wins

    def test_gate_carries_depth_capital_for_the_best_direction(self):
        pair = _pair()
        app = self._app(pair)
        now = self.NOW
        app.store._books[1] = _gate_book(
            1, best_bid=0.39, best_ask=0.40,
            asks=[(0.40, 100)], bids=[(0.39, 100)], received_at=now,
        )
        app.store._books[2] = _gate_book(
            2, best_bid=0.50, best_ask=0.51,
            asks=[(0.51, 100)], bids=[(0.50, 100)], received_at=now,
        )
        app._evaluate_gates(now)
        gate = app._gate_state[pair.key]
        # (ask 0.40 + NO cost 0.50) * 100 pairs, from depth.py — not top-of-book.
        self.assertEqual(gate.capital_locked, Decimal("90"))

    def test_annualized_return_uses_buildsheet_simple_formula(self):
        # (locked/capital) * (365/days), no compounding.
        value = annualized_rolc(Decimal("6.32"), Decimal("90"), 365.0)
        self.assertAlmostEqual(float(value), 6.32 / 90.0, places=6)
        value = annualized_rolc(Decimal("6.32"), Decimal("90"), 36.5)
        self.assertAlmostEqual(float(value), (6.32 / 90.0) * 10.0, places=6)

    def test_resolution_prefers_polymarket_and_marks_wide_disagreement(self):
        november = datetime(2026, 11, 15, tzinfo=timezone.utc)
        january = datetime(2027, 1, 20, tzinfo=timezone.utc)
        # Agreement (same date): Polymarket's value is chosen, no mismatch.
        chosen, mismatch = resolve_pair_expiration(november, november)
        self.assertEqual(chosen, november)
        self.assertFalse(mismatch)
        # Kalshi's padded far-future date (measured: later in 100% of
        # mismatches, median +1y): still Polymarket, but flagged.
        chosen, mismatch = resolve_pair_expiration(january, november)
        self.assertEqual(chosen, november)
        self.assertTrue(mismatch)
        # Only Kalshi known: fall back to it, no mismatch to report.
        chosen, mismatch = resolve_pair_expiration(january, None)
        self.assertEqual(chosen, january)
        self.assertFalse(mismatch)

    def test_resolution_2026_highlight_only_on_live_tracked_rows(self):
        nov_2026 = datetime(2026, 11, 15, tzinfo=timezone.utc)
        # 2026 + live-tracked (pending/confirmed/evaporated): faint yellow bg.
        lit = _resolution_text(nov_2026, highlight=True)
        self.assertEqual(lit.plain, "Nov 26")
        self.assertIn("on #332d14", str(lit.style))
        # 2026 but stale/untracked: plain tone, no highlight.
        plain = _resolution_text(nov_2026, highlight=False)
        self.assertEqual(str(plain.style), "#c9d1d9")
        # Non-2026 never highlights, even when live-tracked.
        later = _resolution_text(
            datetime(2028, 11, 7, tzinfo=timezone.utc), highlight=True
        )
        self.assertEqual(later.plain, "Nov 28")
        self.assertEqual(str(later.style), "#c9d1d9")

    def test_static_snapshot_reobserved_on_timer_never_confirms(self):
        # A quiet market sends one frame; the refresh timer must not ratchet it
        # to CONFIRMED off the same snapshot. Confirmation needs genuine updates.
        pair = _pair()
        app = self._app(pair)  # min_updates = 3
        now = self.NOW
        deep = dict(asks=[(0.40, 100)], bids=[(0.39, 100)])
        app.store._books[1] = _gate_book(1, best_bid=0.39, best_ask=0.40, received_at=now, **deep)
        app.store._books[2] = _gate_book(
            2, best_bid=0.55, best_ask=0.56, asks=[(0.56, 100)], bids=[(0.55, 100)], received_at=now
        )
        # Re-observe the identical snapshot many times on the timer.
        for i in range(6):
            app._evaluate_gates(now + timedelta(seconds=i * 0.5))
        self.assertEqual(app._gate_state[pair.key].status, "PENDING")

        # Genuine updates (fresh received_at on both sides) do let it confirm.
        for i in range(2):
            tick = now + timedelta(seconds=10 + i)
            for rid in (1, 2):
                app.store._books[rid] = replace(app.store.get(rid), received_at=tick)
            app._evaluate_gates(tick)
        self.assertEqual(app._gate_state[pair.key].status, "CONFIRMED")


class DashboardTests(unittest.TestCase):
    def test_cache_loads_only_high_confidence_independent_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    CREATE TABLE matched_pairs (
                        kalshi_market_id TEXT,
                        polymarket_market_id TEXT,
                        confidence REAL,
                        source TEXT,
                        review_status TEXT,
                        kalshi_name TEXT,
                        polymarket_name TEXT
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO matched_pairs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        ("K1", "p1", 0.8, "independent", "high_confidence", "K1", "P1"),
                        ("K2", "p2", 1.0, "legacy", "high_confidence", "K2", "P2"),
                        ("K3", "p3", 0.7, "independent", "needs_review", "K3", "P3"),
                    ),
                )

            pairs = load_dashboard_pairs(path)

        self.assertEqual(
            [
                (pair.kalshi_market_id, pair.polymarket_market_id)
                for pair in pairs
            ],
            [("K1", "p1")],
        )

    def test_view_uses_existing_edge_math(self):
        # Staleness is no longer a view concern — Gate 5's TwoTierStaleness
        # owns that verdict; the view is pure edge display state.
        pair = _pair()
        store = BookStore()
        store.apply_message(_book_message(1, 0.60, 0.62))
        store.apply_message(_book_message(2, 0.70, 0.72))

        view = build_pair_view(pair, store)

        self.assertIsNotNone(view.best_edge)
        self.assertEqual(view.best_edge.buy_yes_venue, "KALSHI")
        self.assertEqual(str(view.gross), "0.08")
        self.assertFalse(hasattr(view, "stale"))

    def test_cache_loads_pair_specific_matcher_subscores(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    CREATE TABLE matched_pairs (
                        kalshi_market_id INTEGER,
                        polymarket_market_id INTEGER,
                        confidence REAL,
                        source TEXT,
                        review_status TEXT,
                        kalshi_name TEXT,
                        polymarket_name TEXT,
                        resolution_similarity REAL,
                        entity_similarity REAL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO matched_pairs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (1, 2, 0.88, "independent", "high_confidence", "K", "P", 0.77, 0.91),
                )

            pair = load_dashboard_pairs(path)[0]

        self.assertEqual(pair.phrase_similarity, 0.77)
        self.assertEqual(pair.entity_similarity, 0.91)


class PairMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_exact_selected_ids_and_maps_live_market_fields(self):
        requested: list[tuple[object, object]] = []

        class FakeClient:
            async def get_pair_markets(self, kalshi_id, polymarket_id):
                requested.append((kalshi_id, polymarket_id))
                return (
                        SimpleNamespace(
                            market_id=1,
                            exchange_name="KALSHI",
                            name="Full Kalshi title",
                            description="Kalshi description",
                            volume=1234,
                            volume24h=56,
                            status=SimpleNamespace(value="active"),
                            expiration_datetime="2026-12-31T23:59:59Z",
                            ticker="KXEXAMPLE-26-T1",
                            slug=None,
                            event_ticker="KXEXAMPLE-26",
                        ),
                        SimpleNamespace(
                            market_id=2,
                            exchange_name="POLYMARKET",
                            name="Full Polymarket title",
                            description="Polymarket description",
                            volume=9876,
                            volume24h=54,
                            status=SimpleNamespace(value="closed"),
                            expiration_datetime="2026-06-30T23:59:59Z",
                            ticker="will-example-happen",
                            slug="will-example-happen",
                            event_ticker="example-event",
                        ),
                )

        details = await fetch_pair_metadata(FakeClient(), _pair())

        self.assertEqual(requested, [(1, 2)])
        self.assertEqual(details["1"].description, "Kalshi description")
        self.assertEqual(details["1"].ticker, "KXEXAMPLE-26-T1")
        self.assertEqual(details["1"].status, "active")
        self.assertEqual(details["2"].volume_24h, 54)
        self.assertEqual(details["2"].status, "closed")
        self.assertEqual(details["2"].slug, "will-example-happen")

    def test_builds_verified_venue_contract_urls(self):
        kalshi = MarketDetail(
            9115,
            "KALSHI",
            "Will the Fed cut rates 5 times?",
            None,
            None,
            None,
            ticker="KXRATECUTCOUNT-26DEC31-T5",
        )
        polymarket = MarketDetail(
            693477,
            "POLYMARKET",
            "Will 5 Fed rate cuts happen in 2026?",
            None,
            None,
            None,
            slug="will-5-fed-rate-cuts-happen-in-2026",
            event_ticker="how-many-fed-rate-cuts-in-2026",
        )

        self.assertEqual(
            _contract_url(kalshi),
            "https://kalshi.com/markets/KXRATECUTCOUNT-26DEC31-T5",
        )
        self.assertEqual(
            _contract_url(polymarket),
            "https://polymarket.us/event/how-many-fed-rate-cuts-in-2026/"
            "will-5-fed-rate-cuts-happen-in-2026",
        )

    def test_contract_url_is_visible_and_has_rich_osc8_link_style(self):
        metadata = MarketDetail(
            1,
            "KALSHI",
            "Full Kalshi title",
            "Description",
            100,
            10,
            ticker="KXEXAMPLE-26-T1",
        )
        rendered = _market_detail_text(
            venue="KALSHI",
            pair=_pair(),
            metadata=metadata,
            fallback_name="Fallback",
            book=None,
            loading_metadata=False,
        )
        expected_url = "https://kalshi.com/markets/KXEXAMPLE-26-T1"

        self.assertIn(expected_url, rendered.plain)
        self.assertTrue(
            any(
                isinstance(span.style, Style) and span.style.link == expected_url
                for span in rendered.spans
            )
        )

    def test_excluded_missing_market_is_shown_as_closed_or_unavailable(self):
        rendered = _market_detail_text(
            venue="KALSHI",
            pair=_pair(),
            metadata=None,
            fallback_name="Saved exclusion name",
            book=None,
            loading_metadata=False,
            unavailable_is_closed=True,
        )

        self.assertIn("CLOSED / NO LONGER AVAILABLE", rendered.plain)
        self.assertIn("Saved exclusion name", rendered.plain)


class DashboardAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_headless_app_mounts_status_and_table_without_sparkline(self):
        app = ArbDashboard((_pair(),), connect_feed=False)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#edge-table", DataTable)
            self.assertEqual(table.row_count, 1)
            self.assertEqual(len(table.columns), 13)
            self.assertIn("no depth or staleness gates applied", TABLE_TITLE)
            self.assertEqual(len(app.query("#spark-panel")), 0)
            self.assertEqual(len(app.query("#edge-sparkline")), 0)

    async def test_source_column_marks_independent_matches(self):
        second = DashboardPair(
            kalshi_market_id=3,
            polymarket_market_id=4,
            kalshi_name="K second",
            polymarket_name="P second",
            confidence=0.95,
            source="independent",
        )
        app = ArbDashboard((_pair(), second), connect_feed=False)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#edge-table", DataTable)
            self.assertEqual(table.get_cell("1:2", "source").plain, "I")
            self.assertEqual(table.get_cell("3:4", "source").plain, "I")

    async def test_status_strip_source_counts_match_the_src_column(self):
        second = DashboardPair(3, 4, "K second", "P second", 0.95, "independent")
        app = ArbDashboard((_pair(), second), connect_feed=False)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            strip = app.query_one("#status-strip", Static).render().plain
            self.assertIn("IND 2", strip)

            # The counts are over the same live pairs the Src column renders, so
            # they stay in lockstep — e.g. after a pair is flagged away.
            table = app.query_one("#edge-table", DataTable)
            src_independent = sum(
                1
                for i in range(table.row_count)
                if table.get_row_at(i)[1].plain == "I"
            )
            self.assertEqual(src_independent, 2)

    async def test_stale_row_renders_status_and_dims_in_the_table(self):
        pair = _pair()
        app = ArbDashboard((pair,), connect_feed=False, refresh_seconds=1000)
        async with app.run_test(size=(200, 20)) as pilot:
            await pilot.pause()
            now = datetime.now(timezone.utc)
            app.store._books[1] = _gate_book(
                1, best_bid=0.39, best_ask=0.40,
                asks=[(0.40, 100)], bids=[(0.39, 100)],
                received_at=now - timedelta(seconds=30),  # stale Kalshi leg
            )
            app.store._books[2] = _gate_book(
                2, best_bid=0.55, best_ask=0.56,
                asks=[(0.56, 100)], bids=[(0.55, 100)], received_at=now,
            )
            app._refresh_dashboard()
            await pilot.pause()
            table = app.query_one("#edge-table", DataTable)
            # The status column is now a compact colored block; STALE renders
            # as the block in the STALE legend color.
            status_cell = table.get_cell(pair.key, "status")
            self.assertEqual(status_cell.plain, "■")
            self.assertEqual(app._gate_state[pair.key].status, "STALE")
            # The whole row dims: every cell carries a 'dim' style span.
            event_cell = table.get_cell(pair.key, "event")
            self.assertIn("dim", [str(span.style) for span in event_cell.spans])

    async def test_capital_return_and_resolution_columns_render_from_live_state(self):
        pair = _pair()
        app = ArbDashboard((pair,), connect_feed=False, refresh_seconds=1000)
        async with app.run_test(size=(220, 20)) as pilot:
            await pilot.pause()
            now = datetime.now(timezone.utc)
            app.store._books[1] = _gate_book(
                1, best_bid=0.39, best_ask=0.40,
                asks=[(0.40, 100)], bids=[(0.39, 100)], received_at=now,
            )
            app.store._books[2] = _gate_book(
                2, best_bid=0.50, best_ask=0.51,
                asks=[(0.51, 100)], bids=[(0.50, 100)], received_at=now,
            )
            app._refresh_dashboard()
            await pilot.pause()
            table = app.query_one("#edge-table", DataTable)
            self.assertEqual(table.get_cell(pair.key, "capital").plain, "90.00")
            # No expiration fetched yet: clear placeholder, not a wrong number.
            self.assertEqual(table.get_cell(pair.key, "return").plain, "? no exp")
            self.assertEqual(table.get_cell(pair.key, "resolution").plain, "—")

            # Expirations land (Polymarket preferred; Kalshi >30d away).
            app._expirations[2] = now + timedelta(days=365)
            app._expirations[1] = now + timedelta(days=460)
            app._refresh_dashboard()
            await pilot.pause()
            # The cell shows the clean Polymarket month/year — no per-row ⚠;
            # the mismatch is aggregated into the status strip count instead.
            resolution = table.get_cell(pair.key, "resolution").plain
            self.assertEqual(resolution, (now + timedelta(days=365)).strftime("%b %y"))
            # Native fee schedule: locked 7.32 / capital 90 over ~365d.
            self.assertEqual(table.get_cell(pair.key, "return").plain, "+8.1%/y")
            strip = app.query_one("#status-strip", Static).render().plain
            self.assertIn("EXP⚠ 1 / 1", strip)
            # Legend for the block column is visible in the status strip.
            self.assertIn("conf", strip)
            self.assertIn("evap", strip)

    async def test_k_and_p_open_contract_urls_in_default_browser(self):
        opened: list[str] = []
        pair = _pair()
        app = ArbDashboard((pair,), connect_feed=False)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            detail = app.screen
            self.assertIsInstance(detail, PairDetailScreen)
            detail.metadata = {
                1: MarketDetail(
                    1, "KALSHI", "K", None, None, None, ticker="KXEXAMPLE-26-T1"
                ),
                2: MarketDetail(
                    2, "POLYMARKET", "P", None, None, None,
                    slug="will-example-happen", event_ticker="example-event",
                ),
            }
            with unittest.mock.patch("webbrowser.open") as mock_open:
                await pilot.press("k")
                await pilot.pause()
                mock_open.assert_called_once_with(
                    "https://kalshi.com/markets/KXEXAMPLE-26-T1"
                )
                status = detail.query_one("#detail-live-status", Label).render().plain
                self.assertIn("OPENED KALSHI IN BROWSER", status)

                mock_open.reset_mock()
                await pilot.press("p")
                await pilot.pause()
                mock_open.assert_called_once_with(
                    "https://polymarket.us/event/example-event/will-example-happen"
                )
            # The keys are documented in the modal footer.
            footer = detail.query_one("#detail-footer").render().plain
            self.assertIn("k / p open Kalshi / Polymarket in browser", footer)

    async def test_detail_modal_flags_per_pair_expiration_mismatch(self):
        app = ArbDashboard((_pair(),), connect_feed=False)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            detail = app.screen
            self.assertIsInstance(detail, PairDetailScreen)
            detail.metadata = {
                1: MarketDetail(
                    1, "KALSHI", "K", None, None, None,
                    expiration_datetime="2029-11-07T00:00:00+00:00",
                ),
                2: MarketDetail(
                    2, "POLYMARKET", "P", None, None, None,
                    expiration_datetime="2028-11-07T00:00:00+00:00",
                ),
            }
            detail._refresh_detail()
            status = detail.query_one("#detail-live-status", Label).render().plain
            self.assertIn("EXP MISMATCH >30d", status)
            self.assertIn("using Polymarket", status)

            # Agreeing dates -> no mismatch note.
            detail.metadata[1] = MarketDetail(
                1, "KALSHI", "K", None, None, None,
                expiration_datetime="2028-11-08T00:00:00+00:00",
            )
            detail._refresh_detail()
            status = detail.query_one("#detail-live-status", Label).render().plain
            self.assertNotIn("EXP MISMATCH", status)

    async def test_enter_opens_detail_and_q_closes_without_quitting_app(self):
        app = ArbDashboard((_pair(),), connect_feed=False)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()

            self.assertIsInstance(app.screen, PairDetailScreen)
            await pilot.press("q")
            await pilot.pause()

            self.assertIs(type(app.screen), Screen)
            self.assertTrue(app.is_running)

    async def test_detail_shows_both_legs_description_prices_volume_and_scores(self):
        pair = _pair()
        app = ArbDashboard((pair,), connect_feed=False)
        app.store.apply_message(_book_message(1, 0.60, 0.62))
        app.store.apply_message(_book_message(2, 0.70, 0.72))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            detail = app.screen
            self.assertIsInstance(detail, PairDetailScreen)
            detail.metadata = {
                1: MarketDetail(
                    1,
                    "KALSHI",
                    "Full K title",
                    "Full K description",
                    1000,
                    25,
                    ticker="KXEXAMPLE-26-T1",
                ),
                2: MarketDetail(
                    2,
                    "POLYMARKET",
                    "Full P title",
                    "Full P description",
                    2000,
                    50,
                    slug="will-example-happen",
                    event_ticker="example-event",
                ),
            }
            detail._refresh_detail()

            kalshi_text = detail.query_one("#kalshi-detail").render().plain
            polymarket_text = detail.query_one("#polymarket-detail").render().plain
            self.assertIn("Full K description", kalshi_text)
            self.assertIn("Bid     0.600", kalshi_text)
            self.assertIn("Spread  0.020", kalshi_text)
            self.assertIn("Last 24h    25", kalshi_text)
            self.assertIn("Phrase      0.770", kalshi_text)
            self.assertIn("Entity      0.910", kalshi_text)
            self.assertIn(
                "https://kalshi.com/markets/KXEXAMPLE-26-T1", kalshi_text
            )
            self.assertIn("Full P description", polymarket_text)
            self.assertIn("Bid     0.700", polymarket_text)
            self.assertIn(
                "https://polymarket.us/event/example-event/will-example-happen",
                polymarket_text,
            )

    async def test_flagging_pair_removes_live_row_and_persists_review_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_path = root / "matcher_cache.sqlite3"
            review_path = root / "false_pairs.md"
            with initialize_cache(cache_path) as connection:
                connection.execute(
                    """
                    INSERT INTO matched_pairs (
                        kalshi_market_id, polymarket_market_id, confidence, matched_at, source,
                        review_status, kalshi_name, polymarket_name,
                        resolution_similarity, entity_similarity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        2,
                        0.88,
                        "2026-07-17T12:00:00+00:00",
                        "independent",
                        "high_confidence",
                        "Will Example happen?",
                        "Will Example happen in 2026?",
                        0.77,
                        0.91,
                    ),
                )

            app = ArbDashboard(
                (_pair(),),
                connect_feed=False,
                cache_path=cache_path,
                false_pairs_path=review_path,
            )
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await pilot.press("d")
                await pilot.pause()

                self.assertIsInstance(app.screen, PairDetailScreen)
                footer = app.screen.query_one("#detail-footer").render().plain
                self.assertIn("f flag as false pair", footer)

                await pilot.press("f")
                await pilot.pause()

                self.assertIs(type(app.screen), Screen)
                self.assertEqual(app.pairs, ())
                self.assertEqual(app.query_one("#edge-table", DataTable).row_count, 0)

            with sqlite3.connect(cache_path) as connection:
                matched_count = connection.execute(
                    "SELECT COUNT(*) FROM matched_pairs"
                ).fetchone()[0]
                exclusion = connection.execute(
                    """
                    SELECT kalshi_market_id, polymarket_market_id, confidence,
                           phrase_similarity, entity_similarity
                    FROM false_pair_exclusions
                    """
                ).fetchone()
            review_text = review_path.read_text(encoding="utf-8")

        self.assertEqual(matched_count, 0)
        self.assertEqual(exclusion, ("1", "2", 0.88, 0.77, 0.91))
        self.assertIn("Will Example happen?", review_text)
        self.assertIn("- Reason:\n", review_text)

    async def test_exclusions_panel_lists_toggles_and_unflags_for_next_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_path = root / "matcher_cache.sqlite3"
            review_path = root / "false_pairs.md"
            initialize_cache(cache_path).close()
            flag_false_pair(
                cache_path,
                kalshi_market_id=10,
                polymarket_market_id=20,
                kalshi_name="Kalshi excluded market",
                polymarket_name="Polymarket excluded market",
                confidence=0.81,
                phrase_similarity=0.79,
                entity_similarity=0.84,
                false_pairs_path=review_path,
            )

            app = ArbDashboard(
                (_pair(),),
                connect_feed=False,
                cache_path=cache_path,
                false_pairs_path=review_path,
            )
            async with app.run_test(size=(150, 40)) as pilot:
                await pilot.pause()
                self.assertIs(type(app.screen), Screen)
                app.store.apply_message(_book_message(10, 0.41, 0.43))
                app.store.apply_message(_book_message(20, 0.57, 0.60))

                await pilot.press("x")
                await pilot.pause()

                self.assertIsInstance(app.screen, ExclusionsScreen)
                table = app.screen.query_one("#exclusions-table", DataTable)
                self.assertEqual(table.row_count, 1)
                self.assertEqual(len(table.columns), 4)
                row_text = " ".join(
                    value.plain if isinstance(value, Text) else str(value)
                    for value in table.get_row_at(0)
                )
                self.assertIn("Kalshi excluded market", row_text)
                self.assertIn("Polymarket excluded market", row_text)
                self.assertIn("0.810", row_text)

                await pilot.press("enter")
                await pilot.pause()

                detail = app.screen
                self.assertIsInstance(detail, PairDetailScreen)
                self.assertTrue(detail.excluded)
                self.assertEqual(detail.pair.kalshi_market_id, "10")
                self.assertEqual(detail.pair.polymarket_market_id, "20")
                self.assertEqual(detail.pair.phrase_similarity, 0.79)
                self.assertEqual(detail.pair.entity_similarity, 0.84)
                detail.metadata = {
                    "10": MarketDetail(
                        10,
                        "KALSHI",
                        "Full excluded Kalshi title",
                        "Full excluded Kalshi description",
                        1234,
                        56,
                        status="active",
                    ),
                    "20": MarketDetail(
                        20,
                        "POLYMARKET",
                        "Full excluded Polymarket title",
                        "Full excluded Polymarket description",
                        9876,
                        54,
                        status="closed",
                    ),
                }
                detail._refresh_detail()
                kalshi_text = detail.query_one("#kalshi-detail").render().plain
                polymarket_text = detail.query_one("#polymarket-detail").render().plain
                self.assertIn("Full excluded Kalshi description", kalshi_text)
                self.assertIn("MARKET STATUS  ACTIVE", kalshi_text)
                self.assertIn("Bid     0.410", kalshi_text)
                self.assertIn("Spread  0.020", kalshi_text)
                self.assertIn("Total       1,234", kalshi_text)
                self.assertIn("Confidence  0.810", kalshi_text)
                self.assertIn("Phrase      0.790", kalshi_text)
                self.assertIn("Entity      0.840", kalshi_text)
                self.assertIn("Full excluded Polymarket description", polymarket_text)
                self.assertIn("MARKET STATUS  CLOSED", polymarket_text)
                self.assertIn("closed or expired", polymarket_text)

                await pilot.press("q")
                await pilot.pause()
                self.assertIsInstance(app.screen, ExclusionsScreen)

                await pilot.press("d")
                await pilot.pause()
                self.assertIsInstance(app.screen, PairDetailScreen)
                await pilot.press("q")
                await pilot.pause()
                self.assertIsInstance(app.screen, ExclusionsScreen)

                await pilot.press("x")
                await pilot.pause()
                self.assertIs(type(app.screen), Screen)

                await pilot.press("x")
                await pilot.pause()
                await pilot.press("u")
                await pilot.pause()

                self.assertIsInstance(app.screen, ExclusionsScreen)
                self.assertEqual(
                    app.screen.query_one("#exclusions-table", DataTable).row_count,
                    0,
                )
                status = app.screen.query_one("#exclusions-status", Label).render().plain
                self.assertIn("will not reappear until the next matcher rebuild", status)
                self.assertEqual(app.query_one("#edge-table", DataTable).row_count, 1)

                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(type(app.screen), Screen)

            self.assertEqual(list_false_pair_exclusions(cache_path), ())


if __name__ == "__main__":
    unittest.main()
