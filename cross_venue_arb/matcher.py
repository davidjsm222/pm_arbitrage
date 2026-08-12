"""Two-stage cross-venue market matcher with labeled validation and SQLite cache."""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from dotenv import load_dotenv

from .api import ApiMarket, DirectApiClient


load_dotenv()

DEFAULT_CONFIDENCE_THRESHOLD = 0.72
DEFAULT_RESOLUTION_SIMILARITY_THRESHOLD = 0.68
DEFAULT_MIN_VALIDATION_RECALL = 0.90
DEFAULT_CACHE_PATH = Path("matcher_cache.sqlite3")
DEFAULT_BACKUP_DIRNAME = "backups"
DEFAULT_MIN_TOTAL_VOLUME = 10_000
DEFAULT_MIN_VOLUME_24H = 100
MAX_TOKEN_POSTINGS = 500
MAX_QUERY_TOKENS = 4
MAX_CANDIDATES_PER_MARKET = 10
MAX_ENTITY_PREFILTER_CANDIDATES = 150
MAX_PHRASE_SCORING_CANDIDATES = 25

FILLER_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "be",
        "by",
        "do",
        "does",
        "for",
        "happen",
        "happens",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "whether",
        "who",
        "will",
        "win",
        "wins",
        "winning",
    }
)
TOKEN_RE = re.compile(r"[a-z0-9]+")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
NUMERIC_THRESHOLD_PATTERNS = (
    re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE),
    re.compile(r"\bat\s+least\s+(\d+)\b", re.IGNORECASE),
    re.compile(r"\bover\s+(\d+)\b", re.IGNORECASE),
    re.compile(r"\bunder\s+(\d+)\b", re.IGNORECASE),
    re.compile(r"\bexactly\s+(\d+)\b", re.IGNORECASE),
)
MARGIN_UNIT = (
    r"(?:%|percent(?:age)?(?:\s+points?)?|points?|votes?|goals?|runs?|"
    r"seats?|games?|sets?|strokes?|seconds?|minutes?)"
)
MARGIN_NUMBER = (
    r"\d+(?:\.\d+)?(?:\s*(?:[-–—]|to)\s*\d+(?:\.\d+)?)?"
)
MARGIN_QUALIFIER_RE = re.compile(
    rf"\bby\s+(?:(?:at\s+least|at\s+most|more\s+than|less\s+than|"
    rf"over|under|exactly)\s+{MARGIN_NUMBER}\s*(?:{MARGIN_UNIT})?"
    rf"|{MARGIN_NUMBER}\s*{MARGIN_UNIT})(?=\W|$)",
    re.IGNORECASE,
)
STAGE_NUMBER_RE = re.compile(r"\bstage\s+(\d+)\b", re.IGNORECASE)
CAMPAIGN_ENTRY_RE = re.compile(
    r"\b(?:run|running)\s+for\b|\bannounce(?:s|d|ment|ing)?\s+"
    r"(?:a\s+)?(?:presidential\s+)?campaign\b",
    re.IGNORECASE,
)
NOMINATION_WIN_RE = re.compile(
    r"\bwin(?:s|ning)?\b[^?]*\b(?:nomination|nominee)\b", re.IGNORECASE
)
BALLOT_QUALIFICATION_RE = re.compile(
    r"\b(?:be|appear|get|qualify)\b[^?]*\bon\s+the\s+ballot\b", re.IGNORECASE
)
ELECTION_WIN_RE = re.compile(
    r"\bwin(?:s|ning)?\b[^?]*\b(?:election|presidency)\b", re.IGNORECASE
)
BEFORE_YEAR_RE = re.compile(r"\bbefore\s+((?:19|20)\d{2})\b", re.IGNORECASE)
DISTRICT_CODE_RE = re.compile(r"\b([A-Z]{2})-(\d{1,2})\b", re.IGNORECASE)
ELECTION_PHASE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("first_round", re.compile(r"\b(?:first|1st)[\s-]+round\b", re.IGNORECASE)),
    ("second_round", re.compile(r"\b(?:second|2nd)[\s-]+round\b", re.IGNORECASE)),
    ("runoff", re.compile(r"\brun(?:[\s-]?off)\b", re.IGNORECASE)),
    ("primary", re.compile(r"\bprimar(?:y|ies)\b", re.IGNORECASE)),
)
RESOLUTION_CONDITION_SENTENCE_RE = re.compile(
    r"(?:^|(?<=[.!?])\s+)([^.!?]*\b(?:market|contract)\s+"
    r"(?:will\s+)?resolve(?:s|d)?\b[^.!?]*[.!?]?)",
    re.IGNORECASE | re.DOTALL,
)
ELECTION_CONTEXT_RE = re.compile(
    r"\b(?:election|electoral|president(?:ial)?|senat(?:e|or|orial)|"
    r"house|congress(?:ional)?|governor|gubernatorial|mayor|mayoral|"
    r"democrat(?:ic)?|republican|candidate|ballot|vote|voting|nominee|"
    r"nomination)\b|\b[A-Z]{2}-\d{1,2}\b",
    re.IGNORECASE,
)
ELECTION_NOMINATION_RE = re.compile(r"\b(?:nominee|nomination)\b", re.IGNORECASE)
CONFUSABLE_NAMED_TITLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ballon_dor", re.compile(r"\bballon\s+d[\s'’]*or\b", re.IGNORECASE)),
    ("golden_ball", re.compile(r"\bgolden\s+ball\b", re.IGNORECASE)),
    ("silver_ball", re.compile(r"\bsilver\s+ball\b", re.IGNORECASE)),
    ("bronze_ball", re.compile(r"\bbronze\s+ball\b", re.IGNORECASE)),
    ("golden_boot", re.compile(r"\bgolden\s+boot\b", re.IGNORECASE)),
    (
        "young_player_award",
        re.compile(r"\b(?:best\s+)?young\s+player\s+award\b", re.IGNORECASE),
    ),
    ("golden_glove", re.compile(r"\bgolden\s+glove\b", re.IGNORECASE)),
    ("fair_play_award", re.compile(r"\bfair\s+play\s+award\b", re.IGNORECASE)),
    (
        "the_open_championship",
        re.compile(r"\bthe\s+open\s+championship\b", re.IGNORECASE),
    ),
    ("tour_championship", re.compile(r"\btour\s+championship\b", re.IGNORECASE)),
    ("us_open", re.compile(r"\bu\.?s\.?\s+open\b", re.IGNORECASE)),
    ("masters", re.compile(r"\bmasters(?:\s+tournament)?\b", re.IGNORECASE)),
    ("pga_championship", re.compile(r"\bpga\s+championship\b", re.IGNORECASE)),
    ("ryder_cup", re.compile(r"\bryder\s+cup\b", re.IGNORECASE)),
)
PAIR_CONTEXT_RE = re.compile(r"\b(?:couple|pair|duo)\b", re.IGNORECASE)
MULTI_PERSON_RE = re.compile(r"(?:\band\b|&)", re.IGNORECASE)
RESOLUTION_FILLER_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "at",
        "be",
        "by",
        "do",
        "does",
        "for",
        "happen",
        "happens",
        "in",
        "is",
        "of",
        "on",
        "or",
        "s",
        "the",
        "to",
        "whether",
        "who",
        "will",
    }
)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    tokens = TOKEN_RE.findall(ascii_text.lower())
    return " ".join(token for token in tokens if token not in FILLER_WORDS)


