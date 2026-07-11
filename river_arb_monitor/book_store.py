"""In-memory storage for River's full orderbook WebSocket frames."""

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
    river_id: int
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
    """Last-valid-book store keyed by ``river_id``.

    River sends complete book payloads for both snapshots and updates. Invalid
    crossed-book frames are counted and discarded, leaving the most recent
    valid book available to readers until River sends its fresh snapshot.
    """

    def __init__(self) -> None:
        self._books: dict[int, Book] = {}
        self._invalid_frames: dict[int, int] = {}
        self._last_invalid_at: dict[int, datetime] = {}

    def get(self, river_id: int) -> Book | None:
        return self._books.get(river_id)

    @property
    def books(self) -> Mapping[int, Book]:
        return self._books.copy()

    def invalid_frame_count(self, river_id: int) -> int:
        return self._invalid_frames.get(river_id, 0)

    def last_invalid_at(self, river_id: int) -> datetime | None:
        return self._last_invalid_at.get(river_id)

    def apply_message(self, message: Any) -> StoreResult:
        frame_type = _field(message, "type")
        if frame_type not in {"snapshot", "update"}:
            return StoreResult.IGNORED

        river_id = _field(message, "river_id")
        if river_id is None:
            raise ValueError(f"{frame_type} frame is missing river_id")
        river_id = int(river_id)

        data = _mapping(_field(message, "data"))
        payload_river_id = data.get("river_id")
        if payload_river_id is not None and int(payload_river_id) != river_id:
            raise ValueError(
                f"frame river_id {river_id} does not match payload river_id {payload_river_id}"
            )

        now = datetime.now(timezone.utc)
        if data.get("is_valid") is False:
            self._invalid_frames[river_id] = self.invalid_frame_count(river_id) + 1
            self._last_invalid_at[river_id] = now
            return StoreResult.DROPPED_INVALID

        bids = _levels(data.get("bids"))
        asks = _levels(data.get("asks"))
        best_bid = data.get("best_bid_price")
        best_ask = data.get("best_ask_price")

        self._books[river_id] = Book(
            river_id=river_id,
            bids=bids,
            asks=asks,
            best_bid_price=float(best_bid) if best_bid is not None else None,
            best_ask_price=float(best_ask) if best_ask is not None else None,
            exchange_timestamp=_timestamp(data.get("exchange_timestamp")),
            received_at=now,
        )
        return StoreResult.STORED

