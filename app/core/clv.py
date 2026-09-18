"""Closing line value. STUB — replaced by the math implementer."""

from __future__ import annotations


def clv(closing_fair: float, cost: float) -> float:
    """closing_fair - cost (probability points)."""
    raise NotImplementedError


def clv_decimal(closing_fair: float, cost: float) -> float:
    """CLV expressed as a decimal-odds ratio: (1 / cost) / (1 / closing_fair) - 1."""
    raise NotImplementedError