def text_tokens(*values: str | None) -> frozenset[str]:
    return frozenset(
        token
        for value in values
        for token in normalize_text(value).split()
        if token
    )


def _bucket_value(value: str | None) -> str:
    return normalize_text(value) or "__none__"


@dataclass(frozen=True, slots=True)
class MarketRecord:
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
    volume_24h: float | None = None
    event_title: str | None = None
    taker_fee_coefficient: float | None = None

    @classmethod
    def from_api(cls, market: ApiMarket) -> "MarketRecord":
        return cls(
            market_id=market.market_id,
            exchange_name=market.exchange_name,
            name=market.name,
            subtitle=market.subtitle,
            description=market.description,
            category=market.category or "Other",
            subcategory=market.subcategory,
            expiration_datetime=market.expiration_datetime,
            ticker=market.ticker,
            slug=market.slug,
            primary_entity_name=market.primary_entity_name,
            event_ticker=market.event_ticker,
            volume=market.volume,
            volume_24h=market.volume24h,
            event_title=market.event_title,
            taker_fee_coefficient=getattr(market, "taker_fee_coefficient", None),
        )

@dataclass(frozen=True, slots=True)
class CandidatePair:
    kalshi: MarketRecord
    polymarket: MarketRecord
    kalshi_entity: str = ""
    polymarket_entity: str = ""
    kalshi_suffix: str | None = None
    kalshi_resolution_phrase: str = ""
    polymarket_resolution_phrase: str = ""
    resolution_similarity: float = 0.0


@dataclass(frozen=True, slots=True)
class ScoredPair:
    kalshi: MarketRecord
    polymarket: MarketRecord
    confidence: float
    source: str = "independent"
    review_status: str = "unverified"
    resolution_similarity: float = 0.0
    entity_similarity: float = 0.0


@dataclass(frozen=True, slots=True)
class FalsePairExclusion:
    kalshi_market_id: str
    polymarket_market_id: str
    flagged_at: str
    kalshi_name: str
    polymarket_name: str
    confidence: float
    phrase_similarity: float
    entity_similarity: float


@dataclass(frozen=True, slots=True)
class ValidationMiss:
    basket_name: str
    kalshi_market_id: str | None
    expected_polymarket_market_id: str | None
    predicted_polymarket_market_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    labeled_count: int
    stage_a_recovered: int
    top1_correct: int
    predictions_made: int
    misses: tuple[ValidationMiss, ...]

    @property
    def stage_a_recall(self) -> float:
        return self.stage_a_recovered / self.labeled_count if self.labeled_count else 0.0

    @property
    def recall(self) -> float:
        return self.top1_correct / self.labeled_count if self.labeled_count else 0.0

    @property
    def precision(self) -> float:
        return self.top1_correct / self.predictions_made if self.predictions_made else 0.0


def filter_liquid_markets(
    markets: Sequence[MarketRecord],
    *,
    min_total_volume: int = DEFAULT_MIN_TOTAL_VOLUME,
    min_volume_24h: int = DEFAULT_MIN_VOLUME_24H,
) -> list[MarketRecord]:
    return [
        market
        for market in markets
        if (market.volume is None and market.volume_24h is None)
        or (market.volume or 0) >= min_total_volume
        or (market.volume_24h or 0) >= min_volume_24h
    ]


def kalshi_ticker_suffix(market: MarketRecord) -> str | None:
    if not market.ticker:
        return None
    if market.event_ticker:
        prefix = f"{market.event_ticker}-"
        if market.ticker.startswith(prefix):
            return market.ticker[len(prefix) :]
    return market.ticker.rsplit("-", 1)[-1]


def _series_sizes(markets: Sequence[MarketRecord]) -> Counter[str]:
    return Counter(market.event_ticker for market in markets if market.event_ticker)


def extract_kalshi_entity(
    market: MarketRecord, series_sizes: Mapping[str, int]
) -> str:
    """Map an opaque suffix to Kalshi's outcome subtitle for multi-outcome events."""
    is_multi_outcome = bool(
        market.event_ticker and series_sizes.get(market.event_ticker, 0) > 1
    )
    if is_multi_outcome:
        # Kalshi documents yes_sub_title as the outcome label. The native adapter keeps it
        # as subtitle; the ticker suffix is an opaque identifier, not decoded.
        return normalize_text(
            market.subtitle
            or market.primary_entity_name
            or kalshi_ticker_suffix(market)
        )
    return normalize_text(
        market.primary_entity_name or market.subtitle or market.name
    )


