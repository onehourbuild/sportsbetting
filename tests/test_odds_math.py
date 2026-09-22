"""Tests for app/core/odds_math.py.

Every hand-checked vector from docs/ARCHITECTURE.md "Hand-checked vectors" appears
verbatim below, each under a comment quoting the line it comes from. Tolerance 1e-5
unless stated, as the contract says.
"""

from __future__ import annotations

import pytest

from app.core.odds_math import (
    DEVIG_METHODS,
    additive_devig,
    american_to_prob,
    consensus,
    decimal_to_prob,
    devig,
    multiplicative_devig,
    power_devig,
    power_exponent,
    prob_to_american,
    shin_devig,
    shin_z,
)
from app.core.types import FairProb

TOL = 1e-5
SUM_TOL = 1e-9

# ARCHITECTURE.md vector block, two-way markets: American odds -> expected de-vigged probs.
# (-110,-110): mult [0.5,0.5]; add [0.5,0.5]; power [0.5,0.5]; shin [0.5,0.5]
# (-200,+170): mult [0.642857,0.357143]; add [0.648148,0.351852];
#              power [0.650822,0.349178]; shin [0.648148,0.351852]
# (-150,+130): mult [0.579832,0.420168]; add [0.582609,0.417391];
#              power [0.583983,0.416017]; shin [0.582609,0.417391]
# (-3000,+1200): mult [0.926366,0.073634]; add [0.945409,0.054591];
#              power [0.959759,0.040241]; shin [0.945409,0.054591]
# (+120,-140): mult [0.437956,0.562044]; add [0.435606,0.564394];
#              power [0.434435,0.565565]; shin [0.435606,0.564394]
TWO_WAY_VECTORS: list[tuple[tuple[int, int], dict[str, list[float]]]] = [
    (
        (-110, -110),
        {
            "multiplicative": [0.5, 0.5],
            "additive": [0.5, 0.5],
            "power": [0.5, 0.5],
            "shin": [0.5, 0.5],
        },
    ),
    (
        (-200, +170),
        {
            "multiplicative": [0.642857, 0.357143],
            "additive": [0.648148, 0.351852],
            "power": [0.650822, 0.349178],
            "shin": [0.648148, 0.351852],
        },
    ),
    (
        (-150, +130),
        {
            "multiplicative": [0.579832, 0.420168],
            "additive": [0.582609, 0.417391],
            "power": [0.583983, 0.416017],
            "shin": [0.582609, 0.417391],
        },
    ),
    (
        (-3000, +1200),
        {
            "multiplicative": [0.926366, 0.073634],
            "additive": [0.945409, 0.054591],
            "power": [0.959759, 0.040241],
            "shin": [0.945409, 0.054591],
        },
    ),
    (
        (+120, -140),
        {
            "multiplicative": [0.437956, 0.562044],
            "additive": [0.435606, 0.564394],
            "power": [0.434435, 0.565565],
            "shin": [0.435606, 0.564394],
        },
    ),
]

# A three-way market (not in the contract block; values checked independently with
# Newton's method for power and a brute-force scan + secant for Shin).
THREE_WAY_ODDS = (+150, +220, +190)
THREE_WAY_EXPECTED = {
    "multiplicative": [0.378312, 0.295556, 0.326131],
    "additive": [0.380891, 0.293391, 0.325718],
    "power": [0.380782, 0.293566, 0.325653],
    "shin": [0.380221, 0.293956, 0.325823],
}

DEVIG_FUNCS = {
    "multiplicative": multiplicative_devig,
    "additive": additive_devig,
    "power": power_devig,
    "shin": shin_devig,
}


def _raw(odds: tuple[int, ...]) -> list[float]:
    return [american_to_prob(o) for o in odds]


# ----------------------------------------------------------------------------- conversions


class TestAmericanToProb:
    # ARCHITECTURE.md vector block:
    # american_to_prob: -110 -> 0.523810; +150 -> 0.400000; -200 -> 0.666667; +100 -> 0.5
    @pytest.mark.parametrize(
        ("odds", "expected"),
        [(-110, 0.523810), (+150, 0.400000), (-200, 0.666667), (+100, 0.5)],
    )
    def test_contract_vectors(self, odds: int, expected: float) -> None:
        assert american_to_prob(odds) == pytest.approx(expected, abs=TOL)

    def test_minus_100_is_even_money(self) -> None:
        assert american_to_prob(-100) == 0.5

    @pytest.mark.parametrize("odds", [0, 1, -1, 50, -50, 99, -99])
    def test_rejects_odds_strictly_between_plus_minus_100(self, odds: int) -> None:
        with pytest.raises(ValueError):
            american_to_prob(odds)

    def test_raw_two_way_sums_exceed_one_when_vigged(self) -> None:
        # (-200,+170): raw [0.666667,0.370370]
        raw = _raw((-200, +170))
        assert raw == pytest.approx([0.666667, 0.370370], abs=TOL)
        assert sum(raw) > 1.0


