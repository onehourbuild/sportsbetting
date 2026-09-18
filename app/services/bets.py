"""Bet ledger: create, settle, closing capture, summary.

Contract: docs/ARCHITECTURE.md "Services API".

Money conventions (see docs/DECISIONS.md "Bet stake and P&L convention"):
- `stake_usd` is the total cash out of pocket, fee included. A taker paying the ask `a`
  with fee rate `r` pays `a + r*a*(1-a)` per share, so `shares = stake / cost` and
  `fee_usd = stake - shares * a`. A maker pays exactly the limit price and no fee.
- A winning share pays $1, so `pnl_won = shares - stake_usd`, `pnl_lost = -stake_usd`,
  and a void returns the stake (`pnl = 0`). A *push* is different from a void: Polymarket
  resolves a cancelled or postponed game 50/50, paying $0.50 a share, so
  `pnl_push = 0.5 * shares - stake_usd` (a loss of the fee and of any premium paid).
- `edge_at_bet = fair_at_bet - cost_per_share`; CLV = `closing_fair - cost_per_share`.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clv as clv_math
from app.core.edge import taker_fee_per_share
from app.core.matching import fair_for_outcome
from app.core.types import PmMarket
from app.models import Bet, Game, Market, Opportunity, PmQuote, utcnow
from app.services.adapters import RESOLVED_LETTER, pm_market_from_row, stored_book_game
from app.services.prefs import get_prefs

log = logging.getLogger(__name__)

BET_MODES: frozenset[str] = frozenset({"taker", "maker"})
# "void" means an order that never filled (stake returned, P&L 0); "push" means Polymarket
# resolved the market 50/50 (a cancelled or postponed game), which pays PUSH_PAYOUT a share.
SETTLE_RESULTS: frozenset[str] = frozenset({"won", "lost", "void", "push"})
DECIDED_STATUSES: frozenset[str] = frozenset({"won", "lost", "push"})
PUSH_PAYOUT = 0.5
PUSH_PRICE_TOL = 1e-6


# --------------------------------------------------------------------------- helpers


def _finite(label: str, value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def fee_rate_for(opportunity: Opportunity, default_rate: float) -> float:
    """The taker fee rate the opportunity was priced with (honours a per-market override
    recorded in `effective_price`); falls back to the preference."""
    ask = opportunity.ask
    if ask is None or not 0.0 < ask < 1.0 or opportunity.effective_price is None:
        return default_rate
    implied = (opportunity.effective_price - ask) / (ask * (1.0 - ask))
    if 0.0 <= implied < 0.2 and abs(implied - default_rate) > 1e-9:
        return round(implied, 6)
    return default_rate


def latest_best_ask(session: Session, token: str) -> float | None:
    """Best ask of the newest stored Polymarket quote for `token` (None when unknown)."""
    quote = session.scalars(
        select(PmQuote)
        .where(PmQuote.token == token, PmQuote.best_ask.is_not(None))
        .order_by(PmQuote.fetched_at.desc(), PmQuote.id.desc())
        .limit(1)
    ).first()
    return quote.best_ask if quote is not None else None


def cost_per_share(bet: Bet) -> float:
    """Fee-inclusive cost per share actually paid (stake / shares); the price for makers."""
    if bet.shares and bet.shares > 0 and bet.stake_usd is not None:
        return bet.stake_usd / bet.shares
    return bet.price


def _apply_result(bet: Bet, result: str, when: datetime) -> None:
    if result == "won":
        pnl = bet.shares - bet.stake_usd
    elif result == "lost":
        pnl = -bet.stake_usd
    elif result == "push":
        # A 50/50 resolution pays $0.50 a share: the stake does NOT come back whole.
        pnl = PUSH_PAYOUT * bet.shares - bet.stake_usd
    else:
        pnl = 0.0
    bet.status = result
    bet.pnl_usd = round(pnl, 2)
    bet.settled_at = when


def is_push(market: PmMarket) -> bool:
    """Does this closed market carry Polymarket's 50/50 (cancelled / postponed) resolution?

    `outcomePrices` settles to ["0.5", "0.5"] rather than ["1","0"] / ["0","1"], so the
    client reports no `resolved_outcome_index` and the bet would otherwise stay open for
    ever. Only a closed market counts: 0.50/0.50 on a live market is just an even price.
    """
    if not market.closed or market.resolved_outcome_index is not None:
        return False
    prices = [outcome.last_price for outcome in market.outcomes]
    return len(prices) == 2 and all(
        price is not None and abs(price - PUSH_PAYOUT) <= PUSH_PRICE_TOL for price in prices
    )


# --------------------------------------------------------------------------- create


def create_bet(
    session: Session,
    opportunity_id: int,
    stake_usd: float,
    price: float,
    mode: str,
    notes: str = "",
    now: datetime | None = None,
) -> Bet:
    """Log a bet from an Opportunity row: shares, fee, fair/edge at bet time; mode taker|maker.

    `now` (optional, additive to the contract) stamps `placed_at`; it defaults to the wall
    clock, so existing call sites are unchanged.
    """
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise ValueError(f"opportunity {opportunity_id} not found")
    market = session.get(Market, opportunity.market_id)
    if market is None:
        raise ValueError(f"market {opportunity.market_id} not found")
    if market.closed:
        raise ValueError("market is closed")

    mode = (mode or "").strip().lower()
    if mode not in BET_MODES:
        raise ValueError("mode must be taker or maker")
    stake = _finite("stake", stake_usd)
    if stake <= 0.0:
        raise ValueError("stake must be greater than 0")
    price_value = _finite("price", price)
    if not 0.0 < price_value < 1.0:
        raise ValueError("price must be between 0 and 1 (exclusive)")

    prefs = get_prefs(session)
    if mode == "taker":
        rate = fee_rate_for(opportunity, prefs.taker_fee_rate)
        fee_per_share = taker_fee_per_share(price_value, rate)
        cost = price_value + fee_per_share
        shares = stake / cost
        fee_usd = stake - shares * price_value
    else:
        # A resting order must sit below the best ask; at or above it the order crosses
        # the book and fills as a taker (fee included), so the fee-free maker maths would
        # record a fictitious price.
        ask = latest_best_ask(session, opportunity.token)
        if ask is not None and price_value >= ask:
            raise ValueError(
                f"a maker order at {price_value * 100:.0f}¢ would cross the current "
                f"{ask * 100:.0f}¢ ask: lower the price or log it as a taker"
            )
        cost = price_value
        shares = stake / price_value
        fee_usd = 0.0

    fair = opportunity.fair_prob
    bet = Bet(
        market_id=market.id,
        token=opportunity.token,
        outcome_key=opportunity.outcome_key,
        outcome_name=opportunity.outcome_name,
        mode=mode,
        price=price_value,
        shares=round(shares, 6),
        stake_usd=round(stake, 2),
        fee_usd=round(max(fee_usd, 0.0), 4),
        fair_at_bet=fair,
        edge_at_bet=(fair - cost) if fair is not None else None,
        placed_at=now or utcnow(),
        status="open",
        notes=(notes or "").strip(),
    )
    session.add(bet)
    session.commit()
    session.refresh(bet)
    return bet


# --------------------------------------------------------------------------- settle


def settle_bet_manual(
    session: Session, bet_id: int, result: str, now: datetime | None = None
) -> Bet:
    """Manual settle: result in {"won", "lost", "void", "push"}; sets pnl_usd and settled_at.

    "void" is an order that never filled (P&L 0); "push" is a 50/50 resolution that pays
    $0.50 a share. `now` (optional, additive to the contract) stamps `settled_at` and
    defaults to the wall clock.
    """
    bet = session.get(Bet, bet_id)
    if bet is None:
        raise ValueError(f"bet {bet_id} not found")
    result = (result or "").strip().lower()
    if result not in SETTLE_RESULTS:
        raise ValueError("result must be won, lost, void or push")
    if bet.status != "open":
        raise ValueError(f"bet {bet_id} is already settled as {bet.status}")
    _apply_result(bet, result, now or utcnow())
    session.commit()
    session.refresh(bet)
    return bet


def settle_open_bets(
    session: Session,
    markets_by_id: Mapping[str, PmMarket],
    now: datetime | None = None,
) -> int:
    """Settle every open Bet whose market is closed with a resolved outcome; returns count.

    A market closed with the 50/50 resolution Polymarket uses for a cancelled or postponed
    game settles as a "push" (see `is_push`): it has no winning outcome, so without this it
    would never settle at all and the owner would have to void it by hand for a P&L of 0,
    when a push actually pays $0.50 a share.

    Also marks the `Market` row closed/resolved. `now` (optional, additive to the
    contract) is the settlement timestamp; defaults to the wall clock.
    """
    when = now or utcnow()
    settled = 0
    open_bets = list(session.scalars(select(Bet).where(Bet.status == "open")))
    for bet in open_bets:
        market = markets_by_id.get(bet.market_id)
        if market is None or not market.closed:
            continue
        index = market.resolved_outcome_index
        push = index is None and is_push(market)
        if index is None and not push:
            continue
        tokens = [o.token_id for o in market.outcomes]
        if bet.token not in tokens:
            log.warning(
                "bet %s token is not an outcome of market %s; skipped", bet.id, bet.market_id
            )
            continue
        if push:
            _apply_result(bet, "push", when)
        else:
            _apply_result(bet, "won" if bet.token == tokens[index] else "lost", when)
        settled += 1
        row = session.get(Market, bet.market_id)
        if row is not None:
            row.closed = True
            row.accepting_orders = False
            # A push has no winning outcome, so resolved_outcome stays NULL.
            row.resolved_outcome = None if push else RESOLVED_LETTER.get(index)
    if settled:
        session.commit()
    return settled


# --------------------------------------------------------------------------- closing line


def _closing_fair(
    session: Session, scan_id: int, bet: Bet, market: Market, game: Game
) -> float | None:
    """Closing fair probability for the bet's outcome: recomputed with the configured
    de-vig method and book weights from the last stored book snapshot fetched at or
    before the game's start time (never from in-play odds fetched after kickoff)."""
    book_game, _ = stored_book_game(session, game, up_to_scan_id=scan_id, not_after=game.start_time)
    if book_game is None:
        return None
    if bet.token == market.outcome_a_token:
        index = 0
    elif bet.token == market.outcome_b_token:
        index = 1
    else:
        return None
    prefs = get_prefs(session)
    try:
        fair = fair_for_outcome(
            pm_market_from_row(market, game),
            index,
            book_game,
            prefs.book_weights,
            prefs.devig_method,
        )
    except ValueError as exc:
        log.warning("closing fair for bet %s failed: %s", bet.id, exc)
        return None
    return fair.value if fair is not None else None