def extract_polymarket_entity(market: MarketRecord) -> str:
    if market.primary_entity_name:
        return normalize_text(market.primary_entity_name)
    if market.slug:
        slug = market.slug.lower().strip("-")
        win_match = re.match(r"^will-(.+?)-win(?:-|$)", slug)
        if win_match:
            return normalize_text(win_match.group(1).replace("-", " "))
        normalized_slug = normalize_text(slug.replace("-", " "))
        if normalized_slug:
            return normalized_slug
    return normalize_text(market.name or market.subtitle)


def extract_numeric_thresholds(market: MarketRecord) -> tuple[int, ...]:
    return tuple(
        int(match.group(1))
        for pattern in NUMERIC_THRESHOLD_PATTERNS
        for match in pattern.finditer(market.name)
    )


def extract_confusable_named_titles(market: MarketRecord) -> tuple[str, ...]:
    return tuple(
        title_name
        for title_name, pattern in CONFUSABLE_NAMED_TITLE_PATTERNS
        if pattern.search(market.name)
    )


def has_margin_qualifier(market: MarketRecord) -> bool:
    return bool(MARGIN_QUALIFIER_RE.search(market.name))


def extract_stage_numbers(market: MarketRecord) -> tuple[int, ...]:
    return tuple(int(value) for value in STAGE_NUMBER_RE.findall(market.name))


def campaign_entry_vs_nomination_win(left: MarketRecord, right: MarketRecord) -> bool:
    left_entry = bool(CAMPAIGN_ENTRY_RE.search(left.name))
    right_entry = bool(CAMPAIGN_ENTRY_RE.search(right.name))
    left_win = bool(NOMINATION_WIN_RE.search(left.name))
    right_win = bool(NOMINATION_WIN_RE.search(right.name))
    return (left_entry and not left_win and right_win and not right_entry) or (
        right_entry and not right_win and left_win and not left_entry
    )


def ballot_qualification_vs_election_win(
    left: MarketRecord, right: MarketRecord
) -> bool:
    left_ballot = bool(BALLOT_QUALIFICATION_RE.search(left.name))
    right_ballot = bool(BALLOT_QUALIFICATION_RE.search(right.name))
    left_win = bool(ELECTION_WIN_RE.search(left.name))
    right_win = bool(ELECTION_WIN_RE.search(right.name))
    return (left_ballot and right_win and not right_ballot) or (
        right_ballot and left_win and not left_ballot
    )


def extract_before_years(market: MarketRecord) -> tuple[int, ...]:
    return tuple(int(value) for value in BEFORE_YEAR_RE.findall(market.name))


def extract_district_codes(market: MarketRecord) -> tuple[tuple[str, int], ...]:
    return tuple(
        (state.upper(), int(district))
        for state, district in DISTRICT_CODE_RE.findall(market.name)
    )


def extract_election_phases(market: MarketRecord) -> tuple[str, ...]:
    """Return phases that define the contract, excluding descriptive boilerplate."""
    title_context = " ".join(filter(None, (market.name, market.event_title)))
    phases = {
        phase
        for phase, pattern in ELECTION_PHASE_PATTERNS
        if pattern.search(title_context)
    }
    # Election titles often express a primary contract as winning a party's
    # nomination without using the literal word "primary".
    if (
        ELECTION_NOMINATION_RE.search(title_context)
        and ELECTION_CONTEXT_RE.search(title_context)
    ):
        phases.add("primary")

    # Venue descriptions are blobs rather than structured resolution fields.
    # criteria. Limit description scanning to the sentence that explicitly
    # says how the market resolves, and require election context there. This
    # excludes methodology text such as "the primary resolution source" and
    # calendar exposition about possible rounds/runoffs.
    description = market.description or ""
    resolution_sentences = (
        match.group(1) for match in RESOLUTION_CONDITION_SENTENCE_RE.finditer(description)
    )
    for sentence in resolution_sentences:
        if not ELECTION_CONTEXT_RE.search(sentence):
            continue
        phases.update(
            phase
            for phase, pattern in ELECTION_PHASE_PATTERNS
            if pattern.search(sentence)
        )
    return tuple(phase for phase, _ in ELECTION_PHASE_PATTERNS if phase in phases)


def hard_constraints_compatible(left: MarketRecord, right: MarketRecord) -> bool:
    left_thresholds = extract_numeric_thresholds(left)
    right_thresholds = extract_numeric_thresholds(right)
    if left_thresholds and right_thresholds and left_thresholds != right_thresholds:
        return False

    if has_margin_qualifier(left) != has_margin_qualifier(right):
        return False

    left_stages = extract_stage_numbers(left)
    right_stages = extract_stage_numbers(right)
    if (left_stages or right_stages) and left_stages != right_stages:
        return False

    if campaign_entry_vs_nomination_win(left, right):
        return False

    if ballot_qualification_vs_election_win(left, right):
        return False

    left_before_years = extract_before_years(left)
    right_before_years = extract_before_years(right)
    if left_before_years and right_before_years and left_before_years != right_before_years:
        return False

    left_districts = extract_district_codes(left)
    right_districts = extract_district_codes(right)
    if left_districts and right_districts and left_districts != right_districts:
        return False

    left_election_phases = extract_election_phases(left)
    right_election_phases = extract_election_phases(right)
    if (left_election_phases or right_election_phases) and (
        left_election_phases != right_election_phases
    ):
        return False

    left_titles = frozenset(extract_confusable_named_titles(left))
    right_titles = frozenset(extract_confusable_named_titles(right))
    if left_titles and right_titles and left_titles != right_titles:
        return False
    return True


