"""Tests for app/core/edge.py.

Every hand-checked vector from docs/ARCHITECTURE.md "Hand-checked vectors" appears
verbatim below, each under a comment quoting the line it comes from. Tolerance 1e-5
unless stated, as the contract says.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

import app.core.edge as edge_mod
from app.core.edge import (
    DEFAULT_TICK,
    NOTE_EDGE_CAPPED,
    NOTE_MIN_ORDER,
    NOTE_NO_EDGE_DEPTH,
    build_opportunity,
    edge,
    edge_clearing_depth,
    effective_price,
    ev_per_dollar,
    kelly_fraction,
    limit_price_for_edge,
    plan_stake,
    resting_limit_price,
    stake_for,
    stake_note,
    taker_fee_per_share,
    walk_asks,
)
from app.core.types import (
    BookGame,
    BookLevel,
    BookMarket,
    BookOutcome,
    BookQuote,
    FairProb,
    Opportunity,
    OrderBook,
    PmMarket,
    PmOutcome,
)

TOL = 1e-5
NOW = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Prefs:
    """A minimal PrefsLike: the six fields the math reads, with the SPEC defaults."""

    bankroll: float = 1000.0
    kelly_fraction: float = 0.25
    max_stake_pct: float = 2.0
    min_edge: float = 0.02
    taker_fee_rate: float = 0.05
    min_liquidity_usd: float = 100.0
    use_market_fee: bool = True


def _levels(*pairs: tuple[float, float]) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(price=p, size=s) for p, s in pairs)


def make_market(**overrides: object) -> PmMarket:
    """Chiefs @ Bills moneyline from docs/FIXTURES.md row 1, synthetic ids."""
    base = PmMarket(
        market_id="500001",
        condition_id="0x" + "ab" * 32,
        slug="nfl-kc-buf-2026-09-20-moneyline",
        question="Chiefs vs. Bills",
        event_id="10001",
        event_slug="nfl-kc-buf-2026-09-20",
        event_title="Chiefs vs. Bills",
        league="nfl",
        market_type="moneyline",
        line=None,
        line_team_key=None,
        outcomes=(
            PmOutcome("7" * 70 + "1", "Chiefs", "KC", 0.55, 0.54, 0.55),
            PmOutcome("7" * 70 + "2", "Bills", "BUF", 0.46, 0.45, 0.46),
        ),
        game_start=datetime(2026, 9, 20, 20, 25, tzinfo=UTC),
        home_team_key="BUF",
        away_team_key="KC",
        accepting_orders=True,
        closed=False,
        resolved_outcome_index=None,
        tick_size=0.01,
        min_order_size=5.0,
        liquidity=2500.0,
        volume=12000.0,
        taker_fee_rate=None,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_book(asks: tuple[BookLevel, ...], token_id: str = "7" * 70 + "1") -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=_levels((0.49, 300.0), (0.48, 500.0)),
        asks=asks,
        tick_size=0.01,
        fetched_at=NOW,
    )


def make_fair(value: float = 0.55) -> FairProb:
    return FairProb(
        value=value,
        method="power",
        n_books=3,
        books_used=("betonlineag", "draftkings", "pinnacle"),
        line=None,
        per_book=(("betonlineag", 0.54), ("draftkings", 0.56), ("pinnacle", 0.55)),
    )


def make_book_game() -> BookGame:
    h2h = BookMarket(
        key="h2h",
        outcomes=(
            BookOutcome("Kansas City Chiefs", "KC", -150, None),
            BookOutcome("Buffalo Bills", "BUF", +130, None),
        ),
        last_update=NOW,
    )
    return BookGame(
        game_id="a" * 32,
        league="nfl",
        commence_time=datetime(2026, 9, 20, 20, 25, tzinfo=UTC),
        home_team_key="BUF",
        away_team_key="KC",
        home_team_name="Buffalo Bills",
        away_team_name="Kansas City Chiefs",
        books=(BookQuote("pinnacle", "Pinnacle", (h2h,)),),
    )


# The contract's walk_asks ladder, reused by the build_opportunity tests.
LADDER = _levels((0.50, 100), (0.51, 200), (0.55, 1000))


# ----------------------------------------------------------------------------- fees


class TestFees:
    # ARCHITECTURE.md vector block:
    # effective_price(0.50,0.05)=0.5125  (0.60)->0.612  (0.30)->0.3105  (0.95)->0.952375
    #   (0.05)->0.052375
    @pytest.mark.parametrize(
        ("price", "expected"),
        [(0.50, 0.5125), (0.60, 0.612), (0.30, 0.3105), (0.95, 0.952375), (0.05, 0.052375)],
    )
    def test_effective_price_contract_vectors(self, price: float, expected: float) -> None:
        assert effective_price(price, 0.05) == pytest.approx(expected, abs=TOL)

    def test_fee_formula_and_peak_at_even_money(self) -> None:
        assert taker_fee_per_share(0.50, 0.05) == pytest.approx(0.0125, abs=1e-12)
        assert taker_fee_per_share(0.30, 0.05) == pytest.approx(0.0105, abs=1e-12)
        assert taker_fee_per_share(0.50, 0.05) > taker_fee_per_share(0.49, 0.05)
        assert taker_fee_per_share(0.50, 0.05) > taker_fee_per_share(0.51, 0.05)

    def test_fee_vanishes_at_the_ends_and_with_zero_rate(self) -> None:
        assert taker_fee_per_share(0.0, 0.05) == 0.0
        assert taker_fee_per_share(1.0, 0.05) == 0.0
        assert taker_fee_per_share(0.5, 0.0) == 0.0
        assert effective_price(0.37, 0.0) == 0.37

    def test_fee_scales_linearly_with_rate(self) -> None:
        assert taker_fee_per_share(0.4, 0.10) == pytest.approx(2 * taker_fee_per_share(0.4, 0.05))

    @pytest.mark.parametrize("price", [-0.01, 1.01])
    def test_rejects_price_outside_unit_interval(self, price: float) -> None:
        with pytest.raises(ValueError):
            taker_fee_per_share(price, 0.05)
        with pytest.raises(ValueError):
            effective_price(price, 0.05)

    def test_rejects_negative_fee_rate(self) -> None:
        with pytest.raises(ValueError):
            effective_price(0.5, -0.05)


# ----------------------------------------------------------------------------- value


class TestValue:
    # ARCHITECTURE.md vector block:
    # kelly_fraction(0.55,0.5125)=0.076923; ev_per_dollar=0.073171; edge=0.0375
    def test_contract_vector(self) -> None:
        assert kelly_fraction(0.55, 0.5125) == pytest.approx(0.076923, abs=TOL)
        assert ev_per_dollar(0.55, 0.5125) == pytest.approx(0.073171, abs=TOL)
        assert edge(0.55, 0.5125) == pytest.approx(0.0375, abs=TOL)

    # kelly_fraction(0.60,0.612)=0.0 (negative clamped)
    def test_kelly_negative_edge_clamps_to_zero(self) -> None:
        assert kelly_fraction(0.60, 0.612) == 0.0
        assert edge(0.60, 0.612) < 0
        assert ev_per_dollar(0.60, 0.612) < 0

    def test_kelly_at_zero_edge_is_zero(self) -> None:
        assert kelly_fraction(0.5125, 0.5125) == 0.0

    def test_kelly_is_the_textbook_binary_formula(self) -> None:
        # f* = (p * b - q) / b with b = (1 - cost) / cost net odds.
        fair, cost = 0.62, 0.55
        b = (1 - cost) / cost
        assert kelly_fraction(fair, cost) == pytest.approx((fair * b - (1 - fair)) / b, abs=1e-12)

    def test_kelly_grows_with_edge(self) -> None:
        assert kelly_fraction(0.60, 0.5125) > kelly_fraction(0.55, 0.5125) > 0.0

    def test_kelly_at_or_above_cost_one_is_zero(self) -> None:
        assert kelly_fraction(0.99, 1.0) == 0.0

    @pytest.mark.parametrize("cost", [0.0, -0.1])
    def test_rejects_non_positive_cost(self, cost: float) -> None:
        with pytest.raises(ValueError):
            ev_per_dollar(0.5, cost)
        with pytest.raises(ValueError):
            kelly_fraction(0.5, cost)


class TestStakeFor:
    # ARCHITECTURE.md vector block:
    # stake_for(1000,0.076923,0.25,2.0)=19.23; stake_for(1000,0.2,0.25,2.0)=20.00 (capped);
    #   stake_for(1000,0.001,0.25,2.0)=0.0 (< $1)
    def test_contract_vectors(self) -> None:
        assert stake_for(1000, 0.076923, 0.25, 2.0) == pytest.approx(19.23, abs=TOL)
        assert stake_for(1000, 0.2, 0.25, 2.0) == pytest.approx(20.00, abs=TOL)
        assert stake_for(1000, 0.001, 0.25, 2.0) == 0.0

    def test_rounds_to_the_cent(self) -> None:
        assert stake_for(1000, 0.076923, 0.25, 2.0) == 19.23
        assert stake_for(333.33, 0.05, 0.5, 10.0) == round(333.33 * 0.05 * 0.5, 2)

    def test_min_stake_is_configurable(self) -> None:
        assert stake_for(1000, 0.001, 0.25, 2.0, min_stake=0.1) == 0.25
        assert stake_for(1000, 0.076923, 0.25, 2.0, min_stake=25.0) == 0.0

    def test_cap_is_a_percentage_of_bankroll(self) -> None:
        assert stake_for(500, 1.0, 1.0, 2.0) == 10.0
        assert stake_for(500, 1.0, 1.0, 5.0) == 25.0

    def test_zero_kelly_or_zero_bankroll_stakes_nothing(self) -> None:
        assert stake_for(1000, 0.0, 0.25, 2.0) == 0.0
        assert stake_for(0.0, 0.5, 0.25, 2.0) == 0.0

    @pytest.mark.parametrize(
        "args",
        [
            (-1000, 0.1, 0.25, 2.0),
            (1000, -0.1, 0.25, 2.0),
            (1000, 0.1, -0.25, 2.0),
            (1000, 0.1, 0.25, -2.0),
        ],
    )
    def test_rejects_negative_arguments(self, args: tuple[float, float, float, float]) -> None:
        with pytest.raises(ValueError):
            stake_for(*args)


# ----------------------------------------------------------------------------- order book


class TestWalkAsks:
    # ARCHITECTURE.md vector block:
    # walk_asks([(0.50,100),(0.51,200),(0.55,1000)], usd=100, fee=0.05)
    #   -> (0.517324, 193.302328, True)
    def test_contract_vector_deep_ladder(self) -> None:
        avg, shares, filled = walk_asks(LADDER, usd=100, fee_rate=0.05)
        assert avg == pytest.approx(0.517324, abs=TOL)
        assert shares == pytest.approx(193.302328, abs=TOL)
        assert filled is True

    # walk_asks([(0.50,10)], usd=100, fee=0.05) -> (0.5125, 10.0, False)
    def test_contract_vector_thin_ladder(self) -> None:
        avg, shares, filled = walk_asks(_levels((0.50, 10)), usd=100, fee_rate=0.05)
        assert avg == pytest.approx(0.5125, abs=TOL)
        assert shares == pytest.approx(10.0, abs=TOL)
        assert filled is False

    def test_spend_equals_usd_when_filled(self) -> None:
        avg, shares, filled = walk_asks(LADDER, usd=100, fee_rate=0.05)
        assert filled
        assert avg * shares == pytest.approx(100.0, abs=1e-9)

    def test_spend_equals_ladder_cost_when_not_filled(self) -> None:
        avg, shares, filled = walk_asks(_levels((0.50, 10), (0.52, 20)), usd=100, fee_rate=0.05)
        assert not filled
        assert shares == 30.0
        expected = 10 * effective_price(0.50, 0.05) + 20 * effective_price(0.52, 0.05)
        assert avg * shares == pytest.approx(expected, abs=1e-9)

    def test_exactly_absorbing_ladder_counts_as_filled(self) -> None:
        usd = 10 * effective_price(0.50, 0.05)  # 5.125
        avg, shares, filled = walk_asks(_levels((0.50, 10)), usd=usd, fee_rate=0.05)
        assert filled is True
        assert shares == pytest.approx(10.0, abs=1e-12)
        assert avg == pytest.approx(0.5125, abs=1e-12)

    def test_stays_on_best_level_when_it_is_deep_enough(self) -> None:
        avg, shares, filled = walk_asks(LADDER, usd=19.23, fee_rate=0.05)
        assert filled
        assert avg == pytest.approx(0.5125, abs=1e-12)
        assert shares == pytest.approx(19.23 / 0.5125, abs=1e-9)

    def test_average_rises_as_the_ladder_is_walked(self) -> None:
        small = walk_asks(LADDER, usd=20, fee_rate=0.05)[0]
        medium = walk_asks(LADDER, usd=100, fee_rate=0.05)[0]
        large = walk_asks(LADDER, usd=300, fee_rate=0.05)[0]
        assert small < medium < large

    def test_zero_fee_walks_raw_prices(self) -> None:
        avg, shares, filled = walk_asks(LADDER, usd=100, fee_rate=0.0)
        assert filled
        # 100 shares at 0.50 = 50, then 50 / 0.51 shares at 0.51
        assert shares == pytest.approx(100 + 50 / 0.51, abs=1e-9)
        assert avg == pytest.approx(100 / shares, abs=1e-12)

    def test_unsorted_ladder_is_walked_cheapest_first(self) -> None:
        shuffled = _levels((0.55, 1000), (0.50, 100), (0.51, 200))
        assert walk_asks(shuffled, usd=100, fee_rate=0.05) == walk_asks(
            LADDER, usd=100, fee_rate=0.05
        )

    def test_empty_ladder_fills_nothing(self) -> None:
        assert walk_asks((), usd=100, fee_rate=0.05) == (0.0, 0.0, False)

    def test_zero_usd_is_trivially_filled(self) -> None:
        assert walk_asks(LADDER, usd=0.0, fee_rate=0.05) == (0.0, 0.0, True)

    def test_zero_size_levels_are_skipped(self) -> None:
        ladder = _levels((0.40, 0), (0.50, 10))
        assert walk_asks(ladder, usd=100, fee_rate=0.05) == pytest.approx((0.5125, 10.0, False))

    def test_negative_usd_raises(self) -> None:
        with pytest.raises(ValueError):
            walk_asks(LADDER, usd=-1.0, fee_rate=0.05)


class TestLimitPriceForEdge:
    # ARCHITECTURE.md vector block:
    # limit_price_for_edge(0.55,0.02)=0.53; (0.5555,0.02)=0.53; (0.01,0.02)=None
    def test_contract_vectors(self) -> None:
        assert limit_price_for_edge(0.55, 0.02) == pytest.approx(0.53, abs=TOL)
        assert limit_price_for_edge(0.5555, 0.02) == pytest.approx(0.53, abs=TOL)
        assert limit_price_for_edge(0.01, 0.02) is None

    def test_default_tick_is_one_cent(self) -> None:
        assert DEFAULT_TICK == 0.01
        assert limit_price_for_edge(0.5555, 0.02) == limit_price_for_edge(0.5555, 0.02, 0.01)

    def test_exact_tick_multiple_is_not_floored_down_by_float_error(self) -> None:
        # 0.55 - 0.02 = 0.5300000000000000266; /0.01 = 52.99999... without the guard.
        for fair, min_edge in ((0.55, 0.02), (0.29, 0.01), (0.83, 0.03), (0.07, 0.02)):
            expected = round(fair - min_edge, 2)
            assert limit_price_for_edge(fair, min_edge) == pytest.approx(expected, abs=1e-12)

    def test_result_is_clean_of_float_dust(self) -> None:
        assert limit_price_for_edge(0.55, 0.02) == 0.53

    def test_respects_finer_and_coarser_ticks(self) -> None:
        assert limit_price_for_edge(0.5555, 0.02, tick=0.001) == pytest.approx(0.535, abs=1e-12)
        assert limit_price_for_edge(0.5555, 0.02, tick=0.05) == pytest.approx(0.50, abs=1e-12)

    def test_never_exceeds_fair_minus_min_edge(self) -> None:
        for fair in (0.11, 0.333, 0.5, 0.6789, 0.95):
            price = limit_price_for_edge(fair, 0.02)
            assert price is not None
            assert price <= fair - 0.02 + 1e-9
            assert fair - 0.02 - price < 0.01

    def test_zero_or_negative_price_is_none(self) -> None:
        assert limit_price_for_edge(0.02, 0.02) is None
        assert limit_price_for_edge(0.015, 0.02) is None
        assert limit_price_for_edge(0.03, 0.02) == pytest.approx(0.01, abs=1e-12)

    def test_rejects_non_positive_tick(self) -> None:
        with pytest.raises(ValueError):
            limit_price_for_edge(0.55, 0.02, tick=0.0)


# ----------------------------------------------------------------------------- build_opportunity


class TestBuildOpportunity:
    def test_happy_path_matches_the_contract_vectors(self) -> None:
        market = make_market()
        book = make_book(LADDER)
        fair = make_fair(0.55)
        game = make_book_game()

        opp = build_opportunity(market, 0, book, fair, Prefs(), game, NOW)

        assert isinstance(opp, Opportunity)
        assert opp.market is market
        assert opp.outcome_index == 0
        assert opp.fair is fair
        assert opp.book_game is game
        assert opp.computed_at == NOW
        assert opp.ask == 0.50
        assert opp.effective_price == pytest.approx(0.5125, abs=TOL)
        assert opp.edge == pytest.approx(0.0375, abs=TOL)
        assert opp.ev_per_dollar == pytest.approx(0.073171, abs=TOL)
        assert opp.kelly == pytest.approx(0.076923, abs=TOL)
        assert opp.suggested_stake == pytest.approx(19.23, abs=TOL)
        # $19.23 sits entirely on the 100-share best level at 0.5125 effective.
        assert opp.fill_price == pytest.approx(0.5125, abs=TOL)
        assert opp.fill_complete is True
        assert opp.fill_usd is None
        # limit_price_for_edge(0.55, 0.02) = 0.53 would cross the 0.50 ask and fill as a
        # taker; the resting maker price is one tick below the best ask.
        assert opp.limit_price == pytest.approx(0.49, abs=TOL)

    def test_book_none_returns_none(self) -> None:
        assert build_opportunity(make_market(), 0, None, make_fair(), Prefs(), None, NOW) is None

    def test_no_asks_returns_none(self) -> None:
        book = make_book(())
        assert build_opportunity(make_market(), 0, book, make_fair(), Prefs(), None, NOW) is None

    def test_edge_below_min_edge_returns_none(self) -> None:
        book = make_book(LADDER)  # cost 0.5125
        assert (
            build_opportunity(make_market(), 0, book, make_fair(0.53), Prefs(), None, NOW) is None
        )
        assert (
            build_opportunity(make_market(), 0, book, make_fair(0.50), Prefs(), None, NOW) is None
        )

    def test_edge_exactly_min_edge_is_kept(self) -> None:
        book = make_book(_levels((0.50, 100)))
        prefs = Prefs(min_edge=0.0375)
        opp = build_opportunity(make_market(), 0, book, make_fair(0.55), prefs, None, NOW)
        assert opp is not None
        assert opp.edge == pytest.approx(0.0375, abs=1e-12)

    def test_edge_compares_fair_against_fee_inclusive_price(self) -> None:
        # Raw edge 0.03 clears min_edge 0.02, but the fee pushes the cost to 0.5325.
        book = make_book(_levels((0.52, 100)))
        prefs = Prefs(min_edge=0.02)
        assert build_opportunity(make_market(), 0, book, make_fair(0.55), prefs, None, NOW) is None
        opp = build_opportunity(
            make_market(), 0, book, make_fair(0.55), Prefs(min_edge=0.015), None, NOW
        )
        assert opp is not None
        assert opp.effective_price == pytest.approx(effective_price(0.52, 0.05), abs=1e-12)

    def test_liquidity_below_minimum_returns_none(self) -> None:
        market = make_market(liquidity=99.99)
        book = make_book(LADDER)
        assert build_opportunity(market, 0, book, make_fair(), Prefs(), None, NOW) is None
        assert (
            build_opportunity(market, 0, book, make_fair(), Prefs(min_liquidity_usd=50), None, NOW)
            is not None
        )

    def test_liquidity_exactly_minimum_is_kept(self) -> None:
        market = make_market(liquidity=100.0)
        opp = build_opportunity(market, 0, make_book(LADDER), make_fair(), Prefs(), None, NOW)
        assert opp is not None

    def test_unknown_liquidity_passes(self) -> None:
        market = make_market(liquidity=None)
        opp = build_opportunity(market, 0, make_book(LADDER), make_fair(), Prefs(), None, NOW)
        assert opp is not None

    def test_market_fee_override_beats_prefs(self) -> None:
        market = make_market(taker_fee_rate=0.0)
        opp = build_opportunity(market, 0, make_book(LADDER), make_fair(), Prefs(), None, NOW)
        assert opp is not None
        assert opp.effective_price == 0.50
        assert opp.edge == pytest.approx(0.05, abs=1e-12)
        assert opp.fill_price == pytest.approx(0.50, abs=1e-12)

    def test_prefs_fee_used_when_market_has_none(self) -> None:
        market = make_market(taker_fee_rate=None)
        prefs = Prefs(taker_fee_rate=0.02)
        opp = build_opportunity(market, 0, make_book(LADDER), make_fair(), prefs, None, NOW)
        assert opp is not None
        assert opp.effective_price == pytest.approx(effective_price(0.50, 0.02), abs=1e-12)

    def test_zero_stake_gives_no_fill_price_and_complete_true(self) -> None:
        prefs = Prefs(bankroll=10.0)  # 10 * 0.076923 * 0.25 = $0.19 < $1
        opp = build_opportunity(make_market(), 0, make_book(LADDER), make_fair(), prefs, None, NOW)
        assert opp is not None
        assert opp.suggested_stake == 0.0
        assert opp.fill_price is None
        assert opp.fill_complete is True
        assert opp.kelly == pytest.approx(0.076923, abs=TOL)

    def test_partial_fill_when_ladder_is_thin(self) -> None:
        book = make_book(_levels((0.50, 10)))  # $5.125 of depth against a $19.23 stake
        opp = build_opportunity(make_market(), 0, book, make_fair(), Prefs(), None, NOW)
        assert opp is not None
        assert opp.suggested_stake == pytest.approx(19.23, abs=TOL)
        assert opp.fill_price == pytest.approx(0.5125, abs=TOL)
        assert opp.fill_complete is False

    def test_fill_price_walks_the_ladder_for_a_large_stake(self) -> None:
        prefs = Prefs(bankroll=5000.0, max_stake_pct=10.0)  # stake 96.15 > level 1 ($51.25)
        opp = build_opportunity(make_market(), 0, make_book(LADDER), make_fair(), prefs, None, NOW)
        assert opp is not None
        assert opp.suggested_stake == pytest.approx(96.15, abs=TOL)
        expected_avg, _, complete = walk_asks(LADDER, 96.15, 0.05)
        assert complete
        assert opp.fill_price == pytest.approx(expected_avg, abs=1e-12)
        assert opp.fill_price > opp.effective_price
        assert opp.fill_complete is True

    def test_stake_is_capped_by_max_stake_pct(self) -> None:
        prefs = Prefs(bankroll=1000.0, kelly_fraction=1.0, max_stake_pct=2.0)
        opp = build_opportunity(make_market(), 0, make_book(LADDER), make_fair(), prefs, None, NOW)
        assert opp is not None
        assert opp.suggested_stake == 20.0

    def test_limit_price_uses_market_tick_size(self) -> None:
        fair = make_fair(0.5555)
        coarse = build_opportunity(
            make_market(tick_size=0.01), 0, make_book(LADDER), fair, Prefs(), None, NOW
        )
        fine = build_opportunity(
            make_market(tick_size=0.001), 0, make_book(LADDER), fair, Prefs(), None, NOW
        )
        assert coarse is not None and fine is not None
        # fair - min_edge = 0.5355 clears the 0.50 best ask, so the resting price is one
        # market tick below the ask: 0.49 on a 1c tick, 0.499 on a 0.1c tick.
        assert coarse.limit_price == pytest.approx(0.49, abs=1e-12)
        assert fine.limit_price == pytest.approx(0.499, abs=1e-12)

    def test_limit_price_falls_back_to_one_cent_tick(self) -> None:
        opp = build_opportunity(
            make_market(tick_size=None), 0, make_book(LADDER), make_fair(0.5555), Prefs(), None, NOW
        )
        assert opp is not None
        # one default 1c tick below the 0.50 ask
        assert opp.limit_price == pytest.approx(0.49, abs=1e-12)

    def test_limit_price_none_when_min_edge_swallows_fair(self) -> None:
        # Edge 0.024 clears min_edge, but fair - min_edge = 0.005 is under one tick.
        market = make_market(taker_fee_rate=0.0)
        book = make_book(_levels((0.001, 1000)))
        prefs = Prefs(min_edge=0.02, min_liquidity_usd=0.0)
        opp = build_opportunity(market, 0, book, make_fair(0.025), prefs, None, NOW)
        assert opp is not None
        assert opp.edge == pytest.approx(0.024, abs=1e-12)
        assert opp.limit_price is None

    def test_second_outcome_and_no_book_game(self) -> None:
        market = make_market()
        book = make_book(_levels((0.44, 500)), token_id=market.outcomes[1].token_id)
        # cost = 0.44 + 0.05 * 0.44 * 0.56 = 0.45232; edge = 0.02768 >= 0.02
        opp = build_opportunity(market, 1, book, make_fair(0.48), Prefs(), None, NOW)
        assert opp is not None
        assert opp.outcome_index == 1
        assert opp.book_game is None
        assert opp.ask == 0.44
        assert opp.effective_price == pytest.approx(0.45232, abs=1e-12)
        assert opp.edge == pytest.approx(0.48 - effective_price(0.44, 0.05), abs=1e-12)

    def test_zero_ask_is_not_a_usable_ask(self) -> None:
        book = make_book(_levels((0.0, 100)))
        assert build_opportunity(make_market(), 0, book, make_fair(), Prefs(), None, NOW) is None

    @pytest.mark.parametrize("index", [-1, 2])
    def test_rejects_bad_outcome_index(self, index: int) -> None:
        with pytest.raises(ValueError):
            build_opportunity(
                make_market(), index, make_book(LADDER), make_fair(), Prefs(), None, NOW
            )

    def test_is_deterministic(self) -> None:
        args = (make_market(), 0, make_book(LADDER), make_fair(), Prefs(), make_book_game(), NOW)
        assert build_opportunity(*args) == build_opportunity(*args)


# ----------------------------------------------------------------------------- review fixes


class TestRestingLimitPrice:
    def test_caps_one_tick_below_the_best_ask(self) -> None:
        # limit_price_for_edge(0.55, 0.02) = 0.53 would cross a 0.50 ask: rest at 0.49 instead
        assert resting_limit_price(0.55, 0.02, 0.50) == pytest.approx(0.49, abs=1e-12)
        assert resting_limit_price(0.55, 0.02, 0.60) == pytest.approx(0.53, abs=1e-12)  # rests
        assert resting_limit_price(0.55, 0.02, 0.54) == pytest.approx(0.53, abs=1e-12)  # one under
        assert resting_limit_price(0.55, 0.02, None) == pytest.approx(0.53, abs=1e-12)
        assert resting_limit_price(0.5555, 0.02, 0.50, tick=0.001) == pytest.approx(
            0.499, abs=1e-12
        )

    def test_none_when_nothing_can_rest(self) -> None:
        assert resting_limit_price(0.55, 0.02, 0.01) is None  # ceiling would be 0
        assert resting_limit_price(0.02, 0.02, 0.50) is None
        with pytest.raises(ValueError):
            resting_limit_price(0.55, 0.02, 0.50, tick=0.0)


class TestBuildOpportunityTradability:
    def test_closed_or_paused_markets_are_never_opportunities(self) -> None:
        book, fair = make_book(LADDER), make_fair()
        assert (
            build_opportunity(make_market(closed=True), 0, book, fair, Prefs(), None, NOW) is None
        )
        paused = make_market(accepting_orders=False)
        assert build_opportunity(paused, 0, book, fair, Prefs(), None, NOW) is None
        assert build_opportunity(make_market(), 0, book, fair, Prefs(), None, NOW) is not None

    def test_started_games_are_never_opportunities(self) -> None:
        book, fair = make_book(LADDER), make_fair()
        start = datetime(2026, 9, 20, 20, 25, tzinfo=UTC)
        assert build_opportunity(make_market(), 0, book, fair, Prefs(), None, start) is None
        after = start + timedelta(hours=1)
        assert build_opportunity(make_market(), 0, book, fair, Prefs(), None, after) is None
        before = start - timedelta(seconds=1)
        assert build_opportunity(make_market(), 0, book, fair, Prefs(), None, before) is not None
        # an unknown start is left to the scan service (which reads the book's commence time)
        unknown = make_market(game_start=None)
        assert build_opportunity(unknown, 0, book, fair, Prefs(), None, after) is not None

    def test_thin_ladder_reports_the_fillable_dollars(self) -> None:
        book = make_book(_levels((0.64, 5.0)))
        opp = build_opportunity(make_market(), 0, book, make_fair(0.688564), Prefs(), None, NOW)
        assert opp is not None
        assert opp.suggested_stake == pytest.approx(20.00, abs=TOL)  # 2% cap, Kelly says more
        assert opp.fill_complete is False
        assert opp.fill_price == pytest.approx(effective_price(0.64, 0.05), abs=TOL)
        assert opp.fill_usd == pytest.approx(round(5 * effective_price(0.64, 0.05), 2), abs=1e-9)
        assert opp.fill_usd == pytest.approx(3.26, abs=1e-9)
        deep = build_opportunity(
            make_market(),
            0,
            make_book(_levels((0.64, 500.0))),
            make_fair(0.688564),
            Prefs(),
            None,
            NOW,
        )
        assert deep is not None and deep.fill_complete is True and deep.fill_usd is None


# ------------------------------------------------------- review round 2: stake vs the ladder

# The Celtics ladder from the finding: ten cheap shares, then a wall well above fair.
THIN_THEN_DEAR = _levels((0.64, 10.0), (0.75, 5000.0))
CELTICS_FAIR = 0.688564


class TestEdgeClearingDepth:
    def test_stops_at_the_first_level_that_does_not_clear_min_edge(self) -> None:
        usd, shares, exhausted = edge_clearing_depth(THIN_THEN_DEAR, CELTICS_FAIR, 0.02, 0.05)
        assert shares == 10.0
        assert usd == pytest.approx(10 * effective_price(0.64, 0.05), abs=1e-9)
        assert exhausted is False  # ran out of edge, not out of ladder

    def test_exhausting_the_ladder_is_reported_separately(self) -> None:
        ladder = _levels((0.64, 10.0))
        usd, shares, exhausted = edge_clearing_depth(ladder, CELTICS_FAIR, 0.02, 0.05)
        assert exhausted is True and shares == 10.0
        assert usd == pytest.approx(10 * effective_price(0.64, 0.05), abs=1e-9)

    def test_walks_several_clearing_levels_cheapest_first(self) -> None:
        # LADDER against fair 0.55: 0.50 and 0.51 clear min_edge 0.02, 0.55 does not.
        usd, shares, exhausted = edge_clearing_depth(LADDER, 0.55, 0.02, 0.05)
        assert shares == 300.0 and exhausted is False
        expected = 100 * effective_price(0.50, 0.05) + 200 * effective_price(0.51, 0.05)
        assert usd == pytest.approx(expected, abs=1e-9)

    def test_nothing_clears(self) -> None:
        assert edge_clearing_depth(_levels((0.75, 100)), CELTICS_FAIR, 0.02, 0.05) == (
            0.0,
            0.0,
            False,
        )

    def test_empty_ladder_and_zero_size_levels(self) -> None:
        assert edge_clearing_depth((), 0.55, 0.02, 0.05) == (0.0, 0.0, True)
        usd, shares, exhausted = edge_clearing_depth(
            _levels((0.50, 0.0), (0.51, 100.0)), 0.55, 0.02, 0.05
        )
        assert shares == 100.0 and exhausted is True
        assert usd == pytest.approx(100 * effective_price(0.51, 0.05), abs=1e-9)


class TestStakeIsCappedByTheEdgeItCanActuallyBuy:
    """The finding: the stake was sized at the best ask and never re-checked against the
    ladder it walks, so the recommendation filled above fair and still claimed a full fill."""

    def test_stake_is_cut_to_the_edge_clearing_depth(self) -> None:
        opp = build_opportunity(
            make_market(), 0, make_book(THIN_THEN_DEAR), make_fair(CELTICS_FAIR), Prefs(), None, NOW
        )
        assert opp is not None
        # what the old sizing did: $20 of Kelly money filled at 0.72052, above fair 0.6886
        old_fill, _, old_complete = walk_asks(THIN_THEN_DEAR, 20.0, 0.05)
        assert old_complete is True and old_fill > CELTICS_FAIR
        assert old_fill == pytest.approx(0.72052, abs=1e-4)

        clearing = 10 * effective_price(0.64, 0.05)  # $6.5152
        assert opp.suggested_stake == pytest.approx(6.51, abs=1e-9)  # floored to the cent
        assert opp.suggested_stake <= clearing
        assert opp.fill_price == pytest.approx(effective_price(0.64, 0.05), abs=TOL)
        assert opp.fill_price < CELTICS_FAIR  # the suggestion is +EV at the price it fills
        assert opp.fill_complete is False  # so the partial-fill UI / prefill takes over
        assert opp.fill_usd == pytest.approx(6.51, abs=1e-9)

    def test_a_deep_ladder_at_one_price_is_not_capped(self) -> None:
        opp = build_opportunity(
            make_market(),
            0,
            make_book(_levels((0.64, 5000.0))),
            make_fair(CELTICS_FAIR),
            Prefs(),
            None,
            NOW,
        )
        assert opp is not None
        assert opp.suggested_stake == pytest.approx(20.00, abs=TOL)  # the 2% cap, as before
        assert opp.fill_complete is True and opp.fill_usd is None

    def test_a_thin_ladder_whose_levels_all_clear_still_reports_the_fillable_dollars(self) -> None:
        """The contract vector: running out of shares is a liquidity limit, not an edge
        limit, so the Kelly stake stands and walk_asks reports the partial fill."""
        opp = build_opportunity(
            make_market(),
            0,
            make_book(_levels((0.64, 5.0))),
            make_fair(CELTICS_FAIR),
            Prefs(),
            None,
            NOW,
        )
        assert opp is not None
        assert opp.suggested_stake == pytest.approx(20.00, abs=TOL)
        assert opp.fill_complete is False
        assert opp.fill_price == pytest.approx(0.65152, abs=TOL)
        assert opp.fill_usd == pytest.approx(3.26, abs=1e-9)

    def test_note_explains_the_cap(self) -> None:
        book = make_book(THIN_THEN_DEAR)
        opp = build_opportunity(make_market(), 0, book, make_fair(CELTICS_FAIR), Prefs(), None, NOW)
        assert opp is not None
        assert stake_note(opp, book, Prefs()) == NOTE_EDGE_CAPPED
        assert stake_note(opp, None, Prefs()) is None

    def test_a_cap_under_a_dollar_suggests_nothing(self) -> None:
        # one share of depth at the good price: $0.65 of edge is not a bet
        book = make_book(_levels((0.64, 1.0), (0.75, 5000.0)))
        prefs = Prefs(min_liquidity_usd=0.0)
        opp = build_opportunity(make_market(), 0, book, make_fair(CELTICS_FAIR), prefs, None, NOW)
        assert opp is not None
        assert opp.suggested_stake == 0.0
        assert opp.fill_price is None and opp.fill_complete is True and opp.fill_usd is None
        assert stake_note(opp, book, prefs) == NOTE_NO_EDGE_DEPTH

    def test_plan_stake_is_the_one_implementation(self) -> None:
        prefs = Prefs()
        kelly = kelly_fraction(CELTICS_FAIR, effective_price(0.64, 0.05))
        plan = plan_stake(THIN_THEN_DEAR, CELTICS_FAIR, kelly, 0.05, prefs, 5.0)
        assert (plan.stake, plan.fill_complete, plan.note) == (6.51, False, NOTE_EDGE_CAPPED)
        assert plan.fill_usd == pytest.approx(6.51, abs=1e-9)


class TestMinimumOrderSize:
    """The finding: a $1.33 stake on a 5-share minimum is an order Polymarket would reject."""

    def test_unplaceable_stake_is_reported_as_zero_with_a_reason(self) -> None:
        prefs = Prefs(bankroll=100.0)
        market = make_market(min_order_size=5.0)  # the fixtures' orderMinSize
        book = make_book(_levels((0.55, 500.0)))
        fair = make_fair(0.585612)  # Chiefs, from docs/FIXTURES.md
        opp = build_opportunity(market, 0, book, fair, prefs, None, NOW)
        assert opp is not None
        assert opp.edge == pytest.approx(0.585612 - effective_price(0.55, 0.05), abs=1e-9)
        # Kelly wanted $1.33, which buys 2.36 shares: under the 5-share minimum
        wanted = stake_for(100.0, opp.kelly, 0.25, 2.0)
        assert wanted == pytest.approx(1.33, abs=1e-9)
        assert wanted / effective_price(0.55, 0.05) == pytest.approx(2.3649, abs=1e-4)
        assert opp.suggested_stake == 0.0
        assert opp.fill_price is None and opp.fill_complete is True and opp.fill_usd is None
        note = stake_note(opp, book, prefs)
        assert note == NOTE_MIN_ORDER.format(shares=2.3649, min_size=5.0)
        assert note is not None and "minimum order" in note and len(note) <= 80

    def test_a_bankroll_that_clears_the_minimum_is_suggested_as_before(self) -> None:
        market = make_market(min_order_size=5.0)
        book = make_book(_levels((0.55, 500.0)))
        opp = build_opportunity(market, 0, book, make_fair(0.585612), Prefs(), None, NOW)
        assert opp is not None
        assert opp.suggested_stake == pytest.approx(13.28, abs=0.01)
        assert opp.suggested_stake / opp.effective_price > 5.0
        assert stake_note(opp, book, Prefs()) is None

    def test_unknown_or_zero_minimum_never_blocks_a_stake(self) -> None:
        prefs = Prefs(bankroll=100.0)
        book = make_book(_levels((0.55, 500.0)))
        for min_size in (None, 0.0):
            opp = build_opportunity(
                make_market(min_order_size=min_size), 0, book, make_fair(0.585612), prefs, None, NOW
            )
            assert opp is not None and opp.suggested_stake == pytest.approx(1.33, abs=1e-9)

    def test_a_ladder_too_thin_for_the_minimum_order_suggests_nothing(self) -> None:
        """Two shares on the book cannot fill a five-share minimum at any stake."""
        market = make_market(min_order_size=5.0, liquidity=None)
        book = make_book(_levels((0.64, 2.0)))
        opp = build_opportunity(market, 0, book, make_fair(CELTICS_FAIR), Prefs(), None, NOW)
        assert opp is not None
        assert opp.suggested_stake == 0.0
        assert stake_note(opp, book, Prefs()) is not None


class TestResolveFeeRate:
    """Which fee an outcome is priced at. Getting this wrong does not fail loudly -- it
    quietly moves every edge in the app by half a point or more."""

    def test_the_market_rate_wins_by_default(self) -> None:
        assert edge_mod.resolve_fee_rate(0.10, Prefs(taker_fee_rate=0.0695)) == 0.10

    def test_the_preference_wins_when_the_owner_turns_the_market_rate_off(self) -> None:
        """polymarket.us charges 0.0695 and publishes no Gamma; the 0.10 that Gamma reports
        is .com's fee and must not silently replace what the owner set."""
        prefs = Prefs(taker_fee_rate=0.0695, use_market_fee=False)
        assert edge_mod.resolve_fee_rate(0.10, prefs) == 0.0695

    def test_the_preference_is_used_when_the_market_states_no_rate(self) -> None:
        for flag in (True, False):
            prefs = Prefs(taker_fee_rate=0.0695, use_market_fee=flag)
            assert edge_mod.resolve_fee_rate(None, prefs) == 0.0695

    def test_turning_it_off_changes_the_edge_an_opportunity_reports(self) -> None:
        """The end-to-end consequence: at 72c the two fee rates differ by 0.6 points of
        edge, which is the difference between showing a bet and hiding it."""
        book = make_book(_levels((0.72, 5000.0)))
        market = make_market(taker_fee_rate=0.10)
        fair = make_fair(0.732)

        on = build_opportunity(
            market, 0, book, fair, Prefs(taker_fee_rate=0.0695, min_edge=-0.25), None, NOW
        )
        off = build_opportunity(
            market,
            0,
            book,
            fair,
            Prefs(taker_fee_rate=0.0695, min_edge=-0.25, use_market_fee=False),
            None,
            NOW,
        )
        assert on is not None and off is not None
        assert on.effective_price == pytest.approx(0.72 + 0.10 * 0.72 * 0.28)
        assert off.effective_price == pytest.approx(0.72 + 0.0695 * 0.72 * 0.28)
        assert off.edge - on.edge == pytest.approx((0.10 - 0.0695) * 0.72 * 0.28)
