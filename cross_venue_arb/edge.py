"""Top-of-book cross-venue edge math for one executable contract pair."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_EVEN

from .book_store import Book


KALSHI_TAKER_COEFFICIENT = Decimal("0.07")
POLYMARKET_US_TAKER_COEFFICIENT = Decimal("0.05")
KALSHI_FEE_INCREMENT = Decimal("0.0001")  # one centicent
POLYMARKET_US_FEE_INCREMENT = Decimal("0.01")


def _decimal(value: float | Decimal | str | int) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _validate_trade(price: Decimal, contracts: int) -> None:
    if not Decimal("0") <= price <= Decimal("1"):
        raise ValueError(f"price must be between 0 and 1, got {price}")
    if contracts < 1:
        raise ValueError(f"contracts must be positive, got {contracts}")


def kalshi_taker_fee(
    price: float | Decimal | str,
    *,
    contracts: int = 1,
    coefficient: Decimal = KALSHI_TAKER_COEFFICIENT,
    multiplier: Decimal = Decimal("1"),
) -> Decimal:
    """Return Kalshi's rounded taker fee in dollars for the whole trade."""
    p = _decimal(price)
    _validate_trade(p, contracts)
    count = Decimal(contracts)
    position_cost = count * p
    raw_fee = multiplier * coefficient * count * p * (Decimal("1") - p)
    rounded_total = (position_cost + raw_fee).quantize(
        KALSHI_FEE_INCREMENT, rounding=ROUND_CEILING
    )
    return rounded_total - position_cost


def polymarket_us_taker_fee(
    price: float | Decimal | str,
    *,
    contracts: int = 1,
    coefficient: Decimal = POLYMARKET_US_TAKER_COEFFICIENT,
) -> Decimal:
    """Return Polymarket US's banker's-rounded taker fee for the whole trade."""
    p = _decimal(price)
    _validate_trade(p, contracts)
    count = Decimal(contracts)
    raw_fee = coefficient * count * p * (Decimal("1") - p)
    return raw_fee.quantize(POLYMARKET_US_FEE_INCREMENT, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class DirectionEdge:
    buy_yes_venue: str
    buy_no_venue: str
    buy_yes_ask: Decimal
    opposing_yes_bid: Decimal
    gross: Decimal
    kalshi_fee: Decimal
    polymarket_fee: Decimal
    net: Decimal


@dataclass(frozen=True, slots=True)
class PairEdges:
    buy_yes_kalshi: DirectionEdge
    buy_yes_polymarket: DirectionEdge


def top_of_book_edges(
    kalshi: Book,
    polymarket: Book,
    *,
    polymarket_coefficient: Decimal = POLYMARKET_US_TAKER_COEFFICIENT,
) -> PairEdges:
    """Calculate both one-pair directions from executable YES bid/ask prices."""
    prices = (
        kalshi.best_bid_price,
        kalshi.best_ask_price,
        polymarket.best_bid_price,
        polymarket.best_ask_price,
    )
    if any(price is None for price in prices):
        raise ValueError("both books must have a best YES bid and ask")

    kalshi_bid = _decimal(kalshi.best_bid_price)
    kalshi_ask = _decimal(kalshi.best_ask_price)
    polymarket_bid = _decimal(polymarket.best_bid_price)
    polymarket_ask = _decimal(polymarket.best_ask_price)

    kalshi_buy_gross = polymarket_bid - kalshi_ask
    kalshi_buy_kalshi_fee = kalshi_taker_fee(kalshi_ask)
    kalshi_buy_polymarket_fee = polymarket_us_taker_fee(
        polymarket_bid, coefficient=polymarket_coefficient
    )
    kalshi_buy = DirectionEdge(
        buy_yes_venue="KALSHI",
        buy_no_venue="POLYMARKET",
        buy_yes_ask=kalshi_ask,
        opposing_yes_bid=polymarket_bid,
        gross=kalshi_buy_gross,
        kalshi_fee=kalshi_buy_kalshi_fee,
        polymarket_fee=kalshi_buy_polymarket_fee,
        net=kalshi_buy_gross
        - kalshi_buy_kalshi_fee
        - kalshi_buy_polymarket_fee,
    )

    polymarket_buy_gross = kalshi_bid - polymarket_ask
    polymarket_buy_kalshi_fee = kalshi_taker_fee(kalshi_bid)
    polymarket_buy_polymarket_fee = polymarket_us_taker_fee(
        polymarket_ask, coefficient=polymarket_coefficient
    )
    polymarket_buy = DirectionEdge(
        buy_yes_venue="POLYMARKET",
        buy_no_venue="KALSHI",
        buy_yes_ask=polymarket_ask,
        opposing_yes_bid=kalshi_bid,
        gross=polymarket_buy_gross,
        kalshi_fee=polymarket_buy_kalshi_fee,
        polymarket_fee=polymarket_buy_polymarket_fee,
        net=polymarket_buy_gross
        - polymarket_buy_kalshi_fee
        - polymarket_buy_polymarket_fee,
    )
    return PairEdges(
        buy_yes_kalshi=kalshi_buy,
        buy_yes_polymarket=polymarket_buy,
    )
