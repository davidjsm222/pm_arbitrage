from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from cross_venue_arb.matcher import (
    CandidatePair,
    DEFAULT_RESOLUTION_SIMILARITY_THRESHOLD,
    MAX_CANDIDATES_PER_MARKET,
    MarketRecord,
    ScoredPair,
    ballot_qualification_vs_election_win,
    backup_cache_snapshot,
    cache_matches,
    campaign_entry_vs_nomination_win,
    extract_kalshi_entity,
    extract_confusable_named_titles,
    extract_before_years,
    extract_district_codes,
    extract_election_phases,
    extract_numeric_thresholds,
    extract_polymarket_entity,
    extract_resolution_phrase,
    extract_stage_numbers,
    fetch_active_markets,
    flag_false_pair,
    filter_liquid_markets,
    generate_candidates,
    hard_constraints_compatible,
    has_margin_qualifier,
    list_false_pair_exclusions,
    kalshi_ticker_suffix,
    normalize_text,
    needs_manual_rules_check,
    resolution_phrase_similarity,
    score_candidate,
    score_candidate_signals,
    unflag_false_pair,
)


def market(
    market_id: int,
    exchange: str,
    name: str,
    *,
    entity: str | None = None,
    slug: str | None = None,
    category: str = "Sports",
    subcategory: str | None = "Basketball",
    subtitle: str | None = None,
    ticker: str | None = None,
    event_ticker: str | None = None,
    event_title: str | None = None,
    description: str | None = None,
    volume: int | None = 20_000,
    volume_24h: int | None = 0,
    taker_fee_coefficient: float | None = None,
) -> MarketRecord:
    return MarketRecord(
        market_id=market_id,
        exchange_name=exchange,
        name=name,
        subtitle=subtitle,
        description=description,
        category=category,
        subcategory=subcategory,
        expiration_datetime=None,
        ticker=ticker,
        slug=slug,
        primary_entity_name=entity,
        event_ticker=event_ticker,
        event_title=event_title,
        volume=volume,
        volume_24h=volume_24h,
        taker_fee_coefficient=taker_fee_coefficient,
    )


