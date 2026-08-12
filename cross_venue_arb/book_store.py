"""Normalized in-memory storage for both venues' full order books."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: float
    qty: float


@dataclass(frozen=True, slots=True)
class Book:
    market_id: str
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    best_bid_price: float | None
    best_ask_price: float | None
    exchange_timestamp: datetime | None
    received_at: datetime


class StoreResult(str, Enum):
    STORED = "stored"
    DROPPED_INVALID = "dropped_invalid"
    IGNORED = "ignored"


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    raise TypeError(f"expected a mapping-like payload, got {type(value).__name__}")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _timestamp(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise TypeError("exchange_timestamp must be an ISO-8601 string or datetime")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _levels(value: Any) -> tuple[PriceLevel, ...]:
    if value is None:
        return ()
    return tuple(
        PriceLevel(price=float(_field(level, "price")), qty=float(_field(level, "qty")))
        for level in value
    )


class BookStore:
    """Last-valid-book store keyed by ``market_id``.

    Venue adapters emit complete normalized books for both snapshots and updates.
    Invalid crossed-book frames are counted and discarded, leaving the most
    recent valid book available to readers until a fresh snapshot arrives.
    """

    def __init__(self) -> None:
        self._books: dict[str | int, Book] = {}
        self._invalid_frames: dict[str, int] = {}
        self._last_invalid_at: dict[str, datetime] = {}

    def get(self, market_id: str | int) -> Book | None:
        return self._books.get(market_id) or self._books.get(str(market_id))

    @property
    def books(self) -> Mapping[str | int, Book]:
        return self._books.copy()

    def invalid_frame_count(self, market_id: str | int) -> int:
        return self._invalid_frames.get(str(market_id), 0)

    def last_invalid_at(self, market_id: str | int) -> datetime | None:
        return self._last_invalid_at.get(str(market_id))

    def apply_message(self, message: Any) -> StoreResult:
        frame_type = _field(message, "type")
        if frame_type not in {"snapshot", "update"}:
            return StoreResult.IGNORED

        market_id = _field(message, "market_id")
        if market_id is None:
            raise ValueError(f"{frame_type} frame is missing market_id")
        market_id = str(market_id)

        data = _mapping(_field(message, "data"))
        payload_market_id = data.get("market_id")
        if payload_market_id is not None and str(payload_market_id) != market_id:
            raise ValueError(
                f"frame market_id {market_id} does not match payload market_id {payload_market_id}"
            )

        now = datetime.now(timezone.utc)
        if data.get("is_valid") is False:
            self._invalid_frames[market_id] = self.invalid_frame_count(market_id) + 1
            self._last_invalid_at[market_id] = now
            return StoreResult.DROPPED_INVALID

        bids = _levels(data.get("bids"))
        asks = _levels(data.get("asks"))
        best_bid = data.get("best_bid_price")
        best_ask = data.get("best_ask_price")

        self._books[market_id] = Book(
            market_id=market_id,
            bids=bids,
            asks=asks,
            best_bid_price=float(best_bid) if best_bid is not None else None,
            best_ask_price=float(best_ask) if best_ask is not None else None,
            exchange_timestamp=_timestamp(data.get("exchange_timestamp")),
            received_at=now,
        )
        return StoreResult.STORED