def _raw_outcome_entity(market: MarketRecord) -> str:
    if market.primary_entity_name or market.subtitle:
        return market.primary_entity_name or market.subtitle or ""
    if market.slug:
        win_match = re.match(r"^will-(.+?)-win(?:-|$)", market.slug, re.IGNORECASE)
        if win_match:
            return win_match.group(1).replace("-", " ")
    return market.name


def needs_manual_rules_check(left: MarketRecord, right: MarketRecord) -> bool:
    """Flag pair/couple outcomes matched against a single named participant."""
    left_entity = _raw_outcome_entity(left)
    right_entity = _raw_outcome_entity(right)
    left_context = " ".join(filter(None, (left.name, left.event_title)))
    right_context = " ".join(filter(None, (right.name, right.event_title)))
    left_is_pair = bool(
        MULTI_PERSON_RE.search(left_entity) and PAIR_CONTEXT_RE.search(left_context)
    )
    right_is_pair = bool(
        MULTI_PERSON_RE.search(right_entity) and PAIR_CONTEXT_RE.search(right_context)
    )
    if left_is_pair == right_is_pair:
        return False
    individual_entity = right_entity if left_is_pair else left_entity
    return bool(individual_entity and not MULTI_PERSON_RE.search(individual_entity))


def extract_resolution_phrase(
    market: MarketRecord, shared_entity_tokens: Iterable[str]
) -> str:
    """Isolate what must happen by removing the shared entity and boilerplate."""
    entity_tokens = set(shared_entity_tokens)
    ascii_text = unicodedata.normalize("NFKD", market.name).encode("ascii", "ignore").decode()
    tokens = TOKEN_RE.findall(ascii_text.lower())
    phrase_tokens = [
        token
        for token in tokens
        if token not in entity_tokens
        and token not in RESOLUTION_FILLER_WORDS
        and not YEAR_RE.fullmatch(token)
    ]
    if phrase_tokens:
        return " ".join(phrase_tokens)

    # Names normally contain the contract, but event_title is the least noisy
    # documented fallback when removing an entity empties a generic name.
    return " ".join(
        token
        for token in TOKEN_RE.findall(
            unicodedata.normalize("NFKD", market.event_title or "")
            .encode("ascii", "ignore")
            .decode()
            .lower()
        )
        if token not in entity_tokens
        and token not in RESOLUTION_FILLER_WORDS
        and not YEAR_RE.fullmatch(token)
    )


@lru_cache(maxsize=250_000)
def resolution_phrase_similarity(left: str, right: str) -> float:
    """Symmetric phrase match that penalizes meaningful extra words on either side."""
    left_tokens = left.split()
    right_tokens = right.split()
    if not left_tokens or not right_tokens:
        return 0.0

    shared = len(set(left_tokens) & set(right_tokens))
    balanced_coverage = min(
        shared / len(set(left_tokens)),
        shared / len(set(right_tokens)),
    )
    ordered = SequenceMatcher(None, left, right).ratio()
    token_sorted = SequenceMatcher(
        None, " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens))
    ).ratio()
    return 0.75 * balanced_coverage + 0.25 * max(ordered, token_sorted)


def _exact_resolution_overlap(left: str, right: str) -> float:
    """Cheap symmetric pre-ranking before the bounded fuzzy phrase comparison."""
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))


def _market_years(market: MarketRecord) -> frozenset[int]:
    values = (
        market.name,
        market.subtitle,
        market.description,
        market.ticker,
        market.slug,
        market.event_ticker,
        market.expiration_datetime,
    )
    return frozenset(
        int(year) for value in values if value for year in YEAR_RE.findall(value)
    )


def loose_date_compatible(left: MarketRecord, right: MarketRecord) -> bool:
    left_years = _market_years(left)
    right_years = _market_years(right)
    if not left_years or not right_years:
        return True
    return any(abs(left - right) <= 1 for left in left_years for right in right_years)


