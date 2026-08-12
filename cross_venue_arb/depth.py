"""Gate 4 — depth walk for capturable size and locked profit.

Top-of-book edge on three contracts is not a trade. This module walks both
venues' full ladders, accumulating contract pairs while each *marginal* pair
stays net-positive, and reports how much size is capturable and the total locked
profit across it — for both cross-venue directions.

The fee assignment mirrors :mod:`cross_venue_arb.edge` exactly, so the first
level of the walk reproduces that module's top-of-book net per pair.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

from .book_store import Book, PriceLevel
from .edge import (
    POLYMARKET_US_TAKER_COEFFICIENT,
    kalshi_taker_fee,
    polymarket_us_taker_fee,
)


def _decimal(value: float | Decimal | str | int) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True, slots=True)
class DepthResult:
    """Capturable depth for one direction of a cross-venue pair.

    ``capturable_size`` is a whole number of contract pairs. A pair is one
    Kalshi contract against one Polymarket share, and Kalshi trades whole
    contracts only, so the count is an integer even though Polymarket itself
    permits fractional shares.

    ``capital_locked`` is the total upfront cost of taking the full capturable
    size: per pair, the buy-YES ask plus the buy-NO cost ``1 - bid``, summed at
    the actual execution price of every level consumed (not estimated from top
    of book). Both legs stay locked until resolution, which is what the
    return-on-locked-capital ranking divides by.
    """

    buy_yes_venue: str
    buy_no_venue: str
    capturable_size: int
    locked_profit: Decimal
    capital_locked: Decimal
    levels_consumed: int

    @property
    def is_capturable(self) -> bool:
        return self.capturable_size > 0


@dataclass(frozen=True, slots=True)
class PairDepth:
    """Both directions, matching :class:`cross_venue_arb.edge.PairEdges`."""

    buy_yes_kalshi: DepthResult
    buy_yes_polymarket: DepthResult

    @property
    def best(self) -> DepthResult:
        """The direction with the larger locked profit."""
        return max(
            (self.buy_yes_kalshi, self.buy_yes_polymarket),
            key=lambda result: result.locked_profit,
        )


def _sorted_levels(levels: Iterable[PriceLevel], *, ascending: bool) -> list[tuple[Decimal, Decimal]]:
    """Return positive-quantity levels as ``(price, qty)`` best-first.

    Venue adapters normalize their own level ordering, but the walk still sorts
    defensively: asks
    rise from the cheapest fill, bids fall from the richest.
    """
    priced = [
        (_decimal(level.price), _decimal(level.qty))
        for level in levels
        if _decimal(level.qty) > 0
    ]
    priced.sort(key=lambda level: level[0], reverse=not ascending)
    return priced


def _walk(
    ask_levels: Iterable[PriceLevel],
    bid_levels: Iterable[PriceLevel],
    *,
    buy_yes_venue: str,
    buy_no_venue: str,
    ask_fee: Callable[[Decimal], Decimal],
    bid_fee: Callable[[Decimal], Decimal],
) -> DepthResult:
    """Accumulate pairs while each marginal pair is net-positive.

    We buy YES on one venue by lifting its ask ladder (prices rising) and buy NO
    on the other by hitting its YES-bid ladder (prices falling). A "pair" is one
    contract on each leg; the marginal net for a pair at a given depth is

        (bid_price - ask_price) - ask_fee(ask_price) - bid_fee(bid_price)

    using per-contract fees at the actual execution prices. As soon as the next
    pair's marginal net is not strictly positive we stop — taking it would only
    give back profit — which fixes the capturable size and total locked profit.
    """
    asks = _sorted_levels(ask_levels, ascending=True)
    bids = _sorted_levels(bid_levels, ascending=False)

    size = 0
    locked = Decimal("0")
    capital = Decimal("0")
    levels_consumed = 0
    i = j = 0
    remaining_ask = asks[0][1] if asks else Decimal("0")
    remaining_bid = bids[0][1] if bids else Decimal("0")

    while i < len(asks) and j < len(bids):
        ask_price = asks[i][0]
        bid_price = bids[j][0]
        # FEE MODEL — ask_fee/bid_fee are the per-contract centicent-ceiled curve
        # fees, summed per contract. This was checked against Kalshi's real
        # two-stage non-direct-member rounding (trade_fee + rounding_fee - rebate,
        # with a whole-cent rebate accumulator) and found immaterial at realistic
        # depth-walk sizes: sub-cent divergence even at 1,200 contracts, from two
        # small effects that partly offset (per-contract vs per-fill ceiling, and
        # the cent-rounding remainder the accumulator keeps under $0.01/order).
        # Not modeled further. It only matters in the 1-3 contract tail, which the
        # depth walk isn't meant to characterize anyway.
        marginal_net = (
            (bid_price - ask_price) - ask_fee(ask_price) - bid_fee(bid_price)
        )
        # STOPPING RULE — deliberate, do not "correct" this to the build sheet's
        # "cumulative net" wording. We stop at the first pair whose *marginal*
        # net is not strictly positive, with no lookahead past it. Asks rise and
        # bids fall with depth, so marginal net is monotone non-increasing; the
        # first non-positive pair is therefore the profit-maximizing cutoff, and
        # both taking it and scanning deeper for a rebound would only give profit
        # back. Cumulative net is maximized exactly here, so the two readings
        # agree — this phrasing is the intended one.
        if marginal_net <= 0:
            break

        # A pair is one whole Kalshi contract against one Polymarket share.
        # Kalshi trades whole contracts only, so the pair count floors to an
        # integer here rather than carrying a fractional Decimal.
        pairs = int(min(remaining_ask, remaining_bid))  # floor; quantities > 0
        if pairs > 0:
            size += pairs
            locked += marginal_net * pairs
            # Upfront cost per pair at THIS level's executable prices: buy YES
            # at the ask, buy NO at (1 - bid). Summed level by level so the
            # capital figure reflects the actual fills, not top-of-book.
            capital += (ask_price + (Decimal("1") - bid_price)) * pairs
            levels_consumed += 1
            remaining_ask -= pairs
            remaining_bid -= pairs

        # Advance past any level that can no longer supply a whole pair. Only
        # Polymarket's fractional leg can leave a sub-1 remainder; it is dropped
        # (stranded) here because it cannot complete a 1:1 cross-venue pair.
        if remaining_ask < 1:
            i += 1
            remaining_ask = asks[i][1] if i < len(asks) else Decimal("0")
        if remaining_bid < 1:
            j += 1
            remaining_bid = bids[j][1] if j < len(bids) else Decimal("0")

    return DepthResult(
        buy_yes_venue=buy_yes_venue,
        buy_no_venue=buy_no_venue,
        capturable_size=size,
        locked_profit=locked,
        capital_locked=capital,
        levels_consumed=levels_consumed,
    )


def depth_walk(
    kalshi: Book,
    polymarket: Book,
    *,
    polymarket_coefficient: Decimal = POLYMARKET_US_TAKER_COEFFICIENT,
) -> PairDepth:
    """Walk both ladders for both directions of one matched pair."""
    buy_yes_kalshi = _walk(
        kalshi.asks,
        polymarket.bids,
        buy_yes_venue="KALSHI",
        buy_no_venue="POLYMARKET",
        ask_fee=lambda price: kalshi_taker_fee(price),
        bid_fee=lambda price: polymarket_us_taker_fee(
            price, coefficient=polymarket_coefficient
        ),
    )
    buy_yes_polymarket = _walk(
        polymarket.asks,
        kalshi.bids,
        buy_yes_venue="POLYMARKET",
        buy_no_venue="KALSHI",
        ask_fee=lambda price: polymarket_us_taker_fee(
            price, coefficient=polymarket_coefficient
        ),
        bid_fee=lambda price: kalshi_taker_fee(price),
    )
    return PairDepth(
        buy_yes_kalshi=buy_yes_kalshi,
        buy_yes_polymarket=buy_yes_polymarket,
    )
