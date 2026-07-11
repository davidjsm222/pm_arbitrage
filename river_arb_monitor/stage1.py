"""Stages 1-3: live books plus two-direction top-of-book edge math."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from rivermarkets import AsyncRiverMarkets

from .book_store import Book, BookStore, StoreResult
from .edge import DirectionEdge, top_of_book_edges


load_dotenv()



@dataclass(frozen=True, slots=True)
class MarketPair:
    label: str
    kalshi_river_id: int
    polymarket_river_id: int


# Confirmed active via separate authenticated KALSHI and POLYMARKET searches.
# These are explicit candidate pairs, not a River equivalence assertion.
MARKET_PAIRS: tuple[MarketPair, ...] = (
    MarketPair("Norway", 1383, 691214),
    MarketPair("Argentina", 1421, 691231),
    MarketPair("Switzerland", 1412, 691191),
)
RIVER_IDS: tuple[int, ...] = tuple(
    river_id
    for pair in MARKET_PAIRS
    for river_id in (pair.kalshi_river_id, pair.polymarket_river_id)
)


def _credentials() -> tuple[str, str]:
    key_id = os.environ.get("RIVER_KEY_ID")
    private_key = os.environ.get("RIVER_PRIVATE_KEY")
    if not key_id or not private_key:
        raise RuntimeError(
            "set RIVER_KEY_ID and RIVER_PRIVATE_KEY from Settings -> API Keys"
        )
    return key_id, private_key


def _client() -> AsyncRiverMarkets:
    key_id, private_key = _credentials()
    return AsyncRiverMarkets(key_id=key_id, private_key=private_key)


async def _close_client(client: AsyncRiverMarkets) -> None:
    # rivermarkets 0.4.0 does not expose a public async close method.
    wrapper = getattr(client, "_client_wrapper", None)
    sdk_http_client = getattr(wrapper, "httpx_client", None)
    httpx_client = getattr(sdk_http_client, "httpx_client", sdk_http_client)
    close = getattr(httpx_client, "aclose", None)
    if callable(close):
        await close()


async def discover() -> None:
    """Query each exchange independently and print live subscription candidates."""
    client = _client()
    try:
        now = datetime.now(timezone.utc)
        for exchange_name in ("KALSHI", "POLYMARKET"):
            response = await client.markets.search_markets(
                exchange_name=exchange_name,
                status="active",
                expiration_date_start=now,
                sort_by="volume",
                limit=5,
            )
            print(f"\n{exchange_name} live candidates")
            for market in response.results:
                native_id = market.ticker or market.slug or market.condition_id or "-"
                status = getattr(market.status, "value", market.status)
                print(
                    f"  river_id={market.river_id:<10} native={native_id} "
                    f"status={status} expires={market.expiration_datetime} "
                    f"name={market.name}"
                )
    finally:
        await _close_client(client)


@asynccontextmanager
async def _orderbook_stream(
    client: AsyncRiverMarkets, river_ids: tuple[int, ...]
) -> AsyncIterator[Any]:
    """Prefer the current docs API, with a PyPI 0.4.0 compatibility path."""
    ws = getattr(client, "ws", None)
    if ws is not None:
        async with ws.orderbooks() as stream:
            await stream.subscribe(river_ids)
            yield stream
        return

    realtime = getattr(client, "realtime", None)
    if realtime is None:
        raise RuntimeError("installed rivermarkets SDK exposes neither .ws nor .realtime")
    async with realtime.orderbooks(river_ids) as stream:
        yield stream


def _top_of_book(book: Book) -> str:
    bid_qty = book.bids[0].qty if book.bids else None
    ask_qty = book.asks[0].qty if book.asks else None
    timestamp = book.exchange_timestamp.isoformat() if book.exchange_timestamp else "-"
    return (
        f"river_id={book.river_id} "
        f"bid={book.best_bid_price} qty={bid_qty} "
        f"ask={book.best_ask_price} qty={ask_qty} "
        f"exchange_timestamp={timestamp}"
    )


def _money(value: Decimal) -> str:
    return f"{value:+.4f}"


def _edge_line(pair: MarketPair, edge: DirectionEdge) -> str:
    return (
        f"pair={pair.label} buy_yes={edge.buy_yes_venue} "
        f"buy_no={edge.buy_no_venue} "
        f"ask_yes={edge.buy_yes_ask} opposing_bid_yes={edge.opposing_yes_bid} "
        f"gross={_money(edge.gross)} "
        f"kalshi_fee={edge.kalshi_fee:.4f} "
        f"polymarket_fee={edge.polymarket_fee:.4f} "
        f"net={_money(edge.net)}"
    )


async def live_edges(duration_seconds: float | None = None) -> None:
    """Print both fee-adjusted top-of-book directions whenever a BBO changes."""
    client = _client()
    store = BookStore()
    pairs_by_river_id = {
        river_id: pair
        for pair in MARKET_PAIRS
        for river_id in (pair.kalshi_river_id, pair.polymarket_river_id)
    }
    last_bbo: dict[str, tuple[float | None, ...]] = {}

    def print_pair(pair: MarketPair) -> None:
        kalshi = store.get(pair.kalshi_river_id)
        polymarket = store.get(pair.polymarket_river_id)
        if kalshi is None or polymarket is None:
            return
        signature = (
            kalshi.best_bid_price,
            kalshi.best_ask_price,
            polymarket.best_bid_price,
            polymarket.best_ask_price,
        )
        if last_bbo.get(pair.label) == signature:
            return
        last_bbo[pair.label] = signature

        edges = top_of_book_edges(kalshi, polymarket)
        directions = (edges.buy_yes_kalshi, edges.buy_yes_polymarket)
        print(f"\nK={pair.kalshi_river_id} P={pair.polymarket_river_id}")
        for direction in directions:
            print(_edge_line(pair, direction), flush=True)

        if pair.label in {"Norway", "Argentina"} and any(
            direction.net > 0 for direction in directions
        ):
            raise RuntimeError(
                f"sanity check failed: unexpected positive net edge for {pair.label}"
            )

    async def consume(subscription: Any) -> None:
        async for message in subscription:
            try:
                result = store.apply_message(message)
            except (TypeError, ValueError) as exc:
                print(f"malformed orderbook frame: {exc}", file=sys.stderr)
                continue
            river_id = getattr(message, "river_id", None)
            if result is StoreResult.DROPPED_INVALID:
                print(
                    f"river_id={river_id} dropped invalid crossed-book frame",
                    file=sys.stderr,
                )
            elif result is StoreResult.STORED and int(river_id) in pairs_by_river_id:
                print_pair(pairs_by_river_id[int(river_id)])

    try:
        async with _orderbook_stream(client, RIVER_IDS) as subscription:
            if duration_seconds is None:
                await consume(subscription)
            else:
                try:
                    async with asyncio.timeout(duration_seconds):
                        await consume(subscription)
                except TimeoutError:
                    pass
    finally:
        await _close_client(client)


async def stream(
    river_ids: tuple[int, ...], duration_seconds: float | None = None
) -> None:
    if not river_ids:
        raise RuntimeError(
            "no river IDs configured; run the discover command, then populate "
            "river_arb_monitor/river_ids.py"
        )

    client = _client()
    store = BookStore()
    snapshots: dict[int, int] = {river_id: 0 for river_id in river_ids}
    updates: dict[int, int] = {river_id: 0 for river_id in river_ids}

    async def consume(subscription: Any) -> None:
        async for message in subscription:
            try:
                result = store.apply_message(message)
            except (TypeError, ValueError) as exc:
                print(f"malformed orderbook frame: {exc}", file=sys.stderr)
                continue

            river_id = getattr(message, "river_id", None)
            frame_type = getattr(message, "type", "unknown")
            if result is StoreResult.DROPPED_INVALID:
                print(
                    f"type={frame_type} river_id={river_id} "
                    f"dropped invalid crossed-book frame "
                    f"count={store.invalid_frame_count(int(river_id))}",
                    file=sys.stderr,
                )
            elif result is StoreResult.STORED:
                typed_river_id = int(river_id)
                if frame_type == "snapshot":
                    snapshots[typed_river_id] += 1
                elif frame_type == "update":
                    updates[typed_river_id] += 1
                book = store.get(typed_river_id)
                if book is not None:
                    print(f"type={frame_type} {_top_of_book(book)}", flush=True)

    try:
        async with _orderbook_stream(client, river_ids) as subscription:
            if duration_seconds is None:
                await consume(subscription)
            else:
                try:
                    async with asyncio.timeout(duration_seconds):
                        await consume(subscription)
                except TimeoutError:
                    pass
    finally:
        await _close_client(client)

    print("\nframe counts")
    for river_id in river_ids:
        print(
            f"  river_id={river_id} snapshots={snapshots[river_id]} "
            f"updates={updates[river_id]} invalid={store.invalid_frame_count(river_id)}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("discover", help="search each exchange for live IDs")
    edge_parser = subparsers.add_parser(
        "edges", help="print both fee-adjusted top-of-book directions"
    )
    edge_parser.add_argument(
        "--duration",
        type=float,
        help="stop after this many seconds (default: run until interrupted)",
    )
    stream_parser = subparsers.add_parser("stream", help="stream configured books")
    stream_parser.add_argument(
        "--river-id",
        dest="river_ids",
        action="append",
        type=int,
        help="override the hardcoded IDs; repeat for multiple markets",
    )
    stream_parser.add_argument(
        "--duration",
        type=float,
        help="stop after this many seconds (default: run until interrupted)",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "discover":
        asyncio.run(discover())
    elif args.command == "edges":
        asyncio.run(live_edges(duration_seconds=args.duration))
    else:
        asyncio.run(
            stream(
                tuple(args.river_ids) if args.river_ids else RIVER_IDS,
                duration_seconds=args.duration,
            )
        )


if __name__ == "__main__":
    main()
