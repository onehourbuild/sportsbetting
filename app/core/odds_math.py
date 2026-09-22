"""Odds conversions, de-vig methods and weighted consensus. Contract: docs/ARCHITECTURE.md.

Everything here is pure arithmetic on floats. There is no I/O and no randomness, so
every function is reproducible from its arguments alone. Invalid inputs raise
``ValueError`` rather than returning a plausible-looking number: a bettor should
never be handed a probability that came from garbage.

Conventions
-----------
* "Raw" or "implied" probabilities are what a bookmaker's price implies *before*
  removing the margin. For a full market they sum to ``S >= 1``; ``S - 1`` is the
  overround (the vig).
* De-vig functions take the raw probabilities of **every** outcome of one market
  from **one** bookmaker and return fair probabilities that sum to 1.
* ``consensus`` takes per-book fair probabilities for **one** outcome and averages
  them; it does not de-vig (that already happened per book).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence

from app.core.types import FairProb

DEVIG_METHODS: tuple[str, ...] = ("multiplicative", "additive", "power", "shin")

# Bisection brackets from the contract. Power: exponent k; Shin: insider share z.
POWER_K_RANGE: tuple[float, float] = (0.5, 5.0)
SHIN_Z_RANGE: tuple[float, float] = (0.0, 0.5)
_MAX_BISECT_ITER = 200  # far more than needed to exhaust double precision


# ----------------------------------------------------------------------------- conversions


def american_to_prob(odds: int) -> float:
    """Implied probability of American odds (-110 -> 0.523810, +150 -> 0.4, +100 -> 0.5).

    Raises ValueError for odds strictly between -100 and +100 (no such price exists).
    """
    if -100 < odds < 100:
        raise ValueError(f"American odds must be <= -100 or >= +100, got {odds}")
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def prob_to_american(p: float) -> int:
    """Inverse of `american_to_prob`, rounded half-up to an integer.

    p > 0.5 gives a negative (favorite) price; p <= 0.5 gives a positive price, so
    exactly 0.5 round-trips to +100. Raises ValueError unless 0 < p < 1.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"probability must be in (0, 1), got {p}")
    if p > 0.5:
        return -_round_half_up(100.0 * p / (1.0 - p))
    return _round_half_up(100.0 * (1.0 - p) / p)


def decimal_to_prob(d: float) -> float:
    """Implied probability of decimal odds (2.0 -> 0.5). Raises ValueError unless d > 1."""
    if not d > 1.0:
        raise ValueError(f"decimal odds must be > 1.0, got {d}")
    return 1.0 / d


def _round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


# ----------------------------------------------------------------------------- de-vig


def _validate_probs(probs: Sequence[float]) -> list[float]:
    """A market needs at least two outcomes, each with a probability strictly in (0, 1)."""
    values = [float(p) for p in probs]
    if len(values) < 2:
        raise ValueError(f"de-vig needs at least two outcomes, got {len(values)}")
    for p in values:
        if not 0.0 < p < 1.0:
            raise ValueError(f"raw probabilities must be in (0, 1), got {p}")
    return values


def multiplicative_devig(probs: Sequence[float]) -> list[float]:
    """Scale every outcome by the same factor so the probabilities sum to 1."""
    values = _validate_probs(probs)
    total = sum(values)
    return [p / total for p in values]


def additive_devig(probs: Sequence[float]) -> list[float]:
    """Subtract an equal share of the overround from each outcome.

    Note: with a large overround and a long shot this can drive the long shot
    negative; the contract keeps the pure formula, so callers that care should
    prefer ``power`` (the default) for such markets.
    """
    values = _validate_probs(probs)
    overround_share = (sum(values) - 1.0) / len(values)
    return [p - overround_share for p in values]


def power_exponent(probs: Sequence[float], tol: float = 1e-10) -> float:
    """The exponent k in [0.5, 5] with sum(p_i ** k) == 1 (within tol), found by bisection.

    k > 1 whenever the market carries an overround: raising probabilities to a power
    above 1 shrinks long shots proportionally more than favorites, which is the
    favorite-longshot bias the power method is built to remove.
    """
    values = _validate_probs(probs)

    def excess(k: float) -> float:
        return sum(p**k for p in values) - 1.0

    lo, hi = POWER_K_RANGE
    return _bisect(
        excess,
        lo,
        hi,
        tol,
        what=f"power de-vig: no exponent in [{lo}, {hi}] normalizes {values}",
    )


def power_devig(probs: Sequence[float], tol: float = 1e-10) -> list[float]:
    """Find k such that sum(p_i ** k) == 1 and return [p_i ** k] (favorite-longshot aware)."""
    k = power_exponent(probs, tol)
    return [float(p) ** k for p in probs]


