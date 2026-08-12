"""Gate 5 — is the edge still there? Staleness and evaporation.

Two independent failure modes kill apparent cross-venue edges:

* **Staleness** — one venue's book is a dead quote while the other moves.
  Modelled in two tiers (:class:`TwoTierStaleness`): a per-pair *adaptive
  baseline* scaled to each market's own update cadence, plus a *price-move
  override* that fires the instant one side jumps while the other sits still,
  regardless of the baseline timer.
* **Evaporation** — the edge shows for a tick or two and vanishes. Caught by a
  persistence tracker that only promotes an opportunity to *confirmed* once it
  survives a minimum number of genuine book updates (or a minimum duration),
  and resets the instant the edge disappears.

The lifetime between ``first_seen`` and ``last_seen`` is part of the study
deliverable, so the tracker preserves those on every state it returns.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum

from .book_store import Book


DEFAULT_MIN_UPDATES = 3

# Tier 1 (adaptive baseline). Chosen from a live 60s cadence gather over ~100
# matched markets: per-market median update gap ran 0.002s–12.1s (median 1.1s,
# p75 3.3s, p90 8.9s), and 27% of markets had a median gap above the old flat
# 3.0s rule — i.e. they were falsely flagged STALE at their normal pace.
DEFAULT_GAP_MULTIPLIER = 4.0            # tolerate ~4 typical gaps of silence
DEFAULT_GAP_FLOOR_SECONDS = 3.0        # active markets never trip faster than old rule
DEFAULT_GAP_CEILING_SECONDS = 30.0     # but never trust a quote longer than this
DEFAULT_WARMUP_THRESHOLD_SECONDS = 10.0  # used until a market's cadence is measured
DEFAULT_MIN_GAP_SAMPLES = 5

# Tier 2 (price-move override), independent of the baseline timer.
DEFAULT_MOVE_THRESHOLD = 0.025         # 2.5% absolute mid-price move
DEFAULT_LAG_WINDOW_SECONDS = 2.0       # "other side hasn't posted in a couple seconds"


def _aware(moment: datetime) -> datetime:
    """Normalize to timezone-aware UTC; the book store stamps aware UTC times."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


# --------------------------------------------------------------------------- #
# Staleness
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StalenessResult:
    stale: bool
    kalshi_stale: bool
    polymarket_stale: bool
    kalshi_age_seconds: float
    polymarket_age_seconds: float
    kalshi_threshold_seconds: float
    polymarket_threshold_seconds: float
    reason: str  # "" | "baseline" | "price_move"


def book_age_seconds(book: Book | None, now: datetime) -> float:
    """Seconds since the book last updated; ``inf`` when there is no book."""
    if book is None:
        return float("inf")
    return (_aware(now) - _aware(book.received_at)).total_seconds()


def _mid_price(book: Book | None) -> float | None:
    if book is None:
        return None
    bid, ask = book.best_bid_price, book.best_ask_price
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return bid if bid is not None else ask


