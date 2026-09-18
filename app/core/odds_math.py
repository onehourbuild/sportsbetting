"""Odds conversions, de-vig methods and weighted consensus. Contract: docs/ARCHITECTURE.md.

STUB — replaced by the math implementer. Every function raises NotImplementedError.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.core.types import FairProb

DEVIG_METHODS: tuple[str, ...] = ("multiplicative", "additive", "power", "shin")


def american_to_prob(odds: int) -> float:
    """Implied probability of American odds (-110 -> 0.523810, +150 -> 0.4)."""
    raise NotImplementedError


def prob_to_american(p: float) -> int:
    """Inverse of `american_to_prob` (rounded to an int)."""
    raise NotImplementedError


def decimal_to_prob(d: float) -> float:
    """Implied probability of decimal odds (2.0 -> 0.5)."""
    raise NotImplementedError


def multiplicative_devig(probs: Sequence[float]) -> list[float]:
    """Normalize so the probabilities sum to 1."""
    raise NotImplementedError


def additive_devig(probs: Sequence[float]) -> list[float]:
    """Subtract the overround equally from each outcome."""
    raise NotImplementedError


def power_devig(probs: Sequence[float], tol: float = 1e-10) -> list[float]:
    """Find k such that sum(p_i ** k) == 1 (favorite–longshot aware)."""
    raise NotImplementedError


def shin_devig(probs: Sequence[float], tol: float = 1e-10) -> list[float]:
    """Shin (1993) insider-trading model; coincides with additive for two-way markets."""
    raise NotImplementedError


def devig(probs: Sequence[float], method: str = "power") -> list[float]:
    """Dispatch on `method`; raises ValueError on an unknown method."""
    raise NotImplementedError


def consensus(
    samples: Sequence[tuple[str, float]],
    weights: Mapping[str, float],
    default_weight: float = 1.0,
    method: str = "power",
    line: float | None = None,
) -> FairProb | None:
    """Weighted mean of per-book de-vigged probabilities.

    Returns None when `samples` is empty. Books missing from `weights` use
    `default_weight`; a weight of 0 excludes the book.
    """
    raise NotImplementedError