class TestProbToAmerican:
    @pytest.mark.parametrize(
        ("p", "expected"),
        [(0.523810, -110), (0.400000, +150), (0.666667, -200), (0.5, +100), (0.9, -900)],
    )
    def test_known_prices(self, p: float, expected: int) -> None:
        assert prob_to_american(p) == expected
        assert isinstance(prob_to_american(p), int)

    @pytest.mark.parametrize("odds", [-110, +150, -200, +100, -3000, +1200, -105, +101])
    def test_round_trips_american_odds(self, odds: int) -> None:
        assert prob_to_american(american_to_prob(odds)) == odds

    @pytest.mark.parametrize("p", [0.0, 1.0, -0.1, 1.5, 2.0])
    def test_rejects_probabilities_outside_open_unit_interval(self, p: float) -> None:
        with pytest.raises(ValueError):
            prob_to_american(p)


class TestDecimalToProb:
    @pytest.mark.parametrize(
        ("d", "expected"), [(2.0, 0.5), (1.909091, 0.523810), (2.5, 0.4), (1.5, 0.666667)]
    )
    def test_known_prices(self, d: float, expected: float) -> None:
        assert decimal_to_prob(d) == pytest.approx(expected, abs=TOL)

    @pytest.mark.parametrize("d", [1.0, 0.5, 0.0, -2.0])
    def test_rejects_decimal_odds_at_or_below_one(self, d: float) -> None:
        with pytest.raises(ValueError):
            decimal_to_prob(d)


# ----------------------------------------------------------------------------- de-vig vectors


class TestDevigContractVectors:
    @pytest.mark.parametrize(("odds", "expected"), TWO_WAY_VECTORS, ids=str)
    @pytest.mark.parametrize("method", DEVIG_METHODS)
    def test_two_way_vector(
        self, odds: tuple[int, int], expected: dict[str, list[float]], method: str
    ) -> None:
        result = DEVIG_FUNCS[method](_raw(odds))
        assert result == pytest.approx(expected[method], abs=TOL)

    @pytest.mark.parametrize(("odds", "expected"), TWO_WAY_VECTORS, ids=str)
    @pytest.mark.parametrize("method", DEVIG_METHODS)
    def test_dispatcher_matches_direct_call(
        self, odds: tuple[int, int], expected: dict[str, list[float]], method: str
    ) -> None:
        assert devig(_raw(odds), method) == DEVIG_FUNCS[method](_raw(odds))

    @pytest.mark.parametrize("method", DEVIG_METHODS)
    def test_three_way_market(self, method: str) -> None:
        result = DEVIG_FUNCS[method](_raw(THREE_WAY_ODDS))
        assert len(result) == 3
        assert result == pytest.approx(THREE_WAY_EXPECTED[method], abs=TOL)
        assert sum(result) == pytest.approx(1.0, abs=SUM_TOL)

    def test_three_way_shin_differs_from_additive(self) -> None:
        # The two-way coincidence must not be an artifact of the implementation.
        raw = _raw(THREE_WAY_ODDS)
        assert shin_devig(raw) != pytest.approx(additive_devig(raw), abs=1e-4)

    def test_default_method_is_power(self) -> None:
        raw = _raw((-200, +170))
        assert devig(raw) == power_devig(raw)

    @pytest.mark.parametrize("method", ["", "Power", "mult", "vig", "espn"])
    def test_unknown_method_raises(self, method: str) -> None:
        with pytest.raises(ValueError):
            devig([0.55, 0.55], method)


# ----------------------------------------------------------------------------- de-vig properties

PROPERTY_MARKETS: list[tuple[int, ...]] = [
    (-110, -110),
    (-200, +170),
    (-150, +130),
    (-3000, +1200),
    (+120, -140),
    (-240, +195),
    (-105, -115),
    THREE_WAY_ODDS,
    (-120, +250, +400),
]