def generate_candidates(
    kalshi_markets: Sequence[MarketRecord],
    polymarket_markets: Sequence[MarketRecord],
) -> list[CandidatePair]:
    """Stage A: compare extracted outcome entities within a loose date window."""
    series_sizes = _series_sizes(kalshi_markets)
    poly_entities = {
        market.market_id: extract_polymarket_entity(market)
        for market in polymarket_markets
    }
    poly_entity_tokens = {
        market_id: frozenset(entity.split())
        for market_id, entity in poly_entities.items()
    }
    poly_by_id = {market.market_id: market for market in polymarket_markets}
    document_frequency = Counter(
        token
        for entity in poly_entities.values()
        for token in entity.split()
    )
    informative = {
        token
        for token, frequency in document_frequency.items()
        if frequency <= MAX_TOKEN_POSTINGS
    }
    inverted: dict[str, set[int]] = defaultdict(set)
    for market_id, entity in poly_entities.items():
        for token in set(entity.split()) & informative:
            inverted[token].add(market_id)

    output: list[CandidatePair] = []
    for kalshi in kalshi_markets:
        kalshi_entity = extract_kalshi_entity(kalshi, series_sizes)
        if not kalshi_entity:
            continue
        kalshi_entity_tokens = frozenset(kalshi_entity.split())
        query_tokens = sorted(
            kalshi_entity_tokens & informative,
            key=lambda token: (document_frequency[token], token),
        )[:MAX_QUERY_TOKENS]
        candidate_ids: set[int] = set()
        for token in query_tokens:
            candidate_ids.update(inverted.get(token, ()))
        # Native venue catalogs are much larger than the former aggregated feed,
        # and Polymarket US currently omits volume from list responses. Bound the
        # rule and fuzzy work with a cheap exact-token rank first.
        entity_prefilter = sorted(
            candidate_ids,
            key=lambda market_id: (
                -len(kalshi_entity_tokens & poly_entity_tokens[market_id])
                / max(
                    len(kalshi_entity_tokens),
                    len(poly_entity_tokens[market_id]),
                    1,
                ),
                abs(
                    len(kalshi_entity_tokens)
                    - len(poly_entity_tokens[market_id])
                ),
                str(market_id),
            ),
        )[:MAX_ENTITY_PREFILTER_CANDIDATES]
        entity_shortlist = [
            market_id
            for market_id in entity_prefilter
            if loose_date_compatible(kalshi, poly_by_id[market_id])
            and hard_constraints_compatible(kalshi, poly_by_id[market_id])
        ]
        candidate_phrases: dict[str, tuple[str, str]] = {}
        for market_id in entity_shortlist:
            shared_entity_tokens = (
                kalshi_entity_tokens & poly_entity_tokens[market_id]
            )
            kalshi_phrase = extract_resolution_phrase(kalshi, shared_entity_tokens)
            polymarket_phrase = extract_resolution_phrase(
                poly_by_id[market_id], shared_entity_tokens
            )
            candidate_phrases[market_id] = (kalshi_phrase, polymarket_phrase)
        phrase_shortlist = sorted(
            entity_shortlist,
            key=lambda market_id: (
                -_exact_resolution_overlap(*candidate_phrases[market_id]),
                -_sequence_similarity(kalshi_entity, poly_entities[market_id]),
                market_id,
            ),
        )[:MAX_PHRASE_SCORING_CANDIDATES]
        candidate_details = {
            market_id: (
                *candidate_phrases[market_id],
                resolution_phrase_similarity(*candidate_phrases[market_id]),
            )
            for market_id in phrase_shortlist
        }
        ranked_ids = sorted(
            phrase_shortlist,
            key=lambda market_id: (
                -candidate_details[market_id][2],
                -_sequence_similarity(kalshi_entity, poly_entities[market_id]),
                market_id,
            ),
        )[:MAX_CANDIDATES_PER_MARKET]
        for market_id in ranked_ids:
            output.append(
                CandidatePair(
                    kalshi=kalshi,
                    polymarket=poly_by_id[market_id],
                    kalshi_entity=kalshi_entity,
                    polymarket_entity=poly_entities[market_id],
                    kalshi_suffix=kalshi_ticker_suffix(kalshi),
                    kalshi_resolution_phrase=candidate_details[market_id][0],
                    polymarket_resolution_phrase=candidate_details[market_id][1],
                    resolution_similarity=candidate_details[market_id][2],
                )
            )
    return output


@lru_cache(maxsize=250_000)
def _sequence_similarity(left: str | None, right: str | None) -> float:
    left_normalized = normalize_text(left)
    right_normalized = normalize_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    direct = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    token_sorted = SequenceMatcher(
        None,
        " ".join(sorted(left_normalized.split())),
        " ".join(sorted(right_normalized.split())),
    ).ratio()
    left_tokens = set(left_normalized.split())
    right_tokens = set(right_normalized.split())
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return 0.65 * max(direct, token_sorted) + 0.35 * jaccard


def _year_signal(left: MarketRecord, right: MarketRecord) -> float:
    left_years = set(YEAR_RE.findall(" ".join(filter(None, (left.name, left.subtitle, left.description)))))
    right_years = set(YEAR_RE.findall(" ".join(filter(None, (right.name, right.subtitle, right.description)))))
    if not left_years or not right_years:
        return 0.0
    return 0.02 if left_years & right_years else -0.05


def score_candidate_signals(
    candidate: CandidatePair,
    *,
    constraints_checked: bool = False,
) -> tuple[float, float, float]:
    """Return confidence, resolution similarity, and entity similarity."""
    kalshi = candidate.kalshi
    polymarket = candidate.polymarket
    kalshi_entity = candidate.kalshi_entity or normalize_text(
        kalshi.primary_entity_name or kalshi.subtitle or kalshi.name
    )
    polymarket_entity = candidate.polymarket_entity or extract_polymarket_entity(
        polymarket
    )
    entity_similarity = _sequence_similarity(kalshi_entity, polymarket_entity)
    if not constraints_checked and not hard_constraints_compatible(kalshi, polymarket):
        return 0.0, 0.0, entity_similarity
    resolution_similarity = candidate.resolution_similarity
    if not candidate.kalshi_resolution_phrase or not candidate.polymarket_resolution_phrase:
        shared_entity_tokens = set(kalshi_entity.split()) & set(
            polymarket_entity.split()
        )
        kalshi_phrase = extract_resolution_phrase(kalshi, shared_entity_tokens)
        polymarket_phrase = extract_resolution_phrase(polymarket, shared_entity_tokens)
        resolution_similarity = resolution_phrase_similarity(
            kalshi_phrase, polymarket_phrase
        )

    # The contract itself is primary. Entity identity can distinguish outcomes
    # only after their resolution phrases describe the same bet.
    score = 0.70 * resolution_similarity + 0.30 * entity_similarity
    confidence = max(0.0, min(0.99, score + _year_signal(kalshi, polymarket)))
    return confidence, resolution_similarity, entity_similarity


def score_candidate(candidate: CandidatePair) -> float:
    return score_candidate_signals(candidate)[0]


def score_candidates(
    candidates: Iterable[CandidatePair],
    *,
    constraints_checked: bool = False,
) -> list[ScoredPair]:
    output: list[ScoredPair] = []
    for candidate in candidates:
        confidence, resolution_similarity, entity_similarity = score_candidate_signals(
            candidate,
            constraints_checked=constraints_checked,
        )
        output.append(
            ScoredPair(
                kalshi=candidate.kalshi,
                polymarket=candidate.polymarket,
                confidence=confidence,
                resolution_similarity=resolution_similarity,
                entity_similarity=entity_similarity,
            )
        )
    return output


