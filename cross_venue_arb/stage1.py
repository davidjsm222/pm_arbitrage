"""Direct discovery, live books, and two-direction top-of-book edge math."""

from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, AsyncIterator, Sequence

from dotenv import load_dotenv

from .api import BookMessage, DirectApiClient
from .book_store import Book, BookStore, StoreResult
from .edge import DirectionEdge, top_of_book_edges


load_dotenv()


@dataclass(frozen=True, slots=True)
class MarketPair:
    label: str
    kalshi_market_id: str
    polymarket_market_id: str


def _client() -> DirectApiClient:
    return DirectApiClient()


async def _close_client(client: DirectApiClient) -> None:
    await client.close()


async def discover(limit: int = 20) -> None:
    """Query each venue independently and print active native identifiers."""
    client = _client()
    try:
        kalshi, polymarket = await asyncio.gather(
            client.list_kalshi_markets(), client.list_polymarket_us_markets()
        )
        for venue, markets in (("KALSHI", kalshi), ("POLYMARKET US", polymarket)):
            print(f"\n{venue} active markets ({len(markets)} discovered)")
            ranked = sorted(
                markets,
                key=lambda market: (market.volume or 0, market.volume24h or 0),
                reverse=True,
            )
            for market in ranked[:limit]:
                print(
                    f"  native_id={market.market_id} status={market.status or '-'} "
                    f"expires={market.expiration_datetime or '-'} name={market.name}"
                )
    finally:
        await _close_client(client)


@asynccontextmanager
async def _orderbook_stream(
    client: DirectApiClient,
    kalshi_market_ids: Sequence[str],
    polymarket_market_ids: Sequence[str],
) -> AsyncIterator[AsyncIterator[BookMessage]]:
    yield client.stream_books(kalshi_market_ids, polymarket_market_ids)


def _top_of_book(book: Book) -> str:
    bid_qty = book.bids[0].qty if book.bids else None
    ask_qty = book.asks[0].qty if book.asks else None
    timestamp = book.exchange_timestamp.isoformat() if book.exchange_timestamp else "-"
    return (
        f"market_id={book.market_id} bid={book.best_bid_price} qty={bid_qty} "
        f"ask={book.best_ask_price} qty={ask_qty} exchange_timestamp={timestamp}"
    )


def _money(value: Decimal) -> str:
    return f"{value:+.4f}"


def _edge_line(pair: MarketPair, edge: DirectionEdge) -> str:
    return (
        f"pair={pair.label} buy_yes={edge.buy_yes_venue} "
        f"buy_no={edge.buy_no_venue} ask_yes={edge.buy_yes_ask} "
        f"opposing_bid_yes={edge.opposing_yes_bid} gross={_money(edge.gross)} "
        f"kalshi_fee={edge.kalshi_fee:.4f} "
        f"polymarket_fee={edge.polymarket_fee:.4f} net={_money(edge.net)}"
    )


async def live_edges(
    pairs: Sequence[MarketPair], duration_seconds: float | None = None
) -> None:
    """Print both fee-adjusted directions whenever either venue's BBO changes."""
    if not pairs:
        raise RuntimeError("provide at least one --pair LABEL KALSHI_TICKER POLYMARKET_SLUG")
    client = _client()
    store = BookStore()
    polymarket_metadata = await client.get_markets(
        [], [pair.polymarket_market_id for pair in pairs]
    )
    polymarket_coefficients = {
        market.market_id: Decimal(str(market.taker_fee_coefficient or 0.05))
        for market in polymarket_metadata
    }
    pairs_by_market_id = {
        market_id: pair
        for pair in pairs
        for market_id in (pair.kalshi_market_id, pair.polymarket_market_id)
    }
    last_bbo: dict[str, tuple[float | None, ...]] = {}

    def print_pair(pair: MarketPair) -> None:
        kalshi = store.get(pair.kalshi_market_id)
        polymarket = store.get(pair.polymarket_market_id)
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
        edges = top_of_book_edges(
            kalshi,
            polymarket,
            polymarket_coefficient=polymarket_coefficients.get(
                pair.polymarket_market_id, Decimal("0.05")
            ),
        )
        print(f"\nK={pair.kalshi_market_id} P={pair.polymarket_market_id}")
        for direction in (edges.buy_yes_kalshi, edges.buy_yes_polymarket):
            print(_edge_line(pair, direction), flush=True)

    async def consume(subscription: AsyncIterator[BookMessage]) -> None:
        async for message in subscription:
            try:
                result = store.apply_message(message)
            except (TypeError, ValueError) as exc:
                print(f"malformed orderbook frame: {exc}", file=sys.stderr)
                continue
            if result is StoreResult.DROPPED_INVALID:
                print(
                    f"market_id={message.market_id} dropped crossed-book frame",
                    file=sys.stderr,
                )
            elif result is StoreResult.STORED and message.market_id in pairs_by_market_id:
                print_pair(pairs_by_market_id[message.market_id])

    try:
        async with _orderbook_stream(
            client,
            [pair.kalshi_market_id for pair in pairs],
            [pair.polymarket_market_id for pair in pairs],
        ) as subscription:
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
    kalshi_market_ids: Sequence[str],
    polymarket_market_ids: Sequence[str],
    duration_seconds: float | None = None,
) -> None:
    """Print normalized books for explicitly selected native market IDs."""
    if not kalshi_market_ids and not polymarket_market_ids:
        raise RuntimeError("provide --kalshi and/or --polymarket native market IDs")
    client = _client()
    store = BookStore()
    counts: dict[str, int] = {
        market_id: 0 for market_id in (*kalshi_market_ids, *polymarket_market_ids)
    }

    async def consume(subscription: AsyncIterator[BookMessage]) -> None:
        async for message in subscription:
            result = store.apply_message(message)
            if result is StoreResult.STORED:
                counts[message.market_id] = counts.get(message.market_id, 0) + 1
                book = store.get(message.market_id)
                if book is not None:
                    print(f"type={message.type} {_top_of_book(book)}", flush=True)

    try:
        async with _orderbook_stream(
            client, kalshi_market_ids, polymarket_market_ids
        ) as subscription:
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
    for market_id, count in counts.items():
        print(
            f"  market_id={market_id} stored={count} "
            f"invalid={store.invalid_frame_count(market_id)}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    discover_parser = commands.add_parser("discover", help="list active native markets")
    discover_parser.add_argument("--limit", type=int, default=20)
    edge_parser = commands.add_parser("edges", help="stream selected cross-venue pairs")
    edge_parser.add_argument(
        "--pair",
        nargs=3,
        action="append",
        metavar=("LABEL", "KALSHI_TICKER", "POLYMARKET_SLUG"),
        required=True,
    )
    edge_parser.add_argument("--duration", type=float)
    stream_parser = commands.add_parser("stream", help="stream selected venue books")
    stream_parser.add_argument("--kalshi", action="append", default=[])
    stream_parser.add_argument("--polymarket", action="append", default=[])
    stream_parser.add_argument("--duration", type=float)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "discover":
        asyncio.run(discover(args.limit))
    elif args.command == "edges":
        pairs = [MarketPair(*values) for values in args.pair]
        asyncio.run(live_edges(pairs, args.duration))
    else:
        asyncio.run(stream(args.kalshi, args.polymarket, args.duration))


if __name__ == "__main__":
    main()
