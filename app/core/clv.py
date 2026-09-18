"""Closing line value. Contract: docs/ARCHITECTURE.md.

CLV compares what a bet cost with the market's fair probability at close. Positive CLV
means the bet was placed at a better price than the closing consensus, which is the
most reliable long-run signal that the edge was real. Both functions are pure; `cost`
is the fee-inclusive price paid per share and must be > 0.
"""

from __future__ import annotations


def clv(closing_fair: float, cost: float) -> float:
    """CLV in probability points: closing_fair - cost."""
    return closing_fair - cost


def clv_decimal(closing_fair: float, cost: float) -> float:
    """CLV as a decimal-odds ratio: (1 / cost) / (1 / closing_fair) - 1 == closing_fair / cost - 1.

    Raises ValueError unless cost > 0.
    """
    if cost <= 0.0:
        raise ValueError(f"cost must be > 0, got {cost}")
    return closing_fair / cost - 1.0


__all__ = ["clv", "clv_decimal"]