def top_candidates_by_kalshi(scored: Iterable[ScoredPair]) -> dict[str, ScoredPair]:
    top: dict[str, ScoredPair] = {}
    for pair in scored:
        current = top.get(pair.kalshi.market_id)
        if (
            current is None
            or pair.confidence > current.confidence
            or (
                pair.confidence == current.confidence
                and pair.polymarket.market_id < current.polymarket.market_id
            )
        ):
            top[pair.kalshi.market_id] = pair
    return top


def validate_against_baskets(
    basket_labels: Sequence[tuple[str, tuple[str, ...]]],
    markets_by_id: Mapping[str, MarketRecord],
    candidates: Sequence[CandidatePair],
    scored: Sequence[ScoredPair],
) -> ValidationReport:
    candidate_ids = {
        (pair.kalshi.market_id, pair.polymarket.market_id) for pair in candidates
    }
    top = top_candidates_by_kalshi(scored)
    stage_a_recovered = 0
    top1_correct = 0
    predictions_made = 0
    misses: list[ValidationMiss] = []

    for basket_name, market_ids in basket_labels:
        members = [markets_by_id.get(market_id) for market_id in market_ids]
        kalshi = next((m for m in members if m and m.exchange_name == "KALSHI"), None)
        polymarket = next(
            (m for m in members if m and m.exchange_name == "POLYMARKET"), None
        )
        if kalshi is None or polymarket is None:
            misses.append(
                ValidationMiss(
                    basket_name,
                    kalshi.market_id if kalshi else None,
                    polymarket.market_id if polymarket else None,
                    None,
                    "one or both labeled members absent from active universe",
                )
            )
            continue

        key = (kalshi.market_id, polymarket.market_id)
        if key in candidate_ids:
            stage_a_recovered += 1
        else:
            misses.append(
                ValidationMiss(
                    basket_name,
                    kalshi.market_id,
                    polymarket.market_id,
                    None,
                    "Stage A did not generate the labeled pair",
                )
            )
            continue

        prediction = top.get(kalshi.market_id)
        if prediction is not None:
            predictions_made += 1
        if prediction and prediction.polymarket.market_id == polymarket.market_id:
            top1_correct += 1
        else:
            misses.append(
                ValidationMiss(
                    basket_name,
                    kalshi.market_id,
                    polymarket.market_id,
                    prediction.polymarket.market_id if prediction else None,
                    "labeled pair was not Stage B's top-scoring candidate",
                )
            )

    return ValidationReport(
        labeled_count=len(basket_labels),
        stage_a_recovered=stage_a_recovered,
        top1_correct=top1_correct,
        predictions_made=predictions_made,
        misses=tuple(misses),
    )


def initialize_cache(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS matched_pairs (
            kalshi_market_id TEXT NOT NULL,
            polymarket_market_id TEXT NOT NULL,
            confidence REAL NOT NULL,
            matched_at TEXT NOT NULL,
            source TEXT NOT NULL,
            review_status TEXT NOT NULL,
            kalshi_name TEXT NOT NULL,
            polymarket_name TEXT NOT NULL,
            resolution_similarity REAL NOT NULL DEFAULT 0,
            entity_similarity REAL NOT NULL DEFAULT 0,
            polymarket_fee_coefficient REAL NOT NULL DEFAULT 0.05,
            PRIMARY KEY (kalshi_market_id, polymarket_market_id)
        )
        """
    )
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(matched_pairs)")
    }
    missing_columns = {
        "resolution_similarity": "REAL NOT NULL DEFAULT 0",
        "entity_similarity": "REAL NOT NULL DEFAULT 0",
        "polymarket_fee_coefficient": "REAL NOT NULL DEFAULT 0.05",
    }
    for column, definition in missing_columns.items():
        if column not in existing_columns:
            connection.execute(
                f"ALTER TABLE matched_pairs ADD COLUMN {column} {definition}"
            )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS false_pair_exclusions (
            kalshi_market_id TEXT NOT NULL,
            polymarket_market_id TEXT NOT NULL,
            flagged_at TEXT NOT NULL,
            kalshi_name TEXT NOT NULL,
            polymarket_name TEXT NOT NULL,
            confidence REAL NOT NULL,
            phrase_similarity REAL NOT NULL,
            entity_similarity REAL NOT NULL,
            PRIMARY KEY (kalshi_market_id, polymarket_market_id)
        )
        """
    )
    return connection


def list_false_pair_exclusions(path: Path) -> tuple[FalsePairExclusion, ...]:
    if not path.exists():
        return ()
    with closing(initialize_cache(path)) as connection:
        with connection:
            rows = connection.execute(
                """
                SELECT kalshi_market_id, polymarket_market_id, flagged_at, kalshi_name,
                       polymarket_name, confidence, phrase_similarity,
                       entity_similarity
                FROM false_pair_exclusions
                ORDER BY flagged_at, kalshi_market_id, polymarket_market_id
                """
            ).fetchall()
    return tuple(FalsePairExclusion(*row) for row in rows)


def _markdown_text(value: str) -> str:
    return " ".join(value.split())


