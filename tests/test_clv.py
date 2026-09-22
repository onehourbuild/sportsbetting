"""Tests for app/core/clv.py."""

from __future__ import annotations

import pytest

from app.core.clv import clv, clv_decimal
from app.core.edge import edge, ev_per_dollar

TOL = 1e-5


class TestClv:
    def test_positive_clv_when_the_line_moved_our_way(self) -> None:
        # Bought at 0.5125 all-in; the market closed at a fair 0.55.
        assert clv(0.55, 0.5125) == pytest.approx(0.0375, abs=TOL)

    def test_negative_clv_when_the_line_moved_against_us(self) -> None:
        assert clv(0.48, 0.5125) == pytest.approx(-0.0325, abs=TOL)

    def test_zero_when_close_equals_cost(self) -> None:
        assert clv(0.5125, 0.5125) == 0.0

    def test_is_the_same_arithmetic_as_edge(self) -> None:
        # CLV is just edge measured at the close instead of at bet time.
        for closing, cost in ((0.55, 0.5125), (0.31, 0.3105), (0.9, 0.952375)):
            assert clv(closing, cost) == edge(closing, cost)


class TestClvDecimal:
    def test_matches_the_contract_formula(self) -> None:
        # (1 / cost) / (1 / closing_fair) - 1
        for closing, cost in ((0.55, 0.5125), (0.31, 0.3105), (0.9, 0.952375), (0.05, 0.052375)):
            expected = (1 / cost) / (1 / closing) - 1
            assert clv_decimal(closing, cost) == pytest.approx(expected, abs=1e-12)

    def test_known_value(self) -> None:
        # Same numbers as the ev_per_dollar contract vector: 0.55 / 0.5125 - 1 = 0.073171.
        assert clv_decimal(0.55, 0.5125) == pytest.approx(0.073171, abs=TOL)
        assert clv_decimal(0.55, 0.5125) == pytest.approx(ev_per_dollar(0.55, 0.5125), abs=1e-12)

    def test_sign_agrees_with_probability_clv(self) -> None:
        for closing, cost in ((0.55, 0.5125), (0.48, 0.5125), (0.5125, 0.5125)):
            points = clv(closing, cost)
            ratio = clv_decimal(closing, cost)
            assert (points > 0) == (ratio > 0)
            assert (points == 0) == (ratio == 0)

    def test_larger_for_long_shots_at_equal_point_move(self) -> None:
        # A 3-point move is worth relatively more on a 0.10 share than on a 0.60 share.
        assert clv_decimal(0.13, 0.10) > clv_decimal(0.63, 0.60)

    @pytest.mark.parametrize("cost", [0.0, -0.5])
    def test_rejects_non_positive_cost(self, cost: float) -> None:
        with pytest.raises(ValueError):
            clv_decimal(0.5, cost)