def capture_closing(session: Session, scan_id: int, now: datetime) -> int:
    """For bets whose game has started and that lack closing data, store the closing fair
    probability and closing Polymarket price and compute CLV; returns count.

    "Closing" means the last snapshot taken *before* kickoff: the book snapshot with the
    greatest `fetched_at <= game.start_time` (up to this scan) and the Polymarket quote
    for the bet's token with the greatest `fetched_at <= game.start_time`. Nothing
    fetched after the start is used, so a scan run during or after the game records the
    pre-game close rather than the in-play state. Open, won and lost bets are all
    eligible (a bet settled by the same scan, or by hand, still gets its CLV); voids are
    left alone. The first capture wins.
    """
    captured = 0
    stmt = select(Bet).where(Bet.status != "void", Bet.closing_fair.is_(None))
    for bet in list(session.scalars(stmt)):
        market = session.get(Market, bet.market_id)
        if market is None or market.game_id is None:
            continue
        game = session.get(Game, market.game_id)
        if game is None or game.start_time is None or game.start_time > now:
            continue
        closing_fair = _closing_fair(session, scan_id, bet, market, game)
        if closing_fair is None:
            continue
        quote = session.scalars(
            select(PmQuote)
            .where(
                PmQuote.token == bet.token,
                PmQuote.scan_id <= scan_id,
                PmQuote.fetched_at <= game.start_time,
                PmQuote.best_ask.is_not(None),
            )
            .order_by(PmQuote.fetched_at.desc(), PmQuote.id.desc())
        ).first()
        bet.closing_fair = closing_fair
        bet.closing_pm_price = quote.best_ask if quote is not None else None
        bet.clv = clv_math.clv(closing_fair, cost_per_share(bet))
        captured += 1
    if captured:
        session.commit()
    return captured


