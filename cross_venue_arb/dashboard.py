"""Live Textual dashboard for raw cross-venue top-of-book edges."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import webbrowser
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import quote

from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Label, Static

from .book_store import Book, BookStore, StoreResult
from .depth import depth_walk
from .edge import DirectionEdge, top_of_book_edges
from .gate5 import (
    DEFAULT_MIN_UPDATES,
    OpportunityStatus,
    PersistenceTracker,
    TwoTierStaleness,
)
from .matcher import (
    DEFAULT_CACHE_PATH,
    FalsePairExclusion,
    flag_false_pair,
    list_false_pair_exclusions,
    unflag_false_pair,
)
from .stage1 import _client, _close_client, _orderbook_stream


REFRESH_SECONDS = 0.5
# How long an evaporated arb keeps showing EVAPORATED in the table after the
# edge dies. The tracker itself resets instantly (its one-shot final state is
# what the lifetime log needs); this is purely display stickiness.
EVAPORATED_LINGER_SECONDS = 60.0
DEFAULT_FALSE_PAIRS_PATH = Path("false_pairs.md")
TABLE_TITLE = (
    "Raw top-of-book edge, unfiltered — no depth or staleness gates applied"
    " · best of both directions"
)


@dataclass(frozen=True, slots=True)
class DashboardPair:
    kalshi_market_id: str
    polymarket_market_id: str
    kalshi_name: str
    polymarket_name: str
    confidence: float
    source: str
    phrase_similarity: float = 0.0
    entity_similarity: float = 0.0
    polymarket_fee_coefficient: float = 0.05

    @property
    def key(self) -> str:
        return f"{self.kalshi_market_id}:{self.polymarket_market_id}"

    @property
    def event_name(self) -> str:
        # Polymarket names are generally outcome-specific, while Kalshi often
        # stores the outcome identity only in its ticker/subtitle.
        return self.polymarket_name or self.kalshi_name


@dataclass(frozen=True, slots=True)
class PairView:
    pair: DashboardPair
    kalshi_book: Book | None
    polymarket_book: Book | None
    best_edge: DirectionEdge | None

    @property
    def net(self) -> Decimal | None:
        return self.best_edge.net if self.best_edge else None

    @property
    def gross(self) -> Decimal | None:
        return self.best_edge.gross if self.best_edge else None


@dataclass(frozen=True, slots=True)
class MarketDetail:
    """The native venue metadata fields displayed for one venue in the detail modal."""

    market_id: str
    exchange_name: str
    name: str
    description: str | None
    volume: float | None
    volume_24h: float | None
    status: str | None = None
    expiration_datetime: str | None = None
    ticker: str | None = None
    slug: str | None = None
    event_ticker: str | None = None

    @classmethod
    def from_api(cls, market: Any) -> "MarketDetail":
        raw_status = getattr(market, "status", None)
        return cls(
            market_id=str(market.market_id),
            exchange_name=str(market.exchange_name),
            name=str(market.name),
            description=market.description,
            volume=market.volume,
            volume_24h=market.volume24h,
            status=(
                str(getattr(raw_status, "value", raw_status))
                if raw_status is not None
                else None
            ),
            expiration_datetime=getattr(market, "expiration_datetime", None),
            ticker=getattr(market, "ticker", None),
            slug=getattr(market, "slug", None),
            event_ticker=getattr(market, "event_ticker", None),
        )


def load_dashboard_pairs(cache_path: Path = DEFAULT_CACHE_PATH) -> tuple[DashboardPair, ...]:
    """Load only cache-approved pairs without invoking matcher logic."""
    if not cache_path.exists():
        raise FileNotFoundError(
            f"matcher cache not found: {cache_path}; run cross_venue_arb.matcher first"
        )
    uri = f"file:{cache_path.resolve()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(matched_pairs)")
        }
        # Old caches predate the two matcher sub-score columns. They remain
        # readable so opening the dashboard never mutates a cache in read-only
        # mode; a fresh matcher run will populate the real values.
        phrase_column = (
            "resolution_similarity" if "resolution_similarity" in columns else "0"
        )
        entity_column = "entity_similarity" if "entity_similarity" in columns else "0"
        fee_column = (
            "polymarket_fee_coefficient"
            if "polymarket_fee_coefficient" in columns
            else "0.05"
        )
        rows = connection.execute(
            f"""
            SELECT kalshi_market_id, polymarket_market_id, kalshi_name, polymarket_name,
                   confidence, source, {phrase_column}, {entity_column}, {fee_column}
            FROM matched_pairs
            WHERE review_status = 'high_confidence'
              AND source = 'independent'
            ORDER BY kalshi_market_id, polymarket_market_id
            """
        ).fetchall()
    return tuple(
        DashboardPair(
            kalshi_market_id=str(row[0]),
            polymarket_market_id=str(row[1]),
            kalshi_name=str(row[2]),
            polymarket_name=str(row[3]),
            confidence=float(row[4]),
            source=str(row[5]),
            phrase_similarity=float(row[6]),
            entity_similarity=float(row[7]),
            polymarket_fee_coefficient=float(row[8]),
        )
        for row in rows
    )


async def fetch_pair_metadata(client: Any, pair: DashboardPair) -> dict[str, MarketDetail]:
    """Fetch current native venue metadata for exactly the selected pair."""
    markets = await client.get_pair_markets(
        pair.kalshi_market_id, pair.polymarket_market_id
    )
    return {
        detail.market_id: detail
        for market in markets
        for detail in (MarketDetail.from_api(market),)
    }


def build_pair_view(pair: DashboardPair, store: BookStore) -> PairView:
    """Build display state using the existing edge implementation unchanged.

    Staleness is not computed here: Gate 5's :class:`TwoTierStaleness` owns
    that verdict (adaptive baseline + price-move override) in the gate pass.
    """
    kalshi = store.get(pair.kalshi_market_id)
    polymarket = store.get(pair.polymarket_market_id)
    best_edge: DirectionEdge | None = None
    if kalshi is not None and polymarket is not None:
        try:
            edges = top_of_book_edges(
                kalshi,
                polymarket,
                polymarket_coefficient=Decimal(str(pair.polymarket_fee_coefficient)),
            )
        except ValueError:
            pass
        else:
            best_edge = max(
                (edges.buy_yes_kalshi, edges.buy_yes_polymarket),
                key=lambda edge: edge.net,
            )
    return PairView(
        pair=pair,
        kalshi_book=kalshi,
        polymarket_book=polymarket,
        best_edge=best_edge,
    )


def _price_text(book: Book | None) -> Text:
    if book is None or book.best_bid_price is None or book.best_ask_price is None:
        return Text("— / —", style="dim #657586")
    text = Text(f"{book.best_bid_price:.3f}", style="bold #44d7ff")
    text.append(" / ", style="dim #657586")
    text.append(f"{book.best_ask_price:.3f}", style="bold #ffaf5f")
    return text


def _edge_text(value: Decimal | None, *, net: bool = False, stale: bool = False) -> Text:
    if value is None:
        return Text("—", style="dim #657586")
    label = f"{value:+.4f}"
    if net and stale:
        return Text(label, style="dim #657586")
    if value > 0:
        return Text(label, style="bold #5fff87" if net else "#5fff87")
    if value < 0:
        return Text(label, style="bold #ff5f6d" if net else "#ff7b86")
    return Text(label, style="#d0d7de")


def _confidence_text(confidence: float) -> Text:
    color = (
        "#5fff87"
        if confidence >= 0.9
        else "#44d7ff"
        if confidence >= 0.8
        else "#ffd75f"
    )
    return Text(f"{confidence:.3f}", style=f"bold {color}")


def _source_text(source: str) -> Text:
    """Compact marker for independently generated native-API matches."""
    return Text("I", style="dim #657586")


# Display strings for the Gate 5 status column. STALE is a dashboard-level
# overlay (from TwoTierStaleness); the rest map from OpportunityStatus.
_GATE_STATUS_STYLES = {
    "CONFIRMED": "bold #5fff87",
    "PENDING": "bold #ffd75f",
    "STALE": "dim #9da7b1",
    "EVAPORATED": "dim #ff7b86",
}


def _gate_size_text(size: int) -> Text:
    if size <= 0:
        return Text("—", style="dim #657586")
    return Text(str(size), style="#c9d1d9")


def _gate_locked_text(profit: Decimal) -> Text:
    if profit <= 0:
        return Text("—", style="dim #657586")
    return Text(f"{profit:+.4f}", style="bold #5fff87")


def _gate_status_text(status: str) -> Text:
    if not status:
        return Text("—", style="dim #657586")
    return Text(status, style=_GATE_STATUS_STYLES.get(status, "#c9d1d9"))


def _gate_block_text(status: str) -> Text:
    """Compact colored block for the table; full text lives in the detail modal."""
    if not status:
        return Text("·", style="dim #657586")
    return Text("■", style=_GATE_STATUS_STYLES.get(status, "#c9d1d9"))


def _capital_text(capital: Decimal) -> Text:
    if capital <= 0:
        return Text("—", style="dim #657586")
    return Text(f"{capital:,.2f}", style="#d7afff")


# Measured live 2026-07-19 across all 548 pairs (DOC_GAPS.md): 42% of pairs
# disagree by >30 days and in every mismatch Kalshi's expiration_datetime is
# LATER (median +365.6d — commonly true date + 1y/2y, worst on standalone
# political binaries), while Polymarket matched the true resolution date in
# every ground-truth-checkable case. Polymarket is therefore the preferred
# source; a disagreement beyond this window is surfaced, never silently hidden.
RESOLUTION_MISMATCH_DAYS = 30.0


def _parse_expiration(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def resolve_pair_expiration(
    kalshi: datetime | None, polymarket: datetime | None
) -> tuple[datetime | None, bool]:
    """Pick the pair's resolution date, Polymarket first, flagging mismatches.

    Returns ``(expiration, mismatch)``. ``mismatch`` is True when both venues
    report a date and they disagree by more than ``RESOLUTION_MISMATCH_DAYS``.
    """
    mismatch = (
        kalshi is not None
        and polymarket is not None
        and abs((kalshi - polymarket).total_seconds()) / 86400.0
        > RESOLUTION_MISMATCH_DAYS
    )
    return polymarket or kalshi, mismatch


def _resolution_text(expiration: datetime | None, *, highlight: bool = False) -> Text:
    # No per-row mismatch marker: 78.8% of live two-sided rows are genuine
    # >30d mismatches (politics-dominated subset), so a per-row ⚠ was accurate
    # but pure noise. The aggregate count lives in the status strip and the
    # per-pair detail in the modal.
    if expiration is None:
        return Text("—", style="dim #657586")
    # Faint yellow highlight for 2026 resolutions so near-term capital-unlock
    # dates stand out — but only on live-tracked rows (pending / confirmed /
    # evaporated). Stale rows keep the plain tone; they dim anyway and a
    # highlight there would lend credibility to a dead quote.
    if expiration.year == 2026 and highlight:
        return Text(expiration.strftime("%b %y"), style="#e6d9a8 on #332d14")
    return Text(expiration.strftime("%b %y"), style="#c9d1d9")


def annualized_rolc(
    locked_profit: Decimal, capital: Decimal, days_to_resolution: float
) -> Decimal:
    """Buildsheet ranking metric: (profit/capital) * (365/days), simple.

    No compounding — the capital cannot be assumed redeployable the moment it
    frees up, so simple annualization is the defensible figure.
    """
    return (locked_profit / capital) * (Decimal(365) / Decimal(str(days_to_resolution)))


def _return_text(
    locked_profit: Decimal,
    capital: Decimal,
    expiration: datetime | None,
    now: datetime,
) -> Text:
    if locked_profit <= 0 or capital <= 0:
        return Text("—", style="dim #657586")
    if expiration is None:
        # No trustworthy resolution date: show a placeholder, never a wrong number.
        return Text("? no exp", style="dim #ffd75f")
    days = (expiration - now).total_seconds() / 86400.0
    if days < 1.0:
        return Text("? <1d", style="dim #ffd75f")
    value = annualized_rolc(locked_profit, capital, days)
    return Text(f"{value * 100:+,.1f}%/y", style="bold #5fff87")


@dataclass(frozen=True, slots=True)
class PairGate:
    """Gate 4 (depth) + Gate 5 (staleness/persistence) result for one pair.

    ``capturable_size``/``locked_profit`` come from the best depth-walk
    direction; ``status`` is the Gate 5 verdict shown in the table and used for
    ranking. ``stale`` drives the row dim.
    """

    stale: bool
    capturable_size: int
    locked_profit: Decimal
    capital_locked: Decimal
    best_direction: str | None
    status: str

    @property
    def is_confirmed_opportunity(self) -> bool:
        return self.status == "CONFIRMED" and self.locked_profit > 0


def _timestamp_text(book: Book | None) -> str:
    if book is None or book.exchange_timestamp is None:
        return "—"
    timestamp = book.exchange_timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _contract_url(metadata: MarketDetail | None) -> str | None:
    """Build a public venue URL from native identifiers."""
    if metadata is None:
        return None
    if metadata.exchange_name == "KALSHI" and metadata.ticker:
        ticker = quote(metadata.ticker, safe="-._~")
        return f"https://kalshi.com/markets/{ticker}"
    if metadata.exchange_name == "POLYMARKET" and metadata.slug:
        market_slug = quote(metadata.slug, safe="-._~")
        if metadata.event_ticker and metadata.event_ticker != metadata.slug:
            event_slug = quote(metadata.event_ticker, safe="-._~")
            return f"https://polymarket.us/event/{event_slug}/{market_slug}"
        return f"https://polymarket.us/event/{market_slug}"
    return None


def _market_detail_text(
    *,
    venue: str,
    pair: DashboardPair,
    metadata: MarketDetail | None,
    fallback_name: str,
    book: Book | None,
    loading_metadata: bool,
    unavailable_is_closed: bool = False,
) -> Text:
    """Render one venue's native metadata and normalized live book."""
    text = Text()
    venue_color = "#44d7ff" if venue == "KALSHI" else "#ffaf5f"
    expected_market_id = (
        pair.kalshi_market_id if venue == "KALSHI" else pair.polymarket_market_id
    )
    text.append(f"{venue}\n", style=f"bold {venue_color}")
    text.append(f"NATIVE ID  {expected_market_id}\n", style="dim #9da7b1")
    text.append("MARKET STATUS  ", style="dim #9da7b1")
    if metadata is not None:
        status = (metadata.status or "unknown").upper()
        active = status == "ACTIVE"
        status_style = (
            "bold #5fff87"
            if active
            else "dim #9da7b1"
            if status == "UNKNOWN"
            else "bold #ff5f6d"
        )
        text.append(f"{status}\n", style=status_style)
        if metadata.expiration_datetime:
            text.append(
                f"Expires  {metadata.expiration_datetime}\n",
                style="dim #9da7b1",
            )
        if not active and status != "UNKNOWN":
            text.append(
                "This market is closed or expired; current book updates may be unavailable.\n",
                style="bold #ff7b86",
            )
    elif loading_metadata:
        text.append("CHECKING…\n", style="dim #9da7b1")
    elif unavailable_is_closed:
        text.append("CLOSED / NO LONGER AVAILABLE\n", style="bold #ff5f6d")
        text.append(
            "No current metadata or book is available for this excluded market.\n",
            style="bold #ff7b86",
        )
    else:
        text.append("UNAVAILABLE\n", style="bold #ff7b86")
    text.append("\n")
    text.append("LIVE CONTRACT\n", style="bold #8be9fd")
    contract_url = _contract_url(metadata)
    if contract_url is not None:
        text.append(
            contract_url,
            style=Style(color=venue_color, underline=True, link=contract_url),
        )
        text.append("\n\n")
    elif loading_metadata:
        text.append("Loading live metadata…\n\n", style="dim #9da7b1")
    else:
        text.append("URL unavailable from native venue metadata.\n\n", style="dim #ff7b86")

    text.append("FULL MARKET NAME / TITLE\n", style="bold #8be9fd")
    text.append(f"{metadata.name if metadata else fallback_name}\n\n", style="#f0f3f6")

    text.append("DESCRIPTION\n", style="bold #8be9fd")
    if metadata is not None:
        text.append(
            f"{metadata.description or 'Not provided by the venue.'}\n\n",
            style="#c9d1d9" if metadata.description else "dim #9da7b1",
        )
    elif loading_metadata:
        text.append("Loading live metadata…\n\n", style="dim #9da7b1")
    else:
        text.append("Live native venue metadata unavailable.\n\n", style="dim #ff7b86")

    bid = book.best_bid_price if book else None
    ask = book.best_ask_price if book else None
    spread = ask - bid if bid is not None and ask is not None else None
    text.append("TOP OF BOOK\n", style="bold #8be9fd")
    text.append("Bid     ", style="dim #9da7b1")
    text.append(f"{bid:.3f}\n" if bid is not None else "—\n", style="bold #44d7ff")
    text.append("Ask     ", style="dim #9da7b1")
    text.append(f"{ask:.3f}\n" if ask is not None else "—\n", style="bold #ffaf5f")
    text.append("Spread  ", style="dim #9da7b1")
    text.append(f"{spread:.3f}\n\n" if spread is not None else "—\n\n", style="#d7afff")

    text.append("VOLUME\n", style="bold #8be9fd")
    if metadata is not None:
        volume = f"{metadata.volume:,}" if metadata.volume is not None else "—"
        volume_24h = (
            f"{metadata.volume_24h:,}" if metadata.volume_24h is not None else "—"
        )
        text.append(f"Total       {volume}\n", style="#c9d1d9")
        text.append(f"Last 24h    {volume_24h}\n\n", style="#c9d1d9")
    else:
        placeholder = "Loading…" if loading_metadata else "—"
        text.append(f"Total       {placeholder}\n", style="dim #9da7b1")
        text.append(f"Last 24h    {placeholder}\n\n", style="dim #9da7b1")

    text.append("BOOK LAST UPDATED\n", style="bold #8be9fd")
    text.append(f"{_timestamp_text(book)}\n\n", style="#c9d1d9")

    text.append("MATCHER SCORES FOR THIS PAIR\n", style="bold #8be9fd")
    text.append("Confidence  ", style="dim #9da7b1")
    text.append(f"{pair.confidence:.3f}\n", style="bold #5fff87")
    text.append("Phrase      ", style="dim #9da7b1")
    text.append(f"{pair.phrase_similarity:.3f}\n", style="bold #d7afff")
    text.append("Entity      ", style="dim #9da7b1")
    text.append(f"{pair.entity_similarity:.3f}\n", style="bold #d7afff")
    return text