class TestDevigProperties:
    @pytest.mark.parametrize("odds", PROPERTY_MARKETS, ids=str)
    @pytest.mark.parametrize("method", DEVIG_METHODS)
    def test_outputs_sum_to_one_and_stay_in_unit_interval(
        self, odds: tuple[int, ...], method: str
    ) -> None:
        result = devig(_raw(odds), method)
        assert len(result) == len(odds)
        assert sum(result) == pytest.approx(1.0, abs=SUM_TOL)
        assert all(0.0 < p < 1.0 for p in result)

    @pytest.mark.parametrize("odds", PROPERTY_MARKETS, ids=str)
    @pytest.mark.parametrize("method", DEVIG_METHODS)
    def test_preserves_ordering_of_inputs(self, odds: tuple[int, ...], method: str) -> None:
        raw = _raw(odds)
        result = devig(raw, method)
        for i in range(len(raw)):
            for j in range(len(raw)):
                if raw[i] > raw[j]:
                    assert result[i] > result[j]
                elif raw[i] == raw[j]:
                    assert result[i] == pytest.approx(result[j], abs=1e-12)

    @pytest.mark.parametrize("method", DEVIG_METHODS)
    def test_monotone_in_inputs(self, method: str) -> None:
        # Shortening one outcome (raising its raw probability) must raise its fair
        # probability and lower every other outcome's.
        base = devig([0.52, 0.52], method)
        bumped = devig([0.56, 0.52], method)
        assert bumped[0] > base[0]
        assert bumped[1] < base[1]
        base3 = devig([0.40, 0.3125, 0.344828], method)
        bumped3 = devig([0.44, 0.3125, 0.344828], method)
        assert bumped3[0] > base3[0]
        assert bumped3[1] < base3[1]
        assert bumped3[2] < base3[2]

    @pytest.mark.parametrize("method", DEVIG_METHODS)
    def test_no_vig_is_identity(self, method: str) -> None:
        assert devig([0.5, 0.5], method) == pytest.approx([0.5, 0.5], abs=SUM_TOL)
        assert devig([0.7, 0.3], method) == pytest.approx([0.7, 0.3], abs=SUM_TOL)

    @pytest.mark.parametrize("odds", [o for o in PROPERTY_MARKETS if len(o) == 2], ids=str)
    def test_shin_equals_additive_for_two_way(self, odds: tuple[int, ...]) -> None:
        raw = _raw(odds)
        assert shin_devig(raw) == pytest.approx(additive_devig(raw), abs=1e-6)

    @pytest.mark.parametrize("odds", PROPERTY_MARKETS, ids=str)
    def test_power_exponent_exceeds_one_when_overround_positive(
        self, odds: tuple[int, ...]
    ) -> None:
        raw = _raw(odds)
        assert sum(raw) > 1.0
        k = power_exponent(raw)
        assert k > 1.0
        assert sum(p**k for p in raw) == pytest.approx(1.0, abs=1e-10)

    def test_power_exponent_below_one_for_underround(self) -> None:
        # Books never post this, but the bracket [0.5, 5] covers it and k < 1 is the
        # only way to inflate probabilities that sum to less than 1.
        assert power_exponent([0.45, 0.45]) < 1.0
        assert sum(power_devig([0.45, 0.45])) == pytest.approx(1.0, abs=SUM_TOL)

    @pytest.mark.parametrize("odds", PROPERTY_MARKETS, ids=str)
    def test_shin_z_is_a_small_positive_insider_share(self, odds: tuple[int, ...]) -> None:
        z = shin_z(_raw(odds))
        assert 0.0 < z < 0.5

    def test_shin_rejects_underround(self) -> None:
        with pytest.raises(ValueError):
            shin_devig([0.45, 0.45])

    def test_power_favours_favorites_more_than_multiplicative(self) -> None:
        # The whole point of the power method: with k > 1 the long shot gives up more
        # of the vig than the favorite does.
        raw = _raw((-3000, +1200))
        assert power_devig(raw)[0] > multiplicative_devig(raw)[0]
        assert power_devig(raw)[1] < multiplicative_devig(raw)[1]

    @pytest.mark.parametrize("method", DEVIG_METHODS)
    def test_tolerance_argument_is_honoured(self, method: str) -> None:
        raw = _raw((-200, +170))
        if method in ("power", "shin"):
            loose = DEVIG_FUNCS[method](raw, tol=1e-3)
            tight = DEVIG_FUNCS[method](raw, tol=1e-12)
            assert abs(sum(tight) - 1.0) <= 1e-12
            assert abs(sum(loose) - 1.0) <= 1e-3
        else:
            assert sum(DEVIG_FUNCS[method](raw)) == pytest.approx(1.0, abs=1e-12)

    @pytest.mark.parametrize("method", DEVIG_METHODS)
    @pytest.mark.parametrize("bad", [[], [0.6], [0.0, 0.6], [1.0, 0.2], [-0.1, 0.9], [0.5, 1.2]])
    def test_rejects_degenerate_inputs(self, method: str, bad: list[float]) -> None:
        with pytest.raises(ValueError):
            devig(bad, method)

    @pytest.mark.parametrize("method", DEVIG_METHODS)
    def test_accepts_tuples_and_returns_list(self, method: str) -> None:
        result = devig((0.6, 0.434783), method)
        assert isinstance(result, list)
        assert len(result) == 2


