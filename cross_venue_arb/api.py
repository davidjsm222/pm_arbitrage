"""Direct, read-only Kalshi and Polymarket US API adapters."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

import httpx
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


KALSHI_REST_URL = os.getenv(
    "KALSHI_REST_URL", "https://external-api.kalshi.com/trade-api/v2"
)
KALSHI_WS_URL = os.getenv(
    "KALSHI_WS_URL", "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
)
POLYMARKET_US_PUBLIC_URL = os.getenv(
    "POLYMARKET_US_PUBLIC_URL", "https://gateway.polymarket.us"
)
POLYMARKET_US_WS_URL = os.getenv(
    "POLYMARKET_US_WS_URL", "wss://api.polymarket.us/v1/ws/markets"
)


def _first(data: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return default


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, Mapping):
        value = _first(value, "value", "amount")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _rows(payload: Any, *keys: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
        if isinstance(value, Mapping):
            nested = _rows(value, *keys)
            if nested:
                return nested
    return []


@dataclass(frozen=True, slots=True)
class ApiMarket:
    market_id: str
    exchange_name: str
    name: str
    subtitle: str | None
    description: str | None
    category: str
    subcategory: str | None
    expiration_datetime: str | None
    ticker: str | None = None
    slug: str | None = None
    primary_entity_name: str | None = None
    event_ticker: str | None = None
    volume: float | None = None
    volume24h: float | None = None
    event_title: str | None = None
    status: str | None = None
    taker_fee_coefficient: float | None = None


@dataclass(frozen=True, slots=True)
class BookMessage:
    type: str
    market_id: str
    data: Mapping[str, Any]


def parse_kalshi_market(data: Mapping[str, Any]) -> ApiMarket:
    ticker = str(data["ticker"])
    rules = "\n\n".join(
        value
        for value in (
            _text(data.get("rules_primary")),
            _text(data.get("rules_secondary")),
        )
        if value
    ) or None
    return ApiMarket(
        market_id=ticker,
        exchange_name="KALSHI",
        name=str(_first(data, "title", "subtitle", default=ticker)),
        subtitle=_text(_first(data, "yes_sub_title", "subtitle")),
        description=rules,
        category=str(_first(data, "category", default="Other")),
        subcategory=_text(data.get("subcategory")),
        expiration_datetime=_iso(
            _first(
                data,
                "expected_expiration_time",
                "expiration_time",
                "close_time",
                "latest_expiration_time",
            )
        ),
        ticker=ticker,
        primary_entity_name=_text(data.get("yes_sub_title")),
        event_ticker=_text(data.get("event_ticker")),
        volume=_number(_first(data, "volume_fp", "volume")),
        volume24h=_number(_first(data, "volume_24h_fp", "volume_24h")),
        event_title=_text(_first(data, "event_title", "title")),
        status=_text(data.get("status")),
        taker_fee_coefficient=_number(
            _first(data, "fee_coefficient", "feeCoefficient")
        ),
    )


def parse_polymarket_us_market(data: Mapping[str, Any]) -> ApiMarket:
    slug = str(
        _first(data, "slug", "marketSlug", "market_slug", default=data.get("id", ""))
    )
    market_id = slug
    question = str(_first(data, "question", "name", default=slug))
    outcome_title = _text(_first(data, "title", "titleShort", "participantName"))
    description = _text(_first(data, "description", "rules", "resolutionRules"))
    name = question
    if outcome_title and outcome_title.casefold() not in question.casefold():
        first_sentence = re.split(r"(?<=[.!?])\s+", description or "", maxsplit=1)[0].strip()
        name = first_sentence or f"{question}: {outcome_title}"
    return ApiMarket(
        market_id=market_id,
        exchange_name="POLYMARKET",
        name=name,
        subtitle=_text(_first(data, "subtitle", "shortTitle")),
        description=description,
        category=str(_first(data, "category", default="Other")),
        subcategory=_text(data.get("subcategory")),
        expiration_datetime=_iso(
            _first(data, "endDate", "end_date", "expirationDate", "closeTime")
        ),
        slug=slug,
        primary_entity_name=_text(
            _first(data, "primaryEntityName", "participantName", "title", "titleShort")
        ),
        event_ticker=_text(_first(data, "eventSlug", "event_slug", "eventId")),
        volume=_number(_first(data, "volumeNum", "volume", "sharesTraded")),
        volume24h=_number(_first(data, "volume24hr", "volume24h", "volume_24h")),
        event_title=_text(_first(data, "eventTitle", "event_title", "question")),
        status=(
            "active"
            if bool(data.get("active")) and not bool(data.get("closed"))
            else "closed"
            if bool(data.get("closed"))
            else _text(data.get("status"))
        ),
        taker_fee_coefficient=_number(
            _first(data, "feeCoefficient", "fee_coefficient")
        ),
    )


class DirectApiClient:
    """Owns the two native REST clients and merges both venue streams."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._http = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, *, params: Mapping[str, Any] | None = None) -> Any:
        for attempt in range(8):
            response = await self._http.get(url, params=params)
            if response.status_code != 429:
                response.raise_for_status()
                return response.json()
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after is not None else 0.0
            except ValueError:
                delay = 0.0
            if delay <= 0:
                delay = min(0.5 * (2**attempt), 8.0)
            await asyncio.sleep(delay)
        response.raise_for_status()
        raise AssertionError("unreachable")

    async def list_kalshi_markets(self) -> list[ApiMarket]:
        records: dict[str, ApiMarket] = {}
        cursor = ""
        while True:
            params: dict[str, Any] = {
                "limit": 1000,
                "status": "open",
                "mve_filter": "exclude",
            }
            if cursor:
                params["cursor"] = cursor
            payload = await self._get(f"{KALSHI_REST_URL}/markets", params=params)
            for row in _rows(payload, "markets"):
                market = parse_kalshi_market(row)
                records[market.market_id] = market
            next_cursor = str(payload.get("cursor") or "") if isinstance(payload, Mapping) else ""
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            await asyncio.sleep(0.1)
        return list(records.values())

    async def list_polymarket_us_markets(self) -> list[ApiMarket]:
        records: dict[str, ApiMarket] = {}
        offset = 0
        limit = 500
        while True:
            payload = await self._get(
                f"{POLYMARKET_US_PUBLIC_URL}/v1/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "archived": "false",
                    "limit": limit,
                    "offset": offset,
                },
            )
            page = _rows(payload, "markets", "data", "results")
            for row in page:
                market = parse_polymarket_us_market(row)
                records[market.market_id] = market
            if len(page) < limit:
                break
            offset += limit
        return list(records.values())

    async def list_active_markets(self, exchange_name: str) -> list[ApiMarket]:
        if exchange_name == "KALSHI":
            return await self.list_kalshi_markets()
        if exchange_name == "POLYMARKET":
            return await self.list_polymarket_us_markets()
        raise ValueError(f"unsupported exchange: {exchange_name}")

    async def get_kalshi_market(self, ticker: str) -> ApiMarket | None:
        try:
            payload = await self._get(f"{KALSHI_REST_URL}/markets/{ticker}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        rows = _rows(payload, "markets")
        row = payload.get("market") if isinstance(payload, Mapping) else None
        if isinstance(row, Mapping):
            return parse_kalshi_market(row)
        if rows:
            return parse_kalshi_market(rows[0])
        return parse_kalshi_market(payload) if isinstance(payload, Mapping) and "ticker" in payload else None

    async def get_polymarket_us_market(self, slug: str) -> ApiMarket | None:
        try:
            payload = await self._get(
                f"{POLYMARKET_US_PUBLIC_URL}/v1/market/slug/{slug}"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        row = payload.get("market") if isinstance(payload, Mapping) else None
        if isinstance(row, Mapping):
            return parse_polymarket_us_market(row)
        rows = _rows(payload, "markets", "data", "results")
        if rows:
            return parse_polymarket_us_market(rows[0])
        return parse_polymarket_us_market(payload) if isinstance(payload, Mapping) and ("id" in payload or "slug" in payload) else None

    async def get_pair_markets(
        self, kalshi_ticker: str, polymarket_slug: str
    ) -> tuple[ApiMarket, ...]:
        results = await asyncio.gather(
            self.get_kalshi_market(kalshi_ticker),
            self.get_polymarket_us_market(polymarket_slug),
        )
        return tuple(item for item in results if item is not None)

    async def get_markets(
        self,
        kalshi_tickers: Sequence[str],
        polymarket_slugs: Sequence[str],
    ) -> tuple[ApiMarket, ...]:
        calls = [self.get_kalshi_market(ticker) for ticker in kalshi_tickers]
        calls.extend(
            self.get_polymarket_us_market(slug) for slug in polymarket_slugs
        )
        results = await asyncio.gather(*calls)
        return tuple(item for item in results if item is not None)

    async def stream_books(
        self,
        kalshi_tickers: Sequence[str],
        polymarket_slugs: Sequence[str],
    ) -> AsyncIterator[BookMessage]:
        queue: asyncio.Queue[BookMessage | BaseException | None] = asyncio.Queue()
        tasks: list[asyncio.Task[None]] = []

        async def run(stream: AsyncIterator[BookMessage]) -> None:
            try:
                async for message in stream:
                    await queue.put(message)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await queue.put(exc)
            finally:
                await queue.put(None)

        if kalshi_tickers:
            tasks.append(asyncio.create_task(run(self._stream_kalshi(kalshi_tickers))))
        if polymarket_slugs:
            tasks.append(
                asyncio.create_task(run(self._stream_polymarket_us(polymarket_slugs)))
            )
        if not tasks:
            return

        completed = 0
        try:
            while completed < len(tasks):
                item = await queue.get()
                if item is None:
                    completed += 1
                elif isinstance(item, BaseException):
                    raise item
                else:
                    yield item
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _stream_kalshi(
        self, market_tickers: Sequence[str]
    ) -> AsyncIterator[BookMessage]:
        headers = _kalshi_headers("GET", "/trade-api/ws/v2")
        books: dict[str, dict[str, dict[float, float]]] = {}
        async with websockets.connect(
            KALSHI_WS_URL, additional_headers=headers, ping_interval=20, ping_timeout=20
        ) as socket:
            for index, chunk in enumerate(_chunks(market_tickers, 100), start=1):
                await socket.send(
                    json.dumps(
                        {
                            "id": index,
                            "cmd": "subscribe",
                            "params": {
                                "channels": ["orderbook_delta"],
                                "market_tickers": list(chunk),
                            },
                        }
                    )
                )
            async for raw in socket:
                frame = json.loads(raw)
                frame_type = frame.get("type")
                body = frame.get("msg") or {}
                if frame_type == "error":
                    raise RuntimeError(f"Kalshi WebSocket error: {body}")
                if frame_type == "orderbook_snapshot":
                    ticker = str(body["market_ticker"])
                    yes = _fixed_point_levels(
                        _first(body, "yes_dollars_fp", "yes", default=[])
                    )
                    no = _fixed_point_levels(
                        _first(body, "no_dollars_fp", "no", default=[])
                    )
                    books[ticker] = {"yes": yes, "no": no}
                    yield _kalshi_book_message(ticker, books[ticker], "snapshot", body)
                elif frame_type == "orderbook_delta":
                    ticker = str(body["market_ticker"])
                    if ticker not in books:
                        continue
                    side = str(body.get("side", "yes")).lower()
                    price = _number(_first(body, "price_dollars", "price"))
                    delta = _number(_first(body, "delta_fp", "delta"))
                    if price is None or delta is None or side not in {"yes", "no"}:
                        continue
                    new_qty = books[ticker][side].get(price, 0.0) + delta
                    if new_qty <= 0:
                        books[ticker][side].pop(price, None)
                    else:
                        books[ticker][side][price] = new_qty
                    yield _kalshi_book_message(ticker, books[ticker], "update", body)

    async def _stream_polymarket_us(
        self, market_slugs: Sequence[str]
    ) -> AsyncIterator[BookMessage]:
        headers = _polymarket_us_headers("GET", "/v1/ws/markets")
        async with websockets.connect(
            POLYMARKET_US_WS_URL,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
        ) as socket:
            for index, chunk in enumerate(_chunks(market_slugs, 100), start=1):
                await socket.send(
                    json.dumps(
                        {
                            "subscribe": {
                                "request_id": f"market-data-{index}",
                                "subscription_type": 1,
                                "market_slugs": list(chunk),
                            }
                        }
                    )
                )
            async for raw in socket:
                frame = json.loads(raw)
                if "error" in frame:
                    raise RuntimeError(f"Polymarket US WebSocket error: {frame['error']}")
                if "heartbeat" in frame:
                    await socket.send(json.dumps({"heartbeat": {}}))
                    continue
                body = _first(frame, "market_data", "marketData")
                if not isinstance(body, Mapping):
                    continue
                slug = str(_first(body, "market_slug", "marketSlug"))
                bids = _polymarket_levels(body.get("bids", []))
                asks = _polymarket_levels(_first(body, "offers", "asks", default=[]))
                timestamp = _first(body, "transact_time", "transactTime")
                best_bid = max((item["price"] for item in bids), default=None)
                best_ask = min((item["price"] for item in asks), default=None)
                yield BookMessage(
                    type="update",
                    market_id=slug,
                    data={
                        "market_id": slug,
                        "is_valid": not (
                            best_bid is not None
                            and best_ask is not None
                            and best_bid >= best_ask
                        ),
                        "bids": bids,
                        "asks": asks,
                        "best_bid_price": best_bid,
                        "best_ask_price": best_ask,
                        "exchange_timestamp": timestamp,
                    },
                )


def _chunks(values: Sequence[str], size: int) -> list[Sequence[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _fixed_point_levels(raw: Any) -> dict[float, float]:
    output: dict[float, float] = {}
    for level in raw or []:
        if not isinstance(level, Sequence) or len(level) < 2:
            continue
        price = _number(level[0])
        qty = _number(level[1])
        if price is not None and qty is not None and qty > 0:
            output[price] = qty
    return output


def _kalshi_book_message(
    ticker: str,
    book: Mapping[str, Mapping[float, float]],
    frame_type: str,
    body: Mapping[str, Any],
) -> BookMessage:
    bids = [
        {"price": price, "qty": qty}
        for price, qty in sorted(book["yes"].items(), reverse=True)
        if qty > 0
    ]
    asks = [
        {"price": 1.0 - price, "qty": qty}
        for price, qty in sorted(book["no"].items(), reverse=True)
        if qty > 0
    ]
    asks.sort(key=lambda level: level["price"])
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    return BookMessage(
        type=frame_type,
        market_id=ticker,
        data={
            "market_id": ticker,
            "is_valid": not (
                best_bid is not None and best_ask is not None and best_bid >= best_ask
            ),
            "bids": bids,
            "asks": asks,
            "best_bid_price": best_bid,
            "best_ask_price": best_ask,
            "exchange_timestamp": _first(body, "ts", "timestamp"),
        },
    )


def _polymarket_levels(raw: Any) -> list[dict[str, float]]:
    output: list[dict[str, float]] = []
    for level in raw or []:
        if not isinstance(level, Mapping):
            continue
        price = _number(_first(level, "px", "price"))
        qty = _number(_first(level, "qty", "quantity", "size"))
        if price is not None and qty is not None and qty > 0:
            output.append({"price": price, "qty": qty})
    return output


def _kalshi_headers(method: str, path: str) -> dict[str, str]:
    key_id = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not private_key_path:
        raise RuntimeError(
            "set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH to stream Kalshi books"
        )
    private_key = serialization.load_pem_private_key(
        Path(private_key_path).expanduser().read_bytes(), password=None
    )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise TypeError("KALSHI_PRIVATE_KEY_PATH must contain an RSA private key")
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{path.split('?', 1)[0]}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


def _polymarket_us_headers(method: str, path: str) -> dict[str, str]:
    key_id = os.getenv("POLYMARKET_US_API_KEY_ID")
    secret = os.getenv("POLYMARKET_US_SECRET_KEY")
    if not key_id or not secret:
        raise RuntimeError(
            "set POLYMARKET_US_API_KEY_ID and POLYMARKET_US_SECRET_KEY to stream Polymarket US books"
        )
    raw_key = base64.b64decode(secret)
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(raw_key[:32])
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{path}".encode()
    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": base64.b64encode(private_key.sign(message)).decode(),
    }