class MatcherTests(unittest.TestCase):
    def test_normalization_strips_filler_words(self):
        self.assertEqual(
            normalize_text("Will the Philadelphia 76ers win?"),
            "philadelphia 76ers",
        )

    def test_stage_a_uses_bounded_entity_inverted_index(self):
        kalshi = [
            market(1, "KALSHI", "Will Philadelphia win the 2026 finals?", entity="Philadelphia"),
            market(2, "KALSHI", "Will Boston win the 2026 finals?", entity="Boston"),
        ]
        polymarket = [
            market(10, "POLYMARKET", "Will the Philadelphia 76ers win the 2026 NBA Finals?", slug="philadelphia-76ers-2026"),
            market(20, "POLYMARKET", "Will the Boston Celtics win the 2026 NBA Finals?", slug="boston-celtics-2026"),
            market(30, "POLYMARKET", "Will the Lakers win the 2026 NBA Finals?", slug="lakers-2026"),
        ]
        polymarket.extend(
            market(
                100 + index,
                "POLYMARKET",
                f"Will Team {index} win the 2026 NBA Finals?",
                slug=f"team-{index}-2026",
            )
            for index in range(12)
        )

        candidates = generate_candidates(kalshi, polymarket)
        ids = {(pair.kalshi.market_id, pair.polymarket.market_id) for pair in candidates}

        self.assertIn((1, 10), ids)
        self.assertIn((2, 20), ids)
        self.assertLessEqual(
            len(candidates), len(kalshi) * MAX_CANDIDATES_PER_MARKET
        )
        self.assertLess(len(candidates), len(kalshi) * len(polymarket))

    def test_stage_b_scores_correct_entity_above_wrong_entity(self):
        kalshi = market(
            1,
            "KALSHI",
            "Will Philadelphia win the 2026 Pro Basketball Finals?",
            entity="Philadelphia 76ers",
        )
        correct = market(
            10,
            "POLYMARKET",
            "Will the Philadelphia 76ers win the 2026 NBA Finals?",
            slug="philadelphia-76ers-2026-nba-finals",
        )
        wrong = market(
            20,
            "POLYMARKET",
            "Will the Boston Celtics win the 2026 NBA Finals?",
            slug="boston-celtics-2026-nba-finals",
        )

        self.assertGreater(
            score_candidate(CandidatePair(kalshi, correct)),
            score_candidate(CandidatePair(kalshi, wrong)),
        )

    def test_multi_outcome_kalshi_suffix_maps_to_documented_subtitle(self):
        abbott = market(
            557,
            "KALSHI",
            "Who will win the next presidential election?",
            subtitle="Greg Abbott",
            ticker="KXPRESPERSON-28-GABB",
            event_ticker="KXPRESPERSON-28",
        )

        self.assertEqual(kalshi_ticker_suffix(abbott), "GABB")
        self.assertEqual(
            extract_kalshi_entity(abbott, {"KXPRESPERSON-28": 30}),
            "greg abbott",
        )

    def test_polymarket_entity_comes_from_per_outcome_slug(self):
        abbott = market(
            691485,
            "POLYMARKET",
            "Will Greg Abbott win the 2028 US Presidential Election?",
            slug="will-greg-abbott-win-the-2028-us-presidential-election",
        )

        self.assertEqual(extract_polymarket_entity(abbott), "greg abbott")

    def test_resolution_phrase_separates_lindor_all_star_from_mvp(self):
        all_star = market(
            6965622,
            "KALSHI",
            "Will Francisco Lindor be selected to the 2026 NL All-Star Team?",
            entity="Francisco Lindor",
            subtitle="Francisco Lindor",
            ticker="KXMLBALLSTAR-26NL-FLINDOR12",
            event_ticker="KXMLBALLSTAR-26NL",
        )
        mvp = market(
            722805,
            "POLYMARKET",
            "Will Francisco Lindor win the 2026 National League MVP Award?",
            slug="will-francisco-lindor-win-the-2026-national-league-mvp-award",
        )

        shared = {"francisco", "lindor"}
        all_star_phrase = extract_resolution_phrase(all_star, shared)
        mvp_phrase = extract_resolution_phrase(mvp, shared)
        similarity = resolution_phrase_similarity(all_star_phrase, mvp_phrase)

        self.assertEqual(all_star_phrase, "selected nl all star team")
        self.assertEqual(mvp_phrase, "win national league mvp award")
        self.assertLess(similarity, DEFAULT_RESOLUTION_SIMILARITY_THRESHOLD)

    def test_different_numeric_thresholds_are_hard_rejected(self):
        top_10 = market(
            1,
            "KALSHI",
            "Genesis Scottish Open: Will Chris Gotterup finish top 10?",
            entity="Chris Gotterup",
        )
        top_20 = market(
            2,
            "POLYMARKET",
            "Will Chris Gotterup finish in the Top 20 at the 2026 Genesis Scottish Open?",
            slug="will-chris-gotterup-finish-in-the-top-20-at-the-2026-genesis-scottish-open",
        )

        self.assertEqual(extract_numeric_thresholds(top_10), (10,))
        self.assertEqual(extract_numeric_thresholds(top_20), (20,))
        self.assertFalse(hard_constraints_compatible(top_10, top_20))
        self.assertEqual(generate_candidates([top_10], [top_20]), [])
        self.assertEqual(score_candidate(CandidatePair(top_10, top_20)), 0.0)

    def test_equal_numeric_thresholds_remain_compatible(self):
        left = market(1, "KALSHI", "Will Chris Gotterup finish top 10?")
        right = market(2, "POLYMARKET", "Will Chris Gotterup finish in the Top 10?")

        self.assertTrue(hard_constraints_compatible(left, right))

    def test_all_requested_numeric_threshold_phrases_are_extracted(self):
        examples = {
            "Will A finish top 12?": (12,),
            "Will A score at least 3 goals?": (3,),
            "Will A score over 4 goals?": (4,),
            "Will A score under 2 goals?": (2,),
            "Will A score exactly 1 goal?": (1,),
        }
        for index, (name, expected) in enumerate(examples.items()):
            with self.subTest(name=name):
                self.assertEqual(
                    extract_numeric_thresholds(market(index, "KALSHI", name)),
                    expected,
                )

    def test_one_sided_margin_qualifier_is_hard_rejected(self):
        no_margin = market(
            4576,
            "KALSHI",
            "Will Flávio Bolsonaro win the first round of the 2026 Brazilian presidential election?",
            entity="Flávio Bolsonaro",
        )
        margin = market(
            722039,
            "POLYMARKET",
            "Will Flávio Bolsonaro win the first round of the 2026 Brazilian presidential election by at least 10%?",
            slug="will-flavio-bolsonaro-win-the-first-round-by-at-least-10-percent",
        )

        self.assertFalse(has_margin_qualifier(no_margin))
        self.assertTrue(has_margin_qualifier(margin))
        self.assertFalse(hard_constraints_compatible(no_margin, margin))
        self.assertEqual(generate_candidates([no_margin], [margin]), [])
        self.assertEqual(score_candidate(CandidatePair(no_margin, margin)), 0.0)

    def test_margin_qualifier_forms_are_detected(self):
        examples = (
            "Will A win by at least 10%?",
            "Will A win by more than 5 points?",
            "Will A win by under 3 goals?",
            "Will A win by exactly 2 seats?",
            "Will A win by 7 votes?",
            "Will A win by 5–10%?",
            "Will A win by 3 to 6 points?",
        )
        for index, name in enumerate(examples):
            with self.subTest(name=name):
                self.assertTrue(
                    has_margin_qualifier(market(index, "KALSHI", name))
                )

    def test_both_sides_without_margin_remain_compatible(self):
        left = market(1, "KALSHI", "Will A win the election?")
        right = market(2, "POLYMARKET", "Will A win the election?")

        self.assertTrue(hard_constraints_compatible(left, right))

    def test_stage_winner_is_not_overall_tour_winner(self):
        stage = market(
            8692061,
            "KALSHI",
            "Will Tadej Pogacar win Stage 10 in the 2026 Tour de France?",
            entity="Tadej Pogacar",
        )
        overall = market(
            8637099,
            "POLYMARKET",
            "Will Tadej Pogačar win the 2026 Tour De France?",
            slug="will-tadej-pogacar-win-the-2026-tour-de-france",
        )

        self.assertEqual(extract_stage_numbers(stage), (10,))
        self.assertEqual(extract_stage_numbers(overall), ())
        self.assertFalse(hard_constraints_compatible(stage, overall))
        self.assertEqual(generate_candidates([stage], [overall]), [])
        self.assertEqual(score_candidate(CandidatePair(stage, overall)), 0.0)

    def test_campaign_entry_is_not_nomination_win(self):
        entry = market(
            2065,
            "KALSHI",
            "Who will run for the Democratic presidential nomination in 2028?",
            entity="Mark Cuban",
        )
        winner = market(
            691286,
            "POLYMARKET",
            "Will Mark Cuban win the 2028 Democratic presidential nomination?",
            slug="will-mark-cuban-win-the-2028-democratic-presidential-nomination",
        )

        self.assertTrue(campaign_entry_vs_nomination_win(entry, winner))
        self.assertFalse(hard_constraints_compatible(entry, winner))
        self.assertEqual(generate_candidates([entry], [winner]), [])
        self.assertEqual(score_candidate(CandidatePair(entry, winner)), 0.0)

    def test_ballot_qualification_is_not_election_win(self):
        ballot = market(
            1,
            "KALSHI",
            "Will Luiz Inácio Lula da Silva be on the ballot in the next Brazilian presidential election?",
            entity="Luiz Inácio Lula da Silva",
        )
        winner = market(
            2,
            "POLYMARKET",
            "Will Luiz Inácio Lula da Silva win the 2026 Brazilian presidential election?",
            slug="will-luiz-inacio-lula-da-silva-win-the-2026-brazilian-presidential-election",
        )

        self.assertTrue(ballot_qualification_vs_election_win(ballot, winner))
        self.assertFalse(hard_constraints_compatible(ballot, winner))
        self.assertEqual(generate_candidates([ballot], [winner]), [])
        self.assertEqual(score_candidate(CandidatePair(ballot, winner)), 0.0)

    def test_different_explicit_before_years_are_hard_rejected(self):
        left = market(1, "KALSHI", "Musk out as Tesla CEO before 2026?")
        right = market(2, "POLYMARKET", "Musk out as Tesla CEO before 2027?")

        self.assertEqual(extract_before_years(left), (2026,))
        self.assertEqual(extract_before_years(right), (2027,))
        self.assertFalse(hard_constraints_compatible(left, right))

    def test_different_district_codes_are_hard_rejected(self):
        left = market(1, "KALSHI", "Will Democratic win the House race for WI-06?")
        right = market(
            2, "POLYMARKET", "Will the Democratic Party win the OR-06 House seat?"
        )

        self.assertEqual(extract_district_codes(left), (("WI", 6),))
        self.assertEqual(extract_district_codes(right), (("OR", 6),))
        self.assertFalse(hard_constraints_compatible(left, right))

    def test_equivalent_district_code_zero_padding_is_compatible(self):
        left = market(1, "KALSHI", "Will A win AZ-1?")
        right = market(2, "POLYMARKET", "Will A win AZ-01?")

        self.assertEqual(extract_district_codes(left), (("AZ", 1),))
        self.assertEqual(extract_district_codes(right), (("AZ", 1),))
        self.assertTrue(hard_constraints_compatible(left, right))

    def test_one_sided_election_phase_is_hard_rejected(self):
        first_round = market(
            1,
            "KALSHI",
            "Will Luiz Inácio Lula da Silva win the first round of the 2026 "
            "Brazilian presidential election?",
            entity="Luiz Inácio Lula da Silva",
        )
        overall = market(
            2,
            "POLYMARKET",
            "Will Luiz Inácio Lula da Silva win the 2026 Brazilian presidential "
            "election?",
            slug="will-luiz-inacio-lula-da-silva-win-the-2026-brazilian-presidential-election",
        )

        self.assertEqual(extract_election_phases(first_round), ("first_round",))
        self.assertEqual(extract_election_phases(overall), ())
        self.assertFalse(hard_constraints_compatible(first_round, overall))
        self.assertEqual(generate_candidates([first_round], [overall]), [])
        self.assertEqual(score_candidate(CandidatePair(first_round, overall)), 0.0)

    def test_different_election_phases_are_hard_rejected(self):
        second_round = market(1, "KALSHI", "Will A win the second round?")
        runoff = market(2, "POLYMARKET", "Will A win the runoff?")

        self.assertEqual(extract_election_phases(second_round), ("second_round",))
        self.assertEqual(extract_election_phases(runoff), ("runoff",))
        self.assertFalse(hard_constraints_compatible(second_round, runoff))

    def test_matching_election_phases_remain_compatible(self):
        left = market(1, "KALSHI", "Will A win the primary?")
        right = market(2, "POLYMARKET", "Will A win the primary election?")

        self.assertEqual(extract_election_phases(left), ("primary",))
        self.assertEqual(extract_election_phases(right), ("primary",))
        self.assertTrue(hard_constraints_compatible(left, right))

    def test_election_phase_in_description_is_enforced(self):
        qualified = market(
            1,
            "KALSHI",
            "Will A win the election?",
            description="This market resolves on the first-round election result.",
        )
        overall = market(2, "POLYMARKET", "Will A win the election?")

        self.assertEqual(extract_election_phases(qualified), ("first_round",))
        self.assertFalse(hard_constraints_compatible(qualified, overall))

    def test_phase_scan_ignores_resolution_source_boilerplate(self):
        market_with_boilerplate = market(
            1,
            "POLYMARKET",
            "Will Gukesh win the World Chess Championship?",
            description=(
                "This market resolves to Yes if Gukesh wins the championship. "
                "The primary resolution source will be official FIDE results."
            ),
        )

        self.assertEqual(extract_election_phases(market_with_boilerplate), ())

    def test_phase_scan_ignores_explanatory_election_mechanics(self):
        general_election = market(
            1,
            "POLYMARKET",
            "Will A win the presidential election?",
            description=(
                "A candidate needs a majority in the first round. If nobody "
                "does, the top two advance to a runoff. This market includes "
                "any potential second round. This market resolves to Yes if A "
                "wins the presidential election."
            ),
        )

        self.assertEqual(extract_election_phases(general_election), ())

    def test_election_nomination_title_is_primary_semantics(self):
        nominee = market(
            1,
            "KALSHI",
            "Will A be the Democratic nominee for Senate?",
        )
        explicit_primary = market(
            2,
            "POLYMARKET",
            "Will A win the Democratic primary for Senate?",
        )

        self.assertEqual(extract_election_phases(nominee), ("primary",))
        self.assertEqual(extract_election_phases(explicit_primary), ("primary",))
        self.assertTrue(hard_constraints_compatible(nominee, explicit_primary))

    def test_confusable_different_awards_are_hard_rejected(self):
        golden_ball = market(
            1, "KALSHI", "Will Ousmane Dembele win the Golden Ball?",
            entity="Ousmane Dembele",
        )
        ballon_dor = market(
            2, "POLYMARKET", "Will Ousmane Dembélé win the 2026 Ballon d'Or?",
            slug="will-ousmane-dembele-win-the-2026-ballon-dor",
        )

        self.assertEqual(extract_confusable_named_titles(golden_ball), ("golden_ball",))
        self.assertEqual(extract_confusable_named_titles(ballon_dor), ("ballon_dor",))
        self.assertFalse(hard_constraints_compatible(golden_ball, ballon_dor))
        self.assertEqual(generate_candidates([golden_ball], [ballon_dor]), [])
        self.assertEqual(score_candidate(CandidatePair(golden_ball, ballon_dor)), 0.0)

    def test_best_young_player_and_young_player_are_same_award(self):
        best = market(1, "KALSHI", "Will A win the Best Young Player Award?")
        plain = market(2, "POLYMARKET", "Will A win the Young Player Award?")

        self.assertEqual(
            extract_confusable_named_titles(best), ("young_player_award",)
        )
        self.assertEqual(
            extract_confusable_named_titles(plain), ("young_player_award",)
        )
        self.assertTrue(hard_constraints_compatible(best, plain))

    def test_full_confusable_award_list_is_extracted(self):
        examples = {
            "Will A win the Golden Ball?": "golden_ball",
            "Will A win the Silver Ball?": "silver_ball",
            "Will A win the Bronze Ball?": "bronze_ball",
            "Will A win the Golden Boot?": "golden_boot",
            "Will A win the Ballon d’Or?": "ballon_dor",
            "Will A win the Young Player Award?": "young_player_award",
            "Will A win the Golden Glove?": "golden_glove",
            "Will A win the Fair Play Award?": "fair_play_award",
        }
        for index, (name, expected) in enumerate(examples.items()):
            with self.subTest(name=name):
                self.assertEqual(
                    extract_confusable_named_titles(market(index, "KALSHI", name)),
                    (expected,),
                )

    def test_distinct_named_tournaments_are_hard_rejected(self):
        the_open = market(
            1, "KALSHI", "Will Scottie Scheffler win the The Open Championship?",
            entity="Scottie Scheffler",
        )
        tour = market(
            2, "POLYMARKET", "Will Scottie Scheffler win the 2026 TOUR Championship?",
            slug="will-scottie-scheffler-win-the-2026-tour-championship",
        )

        self.assertEqual(
            extract_confusable_named_titles(the_open), ("the_open_championship",)
        )
        self.assertEqual(
            extract_confusable_named_titles(tour), ("tour_championship",)
        )
        self.assertFalse(hard_constraints_compatible(the_open, tour))
        self.assertEqual(generate_candidates([the_open], [tour]), [])
        self.assertEqual(score_candidate(CandidatePair(the_open, tour)), 0.0)

    def test_full_named_tournament_list_is_extracted(self):
        examples = {
            "The Open Championship": "the_open_championship",
            "TOUR Championship": "tour_championship",
            "US Open": "us_open",
            "Masters Tournament": "masters",
            "PGA Championship": "pga_championship",
            "Ryder Cup": "ryder_cup",
        }
        for index, (title, expected) in enumerate(examples.items()):
            with self.subTest(title=title):
                self.assertEqual(
                    extract_confusable_named_titles(
                        market(index, "KALSHI", f"Will A win the {title}?")
                    ),
                    (expected,),
                )

    def test_compound_award_is_not_equivalent_to_one_component(self):
        compound = market(
            1,
            "KALSHI",
            "Will Kylian Mbappe win the Golden Boot and the Golden Ball?",
        )
        single = market(
            2, "POLYMARKET", "Will Kylian Mbappe win the Golden Ball?"
        )

        self.assertEqual(
            extract_confusable_named_titles(compound),
            ("golden_ball", "golden_boot"),
        )
        self.assertEqual(
            extract_confusable_named_titles(single), ("golden_ball",)
        )
        self.assertFalse(hard_constraints_compatible(compound, single))

    def test_couple_to_individual_requires_manual_rules_check(self):
        couple = market(
            1,
            "KALSHI",
            "Will Trinity and KC win Love Island USA Season 8?",
            subtitle="Trinity and KC",
            event_title="Love Island USA S8: Winning couple",
        )
        individual = market(
            2,
            "POLYMARKET",
            "Will Trinity Tatum win Love Island USA Season 8?",
            slug="will-trinity-tatum-win-love-island-usa-season-8",
            event_title="Who will win Love Island USA Season 8? (Women)",
        )

        self.assertTrue(needs_manual_rules_check(couple, individual))

    def test_resolution_phrase_rejects_reviewed_failure_patterns(self):
        bad_pairs = [
            (
                "Will Nikki Haley be the nominee for the Vice Presidency for the Republican party?",
                "Will Nikki Haley win the 2028 Republican presidential nomination?",
                {"nikki", "haley"},
            ),
            (
                "Will the Democratic party win the governorship in Massachusetts?",
                "Will the Democratic Party win the AZ-06 House seat?",
                {"democratic", "party"},
            ),
            (
                "Will Jude Bellingham win the Bronze Ball?",
                "Will Jude Bellingham win the 2026 Ballon d'Or?",
                {"jude", "bellingham"},
            ),
            (
                "Will Kylian Mbappe lead FIFA World Cup in Goals for the 2026 World Cup Full Tournament?",
                "Will Kylian Mbappe win the Golden Ball at the 2026 FIFA World Cup?",
                {"kylian", "mbappe"},
            ),
            (
                "Will England win the third-place match at the 2026 Men's FIFA World Cup?",
                "Will England win the 2026 FIFA World Cup?",
                {"england"},
            ),
        ]
        for index, (kalshi_name, poly_name, shared) in enumerate(bad_pairs):
            with self.subTest(index=index):
                left = market(index * 2 + 1, "KALSHI", kalshi_name)
                right = market(index * 2 + 2, "POLYMARKET", poly_name)
                similarity = resolution_phrase_similarity(
                    extract_resolution_phrase(left, shared),
                    extract_resolution_phrase(right, shared),
                )
                self.assertLess(
                    similarity, DEFAULT_RESOLUTION_SIMILARITY_THRESHOLD
                )

    def test_lexically_ambiguous_presidential_nomination_stays_review_only(self):
        kalshi = market(
            1,
            "KALSHI",
            "Will Ron DeSantis be the nominee for the Presidency for the Republican party?",
            entity="Ron DeSantis",
        )
        polymarket = market(
            2,
            "POLYMARKET",
            "Will Ron DeSantis win the 2028 Republican presidential nomination?",
            slug="will-ron-desantis-win-the-2028-republican-presidential-nomination",
        )
        _, similarity, _ = score_candidate_signals(CandidatePair(kalshi, polymarket))

        # This pair is semantically equivalent, but the simple lexical method
        # cannot safely distinguish the paraphrase from nearby bad matches.
        # The conservative high-confidence floor intentionally sends it to review.
        self.assertLess(
            similarity, DEFAULT_RESOLUTION_SIMILARITY_THRESHOLD
        )

    def test_resolution_phrase_accepts_same_alaska_senate_race(self):
        left = market(
            1, "KALSHI", "Will Dan Sullivan win the 2026 Alaska Senate race?"
        )
        right = market(
            2, "POLYMARKET", "Will Dan Sullivan win the Alaska Senate race in 2026?"
        )
        shared = {"dan", "sullivan"}
        similarity = resolution_phrase_similarity(
            extract_resolution_phrase(left, shared),
            extract_resolution_phrase(right, shared),
        )

        self.assertGreaterEqual(
            similarity, DEFAULT_RESOLUTION_SIMILARITY_THRESHOLD
        )

    def test_liquidity_floor_uses_total_or_24h_volume(self):
        total = market(1, "KALSHI", "A", volume=10_000, volume_24h=0)
        recent = market(2, "KALSHI", "B", volume=5, volume_24h=100)
        illiquid = market(3, "KALSHI", "C", volume=9_999, volume_24h=99)

        kept = filter_liquid_markets([total, recent, illiquid])

        self.assertEqual({item.market_id for item in kept}, {1, 2})

    def test_cache_persists_required_pair_fields(self):
        pair = ScoredPair(
            kalshi=market(1, "KALSHI", "Philadelphia"),
            polymarket=market(
                10,
                "POLYMARKET",
                "Philadelphia 76ers",
                taker_fee_coefficient=0.06,
            ),
            confidence=0.91,
            source="independent",
            review_status="high_confidence",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matches.sqlite3"
            cache_matches(path, [pair])
            with sqlite3.connect(path) as connection:
                row = connection.execute(
                    "SELECT kalshi_market_id, polymarket_market_id, confidence, matched_at, "
                    "polymarket_fee_coefficient "
                    "FROM matched_pairs"
                ).fetchone()

        self.assertEqual(row[0:2], ("1", "10"))
        self.assertAlmostEqual(row[2], 0.91)
        self.assertTrue(row[3])
        self.assertEqual(row[4], 0.06)

    def test_cache_replaces_previous_snapshot(self):
        old_pair = ScoredPair(
            kalshi=market(1, "KALSHI", "Old"),
            polymarket=market(10, "POLYMARKET", "Old"),
            confidence=0.80,
        )
        new_pair = ScoredPair(
            kalshi=market(2, "KALSHI", "New"),
            polymarket=market(20, "POLYMARKET", "New"),
            confidence=0.90,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matches.sqlite3"
            cache_matches(path, [old_pair])
            cache_matches(path, [new_pair])
            with sqlite3.connect(path) as connection:
                rows = connection.execute(
                    "SELECT kalshi_market_id, polymarket_market_id FROM matched_pairs"
                ).fetchall()

        self.assertEqual(rows, [("2", "20")])

    def test_false_pair_exclusion_survives_rebuild_and_can_be_reversed(self):
        pair = ScoredPair(
            kalshi=market(1, "KALSHI", "False Kalshi pair"),
            polymarket=market(10, "POLYMARKET", "False Polymarket pair"),
            confidence=0.88,
            source="independent",
            review_status="high_confidence",
            resolution_similarity=0.77,
            entity_similarity=0.91,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "matcher_cache.sqlite3"
            review_path = root / "false_pairs.md"
            cache_matches(path, [pair])

            inserted = flag_false_pair(
                path,
                kalshi_market_id=1,
                polymarket_market_id=10,
                kalshi_name=pair.kalshi.name,
                polymarket_name=pair.polymarket.name,
                confidence=pair.confidence,
                phrase_similarity=pair.resolution_similarity,
                entity_similarity=pair.entity_similarity,
                false_pairs_path=review_path,
                flagged_at=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
            )

            with sqlite3.connect(path) as connection:
                immediate_rows = connection.execute(
                    "SELECT kalshi_market_id, polymarket_market_id FROM matched_pairs"
                ).fetchall()
            exclusions = list_false_pair_exclusions(path)
            review_text = review_path.read_text(encoding="utf-8")

            cache_matches(path, [pair])
            with sqlite3.connect(path) as connection:
                rebuilt_rows = connection.execute(
                    "SELECT kalshi_market_id, polymarket_market_id FROM matched_pairs"
                ).fetchall()
                exclusion_count = connection.execute(
                    "SELECT COUNT(*) FROM false_pair_exclusions"
                ).fetchone()[0]

            reversed_flag = unflag_false_pair(path, 1, 10)
            cache_matches(path, [pair])
            with sqlite3.connect(path) as connection:
                restored_rows = connection.execute(
                    "SELECT kalshi_market_id, polymarket_market_id FROM matched_pairs"
                ).fetchall()

        self.assertTrue(inserted)
        self.assertEqual(immediate_rows, [])
        self.assertEqual(len(exclusions), 1)
        self.assertEqual(exclusions[0].phrase_similarity, 0.77)
        self.assertIn("False Kalshi pair", review_text)
        self.assertIn("False Polymarket pair", review_text)
        self.assertIn("confidence `0.880`", review_text)
        self.assertIn("- Reason:\n", review_text)
        self.assertEqual(rebuilt_rows, [])
        self.assertEqual(exclusion_count, 1)
        self.assertTrue(reversed_flag)
        self.assertEqual(restored_rows, [("1", "10")])

    def test_cache_backup_is_timestamped_and_transactionally_readable(self):
        pair = ScoredPair(
            kalshi=market(1, "KALSHI", "Old"),
            polymarket=market(10, "POLYMARKET", "Old"),
            confidence=0.80,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "matcher_cache.sqlite3"
            cache_matches(path, [pair])

            backup = backup_cache_snapshot(
                path,
                now=datetime(2026, 7, 15, 19, 30, tzinfo=timezone.utc),
            )

            self.assertEqual(
                backup,
                root / "backups" / "matcher_cache_20260715T193000000000Z.sqlite3",
            )
            with sqlite3.connect(backup) as connection:
                row = connection.execute(
                    "SELECT kalshi_market_id, polymarket_market_id FROM matched_pairs"
                ).fetchone()
        self.assertEqual(row, ("1", "10"))

    def test_cache_backup_skips_missing_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite3"
            self.assertIsNone(backup_cache_snapshot(path))
            self.assertFalse((path.parent / "backups").exists())


def sdk_market(market_id: int, event_ticker: str) -> SimpleNamespace:
    return SimpleNamespace(
        market_id=market_id,
        exchange_name="KALSHI",
        name=f"Market {market_id}",
        subtitle=None,
        description=None,
        category="Politics",
        subcategory=None,
        expiration_datetime="2028-01-01T00:00:00+00:00",
        ticker=f"TICKER-{market_id}",
        slug=None,
        primary_entity_name=None,
        event_ticker=event_ticker,
        volume=100,
        volume24h=10,
        event_title=f"Event {event_ticker}",
        neg_risk_id=None,
    )


class MarketDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_the_native_venue_universe_and_reports_coverage(self):
        calls: list[str] = []

        class FakeClient:
            async def list_active_markets(self, exchange_name: str):
                calls.append(exchange_name)
                return [sdk_market("K-1", "A"), sdk_market("K-2", "B")]

        output = StringIO()
        with redirect_stdout(output):
            markets = await fetch_active_markets(FakeClient(), "KALSHI")

        self.assertEqual(calls, ["KALSHI"])
        self.assertEqual([item.market_id for item in markets], ["K-1", "K-2"])
        self.assertIn("venue=KALSHI", output.getvalue())
        self.assertIn("status=OK", output.getvalue())
        self.assertIn("duplicate_rows=0", output.getvalue())

    async def test_fetch_deduplicates_native_ids(self):
        class FakeClient:
            async def list_active_markets(self, exchange_name: str):
                return [sdk_market("K-1", "A"), sdk_market("K-1", "A")]

        output = StringIO()
        with redirect_stdout(output):
            markets = await fetch_active_markets(FakeClient(), "KALSHI")

        self.assertEqual([item.market_id for item in markets], ["K-1"])
        self.assertIn("duplicate_rows=1", output.getvalue())
if __name__ == "__main__":
    unittest.main()