# ----------------------------------------------------------------------------- consensus


class TestConsensus:
    # ARCHITECTURE.md vector block:
    # consensus([("pinnacle",0.58),("betonlineag",0.56),("draftkings",0.60)],
    #           {pinnacle:3, betonlineag:1.5, draftkings:1}) = 0.578182
    def test_contract_vector(self) -> None:
        result = consensus(
            [("pinnacle", 0.58), ("betonlineag", 0.56), ("draftkings", 0.60)],
            {"pinnacle": 3, "betonlineag": 1.5, "draftkings": 1},
        )
        assert result is not None
        assert result.value == pytest.approx(0.578182, abs=TOL)

    def test_fair_prob_fields(self) -> None:
        result = consensus(
            [("pinnacle", 0.58), ("betonlineag", 0.56), ("draftkings", 0.60)],
            {"pinnacle": 3, "betonlineag": 1.5, "draftkings": 1},
            method="shin",
            line=-3.5,
        )
        assert isinstance(result, FairProb)
        assert result.method == "shin"
        assert result.line == -3.5
        assert result.n_books == 3
        # per_book and books_used are sorted by bookmaker, not in input order.
        assert result.books_used == ("betonlineag", "draftkings", "pinnacle")
        assert result.per_book == (("betonlineag", 0.56), ("draftkings", 0.60), ("pinnacle", 0.58))

    def test_empty_samples_return_none(self) -> None:
        assert consensus([], {"pinnacle": 3}) is None

    def test_missing_weights_use_default_weight(self) -> None:
        equal = consensus([("a", 0.50), ("b", 0.60)], {})
        assert equal is not None
        assert equal.value == pytest.approx(0.55, abs=1e-12)
        heavy_a = consensus([("a", 0.50), ("b", 0.60)], {"a": 3.0}, default_weight=1.0)
        assert heavy_a is not None
        assert heavy_a.value == pytest.approx((0.50 * 3 + 0.60) / 4, abs=1e-12)
        # default_weight applies to the unlisted book only.
        light_b = consensus([("a", 0.50), ("b", 0.60)], {"a": 1.0}, default_weight=0.5)
        assert light_b is not None
        assert light_b.value == pytest.approx((0.50 + 0.60 * 0.5) / 1.5, abs=1e-12)

    def test_zero_weight_excludes_the_book(self) -> None:
        result = consensus(
            [("pinnacle", 0.58), ("espn", 0.70), ("draftkings", 0.60)],
            {"pinnacle": 3, "espn": 0, "draftkings": 1},
        )
        assert result is not None
        assert result.value == pytest.approx((0.58 * 3 + 0.60) / 4, abs=1e-12)
        assert result.n_books == 2
        assert result.books_used == ("draftkings", "pinnacle")
        assert result.per_book == (("draftkings", 0.60), ("pinnacle", 0.58))

    def test_all_books_excluded_returns_none(self) -> None:
        assert consensus([("a", 0.5), ("b", 0.6)], {"a": 0, "b": 0}) is None
        assert consensus([("a", 0.5)], {}, default_weight=0.0) is None

    def test_single_book_is_its_own_consensus(self) -> None:
        result = consensus([("pinnacle", 0.58)], {"pinnacle": 3})
        assert result is not None
        assert result.value == pytest.approx(0.58, abs=1e-12)
        assert result.n_books == 1

    def test_weight_scaling_does_not_change_the_value(self) -> None:
        samples = [("a", 0.52), ("b", 0.57), ("c", 0.61)]
        one = consensus(samples, {"a": 1, "b": 2, "c": 3})
        ten = consensus(samples, {"a": 10, "b": 20, "c": 30})
        assert one is not None and ten is not None
        assert one.value == pytest.approx(ten.value, abs=1e-12)

    def test_value_stays_within_sample_range(self) -> None:
        result = consensus([("a", 0.52), ("b", 0.57), ("c", 0.61)], {"a": 5, "c": 0.1})
        assert result is not None
        assert 0.52 <= result.value <= 0.61

    def test_negative_weight_raises(self) -> None:
        with pytest.raises(ValueError):
            consensus([("a", 0.5), ("b", 0.6)], {"a": -1})
        with pytest.raises(ValueError):
            consensus([("a", 0.5)], {}, default_weight=-1.0)

    @pytest.mark.parametrize("prob", [0.0, 1.0, -0.2, 1.3])
    def test_rejects_probabilities_outside_unit_interval(self, prob: float) -> None:
        with pytest.raises(ValueError):
            consensus([("a", prob)], {})

    def test_unknown_method_label_raises(self) -> None:
        with pytest.raises(ValueError):
            consensus([("a", 0.5)], {}, method="magic")