def flag_false_pair(
    path: Path,
    *,
    kalshi_market_id: str,
    polymarket_market_id: str,
    kalshi_name: str,
    polymarket_name: str,
    confidence: float,
    phrase_similarity: float,
    entity_similarity: float,
    false_pairs_path: Path = Path("false_pairs.md"),
    flagged_at: datetime | None = None,
) -> bool:
    """Persist an exclusion, remove its live row, and append its review note."""
    timestamp = (flagged_at or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    ).isoformat()
    with closing(initialize_cache(path)) as connection:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO false_pair_exclusions (
                    kalshi_market_id, polymarket_market_id, flagged_at, kalshi_name,
                    polymarket_name, confidence, phrase_similarity,
                    entity_similarity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kalshi_market_id, polymarket_market_id) DO NOTHING
                """,
                (
                    kalshi_market_id,
                    polymarket_market_id,
                    timestamp,
                    kalshi_name,
                    polymarket_name,
                    confidence,
                    phrase_similarity,
                    entity_similarity,
                ),
            )
            connection.execute(
                "DELETE FROM matched_pairs WHERE kalshi_market_id = ? AND polymarket_market_id = ?",
                (kalshi_market_id, polymarket_market_id),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                false_pairs_path.parent.mkdir(parents=True, exist_ok=True)
                with false_pairs_path.open("a", encoding="utf-8") as review_file:
                    review_file.write(
                        f"## {timestamp}\n\n"
                        f"- Kalshi: {_markdown_text(kalshi_name)} "
                        f"(market_id `{kalshi_market_id}`)\n"
                        f"- Polymarket: {_markdown_text(polymarket_name)} "
                        f"(market_id `{polymarket_market_id}`)\n"
                        f"- Scores: confidence `{confidence:.3f}`, "
                        f"phrase `{phrase_similarity:.3f}`, "
                        f"entity `{entity_similarity:.3f}`\n"
                        "- Reason:\n\n"
                    )
    return inserted


def unflag_false_pair(path: Path, kalshi_market_id: str, polymarket_market_id: str) -> bool:
    if not path.exists():
        return False
    with closing(initialize_cache(path)) as connection:
        with connection:
            cursor = connection.execute(
                "DELETE FROM false_pair_exclusions "
                "WHERE kalshi_market_id = ? AND polymarket_market_id = ?",
                (kalshi_market_id, polymarket_market_id),
            )
    return cursor.rowcount > 0


def backup_cache_snapshot(
    path: Path,
    *,
    backup_dir: Path | None = None,
    now: datetime | None = None,
) -> Path | None:
    """Create a timestamped, transactionally consistent copy before rebuilding."""
    if not path.exists():
        return None
    backup_dir = backup_dir or path.parent / DEFAULT_BACKUP_DIRNAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"{path.stem}_{stamp}{path.suffix}"
    source_uri = f"file:{path.resolve()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
    return destination


def cache_matches(path: Path, matches: Sequence[ScoredPair]) -> None:
    matched_at = datetime.now(timezone.utc).isoformat()
    with closing(initialize_cache(path)) as connection:
        with connection:
            excluded_keys = {
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT kalshi_market_id, polymarket_market_id FROM false_pair_exclusions"
                )
            }
            # Each matcher run is a complete snapshot. Delete first so pairs that
            # disappear from the new funnel cannot survive as stale cache rows.
            connection.execute("DELETE FROM matched_pairs")
            connection.executemany(
                """
                INSERT INTO matched_pairs (
                    kalshi_market_id, polymarket_market_id, confidence, matched_at, source,
                    review_status, kalshi_name, polymarket_name,
                    resolution_similarity, entity_similarity,
                    polymarket_fee_coefficient
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kalshi_market_id, polymarket_market_id) DO UPDATE SET
                    confidence=excluded.confidence,
                    matched_at=excluded.matched_at,
                    source=excluded.source,
                    review_status=excluded.review_status,
                    kalshi_name=excluded.kalshi_name,
                    polymarket_name=excluded.polymarket_name,
                    resolution_similarity=excluded.resolution_similarity,
                    entity_similarity=excluded.entity_similarity,
                    polymarket_fee_coefficient=excluded.polymarket_fee_coefficient
                """,
                [
                    (
                        match.kalshi.market_id,
                        match.polymarket.market_id,
                        match.confidence,
                        matched_at,
                        match.source,
                        match.review_status,
                        match.kalshi.name,
                        match.polymarket.name,
                        match.resolution_similarity,
                        match.entity_similarity,
                        match.polymarket.taker_fee_coefficient or 0.05,
                    )
                    for match in matches
                    if (
                        str(match.kalshi.market_id),
                        str(match.polymarket.market_id),
                    )
                    not in excluded_keys
                ],
            )


async def _close_client(client: DirectApiClient) -> None:
    await client.close()


def _market_event_key(market: Any) -> tuple[str, str]:
    """Return the best native event identity available for diagnostics."""
    exchange = str(market.exchange_name)
    event_ticker = getattr(market, "event_ticker", None)
    if event_ticker:
        return exchange, f"ticker:{event_ticker}"
    neg_risk_id = getattr(market, "neg_risk_id", None)
    if neg_risk_id:
        return exchange, f"neg_risk:{neg_risk_id}"
    event_title = getattr(market, "event_title", None)
    if event_title:
        expiration = getattr(market, "expiration_datetime", None) or ""
        return exchange, f"title:{event_title}|expiration:{expiration}"
    return exchange, f"market:{market.market_id}"


async def fetch_active_markets(
    client: DirectApiClient, exchange_name: str
) -> list[MarketRecord]:
    native_markets = await client.list_active_markets(exchange_name)
    records = {
        market.market_id: MarketRecord.from_api(market) for market in native_markets
    }
    print(
        f"DISCOVERY_COVERAGE venue={exchange_name} status=OK "
        f"raw_market_rows={len(native_markets)} unique_market_ids={len(records)} "
        f"duplicate_rows={len(native_markets) - len(records)}",
        flush=True,
    )
    return list(records.values())


async def run_matcher(
    *,
    cache_path: Path,
    confidence_threshold: float,
    resolution_similarity_threshold: float,
    min_total_volume: int,
    min_volume_24h: int,
) -> int:
    """Discover natively, match independently, and replace the local cache."""
    backup_path = backup_cache_snapshot(cache_path)
    if backup_path is not None:
        print(f"BACKUP path={backup_path}", flush=True)
    client = DirectApiClient()
    try:
        kalshi, polymarket = await asyncio.gather(
            fetch_active_markets(client, "KALSHI"),
            fetch_active_markets(client, "POLYMARKET"),
        )
    finally:
        await _close_client(client)

    liquid_kalshi = filter_liquid_markets(
        kalshi,
        min_total_volume=min_total_volume,
        min_volume_24h=min_volume_24h,
    )
    liquid_polymarket = filter_liquid_markets(
        polymarket,
        min_total_volume=min_total_volume,
        min_volume_24h=min_volume_24h,
    )
    print(
        f"  active_kalshi={len(kalshi)} active_polymarket={len(polymarket)}",
        flush=True,
    )
    print(
        f"  liquidity_floor=volume>={min_total_volume} OR "
        f"volume_24h>={min_volume_24h} (tunable)",
        flush=True,
    )
    print(
        f"  liquid_kalshi={len(liquid_kalshi)} "
        f"shrink={1 - len(liquid_kalshi) / len(kalshi):.2%} "
        f"liquid_polymarket={len(liquid_polymarket)} "
        f"shrink={1 - len(liquid_polymarket) / len(polymarket):.2%}",
        flush=True,
    )
    print(
        f"  liquid_cross_product={len(liquid_kalshi) * len(liquid_polymarket)}",
        flush=True,
    )

    candidates = generate_candidates(liquid_kalshi, liquid_polymarket)
    print(f"FULL MATCHING stage_a_candidates={len(candidates)}", flush=True)
    scored = score_candidates(candidates, constraints_checked=True)

    selected_by_key: dict[tuple[str, str], ScoredPair] = {}
    for pair in top_candidates_by_kalshi(scored).values():
        manual_rules_check = needs_manual_rules_check(pair.kalshi, pair.polymarket)
        selected_by_key[(pair.kalshi.market_id, pair.polymarket.market_id)] = ScoredPair(
            kalshi=pair.kalshi,
            polymarket=pair.polymarket,
            confidence=pair.confidence,
            source="independent",
            review_status=(
                "needs_manual_rules_check"
                if manual_rules_check
                else "high_confidence"
                if (
                    pair.confidence >= confidence_threshold
                    and pair.resolution_similarity
                    >= resolution_similarity_threshold
                )
                else "needs_review"
            ),
            resolution_similarity=pair.resolution_similarity,
            entity_similarity=pair.entity_similarity,
        )

    excluded_keys = {
        (item.kalshi_market_id, item.polymarket_market_id)
        for item in list_false_pair_exclusions(cache_path)
    }
    selected = [
        match
        for key, match in selected_by_key.items()
        if key not in excluded_keys
    ]
    excluded_count = len(selected_by_key) - len(selected)

    cache_matches(cache_path, selected)
    high = sum(match.review_status == "high_confidence" for match in selected)
    review = sum(match.review_status == "needs_review" for match in selected)
    manual = sum(
        match.review_status == "needs_manual_rules_check" for match in selected
    )
    print(
        f"CACHE path={cache_path} rows={len(selected)} high_confidence={high} "
        f"needs_review={review} needs_manual_rules_check={manual} "
        f"excluded_pairs={excluded_count} "
        f"threshold={confidence_threshold:.2f} "
        f"resolution_threshold={resolution_similarity_threshold:.2f} (tunable)"
    )
    for match in sorted(selected, key=lambda item: item.confidence, reverse=True)[:20]:
        print(
            f"  K={match.kalshi.market_id} P={match.polymarket.market_id} "
            f"confidence={match.confidence:.3f} source={match.source} "
            f"resolution={match.resolution_similarity:.3f} "
            f"entity={match.entity_similarity:.3f} "
            f"status={match.review_status} K_name={match.kalshi.name!r} "
            f"P_name={match.polymarket.name!r}"
        )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH)
    exclusion_actions = parser.add_mutually_exclusive_group()
    exclusion_actions.add_argument(
        "--list-exclusions",
        action="store_true",
        help="print persistent false-pair exclusions and exit",
    )
    exclusion_actions.add_argument(
        "--unflag",
        nargs=2,
        metavar=("KALSHI_ID", "POLYMARKET_ID"),
        help="remove a false-pair exclusion; the pair may return next rebuild",
    )
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD,
        help="independent-match confidence threshold (default: 0.72, tunable)",
    )
    parser.add_argument(
        "--resolution-threshold",
        type=float,
        default=DEFAULT_RESOLUTION_SIMILARITY_THRESHOLD,
        help="required resolution-phrase similarity for independent high confidence "
        "(default: 0.68, tunable)",
    )
    parser.add_argument(
        "--min-total-volume",
        type=int,
        default=DEFAULT_MIN_TOTAL_VOLUME,
        help="tunable cumulative-volume floor (default: 10000)",
    )
    parser.add_argument(
        "--min-volume-24h",
        type=int,
        default=DEFAULT_MIN_VOLUME_24H,
        help="tunable 24-hour-volume floor used with OR (default: 100)",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.list_exclusions:
        exclusions = list_false_pair_exclusions(args.cache)
        if not exclusions:
            print("No false-pair exclusions.")
        for item in exclusions:
            print(
                f"K={item.kalshi_market_id} P={item.polymarket_market_id} "
                f"flagged_at={item.flagged_at} confidence={item.confidence:.3f} "
                f"phrase={item.phrase_similarity:.3f} "
                f"entity={item.entity_similarity:.3f} "
                f"K_name={item.kalshi_name!r} P_name={item.polymarket_name!r}"
            )
        return
    if args.unflag:
        kalshi_market_id, polymarket_market_id = args.unflag
        removed = unflag_false_pair(args.cache, kalshi_market_id, polymarket_market_id)
        print(
            f"{'UNFLAGGED' if removed else 'NOT_FOUND'} "
            f"K={kalshi_market_id} P={polymarket_market_id}"
        )
        return
    raise SystemExit(
        asyncio.run(
            run_matcher(
                cache_path=args.cache,
                confidence_threshold=args.threshold,
                resolution_similarity_threshold=args.resolution_threshold,
                min_total_volume=args.min_total_volume,
                min_volume_24h=args.min_volume_24h,
            )
        )
    )


if __name__ == "__main__":
    main()