class PairDetailScreen(ModalScreen[DashboardPair | None]):
    """Live, side-by-side detail for one selected cross-venue pair."""

    BINDINGS = [
        Binding("f", "flag_false_pair", "Flag false pair"),
        Binding("k", "open_kalshi", "Open Kalshi in browser"),
        Binding("p", "open_polymarket", "Open Polymarket in browser"),
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
    ]
    CSS = """
    PairDetailScreen {
        align: center middle;
        background: #000000 65%;
    }

    #detail-dialog {
        width: 94%;
        height: 92%;
        background: #0a1017;
        border: round #805ad5;
    }

    #detail-title {
        height: 3;
        padding: 0 2;
        content-align: left middle;
        color: #f0f3f6;
        text-style: bold;
        background: #101923;
    }

    #detail-live-status {
        height: 1;
        padding: 0 2;
        color: #ffd75f;
        background: #0d141c;
    }

    #detail-columns {
        height: 1fr;
        padding: 1;
    }

    .venue-panel {
        width: 1fr;
        height: 1fr;
        margin: 0 1;
        padding: 1 2;
        background: #0d141c;
        border: round #286983;
    }

    #polymarket-panel {
        border: round #9b6235;
    }

    #detail-footer {
        height: 1;
        padding: 0 2;
        color: #9da7b1;
        background: #101923;
    }
    """

    def __init__(
        self,
        pair: DashboardPair,
        shared_store: BookStore,
        *,
        connect_live: bool = True,
        cache_path: Path = DEFAULT_CACHE_PATH,
        false_pairs_path: Path = DEFAULT_FALSE_PAIRS_PATH,
        excluded: bool = False,
        gate_status: str = "",
    ) -> None:
        super().__init__()
        self.pair = pair
        self.shared_store = shared_store
        self.connect_live = connect_live
        self.cache_path = cache_path
        self.false_pairs_path = false_pairs_path
        self.excluded = excluded
        self.gate_status = gate_status
        self._notice: str | None = None
        self._notice_expires_at = 0.0
        self.detail_store = BookStore()
        self.metadata: dict[str, MarketDetail] = {}
        self.metadata_loading = connect_live
        self.metadata_error: str | None = None
        self.book_status = "OPENING DEDICATED LIVE BOOK STREAM" if connect_live else "OFFLINE"

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-dialog"):
            title = (
                "EXCLUDED PAIR DETAIL · LIVE VENUE DATA"
                if self.excluded
                else "PAIR DETAIL · LIVE VENUE DATA"
            )
            yield Label(title, id="detail-title")
            yield Label("Refreshing selected pair…", id="detail-live-status")
            with Horizontal(id="detail-columns"):
                with VerticalScroll(classes="venue-panel", id="kalshi-panel"):
                    yield Static(id="kalshi-detail")
                with VerticalScroll(classes="venue-panel", id="polymarket-panel"):
                    yield Static(id="polymarket-detail")
            footer = (
                "k / p open Kalshi / Polymarket in browser · Esc / q close · "
                "returns to exclusions"
                if self.excluded
                else "f flag as false pair · k / p open Kalshi / Polymarket in "
                "browser · Esc / q close · underlying feed remains live"
            )
            yield Label(footer, id="detail-footer")

    def on_mount(self) -> None:
        self._refresh_detail()
        self.set_interval(0.25, self._refresh_detail)
        if self.connect_live:
            self.run_worker(self._fetch_live_metadata(), name="pair-metadata")
            self.run_worker(self._consume_live_books(), name="pair-books")

    def action_dismiss(self) -> None:
        self.dismiss(None)

    def action_flag_false_pair(self) -> None:
        if self.excluded:
            self.query_one("#detail-live-status", Label).update(
                "PAIR IS ALREADY IN FALSE-PAIR EXCLUSIONS"
            )
            return
        try:
            flag_false_pair(
                self.cache_path,
                kalshi_market_id=self.pair.kalshi_market_id,
                polymarket_market_id=self.pair.polymarket_market_id,
                kalshi_name=self.pair.kalshi_name,
                polymarket_name=self.pair.polymarket_name,
                confidence=self.pair.confidence,
                phrase_similarity=self.pair.phrase_similarity,
                entity_similarity=self.pair.entity_similarity,
                false_pairs_path=self.false_pairs_path,
            )
        except Exception as exc:
            self.query_one("#detail-live-status", Label).update(
                f"FLAG FAILED · {type(exc).__name__}: {exc}"
            )
            self.log.error("False-pair flag failed: %s", exc)
            return
        self.dismiss(self.pair)

    def action_open_kalshi(self) -> None:
        self._open_in_browser(self.pair.kalshi_market_id, "Kalshi")

    def action_open_polymarket(self) -> None:
        self._open_in_browser(self.pair.polymarket_market_id, "Polymarket")

    def _open_in_browser(self, market_id: str, venue_label: str) -> None:
        """Open the venue's verified contract URL in the default browser.

        Complements the OSC 8 hyperlinks, which many terminals don't make
        clickable; the plain-text URLs stay visible in the panels either way.
        """
        url = _contract_url(self.metadata.get(market_id))
        if url is None:
            reason = (
                "still loading" if self.metadata_loading else "unavailable from the venue"
            )
            self._show_notice(f"NO {venue_label.upper()} URL · metadata {reason}")
            return
        try:
            webbrowser.open(url)
        except Exception as exc:
            self._show_notice(f"BROWSER OPEN FAILED · {type(exc).__name__}: {exc}")
            return
        self._show_notice(f"OPENED {venue_label.upper()} IN BROWSER · {url}")

    def _show_notice(self, message: str, *, seconds: float = 4.0) -> None:
        self._notice = message
        self._notice_expires_at = monotonic() + seconds
        self.query_one("#detail-live-status", Label).update(message)

    def _freshest_book(self, market_id: str) -> Book | None:
        shared = self.shared_store.get(market_id)
        dedicated = self.detail_store.get(market_id)
        if shared is None:
            return dedicated
        if dedicated is None:
            return shared
        return max((shared, dedicated), key=lambda book: book.received_at)

    async def _fetch_live_metadata(self) -> None:
        client = None
        try:
            client = _client()
            self.metadata = await fetch_pair_metadata(client, self.pair)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.metadata_error = f"{type(exc).__name__}: {exc}"
            self.log.warning("Selected-pair metadata refresh failed: %s", exc)
        finally:
            self.metadata_loading = False
            if client is not None:
                await _close_client(client)

    async def _consume_live_books(self) -> None:
        client = None
        try:
            client = _client()
            async with _orderbook_stream(
                client,
                [self.pair.kalshi_market_id],
                [self.pair.polymarket_market_id],
            ) as subscription:
                self.book_status = "DEDICATED PAIR BOOK STREAM LIVE"
                async for message in subscription:
                    try:
                        self.detail_store.apply_message(message)
                    except (TypeError, ValueError) as exc:
                        self.log.warning("Malformed selected-pair venue frame: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.book_status = f"PAIR STREAM ERROR · {type(exc).__name__}: {exc}"
            self.log.warning("Selected-pair book refresh failed: %s", exc)
        finally:
            if client is not None:
                await _close_client(client)

    def _refresh_detail(self) -> None:
        kalshi_book = self._freshest_book(self.pair.kalshi_market_id)
        polymarket_book = self._freshest_book(self.pair.polymarket_market_id)
        self.query_one("#kalshi-detail", Static).update(
            _market_detail_text(
                venue="KALSHI",
                pair=self.pair,
                metadata=self.metadata.get(self.pair.kalshi_market_id),
                fallback_name=self.pair.kalshi_name,
                book=kalshi_book,
                loading_metadata=self.metadata_loading,
                unavailable_is_closed=self.excluded and self.metadata_error is None,
            )
        )
        self.query_one("#polymarket-detail", Static).update(
            _market_detail_text(
                venue="POLYMARKET",
                pair=self.pair,
                metadata=self.metadata.get(self.pair.polymarket_market_id),
                fallback_name=self.pair.polymarket_name,
                book=polymarket_book,
                loading_metadata=self.metadata_loading,
                unavailable_is_closed=self.excluded and self.metadata_error is None,
            )
        )
        # A transient notice (browser-open feedback) wins over the rolling
        # status line until it expires, so the 0.25s refresh can't clobber it.
        if self._notice is not None and monotonic() < self._notice_expires_at:
            self.query_one("#detail-live-status", Label).update(self._notice)
            return
        self._notice = None
        metadata_status = (
            "METADATA REFRESHING"
            if self.metadata_loading
            else f"METADATA ERROR · {self.metadata_error}"
            if self.metadata_error
            else "METADATA LIVE"
        )
        gate_segment = f"   ·   GATE {self.gate_status}" if self.gate_status else ""
        # Per-pair expiration-mismatch detail (the table shows only an
        # aggregate count). Both venues' raw dates are in the panels' Expires
        # lines; this flags when they disagree beyond the threshold.
        kalshi_meta = self.metadata.get(self.pair.kalshi_market_id)
        polymarket_meta = self.metadata.get(self.pair.polymarket_market_id)
        _, mismatch = resolve_pair_expiration(
            _parse_expiration(kalshi_meta.expiration_datetime if kalshi_meta else None),
            _parse_expiration(
                polymarket_meta.expiration_datetime if polymarket_meta else None
            ),
        )
        mismatch_segment = (
            f"   ·   EXP MISMATCH >{RESOLUTION_MISMATCH_DAYS:.0f}d — using Polymarket"
            if mismatch
            else ""
        )
        self.query_one("#detail-live-status", Label).update(
            f"{metadata_status}   ·   {self.book_status}{gate_segment}{mismatch_segment}"
        )


class ExclusionsScreen(ModalScreen[None]):
    """Scrollable view of persistent false-pair exclusions."""

    BINDINGS = [
        Binding("d", "show_detail", "Pair details"),
        Binding("u", "unflag_selected", "Unflag selected"),
        Binding("x", "dismiss", "Close"),
        Binding("escape", "dismiss", "Close", show=False),
    ]

    CSS = """
    ExclusionsScreen {
        align: center middle;
        background: #000000 65%;
    }

    #exclusions-dialog {
        width: 94%;
        height: 82%;
        padding: 0 1;
        background: #0a1017;
        border: round #805ad5;
    }

    #exclusions-title {
        height: 2;
        content-align: center middle;
        color: #e6edf3;
        text-style: bold;
    }

    #exclusions-status {
        height: 2;
        padding: 0 1;
        content-align: left middle;
        color: #9da7b1;
    }

    #exclusions-table {
        height: 1fr;
        background: #0a1017;
    }

    ExclusionsScreen DataTable > .datatable--header {
        background: #13212d;
        color: #8be9fd;
        text-style: bold;
    }

    ExclusionsScreen DataTable > .datatable--cursor {
        background: #183348;
        color: #ffffff;
    }

    #exclusions-footer {
        height: 2;
        content-align: center middle;
        color: #9da7b1;
    }
    """

    def __init__(
        self,
        cache_path: Path,
        shared_store: BookStore,
        *,
        connect_live: bool,
        false_pairs_path: Path,
    ) -> None:
        super().__init__()
        self.cache_path = cache_path
        self.shared_store = shared_store
        self.connect_live = connect_live
        self.false_pairs_path = false_pairs_path
        self._rows: tuple[FalsePairExclusion, ...] = ()

    def compose(self) -> ComposeResult:
        with Vertical(id="exclusions-dialog"):
            yield Label("FALSE PAIR EXCLUSIONS", id="exclusions-title")
            yield Label("", id="exclusions-status")
            yield DataTable(id="exclusions-table", zebra_stripes=True)
            yield Label(
                "Enter / d detail  ·  u unflag selected  ·  x / Esc close  ·  "
                "restored only after next matcher rebuild",
                id="exclusions-footer",
            )

    def on_mount(self) -> None:
        table = self.query_one("#exclusions-table", DataTable)
        table.cursor_type = "row"
        table.add_column("Kalshi name", key="kalshi", width=48)
        table.add_column("Polymarket name", key="polymarket", width=52)
        table.add_column("Confidence", key="confidence", width=12)
        table.add_column("Flagged at", key="flagged_at", width=34)
        self._reload_rows()
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "exclusions-table":
            self._open_detail_at(event.cursor_row)

    def action_show_detail(self) -> None:
        table = self.query_one("#exclusions-table", DataTable)
        self._open_detail_at(table.cursor_row)

    def _open_detail_at(self, cursor_row: int) -> None:
        if cursor_row < 0 or cursor_row >= len(self._rows):
            self.query_one("#exclusions-status", Label).update(
                "No exclusion selected."
            )
            return
        exclusion = self._rows[cursor_row]
        pair = DashboardPair(
            kalshi_market_id=exclusion.kalshi_market_id,
            polymarket_market_id=exclusion.polymarket_market_id,
            kalshi_name=exclusion.kalshi_name,
            polymarket_name=exclusion.polymarket_name,
            confidence=exclusion.confidence,
            source="false_pair_exclusion",
            phrase_similarity=exclusion.phrase_similarity,
            entity_similarity=exclusion.entity_similarity,
        )
        self.app.push_screen(
            PairDetailScreen(
                pair,
                self.shared_store,
                connect_live=self.connect_live,
                cache_path=self.cache_path,
                false_pairs_path=self.false_pairs_path,
                excluded=True,
            )
        )

    def _reload_rows(self, message: str | None = None) -> None:
        table = self.query_one("#exclusions-table", DataTable)
        # DataTable.clear() resets scroll and cursor to the top; capture them so
        # unflagging a row (which rebuilds from disk) does not scroll the panel
        # back up under the user.
        previous_scroll_y = table.scroll_y
        previous_cursor_row = table.cursor_row
        self._rows = list_false_pair_exclusions(self.cache_path)
        table.clear()
        for exclusion in self._rows:
            table.add_row(
                Text(exclusion.kalshi_name, style="#e6edf3"),
                Text(exclusion.polymarket_name, style="#e6edf3"),
                _confidence_text(exclusion.confidence),
                Text(exclusion.flagged_at, style="#9da7b1"),
                key=f"{exclusion.kalshi_market_id}:{exclusion.polymarket_market_id}",
            )
        if self._rows:
            table.move_cursor(
                row=min(previous_cursor_row, len(self._rows) - 1), scroll=False
            )
            table.scroll_y = previous_scroll_y
            table.scroll_target_y = previous_scroll_y
        status = message or (
            f"{len(self._rows)} persistent exclusion(s)"
            if self._rows
            else "No false-pair exclusions."
        )
        self.query_one("#exclusions-status", Label).update(status)

    def action_unflag_selected(self) -> None:
        table = self.query_one("#exclusions-table", DataTable)
        if (
            not self._rows
            or table.cursor_row < 0
            or table.cursor_row >= len(self._rows)
        ):
            self.query_one("#exclusions-status", Label).update("No exclusion selected.")
            return

        exclusion = self._rows[table.cursor_row]
        removed = unflag_false_pair(
            self.cache_path,
            exclusion.kalshi_market_id,
            exclusion.polymarket_market_id,
        )
        if removed:
            message = (
                f"Unflagged K={exclusion.kalshi_market_id} / P={exclusion.polymarket_market_id}. "
                "It is eligible again, but will not reappear until the next "
                "matcher rebuild."
            )
        else:
            message = (
                "That exclusion was already removed. The pair can reappear only "
                "after the next matcher rebuild."
            )
        self._reload_rows(message)

    def action_dismiss(self) -> None:
        self.dismiss(None)


class ArbDashboard(App[None]):
    """Btop-inspired live status display; no gate or trading behavior."""

    TITLE = "Cross-Venue Markets Cross-Venue Arb Monitor"
    BINDINGS = [
        Binding("d", "show_detail", "Pair details"),
        Binding("x", "show_exclusions", "Exclusions"),
        Binding("r", "resort", "Re-sort now"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]
    CSS = """
    Screen {
        background: #070b10;
        color: #c9d1d9;
        layout: vertical;
    }

    #status-strip {
        height: 3;
        margin: 0 1;
        padding: 0 1;
        background: #0d141c;
        border: round #00afaf;
        content-align: left middle;
    }

    #edge-panel {
        height: 1fr;
        margin: 0 1;
        background: #0a1017;
        border: round #286983;
    }

    #edge-title {
        height: 1;
        padding: 0 1;
        color: #8be9fd;
        text-style: bold;
        background: #101923;
    }

    #edge-table {
        height: 1fr;
        background: #0a1017;
    }

    DataTable > .datatable--header {
        background: #13212d;
        color: #8be9fd;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #183348;
        color: #ffffff;
    }

    Footer {
        height: 1;
        background: #101923;
        color: #9da7b1;
    }
    """

    def __init__(
        self,
        pairs: tuple[DashboardPair, ...],
        *,
        refresh_seconds: float = REFRESH_SECONDS,
        duration_seconds: float | None = None,
        connect_feed: bool = True,
        cache_path: Path = DEFAULT_CACHE_PATH,
        false_pairs_path: Path = DEFAULT_FALSE_PAIRS_PATH,
        persistence_min_updates: int | None = DEFAULT_MIN_UPDATES,
        persistence_min_duration_seconds: float | None = None,
    ) -> None:
        super().__init__()
        if not pairs:
            raise RuntimeError("matcher cache contains no subscribed high-confidence pairs")
        self.pairs = pairs
        self.refresh_seconds = refresh_seconds
        self.duration_seconds = duration_seconds
        self.connect_feed = connect_feed
        self.cache_path = cache_path
        self.false_pairs_path = false_pairs_path
        self.store = BookStore()
        self.started_at = monotonic()
        self.connection_status = "STARTING"
        self.connection_error: str | None = None
        self.subscribed_pair_count = 0
        self._venue_by_market_id = {
            market_id: venue
            for pair in pairs
            for market_id, venue in (
                (pair.kalshi_market_id, "KALSHI"),
                (pair.polymarket_market_id, "POLYMARKET"),
            )
        }
        self._frame_times: dict[str, deque[float]] = {
            "KALSHI": deque(),
            "POLYMARKET": deque(),
        }
        self._row_order: tuple[str, ...] = ()
        self._row_signatures: dict[str, tuple[object, ...]] = {}
        self._resort_requested = False
        self._sort_frozen = False
        # Gate 4 + Gate 5 live state. The tracker persists across evaluations so
        # it can count consecutive survivals and detect evaporation; _gate_state
        # holds the latest per-pair verdict for rendering and ranking.
        self._persistence = PersistenceTracker(
            min_updates=persistence_min_updates,
            min_duration_seconds=persistence_min_duration_seconds,
        )
        # Two-tier staleness: an adaptive per-market baseline plus a price-move
        # override. Sole owner of the staleness verdict (the old flat
        # stale_after threshold is gone).
        self._staleness = TwoTierStaleness()
        self._gate_state: dict[str, PairGate] = {}
        # When each pair's arb last genuinely evaporated, for display linger.
        self._evaporated_at: dict[str, datetime] = {}
        # Per-market resolution dates, fetched once at startup via REST — the
        # matcher cache stores none. Keyed by market_id; None means the venue
        # reported no usable expiration.
        self._expirations: dict[str, datetime | None] = {}

    def compose(self) -> ComposeResult:
        yield Static(id="status-strip")
        with Vertical(id="edge-panel"):
            yield Label(TABLE_TITLE, id="edge-title")
            yield DataTable(id="edge-table", zebra_stripes=True, fixed_columns=1)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#edge-table", DataTable)
        table.cursor_type = "row"
        table.add_column("Event", width=42, key="event")
        table.add_column("Src", width=5, key="source")
        table.add_column("Kalshi bid / ask", width=18, key="kalshi")
        table.add_column("Polymarket bid / ask", width=22, key="polymarket")
        table.add_column("Gross / pair", width=14, key="gross")
        table.add_column("Net / pair", width=13, key="net")
        table.add_column("Match", width=9, key="confidence")
        table.add_column("Depth size", width=11, key="size")
        table.add_column("Locked / pairs", width=15, key="profit")
        table.add_column("Capital", width=10, key="capital")
        table.add_column("Return", width=11, key="return")
        table.add_column("Resolution", width=11, key="resolution")
        table.add_column("St", width=3, key="status")
        self._refresh_dashboard()
        self.set_interval(self.refresh_seconds, self._refresh_dashboard)
        if self.duration_seconds is not None:
            self.set_timer(self.duration_seconds, self.exit)
        if self.connect_feed:
            self.run_worker(self._consume_feed(), name="market-feed", exclusive=True)
            self.run_worker(self._fetch_expirations(), name="expirations")
        else:
            self.connection_status = "OFFLINE TEST"

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "edge-table":
            self._open_pair_detail(str(event.row_key.value))

    def action_show_detail(self) -> None:
        table = self.query_one("#edge-table", DataTable)
        cursor_row = table.cursor_row
        if 0 <= cursor_row < len(self._row_order):
            self._open_pair_detail(self._row_order[cursor_row])

    def action_resort(self) -> None:
        """Force an immediate re-sort even while the order is frozen.

        This is the explicit escape hatch from the frozen state: it re-applies
        the live net-edge order and returns the view to the top of the freshly
        sorted list.
        """
        self._resort_requested = True
        self._refresh_dashboard()

    def action_show_exclusions(self) -> None:
        self.push_screen(
            ExclusionsScreen(
                self.cache_path,
                self.store,
                connect_live=self.connect_feed,
                false_pairs_path=self.false_pairs_path,
            )
        )

    def _open_pair_detail(self, pair_key: str) -> None:
        pair = next((candidate for candidate in self.pairs if candidate.key == pair_key), None)
        if pair is not None:
            gate = self._gate_state.get(pair.key)
            self.push_screen(
                PairDetailScreen(
                    pair,
                    self.store,
                    connect_live=self.connect_feed,
                    cache_path=self.cache_path,
                    false_pairs_path=self.false_pairs_path,
                    gate_status=gate.status if gate else "",
                ),
                callback=self._pair_flagged,
            )

    def _pair_flagged(self, pair: DashboardPair | None) -> None:
        if pair is None:
            return
        self.pairs = tuple(candidate for candidate in self.pairs if candidate != pair)
        self.subscribed_pair_count = len(self.pairs)
        self._gate_state.pop(pair.key, None)
        self._evaporated_at.pop(pair.key, None)
        self._refresh_dashboard()

    async def _fetch_expirations(self) -> None:
        """One-off REST fetch of every leg's expiration for Return/Resolution.

        The matcher cache stores no resolution dates, so they are looked up in
        batches at startup. Until this completes the two columns show their
        placeholder; a failure leaves the placeholder rather than a wrong number.
        """
        client = None
        try:
            client = _client()
            markets = await client.get_markets(
                [pair.kalshi_market_id for pair in self.pairs],
                [pair.polymarket_market_id for pair in self.pairs],
            )
            for market in markets:
                self._expirations[str(market.market_id)] = _parse_expiration(
                    getattr(market, "expiration_datetime", None)
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.warning("Expiration metadata fetch failed: %s", exc)
        finally:
            if client is not None:
                await _close_client(client)

    async def _consume_feed(self) -> None:
        client = None
        self.connection_status = "CONNECTING"
        try:
            client = _client()
            async with _orderbook_stream(
                client,
                [pair.kalshi_market_id for pair in self.pairs],
                [pair.polymarket_market_id for pair in self.pairs],
            ) as subscription:
                self.connection_status = "LIVE"
                self.subscribed_pair_count = len(self.pairs)
                async for message in subscription:
                    try:
                        result = self.store.apply_message(message)
                    except (TypeError, ValueError) as exc:
                        self.log.warning("Malformed venue frame: %s", exc)
                        continue
                    if result is not StoreResult.STORED:
                        continue
                    market_id = str(getattr(message, "market_id"))
                    venue = self._venue_by_market_id.get(market_id)
                    if venue is not None:
                        self._frame_times[venue].append(monotonic())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.connection_status = "ERROR"
            self.connection_error = f"{type(exc).__name__}: {exc}"
            self.log.error("direct venue feeds failed: %s", self.connection_error)
        finally:
            self.subscribed_pair_count = 0
            if client is not None:
                await _close_client(client)
            if self.connection_status != "ERROR":
                self.connection_status = "STOPPED"

    def _rates(self, now: float) -> tuple[int, int]:
        rates: list[int] = []
        for venue in ("KALSHI", "POLYMARKET"):
            times = self._frame_times[venue]
            while times and times[0] < now - 1.0:
                times.popleft()
            rates.append(len(times))
        return rates[0], rates[1]

    def _update_status(self, now: float) -> None:
        status = Text()
        status_color = {
            "LIVE": "bold #5fff87",
            "CONNECTING": "bold #ffd75f",
            "ERROR": "bold #ff5f6d",
        }.get(self.connection_status, "bold #9da7b1")
        status.append(" FEED ", style="bold #070b10 on #00afaf")
        status.append(f" {self.connection_status}", style=status_color)
        uptime = max(0, int(now - self.started_at))
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        status.append(
            f"   UPTIME {hours:02d}:{minutes:02d}:{seconds:02d}", style="#c9d1d9"
        )
        kalshi_rate, polymarket_rate = self._rates(now)
        status.append("   KALSHI ", style="#9da7b1")
        status.append(f"{kalshi_rate:>4}/s", style="bold #44d7ff")
        status.append("   POLYMARKET ", style="#9da7b1")
        status.append(f"{polymarket_rate:>4}/s", style="bold #ffaf5f")
        status.append("   PAIRS ", style="#9da7b1")
        status.append(str(self.subscribed_pair_count), style="bold #d7afff")
        status.append(f" / {len(self.pairs)}", style="dim #9da7b1")
        # Provenance breakdown over the same loaded pairs that back the table and
        # the Src column, so it can never drift from what those rows show.
        independent = sum(1 for pair in self.pairs if pair.source == "independent")
        status.append("   IND ", style="#9da7b1")
        status.append(str(independent), style="bold #9da7b1")
        status.append("   SORT ", style="#9da7b1")
        if self._sort_frozen:
            status.append("FROZEN", style="bold #ffd75f")
            status.append(" · scroll to top or r to re-sort", style="dim #9da7b1")
        else:
            status.append("LIVE", style="bold #5fff87")
        # Aggregate expiration-mismatch count (replaces the per-row ⚠, which
        # fired on ~79% of live two-sided rows and was accurate but noisy).
        if self._expirations:
            mismatched = sum(
                1 for pair in self.pairs if self._pair_expiration(pair)[1]
            )
            status.append("   EXP⚠ ", style="#9da7b1")
            status.append(
                f"{mismatched}", style="bold #ffd75f" if mismatched else "bold #5fff87"
            )
            status.append(f" / {len(self.pairs)}", style="dim #9da7b1")
        # Legend for the compact St column blocks.
        status.append("   ST ", style="#9da7b1")
        for state, label in (
            ("CONFIRMED", "conf"),
            ("PENDING", "pend"),
            ("STALE", "stale"),
            ("EVAPORATED", "evap"),
        ):
            status.append("■", style=_GATE_STATUS_STYLES[state])
            status.append(f"{label} ", style="dim #9da7b1")
        status.append("  q quit", style="dim #657586")
        if self.connection_error:
            status.append(f"   {self.connection_error}", style="bold #ff5f6d")
        self.screen_stack[0].query_one("#status-strip", Static).update(status)

    def _pair_expiration(self, pair: DashboardPair) -> tuple[datetime | None, bool]:
        return resolve_pair_expiration(
            self._expirations.get(pair.kalshi_market_id),
            self._expirations.get(pair.polymarket_market_id),
        )

    def _cells(self, view: PairView) -> tuple[Text, ...]:
        gate = self._gate_state.get(view.pair.key)
        size = gate.capturable_size if gate else 0
        locked = gate.locked_profit if gate else Decimal("0")
        capital = gate.capital_locked if gate else Decimal("0")
        status = gate.status if gate else ""
        stale = gate.stale if gate else False
        expiration, _ = self._pair_expiration(view.pair)
        cells = (
            Text(view.pair.event_name, style="#e6edf3"),
            _source_text(view.pair.source),
            _price_text(view.kalshi_book),
            _price_text(view.polymarket_book),
            _edge_text(view.gross),
            _edge_text(view.net, net=True, stale=stale),
            _confidence_text(view.pair.confidence),
            _gate_size_text(size),
            _gate_locked_text(locked),
            _capital_text(capital),
            _return_text(locked, capital, expiration, datetime.now(timezone.utc)),
            _resolution_text(
                expiration,
                highlight=status in ("PENDING", "CONFIRMED", "EVAPORATED"),
            ),
            _gate_block_text(status),
        )
        # Gate 5 staleness dims the whole row regardless of what the raw
        # top-of-book net shows — a stale quote's edge is not to be trusted.
        if stale:
            for cell in cells:
                cell.stylize("dim")
        return cells

    def _update_table(self, views: list[PairView]) -> None:
        table = self.screen_stack[0].query_one("#edge-table", DataTable)
        row_order = tuple(view.pair.key for view in views)
        desired = set(row_order)

        # Drop rows for pairs that are gone (e.g. just flagged as false pairs).
        for key in tuple(self._row_signatures):
            if key not in desired:
                table.remove_row(key)
                del self._row_signatures[key]

        # Add genuinely new rows; update existing rows' cells in place. We never
        # clear the table here: DataTable.clear() zeroes scroll_x/scroll_y, so
        # rebuilding every tick snapped the user back to the top whenever the
        # net-edge sort order shifted (which happens on nearly every live tick).
        for view in views:
            key = view.pair.key
            signature = self._view_signature(view)
            if key not in self._row_signatures:
                table.add_row(*self._cells(view), key=key)
                self._row_signatures[key] = signature
                continue
            if self._row_signatures[key] == signature:
                continue
            for column, value in zip(
                (
                    "event", "source", "kalshi", "polymarket", "gross", "net",
                    "confidence", "size", "profit", "capital", "return",
                    "resolution", "status",
                ),
                self._cells(view),
                strict=True,
            ):
                table.update_cell(key, column, value)
            self._row_signatures[key] = signature

        # Re-impose the sorted order in place — but only when it is safe to move
        # rows under the user. The rows are sorted by live net edge, so their
        # order churns on almost every tick; reshuffling while the user is
        # reading a specific row loses their place. We therefore freeze the
        # visible order whenever they have scrolled away from the top or selected
        # a row below the first, and resume automatically once they return to the
        # top. Pressing the re-sort key forces it regardless and jumps to the top
        # of the freshly sorted list. Cell contents keep updating in place either
        # way, so prices and edges stay live even while the order is frozen.
        forced = self._resort_requested
        self._resort_requested = False
        frozen = self._sort_is_frozen(table)
        if forced:
            if row_order != self._row_order:
                self._reorder_rows(table, row_order)
                self._row_order = row_order
            table.move_cursor(row=0, scroll=False)
            table.scroll_y = 0
            table.scroll_target_y = 0
            frozen = False
        elif not frozen and row_order != self._row_order:
            self._reorder_rows(table, row_order)
            self._row_order = row_order
        self._sort_frozen = frozen

    def _sort_is_frozen(self, table: DataTable) -> bool:
        """Whether live re-sorting is paused because the user is reading a row.

        A non-zero scroll offset or a cursor below the first row means the user
        has navigated to something in particular, so reshuffling rows underneath
        them would lose their place. At the resting top-of-list state (scroll at
        the top, cursor on the first row) the live net-edge sort runs freely.
        """
        return table.scroll_offset.y > 0 or table.cursor_row > 0

    @staticmethod
    def _reorder_rows(table: DataTable, row_order: tuple[str, ...]) -> None:
        """Reorder existing rows to ``row_order`` without clearing the table.

        This mirrors what ``DataTable.sort()`` does internally — it only rewrites
        the row-location map and refreshes, leaving ``scroll_y`` untouched — but
        lets us impose our own precomputed order (net edge, then confidence)
        keyed by the market-id pair rather than by re-parsing displayed cells.
        Callers only reorder at the resting top-of-list position, so the cursor
        is intentionally left on the first row rather than following its pair.
        """
        row_keys = {row_key.value: row_key for row_key in table._row_locations}
        if set(row_keys) != set(row_order):
            # Row set is momentarily out of sync (a row add/remove has not been
            # reflected yet); skip this tick rather than corrupt the mapping.
            return
        locations_type = type(table._row_locations)
        table._row_locations = locations_type(
            {row_keys[key]: index for index, key in enumerate(row_order)}
        )
        table._update_count += 1
        table.refresh()

    def _view_signature(self, view: PairView) -> tuple[object, ...]:
        gate = self._gate_state.get(view.pair.key)
        return (
            view.kalshi_book.best_bid_price if view.kalshi_book else None,
            view.kalshi_book.best_ask_price if view.kalshi_book else None,
            view.polymarket_book.best_bid_price if view.polymarket_book else None,
            view.polymarket_book.best_ask_price if view.polymarket_book else None,
            view.gross,
            view.net,
            gate.capturable_size if gate else 0,
            gate.locked_profit if gate else Decimal("0"),
            gate.capital_locked if gate else Decimal("0"),
            gate.status if gate else "",
            gate.stale if gate else False,
            # Expirations load once via REST; including them re-renders the
            # Return/Resolution cells the moment the fetch lands.
            self._expirations.get(view.pair.kalshi_market_id),
            self._expirations.get(view.pair.polymarket_market_id),
        )

    def _evaluate_gates(self, now: datetime) -> None:
        """Run Gate 4 (depth) + Gate 5 (staleness/persistence) for every pair.

        Runs on the refresh timer so it can notice a venue going *silent* — the
        staleness failure mode Gate 5 exists to catch — even when no frame
        arrives; staleness is deliberately time-driven. Confirmation is not:
        each observation carries a ``data_version`` (the venues' received_at),
        so ``min_updates`` counts genuine market updates, and a re-observed
        snapshot on a quiet market holds rather than ratcheting to CONFIRMED.
        The two jobs — staleness (clock) and persistence (data) — stay separate.
        """
        for pair in self.pairs:
            self._gate_state[pair.key] = self._evaluate_gate(pair, now)

    def _evaluate_gate(self, pair: DashboardPair, now: datetime) -> PairGate:
        kalshi = self.store.get(pair.kalshi_market_id)
        polymarket = self.store.get(pair.polymarket_market_id)
        staleness = self._staleness.assess(kalshi, polymarket, now)
        depth = (
            depth_walk(
                kalshi,
                polymarket,
                polymarket_coefficient=Decimal(str(pair.polymarket_fee_coefficient)),
            )
            if kalshi is not None and polymarket is not None
            else None
        )

        # Marker of the underlying market data: the two venues' latest update
        # timestamps. It advances only when a genuine new frame arrives on at
        # least one side, so the persistence tracker counts real market updates
        # rather than every refresh tick re-observing the same snapshot.
        data_version = (
            kalshi.received_at if kalshi is not None else None,
            polymarket.received_at if polymarket is not None else None,
        )

        # Observe both directions, keyed by (pair, buy-YES venue). A direction is
        # "present" only when it has positive locked profit AND the pair is fresh
        # — a stale quote is never a live opportunity, so staleness resets the
        # persistence counter just like the edge vanishing does.
        states = []
        for venue in ("KALSHI", "POLYMARKET"):
            direction = None
            if depth is not None:
                direction = (
                    depth.buy_yes_kalshi
                    if venue == "KALSHI"
                    else depth.buy_yes_polymarket
                )
            present = (
                direction is not None
                and direction.locked_profit > 0
                and not staleness.stale
            )
            state = self._persistence.observe(
                f"{pair.key}:{venue}", present, now, data_version=data_version
            )
            if state is not None:
                states.append(state)

        best = depth.best if depth is not None else None

        # The tracker reports EVAPORATED exactly once (so lifetimes are logged
        # once); on a 0.5s refresh that flashes for a single tick — invisible.
        # Remember genuine evaporations (edge died while both quotes were
        # live, not a staleness reset) and keep showing EVAPORATED for
        # EVAPORATED_LINGER_SECONDS so the disappearance is actually catchable.
        # A reappearing edge clears the memo immediately.
        if not staleness.stale and any(
            s.status is OpportunityStatus.EVAPORATED for s in states
        ):
            self._evaporated_at[pair.key] = now
        if any(
            s.status in (OpportunityStatus.PENDING, OpportunityStatus.CONFIRMED)
            for s in states
        ):
            self._evaporated_at.pop(pair.key, None)
        evaporated_at = self._evaporated_at.get(pair.key)
        lingering = (
            evaporated_at is not None
            and (now - evaporated_at).total_seconds() <= EVAPORATED_LINGER_SECONDS
        )
        if evaporated_at is not None and not lingering:
            del self._evaporated_at[pair.key]

        if staleness.stale:
            status = "STALE"
        elif any(s.status is OpportunityStatus.CONFIRMED for s in states):
            status = "CONFIRMED"
        elif any(s.status is OpportunityStatus.PENDING for s in states):
            status = "PENDING"
        elif lingering:
            status = "EVAPORATED"
        else:
            status = ""

        return PairGate(
            stale=staleness.stale,
            capturable_size=best.capturable_size if best is not None else 0,
            locked_profit=best.locked_profit if best is not None else Decimal("0"),
            capital_locked=best.capital_locked if best is not None else Decimal("0"),
            best_direction=best.buy_yes_venue if best is not None else None,
            status=status,
        )

    def _rank_key(self, view: PairView) -> tuple[object, ...]:
        """Rank confirmed, positive-locked-profit opportunities to the top.

        Gate-filtered results — not the raw top-of-book net — drive ordering,
        since that is the only column that is actually closer to real. Stale
        rows sink below every fresh row regardless of apparent edge.
        """
        gate = self._gate_state.get(view.pair.key)
        confirmed = 1 if (gate is not None and gate.is_confirmed_opportunity) else 0
        fresh = 0 if (gate is not None and gate.stale) else 1
        locked = gate.locked_profit if gate is not None else Decimal("0")
        net = view.net if view.net is not None else Decimal("-Infinity")
        return (confirmed, fresh, locked, net, view.pair.confidence)

    def _refresh_dashboard(self) -> None:
        now_monotonic = monotonic()
        now_datetime = datetime.now(timezone.utc)
        self._evaluate_gates(now_datetime)
        views = [build_pair_view(pair, self.store) for pair in self.pairs]
        views.sort(key=self._rank_key, reverse=True)
        self._update_table(views)
        self._update_status(now_monotonic)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH)
    parser.add_argument(
        "--duration",
        type=float,
        help="exit after N seconds; useful for development smoke tests",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run without terminal rendering; useful with --duration",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    pairs = load_dashboard_pairs(args.cache)
    app = ArbDashboard(
        pairs,
        duration_seconds=args.duration,
        cache_path=args.cache,
    )
    app.run(headless=args.headless)


if __name__ == "__main__":
    main()