# --------------------------------------------------------------------------- summary


def ledger_summary(session: Session) -> dict:
    """Keys: n_open, n_settled, n_won, n_lost, n_push, total_staked, total_pnl, roi,
    avg_clv, n_clv_positive, n_clv_recorded.

    `total_staked` and `roi` cover decided bets only (won, lost and pushed — a push risked
    the money and returned only half a dollar a share); a void never filled, so it returns
    the stake and counts in neither. `avg_clv` is None until at least one bet has closing
    data. `n_push` is additive (review round); see docs/notes_pricing.md.
    """
    bets = list(session.scalars(select(Bet)))
    open_bets = [b for b in bets if b.status == "open"]
    settled = [b for b in bets if b.status != "open"]
    won = [b for b in settled if b.status == "won"]
    lost = [b for b in settled if b.status == "lost"]
    decided = [b for b in settled if b.status in DECIDED_STATUSES]
    total_staked = sum(b.stake_usd for b in decided)
    total_pnl = sum(b.pnl_usd or 0.0 for b in settled)
    clvs = [b.clv for b in bets if b.clv is not None]
    return {
        "n_open": len(open_bets),
        "n_settled": len(settled),
        "n_won": len(won),
        "n_lost": len(lost),
        "n_push": sum(1 for b in settled if b.status == "push"),
        "total_staked": round(total_staked, 2),
        "total_pnl": round(total_pnl, 2),
        "roi": (total_pnl / total_staked) if total_staked > 0 else 0.0,
        "avg_clv": (sum(clvs) / len(clvs)) if clvs else None,
        "n_clv_positive": sum(1 for c in clvs if c > 0),
        "n_clv_recorded": len(clvs),
    }


__all__ = [
    "BET_MODES",
    "DECIDED_STATUSES",
    "PUSH_PAYOUT",
    "SETTLE_RESULTS",
    "capture_closing",
    "cost_per_share",
    "create_bet",
    "fee_rate_for",
    "is_push",
    "latest_best_ask",
    "ledger_summary",
    "settle_bet_manual",
    "settle_open_bets",
]