def shin_z(probs: Sequence[float], tol: float = 1e-10) -> float:
    """Shin's insider share z in [0, 0.5] such that the Shin probabilities sum to 1.

    Raises ValueError when the raw probabilities sum to less than 1 (no overround):
    Shin's model only describes markets with a margin.
    """
    values = _validate_probs(probs)
    total = sum(values)
    if total < 1.0:
        raise ValueError(f"shin de-vig needs an overround (sum >= 1), got sum {total}")

    def excess(z: float) -> float:
        return sum(_shin_probs(values, total, z)) - 1.0

    lo, hi = SHIN_Z_RANGE
    return _bisect(
        excess,
        lo,
        hi,
        tol,
        what=f"shin de-vig: no z in [{lo}, {hi}] normalizes {values}",
    )


def shin_devig(probs: Sequence[float], tol: float = 1e-10) -> list[float]:
    """Shin (1993) insider-trading model; coincides with additive for two-way markets.

    p_i = (sqrt(z^2 + 4 (1 - z) pi_i^2 / S) - z) / (2 (1 - z)), with S = sum(pi_i) and z
    chosen by bisection so that the p_i sum to 1.
    """
    values = _validate_probs(probs)
    z = shin_z(values, tol)
    return _shin_probs(values, sum(values), z)


def _shin_probs(values: Sequence[float], total: float, z: float) -> list[float]:
    denominator = 2.0 * (1.0 - z)
    return [(math.sqrt(z * z + 4.0 * (1.0 - z) * p * p / total) - z) / denominator for p in values]


def _bisect(
    f: Callable[[float], float],
    lo: float,
    hi: float,
    tol: float,
    *,
    what: str,
) -> float:
    """Root of a monotone f on [lo, hi]: |f(root)| <= tol. Raises ValueError without a bracket."""
    f_lo, f_hi = f(lo), f(hi)
    if abs(f_lo) <= tol:
        return lo
    if abs(f_hi) <= tol:
        return hi
    if (f_lo > 0.0) == (f_hi > 0.0):
        raise ValueError(what)
    for _ in range(_MAX_BISECT_ITER):
        mid = (lo + hi) / 2.0
        if mid in (lo, hi):
            break  # the bracket can no longer be split in double precision
        f_mid = f(mid)
        if abs(f_mid) <= tol:
            return mid
        if (f_mid > 0.0) == (f_lo > 0.0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid
    return (lo + hi) / 2.0


_DEVIG_DISPATCH: dict[str, Callable[[Sequence[float]], list[float]]] = {
    "multiplicative": multiplicative_devig,
    "additive": additive_devig,
    "power": power_devig,
    "shin": shin_devig,
}


def devig(probs: Sequence[float], method: str = "power") -> list[float]:
    """Dispatch on `method` (one of DEVIG_METHODS); raises ValueError on an unknown method."""
    fn = _DEVIG_DISPATCH.get(method)
    if fn is None:
        raise ValueError(f"unknown devig method {method!r}; expected one of {DEVIG_METHODS}")
    return fn(probs)


# ----------------------------------------------------------------------------- consensus


def consensus(
    samples: Sequence[tuple[str, float]],
    weights: Mapping[str, float],
    default_weight: float = 1.0,
    method: str = "power",
    line: float | None = None,
) -> FairProb | None:
    """Weighted mean of per-book de-vigged probabilities for one outcome.

    `samples` are (bookmaker, fair probability) pairs that have already been de-vigged
    with `method`; `method` and `line` are recorded on the result for display and audit.
    Books missing from `weights` use `default_weight`; a weight of 0 excludes the book.
    Returns None when `samples` is empty or every book is excluded. `per_book` and
    `books_used` are sorted by bookmaker so equal inputs give equal outputs.
    """
    if method not in _DEVIG_DISPATCH:
        raise ValueError(f"unknown devig method {method!r}; expected one of {DEVIG_METHODS}")
    if default_weight < 0.0:
        raise ValueError(f"default_weight must be >= 0, got {default_weight}")

    used: list[tuple[str, float, float]] = []  # (bookmaker, prob, weight)
    for bookmaker, prob in samples:
        if not 0.0 < prob < 1.0:
            raise ValueError(f"{bookmaker}: probability must be in (0, 1), got {prob}")
        weight = float(weights.get(bookmaker, default_weight))
        if weight < 0.0:
            raise ValueError(f"{bookmaker}: weight must be >= 0, got {weight}")
        if weight == 0.0:
            continue
        used.append((bookmaker, float(prob), weight))

    if not used:
        return None

    used.sort(key=lambda item: (item[0], item[1]))
    total_weight = sum(weight for _, _, weight in used)
    value = sum(prob * weight for _, prob, weight in used) / total_weight
    return FairProb(
        value=value,
        method=method,
        n_books=len(used),
        books_used=tuple(bookmaker for bookmaker, _, _ in used),
        line=line,
        per_book=tuple((bookmaker, prob) for bookmaker, prob, _ in used),
    )


__all__ = [
    "DEVIG_METHODS",
    "POWER_K_RANGE",
    "SHIN_Z_RANGE",
    "additive_devig",
    "american_to_prob",
    "consensus",
    "decimal_to_prob",
    "devig",
    "multiplicative_devig",
    "power_devig",
    "power_exponent",
    "prob_to_american",
    "shin_devig",
    "shin_z",
]