class TwoTierStaleness:
    """Two-tier staleness detector; replaces the flat max-age threshold.

    Feed it books (via :meth:`assess`, which records them, or :meth:`record`
    directly) and it learns each market's update cadence. A pair is flagged
    stale by whichever tier trips first:

    * **Tier 1 — adaptive baseline.** Each venue's allowed silence is
      ``clamp(multiplier * median_gap, floor, ceiling)`` using that venue's own
      recent inter-update gaps. A thin market that normally updates every 9s
      gets a ~30s window; an actively quoted one stays near the floor. Until a
      market has ``min_gap_samples`` gaps, a warm-up threshold is used.
    * **Tier 2 — price-move override.** Independent of Tier 1: if one venue's
      mid has moved more than ``move_threshold`` since the *other* venue's last
      update, and that other venue hasn't posted within ``lag_window_seconds``,
      flag immediately. This is the real risk case — a live move on one side
      against a still quote on the other — and it fires even when the baseline
      timer has plenty of room left.
    """

    def __init__(
        self,
        *,
        gap_multiplier: float = DEFAULT_GAP_MULTIPLIER,
        gap_floor_seconds: float = DEFAULT_GAP_FLOOR_SECONDS,
        gap_ceiling_seconds: float = DEFAULT_GAP_CEILING_SECONDS,
        warmup_threshold_seconds: float = DEFAULT_WARMUP_THRESHOLD_SECONDS,
        min_gap_samples: int = DEFAULT_MIN_GAP_SAMPLES,
        gap_window: int = 128,
        move_threshold: float = DEFAULT_MOVE_THRESHOLD,
        lag_window_seconds: float = DEFAULT_LAG_WINDOW_SECONDS,
        price_window: int = 64,
    ) -> None:
        self.gap_multiplier = gap_multiplier
        self.gap_floor_seconds = gap_floor_seconds
        self.gap_ceiling_seconds = gap_ceiling_seconds
        self.warmup_threshold_seconds = warmup_threshold_seconds
        self.min_gap_samples = min_gap_samples
        self.gap_window = gap_window
        self.move_threshold = move_threshold
        self.lag_window_seconds = lag_window_seconds
        self.price_window = price_window
        self._last_update: dict[str, datetime] = {}
        self._gaps: dict[str, deque[float]] = {}
        self._prices: dict[str, deque[tuple[datetime, float]]] = {}

    def record(self, market_id: str, mid: float | None, at: datetime) -> None:
        """Record one genuine update. Dedupes: only a newer ``at`` counts."""
        at = _aware(at)
        prev = self._last_update.get(market_id)
        if prev is not None and at <= prev:
            return
        if prev is not None:
            self._gaps.setdefault(
                market_id, deque(maxlen=self.gap_window)
            ).append((at - prev).total_seconds())
        self._last_update[market_id] = at
        if mid is not None:
            self._prices.setdefault(
                market_id, deque(maxlen=self.price_window)
            ).append((at, mid))

    def median_gap(self, market_id: str) -> float | None:
        """Median recent inter-update gap, or ``None`` until enough samples."""
        gaps = self._gaps.get(market_id)
        if not gaps or len(gaps) < self.min_gap_samples:
            return None
        return statistics.median(gaps)

    def baseline_threshold(self, market_id: str) -> float:
        """Tier 1 allowed-silence window for this market, in seconds."""
        median = self.median_gap(market_id)
        if median is None:
            return self.warmup_threshold_seconds
        return min(
            self.gap_ceiling_seconds,
            max(self.gap_floor_seconds, self.gap_multiplier * median),
        )

    def _price_as_of(self, market_id: str, moment: datetime) -> float | None:
        history = self._prices.get(market_id)
        if not history:
            return None
        chosen = history[0][1]
        for timestamp, price in history:
            if timestamp <= moment:
                chosen = price
            else:
                break
        return chosen

    def assess(
        self, kalshi: Book | None, polymarket: Book | None, now: datetime
    ) -> StalenessResult:
        now = _aware(now)
        for book in (kalshi, polymarket):
            if book is not None:
                self.record(book.market_id, _mid_price(book), book.received_at)

        k_age = book_age_seconds(kalshi, now)
        p_age = book_age_seconds(polymarket, now)
        k_thr = (
            self.baseline_threshold(kalshi.market_id)
            if kalshi is not None
            else self.warmup_threshold_seconds
        )
        p_thr = (
            self.baseline_threshold(polymarket.market_id)
            if polymarket is not None
            else self.warmup_threshold_seconds
        )
        k_base = k_age > k_thr
        p_base = p_age > p_thr

        # Tier 2: a real move on one side while the other sits still. Measured
        # only when both books exist (a missing book is already Tier-1 stale).
        tier2_side: str | None = None
        if kalshi is not None and polymarket is not None:
            k_mid = _mid_price(kalshi)
            p_mid = _mid_price(polymarket)
            # Kalshi moved while Polymarket has been silent past the lag window.
            if p_age > self.lag_window_seconds and k_mid is not None:
                ref = self._price_as_of(kalshi.market_id, _aware(polymarket.received_at))
                if ref is not None and abs(k_mid - ref) > self.move_threshold:
                    tier2_side = "POLYMARKET"
            # Polymarket moved while Kalshi has been silent past the lag window.
            if tier2_side is None and k_age > self.lag_window_seconds and p_mid is not None:
                ref = self._price_as_of(polymarket.market_id, _aware(kalshi.received_at))
                if ref is not None and abs(p_mid - ref) > self.move_threshold:
                    tier2_side = "KALSHI"

        kalshi_stale = k_base or tier2_side == "KALSHI"
        polymarket_stale = p_base or tier2_side == "POLYMARKET"
        stale = kalshi_stale or polymarket_stale
        reason = (
            "price_move" if tier2_side is not None
            else "baseline" if stale
            else ""
        )
        return StalenessResult(
            stale=stale,
            kalshi_stale=kalshi_stale,
            polymarket_stale=polymarket_stale,
            kalshi_age_seconds=k_age,
            polymarket_age_seconds=p_age,
            kalshi_threshold_seconds=k_thr,
            polymarket_threshold_seconds=p_thr,
            reason=reason,
        )


# --------------------------------------------------------------------------- #
# Persistence / evaporation
# --------------------------------------------------------------------------- #


class OpportunityStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    EVAPORATED = "evaporated"


@dataclass(frozen=True, slots=True)
class OpportunityState:
    key: str
    status: OpportunityStatus
    observations: int
    first_seen: datetime
    last_seen: datetime
    confirmed_at: datetime | None
    # Opaque marker of the market data behind the last *counted* observation
    # (the caller passes the venues' received_at). Lets the tracker tell a
    # genuine new frame from the same snapshot re-seen on a timer.
    last_data_version: object = None

    @property
    def lifetime_seconds(self) -> float:
        return (_aware(self.last_seen) - _aware(self.first_seen)).total_seconds()

    @property
    def is_confirmed(self) -> bool:
        return self.status is OpportunityStatus.CONFIRMED


class PersistenceTracker:
    """Promote opportunities to ``CONFIRMED`` only after they persist.

    Call :meth:`observe` once per book update for each candidate opportunity
    (identified by a stable ``key``, e.g. ``"<market_pair>:<direction>"``),
    passing whether the edge is present this tick. An opportunity is confirmed
    once it has been present for ``min_updates`` consecutive updates *or* for
    ``min_duration_seconds``, whichever threshold is configured and reached
    first. The moment ``present`` is false the state resets — a one-tick flicker
    never reaches confirmation.
    """

    def __init__(
        self,
        *,
        min_updates: int | None = DEFAULT_MIN_UPDATES,
        min_duration_seconds: float | None = None,
    ) -> None:
        if min_updates is None and min_duration_seconds is None:
            raise ValueError(
                "configure at least one of min_updates or min_duration_seconds"
            )
        if min_updates is not None and min_updates < 1:
            raise ValueError(f"min_updates must be >= 1, got {min_updates}")
        self.min_updates = min_updates
        self.min_duration_seconds = min_duration_seconds
        self._states: dict[str, OpportunityState] = {}

    def get(self, key: str) -> OpportunityState | None:
        """Current tracked state for ``key``, or ``None`` if not active."""
        return self._states.get(key)

    @property
    def confirmed_keys(self) -> frozenset[str]:
        return frozenset(
            key
            for key, state in self._states.items()
            if state.status is OpportunityStatus.CONFIRMED
        )

    def observe(
        self,
        key: str,
        present: bool,
        now: datetime,
        *,
        data_version: object = None,
    ) -> OpportunityState | None:
        """Record one update for ``key``.

        Returns the opportunity's current state while it is present (``PENDING``
        or ``CONFIRMED``). When ``present`` is false and the opportunity was
        being tracked, it returns the final ``EVAPORATED`` state once (so the
        caller can log its lifetime) and drops it; otherwise returns ``None``.

        ``data_version`` marks the market data behind this observation (the
        caller passes the venues' latest received_at). When supplied, an
        observation only *counts* toward confirmation if the marker advanced
        since the last counted one: re-seeing the same snapshot on a refresh
        timer holds the state instead of ratcheting it to CONFIRMED, so
        persistence reflects genuine independent market updates rather than the
        clock. Confirmation is thus data-driven; the separate staleness check
        stays time-driven. Absence (``present`` False) still resets immediately,
        and ``data_version=None`` keeps the plain every-call counting behavior.
        """
        now = _aware(now)
        prior = self._states.get(key)

        if not present:
            if prior is None:
                return None
            del self._states[key]
            return replace(prior, status=OpportunityStatus.EVAPORATED)

        # Present, but no fresh data since the last counted observation: hold
        # the current state rather than counting a re-observed snapshot.
        if (
            prior is not None
            and data_version is not None
            and prior.last_data_version == data_version
        ):
            return prior

        if prior is None:
            state = OpportunityState(
                key=key,
                status=OpportunityStatus.PENDING,
                observations=1,
                first_seen=now,
                last_seen=now,
                confirmed_at=None,
                last_data_version=data_version,
            )
        else:
            state = replace(
                prior,
                observations=prior.observations + 1,
                last_seen=now,
                last_data_version=data_version,
            )

        if self._meets_threshold(state, now):
            state = replace(
                state,
                status=OpportunityStatus.CONFIRMED,
                confirmed_at=state.confirmed_at or now,
            )

        self._states[key] = state
        return state

    def _meets_threshold(self, state: OpportunityState, now: datetime) -> bool:
        if self.min_updates is not None and state.observations >= self.min_updates:
            return True
        if self.min_duration_seconds is not None:
            elapsed = (now - _aware(state.first_seen)).total_seconds()
            if elapsed >= self.min_duration_seconds:
                return True
        return False
