"""Tests for app/core/matching.py: team resolution, market-to-game matching, fair probs.

The team tables below are written independently of the implementation (they are the
Odds API names and nicknames as the acceptance criteria list them), so a typo in
`TEAM_ALIASES` cannot hide behind itself.

`fair_for_outcome` calls `app.core.odds_math`, which is implemented in a parallel step.
While that module is still a stub (raises NotImplementedError) the autouse fixture below
swaps in a small reference implementation that reproduces the hand-checked vectors from
docs/ARCHITECTURE.md; once the real module lands these tests run against it unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from app.core import odds_math
from app.core.matching import (
    DIFFERENT_GAME_DAY,
    TEAM_ALIASES,
    fair_for_outcome,
    match_games,
    normalize_name,
    team_key,
    unmatched_reasons,
)
from app.core.types import (
    BookGame,
    BookMarket,
    BookOutcome,
    BookQuote,
    FairProb,
    PmMarket,
    PmOutcome,
)

TOL = 1e-5

# --------------------------------------------------------------------------- reference math


def _ref_american_to_prob(odds: int) -> float:
    odds = int(odds)
    return (-odds) / (-odds + 100) if odds < 0 else 100 / (odds + 100)


def _ref_power_devig(probs: Sequence[float]) -> list[float]:
    lo, hi = 0.0, 50.0
    for _ in range(300):
        mid = (lo + hi) / 2
        if sum(p**mid for p in probs) > 1:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2
    return [p**k for p in probs]


def _ref_devig(probs: Sequence[float], method: str = "power") -> list[float]:
    if method == "power":
        return _ref_power_devig(probs)
    if method == "multiplicative":
        total = sum(probs)
        return [p / total for p in probs]
    if method in ("additive", "shin"):
        overround = (sum(probs) - 1) / len(probs)
        return [p - overround for p in probs]
    raise ValueError(f"unknown devig method {method!r}")


def _ref_consensus(
    samples: Sequence[tuple[str, float]],
    weights: Mapping[str, float],
    default_weight: float = 1.0,
    method: str = "power",
    line: float | None = None,
) -> FairProb | None:
    used = [(b, p, float(weights.get(b, default_weight))) for b, p in samples]
    used = [(b, p, w) for b, p, w in used if w > 0]
    if not used:
        return None
    total = sum(w for _, _, w in used)
    value = sum(p * w for _, p, w in used) / total
    return FairProb(
        value=value,
        method=method,
        n_books=len(used),
        books_used=tuple(b for b, _, _ in used),
        line=line,
        per_book=tuple((b, p) for b, p, _ in used),
    )


def _is_stub(fn, *args) -> bool:
    try:
        fn(*args)
    except NotImplementedError:
        return True
    except Exception:  # noqa: BLE001 - any other behavior means it is implemented
        return False
    return False


@pytest.fixture(autouse=True)
def _math_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the real odds_math when implemented; otherwise the reference above."""
    if _is_stub(odds_math.american_to_prob, -110):
        monkeypatch.setattr(odds_math, "american_to_prob", _ref_american_to_prob)
    if _is_stub(odds_math.devig, [0.52, 0.52], "power"):
        monkeypatch.setattr(odds_math, "devig", _ref_devig)
    if _is_stub(odds_math.consensus, [("pinnacle", 0.5)], {}):
        monkeypatch.setattr(odds_math, "consensus", _ref_consensus)


def test_reference_math_reproduces_contract_vectors() -> None:
    """Guards the stand-in itself so a wrong reference cannot make matching tests lie."""
    assert _ref_american_to_prob(-110) == pytest.approx(0.523810, abs=TOL)
    assert _ref_american_to_prob(150) == pytest.approx(0.4, abs=TOL)
    raw = [_ref_american_to_prob(-200), _ref_american_to_prob(170)]
    assert _ref_power_devig(raw) == pytest.approx([0.650822, 0.349178], abs=TOL)
    raw = [_ref_american_to_prob(-150), _ref_american_to_prob(130)]
    assert _ref_power_devig(raw) == pytest.approx([0.583983, 0.416017], abs=TOL)
    fair = _ref_consensus(
        [("pinnacle", 0.58), ("betonlineag", 0.56), ("draftkings", 0.60)],
        {"pinnacle": 3, "betonlineag": 1.5, "draftkings": 1},
    )
    assert fair is not None and fair.value == pytest.approx(0.578182, abs=TOL)


# --------------------------------------------------------------------------- team tables

NFL_KEYS = (
    "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LV LAC LAR MIA MIN NE NO "
    "NYG NYJ PHI PIT SF SEA TB TEN WAS"
).split()
NBA_KEYS = (
    "ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL "
    "PHI PHX POR SAC SAS TOR UTA WAS"
).split()
MLB_KEYS = (
    "ARI ATL BAL BOS CHC CWS CIN CLE COL DET HOU KC LAA LAD MIA MIL MIN NYM NYY ATH PHI PIT SD "
    "SF SEA STL TB TEX TOR WSH"
).split()

# key -> (The Odds API full name, nickname)
NFL_TEAMS = {
    "ARI": ("Arizona Cardinals", "Cardinals"),
    "ATL": ("Atlanta Falcons", "Falcons"),
    "BAL": ("Baltimore Ravens", "Ravens"),
    "BUF": ("Buffalo Bills", "Bills"),
    "CAR": ("Carolina Panthers", "Panthers"),
    "CHI": ("Chicago Bears", "Bears"),
    "CIN": ("Cincinnati Bengals", "Bengals"),
    "CLE": ("Cleveland Browns", "Browns"),
    "DAL": ("Dallas Cowboys", "Cowboys"),
    "DEN": ("Denver Broncos", "Broncos"),
    "DET": ("Detroit Lions", "Lions"),
    "GB": ("Green Bay Packers", "Packers"),
    "HOU": ("Houston Texans", "Texans"),
    "IND": ("Indianapolis Colts", "Colts"),
    "JAX": ("Jacksonville Jaguars", "Jaguars"),
    "KC": ("Kansas City Chiefs", "Chiefs"),
    "LV": ("Las Vegas Raiders", "Raiders"),
    "LAC": ("Los Angeles Chargers", "Chargers"),
    "LAR": ("Los Angeles Rams", "Rams"),
    "MIA": ("Miami Dolphins", "Dolphins"),
    "MIN": ("Minnesota Vikings", "Vikings"),
    "NE": ("New England Patriots", "Patriots"),
    "NO": ("New Orleans Saints", "Saints"),
    "NYG": ("New York Giants", "Giants"),
    "NYJ": ("New York Jets", "Jets"),
    "PHI": ("Philadelphia Eagles", "Eagles"),
    "PIT": ("Pittsburgh Steelers", "Steelers"),
    "SF": ("San Francisco 49ers", "49ers"),
    "SEA": ("Seattle Seahawks", "Seahawks"),
    "TB": ("Tampa Bay Buccaneers", "Buccaneers"),
    "TEN": ("Tennessee Titans", "Titans"),
    "WAS": ("Washington Commanders", "Commanders"),
}
NBA_TEAMS = {
    "ATL": ("Atlanta Hawks", "Hawks"),
    "BOS": ("Boston Celtics", "Celtics"),
    "BKN": ("Brooklyn Nets", "Nets"),
    "CHA": ("Charlotte Hornets", "Hornets"),
    "CHI": ("Chicago Bulls", "Bulls"),
    "CLE": ("Cleveland Cavaliers", "Cavaliers"),
    "DAL": ("Dallas Mavericks", "Mavericks"),
    "DEN": ("Denver Nuggets", "Nuggets"),
    "DET": ("Detroit Pistons", "Pistons"),
    "GSW": ("Golden State Warriors", "Warriors"),
    "HOU": ("Houston Rockets", "Rockets"),
    "IND": ("Indiana Pacers", "Pacers"),
    "LAC": ("Los Angeles Clippers", "Clippers"),
    "LAL": ("Los Angeles Lakers", "Lakers"),
    "MEM": ("Memphis Grizzlies", "Grizzlies"),
    "MIA": ("Miami Heat", "Heat"),
    "MIL": ("Milwaukee Bucks", "Bucks"),
    "MIN": ("Minnesota Timberwolves", "Timberwolves"),
    "NOP": ("New Orleans Pelicans", "Pelicans"),
    "NYK": ("New York Knicks", "Knicks"),
    "OKC": ("Oklahoma City Thunder", "Thunder"),
    "ORL": ("Orlando Magic", "Magic"),
    "PHI": ("Philadelphia 76ers", "76ers"),
    "PHX": ("Phoenix Suns", "Suns"),
    "POR": ("Portland Trail Blazers", "Trail Blazers"),
    "SAC": ("Sacramento Kings", "Kings"),
    "SAS": ("San Antonio Spurs", "Spurs"),
    "TOR": ("Toronto Raptors", "Raptors"),
    "UTA": ("Utah Jazz", "Jazz"),
    "WAS": ("Washington Wizards", "Wizards"),
}
MLB_TEAMS = {
    "ARI": ("Arizona Diamondbacks", "Diamondbacks"),
    "ATL": ("Atlanta Braves", "Braves"),
    "BAL": ("Baltimore Orioles", "Orioles"),
    "BOS": ("Boston Red Sox", "Red Sox"),
    "CHC": ("Chicago Cubs", "Cubs"),
    "CWS": ("Chicago White Sox", "White Sox"),
    "CIN": ("Cincinnati Reds", "Reds"),
    "CLE": ("Cleveland Guardians", "Guardians"),
    "COL": ("Colorado Rockies", "Rockies"),
    "DET": ("Detroit Tigers", "Tigers"),
    "HOU": ("Houston Astros", "Astros"),
    "KC": ("Kansas City Royals", "Royals"),
    "LAA": ("Los Angeles Angels", "Angels"),
    "LAD": ("Los Angeles Dodgers", "Dodgers"),
    "MIA": ("Miami Marlins", "Marlins"),
    "MIL": ("Milwaukee Brewers", "Brewers"),
    "MIN": ("Minnesota Twins", "Twins"),
    "NYM": ("New York Mets", "Mets"),
    "NYY": ("New York Yankees", "Yankees"),
    "ATH": ("Athletics", "Athletics"),
    "PHI": ("Philadelphia Phillies", "Phillies"),
    "PIT": ("Pittsburgh Pirates", "Pirates"),
    "SD": ("San Diego Padres", "Padres"),
    "SF": ("San Francisco Giants", "Giants"),
    "SEA": ("Seattle Mariners", "Mariners"),
    "STL": ("St. Louis Cardinals", "Cardinals"),
    "TB": ("Tampa Bay Rays", "Rays"),
    "TEX": ("Texas Rangers", "Rangers"),
    "TOR": ("Toronto Blue Jays", "Blue Jays"),
    "WSH": ("Washington Nationals", "Nationals"),
}
ALL_TEAMS = {"nfl": NFL_TEAMS, "nba": NBA_TEAMS, "mlb": MLB_TEAMS}
TEAM_CASES = [
    pytest.param(league, key, odds_name, nickname, id=f"{league}-{key}")
    for league, table in ALL_TEAMS.items()
    for key, (odds_name, nickname) in table.items()
]


@pytest.mark.parametrize(
    "league,expected", [("nfl", NFL_KEYS), ("nba", NBA_KEYS), ("mlb", MLB_KEYS)]
)
def test_team_aliases_cover_every_canonical_key(league: str, expected: list[str]) -> None:
    assert sorted(TEAM_ALIASES[league]) == sorted(expected)
    assert len(TEAM_ALIASES[league]) == {"nfl": 32, "nba": 30, "mlb": 30}[league]


@pytest.mark.parametrize("league,key,odds_name,nickname", TEAM_CASES)
def test_every_team_resolves_from_odds_name_nickname_and_abbreviation(
    league: str, key: str, odds_name: str, nickname: str
) -> None:
    assert team_key(odds_name, league) == key  # The Odds API / ESPN displayName
    assert team_key(nickname, league) == key  # Polymarket outcome / ESPN shortDisplayName
    assert team_key(key, league) == key  # abbreviation
    assert team_key(key.lower(), league) == key  # slug style
    assert team_key(odds_name.upper(), league) == key
    assert TEAM_ALIASES[league][key] == odds_name


@pytest.mark.parametrize(
    "league,name,expected",
    [
        # NFL abbreviation and name variants
        ("nfl", "KAN", "KC"),
        ("nfl", "KC Chiefs", "KC"),
        ("nfl", "Kansas City", "KC"),
        ("nfl", "GNB", "GB"),
        ("nfl", "Green Bay", "GB"),
        ("nfl", "JAC", "JAX"),
        ("nfl", "Jags", "JAX"),
        ("nfl", "LVR", "LV"),
        ("nfl", "Las Vegas", "LV"),
        ("nfl", "Oakland Raiders", "LV"),
        ("nfl", "OAK", "LV"),
        ("nfl", "WSH", "WAS"),
        ("nfl", "Washington", "WAS"),
        ("nfl", "Washington Football Team", "WAS"),
        ("nfl", "NOR", "NO"),
        ("nfl", "New Orleans", "NO"),
        ("nfl", "TAM", "TB"),
        ("nfl", "Tampa Bay", "TB"),
        ("nfl", "Bucs", "TB"),
        ("nfl", "SFO", "SF"),
        ("nfl", "Niners", "SF"),
        ("nfl", "San Francisco", "SF"),
        ("nfl", "NY Giants", "NYG"),
        ("nfl", "NY Jets", "NYJ"),
        ("nfl", "LA Rams", "LAR"),
        ("nfl", "LA Chargers", "LAC"),
        ("nfl", "San Diego Chargers", "LAC"),
        ("nfl", "St. Louis Rams", "LAR"),
        ("nfl", "NWE", "NE"),
        ("nfl", "Pats", "NE"),
        ("nfl", "New England", "NE"),
        ("nfl", "ARZ", "ARI"),
        ("nfl", "Philly", "PHI"),
        # NBA
        ("nba", "NO Pelicans", "NOP"),
        ("nba", "NO", "NOP"),
        ("nba", "New Orleans", "NOP"),
        ("nba", "GS", "GSW"),
        ("nba", "Golden State", "GSW"),
        ("nba", "PHO", "PHX"),
        ("nba", "Phoenix", "PHX"),
        ("nba", "SA Spurs", "SAS"),
        ("nba", "SA", "SAS"),
        ("nba", "San Antonio", "SAS"),
        ("nba", "UTAH", "UTA"),
        ("nba", "Utah", "UTA"),
        ("nba", "BRK", "BKN"),
        ("nba", "Brooklyn", "BKN"),
        ("nba", "CHO", "CHA"),
        ("nba", "Charlotte", "CHA"),
        ("nba", "NY", "NYK"),
        ("nba", "New York", "NYK"),
        ("nba", "LA Lakers", "LAL"),
        ("nba", "LA Clippers", "LAC"),
        ("nba", "Trail Blazers", "POR"),
        ("nba", "Blazers", "POR"),
        ("nba", "Sixers", "PHI"),
        ("nba", "76ers", "PHI"),
        ("nba", "Cavs", "CLE"),
        ("nba", "Mavs", "DAL"),
        ("nba", "Wolves", "MIN"),
        ("nba", "OKC", "OKC"),
        ("nba", "Oklahoma City", "OKC"),
        ("nba", "WSH", "WAS"),
        # MLB
        ("mlb", "CHW", "CWS"),
        ("mlb", "White Sox", "CWS"),
        ("mlb", "Chicago White Sox", "CWS"),
        ("mlb", "Cubs", "CHC"),
        ("mlb", "Chicago Cubs", "CHC"),
        ("mlb", "OAK", "ATH"),
        ("mlb", "A's", "ATH"),
        ("mlb", "Oakland Athletics", "ATH"),
        ("mlb", "Sacramento Athletics", "ATH"),
        ("mlb", "Oakland", "ATH"),
        ("mlb", "SDP", "SD"),
        ("mlb", "San Diego", "SD"),
        ("mlb", "SFG", "SF"),
        ("mlb", "SF Giants", "SF"),
        ("mlb", "TBR", "TB"),
        ("mlb", "Tampa Bay", "TB"),
        ("mlb", "KCR", "KC"),
        ("mlb", "Kansas City", "KC"),
        ("mlb", "WSN", "WSH"),
        ("mlb", "WAS", "WSH"),
        ("mlb", "Washington", "WSH"),
        ("mlb", "Nats", "WSH"),
        ("mlb", "AZ", "ARI"),
        ("mlb", "D-backs", "ARI"),
        ("mlb", "Dbacks", "ARI"),
        ("mlb", "Arizona", "ARI"),
        ("mlb", "NY Yankees", "NYY"),
        ("mlb", "Yanks", "NYY"),
        ("mlb", "NY Mets", "NYM"),
        ("mlb", "LA Dodgers", "LAD"),
        ("mlb", "LA Angels", "LAA"),
        ("mlb", "Anaheim", "LAA"),
        ("mlb", "Los Angeles Angels of Anaheim", "LAA"),
        ("mlb", "St Louis Cardinals", "STL"),
        ("mlb", "St. Louis", "STL"),
        ("mlb", "Cards", "STL"),
        ("mlb", "Jays", "TOR"),
        ("mlb", "Blue Jays", "TOR"),
        ("mlb", "Toronto", "TOR"),
        ("mlb", "Cleveland Guardians", "CLE"),
        ("mlb", "Guardians", "CLE"),
    ],
)
def test_alias_variants(league: str, name: str, expected: str) -> None:
    assert team_key(name, league) == expected


@pytest.mark.parametrize(
    "name,league",
    [
        ("  kansas   city  chiefs ", "nfl"),
        ("KANSAS CITY CHIEFS", "nfl"),
        ("Kansas-City Chiefs", "nfl"),
        ("The Kansas City Chiefs", "nfl"),
        ("Spread: Chiefs", "nfl"),  # last word
        ("KC Chiefs", "nfl"),
    ],
)
def test_normalization_and_last_word_fallback(name: str, league: str) -> None:
    assert team_key(name, league) == "KC"


def test_last_two_words_fallback() -> None:
    assert team_key("Home team: Boston Red Sox", "mlb") == "BOS"
    assert team_key("Spread: White Sox", "mlb") == "CWS"
    assert team_key("Spread: Portland Trail Blazers", "nba") == "POR"
    assert normalize_name("St. Louis Cardinals") == "st louis cardinals"
    assert normalize_name("A's") == "as"
    assert normalize_name("D-backs") == "d backs"


@pytest.mark.parametrize(
    "name,league",
    [
        ("Manchester United", "nfl"),
        ("", "nfl"),
        ("   ", "nba"),
        ("Over", "nfl"),
        ("Under", "mlb"),
        ("Yes", "nfl"),
        ("No", "nfl"),  # Polymarket binary label, not New Orleans
        ("No", "nba"),
        ("No.", "nba"),
        ("Yes", "mlb"),
        ("yes", "mlb"),
        ("YES", "nfl"),
        ("Sox", "mlb"),  # ambiguous nickname
        ("Chiefs", "nhl"),  # unknown league
        ("Chiefs", ""),
    ],
)
def test_unknown_names_return_none(name: str, league: str) -> None:
    assert team_key(name, league) is None  # type: ignore[arg-type]


def test_team_key_never_raises() -> None:
    assert team_key(None, "nfl") is None  # type: ignore[arg-type]
    assert team_key(42, "nfl") is None  # type: ignore[arg-type]
    assert team_key("Chiefs", None) is None  # type: ignore[arg-type]
    assert team_key("NO", "nfl") == "NO"  # the abbreviation still resolves
    assert team_key("no", "nfl") == "NO"  # event-slug token
    assert team_key("N.O.", "nba") == "NOP"


class TestCollisions:
    def test_lac_is_chargers_in_nfl_and_clippers_in_nba(self) -> None:
        assert team_key("LAC", "nfl") == "LAC"
        assert team_key("LAC", "nba") == "LAC"
        assert TEAM_ALIASES["nfl"]["LAC"] == "Los Angeles Chargers"
        assert TEAM_ALIASES["nba"]["LAC"] == "Los Angeles Clippers"
        assert team_key("Chargers", "nfl") == "LAC"
        assert team_key("Chargers", "nba") is None
        assert team_key("Clippers", "nba") == "LAC"
        assert team_key("Clippers", "nfl") is None
        assert team_key("LAC", "mlb") is None

    def test_washington_per_league(self) -> None:
        assert team_key("Washington", "nfl") == "WAS"
        assert team_key("Washington", "nba") == "WAS"
        assert team_key("Washington", "mlb") == "WSH"
        for league in ("nfl", "nba", "mlb"):
            assert team_key("WAS", league) == team_key("WSH", league)
        assert team_key("Commanders", "mlb") is None
        assert team_key("Nationals", "nfl") is None

    def test_kansas_city_per_league(self) -> None:
        assert team_key("Kansas City", "nfl") == "KC"
        assert team_key("Kansas City", "mlb") == "KC"
        assert team_key("Kansas City", "nba") is None
        assert team_key("Chiefs", "mlb") is None
        assert team_key("Royals", "nfl") is None

    def test_miami_per_league(self) -> None:
        assert team_key("Miami", "nfl") == "MIA"
        assert team_key("Miami", "nba") == "MIA"
        assert team_key("Miami", "mlb") == "MIA"
        assert team_key("Dolphins", "mlb") is None
        assert team_key("Heat", "nfl") is None

    def test_new_york_teams(self) -> None:
        assert team_key("New York", "nfl") is None  # Giants or Jets
        assert team_key("NY", "nfl") is None
        assert team_key("New York Giants", "nfl") == "NYG"
        assert team_key("New York Jets", "nfl") == "NYJ"
        assert team_key("New York", "nba") == "NYK"
        assert team_key("Brooklyn Nets", "nba") == "BKN"
        assert team_key("New York", "mlb") is None  # Yankees or Mets
        assert team_key("NY", "mlb") is None
        assert team_key("New York Yankees", "mlb") == "NYY"
        assert team_key("New York Mets", "mlb") == "NYM"

    def test_los_angeles_teams(self) -> None:
        for league in ("nfl", "nba", "mlb"):
            assert team_key("Los Angeles", league) is None
            assert team_key("LA", league) is None
        assert team_key("Los Angeles Rams", "nfl") == "LAR"
        assert team_key("Los Angeles Chargers", "nfl") == "LAC"
        assert team_key("Los Angeles Lakers", "nba") == "LAL"
        assert team_key("Los Angeles Clippers", "nba") == "LAC"
        assert team_key("Los Angeles Dodgers", "mlb") == "LAD"
        assert team_key("Los Angeles Angels", "mlb") == "LAA"

    def test_chicago_teams(self) -> None:
        assert team_key("Chicago", "nfl") == "CHI"
        assert team_key("Chicago", "nba") == "CHI"
        assert team_key("Chicago", "mlb") is None  # Cubs or White Sox
        assert team_key("Chicago Cubs", "mlb") == "CHC"
        assert team_key("Chicago White Sox", "mlb") == "CWS"

    def test_giants_and_cardinals_per_league(self) -> None:
        assert team_key("Giants", "nfl") == "NYG"
        assert team_key("Giants", "mlb") == "SF"
        assert team_key("Cardinals", "nfl") == "ARI"
        assert team_key("Cardinals", "mlb") == "STL"
        assert team_key("SF", "nfl") == "SF"
        assert team_key("SF", "mlb") == "SF"


# --------------------------------------------------------------------------- builders

FIXED_NOW = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)
_TOKEN = 0


def _token() -> str:
    global _TOKEN
    _TOKEN += 1
    return str(10**70 + _TOKEN)


def pm_market(
    market_id: str,
    league: str,
    market_type: str,
    outcomes: Sequence[str],
    *,
    start: datetime | None,
    home: str | None,
    away: str | None,
    line: float | None = None,
    line_team: str | None = None,
    question: str = "",
    resolve: bool = True,
) -> PmMarket:
    outs = tuple(
        PmOutcome(
            token_id=_token(),
            name=name,
            team_key=team_key(name, league) if resolve else None,  # type: ignore[arg-type]
            last_price=None,
            best_bid=None,
            best_ask=None,
        )
        for name in outcomes
    )
    assert len(outs) == 2
    return PmMarket(
        market_id=market_id,
        condition_id="0x" + market_id.ljust(64, "0"),
        slug=f"{league}-{market_id}",
        question=question,
        event_id="1" + market_id[1:],
        event_slug=f"{league}-{(away or 'x').lower()}-{(home or 'y').lower()}",
        event_title=f"{away} @ {home}",
        league=league,  # type: ignore[arg-type]
        market_type=market_type,  # type: ignore[arg-type]
        line=line,
        line_team_key=line_team,
        outcomes=outs,  # type: ignore[arg-type]
        game_start=start,
        home_team_key=home,
        away_team_key=away,
        accepting_orders=True,
        closed=False,
        resolved_outcome_index=None,
        tick_size=0.01,
        min_order_size=5.0,
        liquidity=5000.0,
        volume=10000.0,
        taker_fee_rate=None,
    )


def _bo(league: str, name: str, price: int, point: float | None = None) -> BookOutcome:
    key = None if name in ("Over", "Under") else team_key(name, league)  # type: ignore[arg-type]
    return BookOutcome(name=name, team_key=key, price_american=price, point=point)


def book_quote(
    bookmaker: str,
    league: str,
    *,
    h2h: Sequence[tuple[str, int]] = (),
    spreads: Sequence[tuple[str, int, float]] = (),
    totals: Sequence[tuple[str, int, float]] = (),
) -> BookQuote:
    markets: list[BookMarket] = []
    if h2h:
        markets.append(
            BookMarket(
                key="h2h",
                outcomes=tuple(_bo(league, n, p) for n, p in h2h),
                last_update=FIXED_NOW,
            )
        )
    if spreads:
        markets.append(
            BookMarket(
                key="spreads",
                outcomes=tuple(_bo(league, n, p, pt) for n, p, pt in spreads),
                last_update=FIXED_NOW,
            )
        )
    if totals:
        markets.append(
            BookMarket(
                key="totals",
                outcomes=tuple(_bo(league, n, p, pt) for n, p, pt in totals),
                last_update=FIXED_NOW,
            )
        )
    return BookQuote(bookmaker=bookmaker, title=bookmaker.title(), markets=tuple(markets))


def book_game(
    game_id: str,
    league: str,
    commence: datetime,
    home: str,
    away: str,
    books: Sequence[BookQuote] = (),
) -> BookGame:
    home_key = team_key(home, league)  # type: ignore[arg-type]
    away_key = team_key(away, league)  # type: ignore[arg-type]
    assert home_key and away_key, (home, away)
    return BookGame(
        game_id=game_id,
        league=league,  # type: ignore[arg-type]
        commence_time=commence,
        home_team_key=home_key,
        away_team_key=away_key,
        home_team_name=home,
        away_team_name=away,
        books=tuple(books),
    )


# --------------------------------------------------------------------------- the FIXTURES slate

T_KC_BUF = datetime(2026, 9, 20, 20, 25, tzinfo=UTC)
T_DAL_PHI = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
T_LAL_BOS = datetime(2026, 10, 22, 23, 30, tzinfo=UTC)
T_GSW_DEN = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
T_NYY_LAD = datetime(2026, 9, 20, 2, 10, tzinfo=UTC)
T_ATH_SEA = datetime(2026, 9, 20, 1, 40, tzinfo=UTC)
T_BAL_TOR = datetime(2026, 9, 17, 23, 7, tzinfo=UTC)


def slate_book_games() -> list[BookGame]:
    """Games 1-6 of docs/FIXTURES.md with the pinnacle prices from the table."""
    return [
        book_game(
            "a" * 32,
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [
                book_quote(
                    "pinnacle",
                    "nfl",
                    h2h=[("Kansas City Chiefs", -150), ("Buffalo Bills", 130)],
                    spreads=[("Kansas City Chiefs", -110, -3.5), ("Buffalo Bills", -110, 3.5)],
                    totals=[("Over", -110, 47.5), ("Under", -110, 47.5)],
                )
            ],
        ),
        book_game(
            "b" * 32,
            "nfl",
            T_DAL_PHI,
            "Philadelphia Eagles",
            "Dallas Cowboys",
            [
                book_quote(
                    "pinnacle",
                    "nfl",
                    h2h=[("Philadelphia Eagles", -200), ("Dallas Cowboys", 170)],
                    spreads=[("Philadelphia Eagles", -110, -4.5), ("Dallas Cowboys", -110, 4.5)],
                    totals=[("Over", -110, 44.5), ("Under", -110, 44.5)],
                )
            ],
        ),
        book_game(
            "c" * 32,
            "nba",
            T_LAL_BOS,
            "Boston Celtics",
            "Los Angeles Lakers",
            [
                book_quote(
                    "pinnacle",
                    "nba",
                    h2h=[("Boston Celtics", -240), ("Los Angeles Lakers", 195)],
                    spreads=[("Boston Celtics", -110, -6.5), ("Los Angeles Lakers", -110, 6.5)],
                    totals=[("Over", -110, 224.5), ("Under", -110, 224.5)],
                )
            ],
        ),
        book_game(
            "d" * 32,
            "nba",
            T_GSW_DEN,
            "Denver Nuggets",
            "Golden State Warriors",
            [
                book_quote(
                    "pinnacle",
                    "nba",
                    h2h=[("Denver Nuggets", -130), ("Golden State Warriors", 110)],
                    spreads=[("Denver Nuggets", -110, -2.5), ("Golden State Warriors", -110, 2.5)],
                    totals=[("Over", -105, 231.5), ("Under", -115, 231.5)],
                )
            ],
        ),
        book_game(
            "e" * 32,
            "mlb",
            T_NYY_LAD,
            "Los Angeles Dodgers",
            "New York Yankees",
            [
                book_quote(
                    "pinnacle",
                    "mlb",
                    h2h=[("Los Angeles Dodgers", -140), ("New York Yankees", 120)],
                    spreads=[("Los Angeles Dodgers", 120, -1.5), ("New York Yankees", -140, 1.5)],
                    totals=[("Over", -110, 8.5), ("Under", -110, 8.5)],
                )
            ],
        ),
        book_game(
            "f" * 32,
            "mlb",
            T_ATH_SEA,
            "Seattle Mariners",
            "Athletics",
            [
                book_quote(
                    "pinnacle",
                    "mlb",
                    h2h=[("Seattle Mariners", -165), ("Athletics", 140)],
                    totals=[("Over", -110, 7.5), ("Under", -110, 7.5)],
                )
            ],
        ),
    ]


def slate_markets() -> dict[str, PmMarket]:
    """Polymarket markets of docs/FIXTURES.md keyed by a readable name."""
    m: dict[str, PmMarket] = {}
    m["kc_buf_ml"] = pm_market(
        "500101",
        "nfl",
        "moneyline",
        ["Chiefs", "Bills"],
        start=T_KC_BUF,
        home="BUF",
        away="KC",
        question="Chiefs vs. Bills",
    )
    m["kc_buf_spread"] = pm_market(
        "500102",
        "nfl",
        "spread",
        ["Chiefs", "Bills"],
        start=T_KC_BUF,
        home="BUF",
        away="KC",
        line=-3.5,
        line_team="KC",
        question="Spread: Chiefs (-3.5)",
    )
    m["kc_buf_total"] = pm_market(
        "500103",
        "nfl",
        "total",
        ["Over", "Under"],
        start=T_KC_BUF,
        home="BUF",
        away="KC",
        line=47.5,
        question="O/U 47.5",
    )
    m["dal_phi_ml"] = pm_market(
        "500201",
        "nfl",
        "moneyline",
        ["Cowboys", "Eagles"],
        start=T_DAL_PHI,
        home="PHI",
        away="DAL",
    )
    m["dal_phi_spread"] = pm_market(
        "500202",
        "nfl",
        "spread",
        ["Cowboys", "Eagles"],
        start=T_DAL_PHI,
        home="PHI",
        away="DAL",
        line=-4.5,
        line_team="PHI",
    )
    m["dal_phi_total"] = pm_market(
        "500203",
        "nfl",
        "total",
        ["Over", "Under"],
        start=T_DAL_PHI,
        home="PHI",
        away="DAL",
        line=44.5,
    )
    m["lal_bos_ml"] = pm_market(
        "500301",
        "nba",
        "moneyline",
        ["Lakers", "Celtics"],
        start=T_LAL_BOS,
        home="BOS",
        away="LAL",
    )
    m["lal_bos_spread"] = pm_market(
        "500302",
        "nba",
        "spread",
        ["Lakers", "Celtics"],
        start=T_LAL_BOS,
        home="BOS",
        away="LAL",
        line=-6.5,
        line_team="BOS",
    )
    m["lal_bos_total"] = pm_market(
        "500303",
        "nba",
        "total",
        ["Over", "Under"],
        start=T_LAL_BOS,
        home="BOS",
        away="LAL",
        line=224.5,
    )
    m["gsw_den_ml"] = pm_market(
        "500401",
        "nba",
        "moneyline",
        ["Warriors", "Nuggets"],
        start=T_GSW_DEN,
        home="DEN",
        away="GSW",
    )
    m["gsw_den_spread"] = pm_market(  # deliberate line mismatch vs the book (-2.5)
        "500402",
        "nba",
        "spread",
        ["Warriors", "Nuggets"],
        start=T_GSW_DEN,
        home="DEN",
        away="GSW",
        line=-3.5,
        line_team="DEN",
    )
    m["gsw_den_total"] = pm_market(
        "500403",
        "nba",
        "total",
        ["Over", "Under"],
        start=T_GSW_DEN,
        home="DEN",
        away="GSW",
        line=231.5,
    )
    m["nyy_lad_ml"] = pm_market(
        "500501",
        "mlb",
        "moneyline",
        ["Yankees", "Dodgers"],
        start=T_NYY_LAD,
        home="LAD",
        away="NYY",
    )
    m["nyy_lad_spread"] = pm_market(
        "500502",
        "mlb",
        "spread",
        ["Yankees", "Dodgers"],
        start=T_NYY_LAD,
        home="LAD",
        away="NYY",
        line=-1.5,
        line_team="LAD",
    )
    m["nyy_lad_total"] = pm_market(
        "500503",
        "mlb",
        "total",
        ["Over", "Under"],
        start=T_NYY_LAD,
        home="LAD",
        away="NYY",
        line=8.5,
    )
    m["ath_sea_ml"] = pm_market(
        "500601",
        "mlb",
        "moneyline",
        ["Athletics", "Mariners"],
        start=T_ATH_SEA,
        home="SEA",
        away="ATH",
    )
    m["ath_sea_total"] = pm_market(
        "500602",
        "mlb",
        "total",
        ["Over", "Under"],
        start=T_ATH_SEA,
        home="SEA",
        away="ATH",
        line=7.5,
        question="O/U 7.5",
    )
    m["bal_tor_ml"] = pm_market(  # finished game; not in the Odds API fixture
        "500701",
        "mlb",
        "moneyline",
        ["Orioles", "Blue Jays"],
        start=T_BAL_TOR,
        home="TOR",
        away="BAL",
    )
    return m


@pytest.fixture
def slate() -> tuple[dict[str, PmMarket], list[BookGame]]:
    return slate_markets(), slate_book_games()


# --------------------------------------------------------------------------- match_games


class TestMatchGames:
    def test_slate_matches_every_live_game_and_not_the_finished_one(self, slate) -> None:
        markets, games = slate
        matched = match_games(list(markets.values()), games)
        assert len(matched) == len(markets) - 1
        assert "500701" not in matched
        by_id = {g.game_id: g for g in games}
        assert matched["500101"] is by_id["a" * 32]
        assert matched["500102"] is by_id["a" * 32]
        assert matched["500103"] is by_id["a" * 32]
        assert matched["500202"] is by_id["b" * 32]
        assert matched["500301"] is by_id["c" * 32]
        assert matched["500402"] is by_id["d" * 32]
        assert matched["500503"] is by_id["e" * 32]
        assert matched["500601"] is by_id["f" * 32]
        assert matched["500602"] is by_id["f" * 32]
        assert unmatched_reasons(list(markets.values()), games) == [
            {
                "market_id": "500701",
                "question": "",
                "league": "mlb",
                "market_type": "moneyline",
                "reason": "no book game",
            }
        ]

    def test_market_outcome_order_matches_pm_outcome_team_keys(self, slate) -> None:
        markets, _ = slate
        assert [o.team_key for o in markets["kc_buf_ml"].outcomes] == ["KC", "BUF"]
        assert [o.team_key for o in markets["ath_sea_ml"].outcomes] == ["ATH", "SEA"]
        assert [o.team_key for o in markets["kc_buf_total"].outcomes] == [None, None]

    def test_order_insensitive(self, slate) -> None:
        markets, _ = slate
        swapped = book_game("z" * 32, "nfl", T_KC_BUF, "Kansas City Chiefs", "Buffalo Bills")
        assert match_games([markets["kc_buf_ml"]], [swapped]) == {"500101": swapped}
        market_swapped = pm_market(
            "500199",
            "nfl",
            "moneyline",
            ["Bills", "Chiefs"],
            start=T_KC_BUF,
            home="KC",
            away="BUF",
        )
        game = slate_book_games()[0]
        assert match_games([market_swapped], [game]) == {"500199": game}

    @pytest.mark.parametrize("hours", [0, 1, 12, 35.99, 36])
    def test_window_inclusive(self, hours: float) -> None:
        game = slate_book_games()[0]
        market = pm_market(
            "1",
            "nfl",
            "moneyline",
            ["Chiefs", "Bills"],
            start=T_KC_BUF + timedelta(hours=hours),
            home="BUF",
            away="KC",
        )
        assert match_games([market], [game]) == {"1": game}
        earlier = pm_market(
            "2",
            "nfl",
            "moneyline",
            ["Chiefs", "Bills"],
            start=T_KC_BUF - timedelta(hours=hours),
            home="BUF",
            away="KC",
        )
        assert match_games([earlier], [game]) == {"2": game}

    def test_outside_window(self) -> None:
        game = slate_book_games()[0]
        late = pm_market(
            "1",
            "nfl",
            "moneyline",
            ["Chiefs", "Bills"],
            start=T_KC_BUF + timedelta(hours=36, seconds=1),
            home="BUF",
            away="KC",
        )
        assert match_games([late], [game]) == {}
        assert unmatched_reasons([late], [game])[0]["reason"] == "outside match window"
        assert match_games([late], [game], window_hours=37) == {"1": game}
        week_later = pm_market(
            "3",
            "nfl",
            "moneyline",
            ["Chiefs", "Bills"],
            start=T_KC_BUF + timedelta(days=7),
            home="BUF",
            away="KC",
        )
        assert match_games([week_later], [game]) == {}

    def test_nearest_start_wins_for_doubleheader(self) -> None:
        early = book_game(
            "g1", "mlb", T_NYY_LAD - timedelta(hours=5), "Los Angeles Dodgers", "New York Yankees"
        )
        late = book_game(
            "g2", "mlb", T_NYY_LAD + timedelta(hours=1), "Los Angeles Dodgers", "New York Yankees"
        )
        market = pm_market(
            "1",
            "mlb",
            "moneyline",
            ["Yankees", "Dodgers"],
            start=T_NYY_LAD,
            home="LAD",
            away="NYY",
        )
        assert match_games([market], [early, late]) == {"1": late}
        assert match_games([market], [late, early]) == {"1": late}

    def test_league_mismatch_never_matches(self) -> None:
        mlb_game = book_game("g", "mlb", T_KC_BUF, "Miami Marlins", "Kansas City Royals")
        nfl_market = pm_market(
            "1",
            "nfl",
            "moneyline",
            ["Chiefs", "Dolphins"],
            start=T_KC_BUF,
            home="MIA",
            away="KC",
        )
        assert match_games([nfl_market], [mlb_game]) == {}
        assert unmatched_reasons([nfl_market], [mlb_game])[0]["reason"] == "no book game"

    def test_infers_teams_from_outcomes_when_home_away_missing(self) -> None:
        game = slate_book_games()[0]
        ml = pm_market(
            "1", "nfl", "moneyline", ["Chiefs", "Bills"], start=T_KC_BUF, home=None, away=None
        )
        spread = pm_market(
            "2",
            "nfl",
            "spread",
            ["Bills", "Chiefs"],
            start=T_KC_BUF,
            home=None,
            away=None,
            line=-3.5,
            line_team="KC",
        )
        half = pm_market(
            "3", "nfl", "moneyline", ["Chiefs", "Bills"], start=T_KC_BUF, home="BUF", away=None
        )
        assert match_games([ml, spread, half], [game]) == {"1": game, "2": game, "3": game}

    def test_total_without_teams_is_unmatched_with_reason(self) -> None:
        game = slate_book_games()[0]
        total = pm_market(
            "1",
            "nfl",
            "total",
            ["Over", "Under"],
            start=T_KC_BUF,
            home=None,
            away=None,
            line=47.5,
        )
        assert match_games([total], [game]) == {}
        reasons = unmatched_reasons([total], [game])
        assert reasons[0]["reason"] == "no teams"
        assert reasons[0]["market_id"] == "1"
        unresolved = pm_market(
            "2", "nfl", "moneyline", ["Yes", "No"], start=T_KC_BUF, home=None, away=None
        )
        assert unmatched_reasons([unresolved], [game])[0]["reason"] == "no teams"

    def test_missing_start_time(self) -> None:
        game = slate_book_games()[0]
        market = pm_market(
            "1", "nfl", "moneyline", ["Chiefs", "Bills"], start=None, home="BUF", away="KC"
        )
        assert match_games([market], [game]) == {"1": game}  # unique pairing is enough
        twin = book_game(
            "t", "nfl", T_KC_BUF + timedelta(days=1), "Buffalo Bills", "Kansas City Chiefs"
        )
        assert match_games([market], [game, twin]) == {}
        assert unmatched_reasons([market], [game, twin])[0]["reason"] == "no start time"

    def test_naive_datetimes_are_treated_as_utc(self) -> None:
        game = slate_book_games()[0]
        market = pm_market(
            "1",
            "nfl",
            "moneyline",
            ["Chiefs", "Bills"],
            start=T_KC_BUF.replace(tzinfo=None),
            home="BUF",
            away="KC",
        )
        assert match_games([market], [game]) == {"1": game}

    def test_empty_inputs(self, slate) -> None:
        markets, games = slate
        assert match_games([], games) == {}
        assert match_games(list(markets.values()), []) == {}
        assert len(unmatched_reasons(list(markets.values()), [])) == len(markets)


# ------------------------------------------------- review round 2: series games (MLB / NBA)


class TestSeriesWindow:
    """MLB and NBA play the same opponent on consecutive days and books post the next day's
    MLB lines late, so a 36 h window happily priced Polymarket's game N+1 against the book's
    game N (different starting pitchers) and then stored those lines under the wrong game."""

    GAME_N = datetime(2026, 9, 19, 20, 10, tzinfo=UTC)  # 16:10 ET on the 19th
    GAME_N1 = datetime(2026, 9, 20, 17, 35, tzinfo=UTC)  # 13:35 ET on the 20th

    def _yankees_at_dodgers(self, market_id: str, start: datetime) -> PmMarket:
        return pm_market(
            market_id,
            "mlb",
            "moneyline",
            ["Yankees", "Dodgers"],
            start=start,
            home="LAD",
            away="NYY",
        )

    def _book(self, game_id: str, commence: datetime) -> BookGame:
        return book_game(
            game_id,
            "mlb",
            commence,
            "Los Angeles Dodgers",
            "New York Yankees",
            slate_book_games()[4].books,
        )

    def test_the_next_day_of_a_series_never_matches_yesterdays_lines(self) -> None:
        tomorrow = self._yankees_at_dodgers("500801", self.GAME_N1)
        today_only = [self._book("n1", self.GAME_N)]
        assert abs(self.GAME_N1 - self.GAME_N) < timedelta(hours=36)  # the old window matched
        assert match_games([tomorrow], today_only) == {}
        reasons = unmatched_reasons([tomorrow], today_only)
        assert [r["market_id"] for r in reasons] == ["500801"]
        assert reasons[0]["reason"] == DIFFERENT_GAME_DAY

    def test_each_game_of_the_series_still_matches_its_own_lines(self) -> None:
        tonight = self._yankees_at_dodgers("500801", self.GAME_N)
        tomorrow = self._yankees_at_dodgers("500802", self.GAME_N1)
        books = [self._book("n", self.GAME_N), self._book("n1", self.GAME_N1)]
        matched = match_games([tonight, tomorrow], books)
        assert matched["500801"].game_id == "n"
        assert matched["500802"].game_id == "n1"

    def test_a_day_night_pair_on_the_same_eastern_date_still_matches(self) -> None:
        """A 13:05 ET first pitch and a 20:10 ET one are seven hours apart but the same game
        day; only the calendar rolling over means a different game."""
        day_game = datetime(2026, 9, 20, 17, 5, tzinfo=UTC)  # 13:05 ET on the 20th
        night_lines = datetime(2026, 9, 21, 0, 10, tzinfo=UTC)  # 20:10 ET on the 20th
        market = self._yankees_at_dodgers("500803", day_game)
        late = self._book("late", night_lines)
        assert night_lines - day_game > timedelta(hours=6)
        assert match_games([market], [late]) == {"500803": late}

    def test_the_nba_back_to_back_is_tightened_too(self) -> None:
        tip_off = datetime(2026, 10, 22, 23, 30, tzinfo=UTC)
        next_night = tip_off + timedelta(days=1)
        market = pm_market(
            "500804",
            "nba",
            "moneyline",
            ["Lakers", "Celtics"],
            start=next_night,
            home="BOS",
            away="LAL",
        )
        yesterday = book_game("y", "nba", tip_off, "Boston Celtics", "Los Angeles Lakers")
        assert match_games([market], [yesterday]) == {}
        assert unmatched_reasons([market], [yesterday])[0]["reason"] == DIFFERENT_GAME_DAY

    def test_nfl_keeps_the_wide_window(self) -> None:
        """One meeting a season and lines posted days ahead: 21 h of drift is still the game."""
        game = slate_book_games()[0]
        market = pm_market(
            "500805",
            "nfl",
            "moneyline",
            ["Chiefs", "Bills"],
            start=T_KC_BUF + timedelta(hours=21),
            home="BUF",
            away="KC",
        )
        assert match_games([market], [game]) == {"500805": game}

    def test_the_caller_can_still_narrow_the_window_but_not_widen_it(self) -> None:
        tomorrow = self._yankees_at_dodgers("500806", self.GAME_N1)
        today_only = [self._book("n1", self.GAME_N)]
        assert match_games([tomorrow], today_only, window_hours=72) == {}
        near = self._yankees_at_dodgers("500807", self.GAME_N + timedelta(hours=3))
        assert match_games([near], today_only) == {"500807": today_only[0]}
        assert match_games([near], today_only, window_hours=1) == {}
        assert unmatched_reasons([near], today_only, window_hours=1)[0]["reason"] == (
            "outside match window"
        )


# --------------------------------------------------------------------------- fair_for_outcome

W = {"pinnacle": 3.0, "betonlineag": 1.5, "lowvig": 1.5, "draftkings": 1.0, "fanduel": 1.0}


class TestFairMoneyline:
    def test_single_book_hand_checked(self, slate) -> None:
        markets, games = slate
        chiefs = fair_for_outcome(markets["kc_buf_ml"], 0, games[0], W)
        bills = fair_for_outcome(markets["kc_buf_ml"], 1, games[0], W)
        assert chiefs is not None and bills is not None
        assert chiefs.value == pytest.approx(0.583983, abs=TOL)
        assert bills.value == pytest.approx(0.416017, abs=TOL)
        assert chiefs.method == "power"
        assert chiefs.n_books == 1
        assert chiefs.books_used == ("pinnacle",)
        assert chiefs.line is None
        assert len(chiefs.per_book) == 1
        assert chiefs.per_book[0][0] == "pinnacle"
        assert chiefs.per_book[0][1] == pytest.approx(0.583983, abs=TOL)

    def test_slate_moneylines(self, slate) -> None:
        markets, games = slate
        by_id = {g.game_id: g for g in games}
        expected = {
            ("dal_phi_ml", 1, "b"): 0.650822,  # Eagles -200
            ("dal_phi_ml", 0, "b"): 0.349178,  # Cowboys +170
            ("lal_bos_ml", 1, "c"): 0.687579,  # Celtics -240
            ("lal_bos_ml", 0, "c"): 0.312421,  # Lakers +195
            ("gsw_den_ml", 1, "d"): 0.545403,  # Nuggets -130
            ("gsw_den_ml", 0, "d"): 0.454597,  # Warriors +110
            ("nyy_lad_ml", 1, "e"): 0.565565,  # Dodgers -140
            ("nyy_lad_ml", 0, "e"): 0.434435,  # Yankees +120
            ("ath_sea_ml", 1, "f"): 0.604948,  # Mariners -165
            ("ath_sea_ml", 0, "f"): 0.395052,  # Athletics +140
        }
        for (name, index, gid), value in expected.items():
            fair = fair_for_outcome(markets[name], index, by_id[gid * 32], W)
            assert fair is not None, name
            assert fair.value == pytest.approx(value, abs=TOL), name

    def test_fixture_expected_edges_hold(self, slate) -> None:
        """The FIXTURES table promises Chiefs ML ~ +0.021 and Under 7.5 ~ +0.038 with a
        0.05 taker fee; check the fair side of that arithmetic here (fee math is edge.py)."""
        markets, games = slate

        def eff(ask: float) -> float:
            return ask + 0.05 * ask * (1 - ask)

        chiefs = fair_for_outcome(markets["kc_buf_ml"], 0, games[0], W)
        assert chiefs is not None
        assert chiefs.value - eff(0.55) == pytest.approx(0.0216, abs=1e-3)
        under = fair_for_outcome(markets["ath_sea_total"], 1, games[5], W)
        assert under is not None
        assert under.value - eff(0.45) == pytest.approx(0.0376, abs=1e-3)
        yankees = fair_for_outcome(markets["nyy_lad_ml"], 0, games[4], W)
        assert yankees is not None
        assert yankees.value - eff(0.40) == pytest.approx(0.0224, abs=1e-3)

    def test_weighted_consensus_across_books(self, slate) -> None:
        markets, _ = slate
        game = book_game(
            "a" * 32,
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [
                book_quote(
                    "pinnacle", "nfl", h2h=[("Kansas City Chiefs", -150), ("Buffalo Bills", 130)]
                ),
                book_quote(
                    "draftkings", "nfl", h2h=[("Buffalo Bills", 135), ("Kansas City Chiefs", -155)]
                ),
            ],
        )
        fair = fair_for_outcome(markets["kc_buf_ml"], 0, game, W)
        assert fair is not None
        # (3 * 0.583983 + 1 * 0.592608) / 4
        assert fair.value == pytest.approx(0.586139, abs=TOL)
        assert fair.n_books == 2
        assert set(fair.books_used) == {"pinnacle", "draftkings"}
        assert dict(fair.per_book)["draftkings"] == pytest.approx(0.592608, abs=TOL)
        # missing weights use default_weight
        plain = fair_for_outcome(markets["kc_buf_ml"], 0, game, {}, default_weight=1.0)
        assert plain is not None
        assert plain.value == pytest.approx((0.583983 + 0.592608) / 2, abs=TOL)
        # a zero weight excludes the book entirely
        only_pin = fair_for_outcome(markets["kc_buf_ml"], 0, game, {"draftkings": 0.0})
        assert only_pin is not None
        assert only_pin.n_books == 1
        assert only_pin.value == pytest.approx(0.583983, abs=TOL)
        # complementary side sums to one per book
        other = fair_for_outcome(markets["kc_buf_ml"], 1, game, W)
        assert other is not None
        assert fair.value + other.value == pytest.approx(1.0, abs=TOL)

    def test_book_missing_one_side_is_skipped(self, slate) -> None:
        markets, _ = slate
        game = book_game(
            "a" * 32,
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [
                book_quote("pinnacle", "nfl", h2h=[("Kansas City Chiefs", -150)]),
                book_quote(
                    "lowvig", "nfl", h2h=[("Kansas City Chiefs", -145), ("Buffalo Bills", 125)]
                ),
            ],
        )
        fair = fair_for_outcome(markets["kc_buf_ml"], 0, game, W)
        assert fair is not None
        assert fair.books_used == ("lowvig",)
        none_game = book_game(
            "a" * 32,
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [book_quote("pinnacle", "nfl", h2h=[("Kansas City Chiefs", -150)])],
        )
        assert fair_for_outcome(markets["kc_buf_ml"], 0, none_game, W) is None

    def test_no_books_or_wrong_market_returns_none(self, slate) -> None:
        markets, _ = slate
        empty = book_game("a" * 32, "nfl", T_KC_BUF, "Buffalo Bills", "Kansas City Chiefs", [])
        assert fair_for_outcome(markets["kc_buf_ml"], 0, empty, W) is None
        totals_only = book_game(
            "a" * 32,
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [book_quote("pinnacle", "nfl", totals=[("Over", -110, 47.5), ("Under", -110, 47.5)])],
        )
        assert fair_for_outcome(markets["kc_buf_ml"], 0, totals_only, W) is None

    def test_outcome_without_team_key_returns_none(self, slate) -> None:
        _, games = slate
        yes_no = pm_market(
            "9", "nfl", "moneyline", ["Yes", "No"], start=T_KC_BUF, home="BUF", away="KC"
        )
        assert fair_for_outcome(yes_no, 0, games[0], W) is None
        assert fair_for_outcome(yes_no, 1, games[0], W) is None

    def test_other_outcome_key_falls_back_to_game(self, slate) -> None:
        _, games = slate
        market = pm_market(
            "9",
            "nfl",
            "moneyline",
            ["Chiefs", "Buffalo"],
            start=T_KC_BUF,
            home="BUF",
            away="KC",
            resolve=False,
        )
        market = PmMarket(
            **{
                **market.__dict__,
                "outcomes": (
                    PmOutcome(_token(), "Chiefs", "KC", None, None, None),
                    PmOutcome(_token(), "Buffalo", None, None, None, None),
                ),
            }
        )
        fair = fair_for_outcome(market, 0, games[0], W)
        assert fair is not None and fair.value == pytest.approx(0.583983, abs=TOL)

    def test_method_is_passed_through(self, slate) -> None:
        markets, games = slate
        mult = fair_for_outcome(markets["kc_buf_ml"], 0, games[0], W, method="multiplicative")
        assert mult is not None
        assert mult.method == "multiplicative"
        assert mult.value == pytest.approx(0.579832, abs=TOL)
        with pytest.raises(ValueError):
            fair_for_outcome(markets["kc_buf_ml"], 0, games[0], W, method="astrology")

    def test_invalid_outcome_index(self, slate) -> None:
        markets, games = slate
        with pytest.raises(ValueError):
            fair_for_outcome(markets["kc_buf_ml"], 2, games[0], W)

    def test_espn_single_book(self, slate) -> None:
        markets, _ = slate
        game = book_game(
            "espn:401000001",
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [book_quote("espn", "nfl", h2h=[("Kansas City Chiefs", -150), ("Buffalo Bills", 130)])],
        )
        fair = fair_for_outcome(markets["kc_buf_ml"], 0, game, {"espn": 0.5})
        assert fair is not None
        assert fair.books_used == ("espn",)
        assert fair.value == pytest.approx(0.583983, abs=TOL)


class TestFairSpread:
    def test_same_line_both_sides(self, slate) -> None:
        markets, games = slate
        chiefs = fair_for_outcome(markets["kc_buf_spread"], 0, games[0], W)
        bills = fair_for_outcome(markets["kc_buf_spread"], 1, games[0], W)
        assert chiefs is not None and bills is not None
        assert chiefs.value == pytest.approx(0.5, abs=TOL)
        assert bills.value == pytest.approx(0.5, abs=TOL)
        assert chiefs.line == -3.5
        assert bills.line == -3.5  # the market line, as stored on the market
        assert chiefs.books_used == ("pinnacle",)

    def test_run_line_prices(self, slate) -> None:
        markets, games = slate
        # LAD -1.5 (+120) / NYY +1.5 (-140); market line -1.5 for LAD, outcomes [Yankees, Dodgers]
        yankees = fair_for_outcome(markets["nyy_lad_spread"], 0, games[4], W)
        dodgers = fair_for_outcome(markets["nyy_lad_spread"], 1, games[4], W)
        assert yankees is not None and dodgers is not None
        assert dodgers.value == pytest.approx(0.434435, abs=TOL)
        assert yankees.value == pytest.approx(0.565565, abs=TOL)

    def test_weighted_with_asymmetric_prices(self, slate) -> None:
        markets, _ = slate
        game = book_game(
            "a" * 32,
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [
                book_quote(
                    "pinnacle",
                    "nfl",
                    spreads=[("Kansas City Chiefs", -115, -3.5), ("Buffalo Bills", -105, 3.5)],
                ),
                book_quote(
                    "fanduel",
                    "nfl",
                    spreads=[("Buffalo Bills", -110, 3.5), ("Kansas City Chiefs", -110, -3.5)],
                ),
            ],
        )
        chiefs = fair_for_outcome(markets["kc_buf_spread"], 0, game, W)
        bills = fair_for_outcome(markets["kc_buf_spread"], 1, game, W)
        assert chiefs is not None and bills is not None
        assert chiefs.value == pytest.approx((3 * 0.511604 + 0.5) / 4, abs=TOL)
        assert bills.value == pytest.approx((3 * 0.488396 + 0.5) / 4, abs=TOL)
        assert chiefs.n_books == 2

    def test_book_at_different_line_is_skipped(self, slate) -> None:
        markets, _ = slate
        game = book_game(
            "a" * 32,
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [
                book_quote(
                    "pinnacle",
                    "nfl",
                    spreads=[("Kansas City Chiefs", -110, -3.0), ("Buffalo Bills", -110, 3.0)],
                ),
                book_quote(
                    "fanduel",
                    "nfl",
                    spreads=[("Kansas City Chiefs", -110, -3.5), ("Buffalo Bills", -110, 3.5)],
                ),
            ],
        )
        fair = fair_for_outcome(markets["kc_buf_spread"], 0, game, W)
        assert fair is not None
        assert fair.books_used == ("fanduel",)

    def test_gsw_den_line_mismatch_returns_none(self, slate) -> None:
        markets, games = slate
        assert fair_for_outcome(markets["gsw_den_spread"], 0, games[3], W) is None
        assert fair_for_outcome(markets["gsw_den_spread"], 1, games[3], W) is None
        # the same game's moneyline and total still price
        assert fair_for_outcome(markets["gsw_den_ml"], 0, games[3], W) is not None
        assert fair_for_outcome(markets["gsw_den_total"], 0, games[3], W) is not None

    def test_mirrored_point_must_exist(self, slate) -> None:
        markets, _ = slate
        game = book_game(
            "a" * 32,
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [
                book_quote(
                    "pinnacle",
                    "nfl",
                    spreads=[("Kansas City Chiefs", -110, -3.5), ("Buffalo Bills", -110, 4.5)],
                )
            ],
        )
        assert fair_for_outcome(markets["kc_buf_spread"], 0, game, W) is None

    def test_line_team_is_the_other_outcome(self, slate) -> None:
        _, games = slate
        market = pm_market(
            "9",
            "nfl",
            "spread",
            ["Bills", "Chiefs"],
            start=T_KC_BUF,
            home="BUF",
            away="KC",
            line=-3.5,
            line_team="KC",
        )
        bills = fair_for_outcome(market, 0, games[0], W)  # Bills need +3.5 at the book
        assert bills is not None and bills.value == pytest.approx(0.5, abs=TOL)

    def test_missing_line_or_line_team_returns_none(self, slate) -> None:
        _, games = slate
        no_line = pm_market(
            "9",
            "nfl",
            "spread",
            ["Chiefs", "Bills"],
            start=T_KC_BUF,
            home="BUF",
            away="KC",
            line=None,
            line_team="KC",
        )
        no_team = pm_market(
            "9",
            "nfl",
            "spread",
            ["Chiefs", "Bills"],
            start=T_KC_BUF,
            home="BUF",
            away="KC",
            line=-3.5,
            line_team=None,
        )
        foreign = pm_market(
            "9",
            "nfl",
            "spread",
            ["Chiefs", "Bills"],
            start=T_KC_BUF,
            home="BUF",
            away="KC",
            line=-3.5,
            line_team="DAL",
        )
        for market in (no_line, no_team, foreign):
            assert fair_for_outcome(market, 0, games[0], W) is None


class TestFairTotal:
    def test_same_total(self, slate) -> None:
        markets, games = slate
        over = fair_for_outcome(markets["kc_buf_total"], 0, games[0], W)
        under = fair_for_outcome(markets["kc_buf_total"], 1, games[0], W)
        assert over is not None and under is not None
        assert over.value == pytest.approx(0.5, abs=TOL)
        assert under.value == pytest.approx(0.5, abs=TOL)
        assert over.line == 47.5

    def test_asymmetric_total_prices(self, slate) -> None:
        markets, games = slate
        over = fair_for_outcome(markets["gsw_den_total"], 0, games[3], W)  # O -105 / U -115
        under = fair_for_outcome(markets["gsw_den_total"], 1, games[3], W)
        assert over is not None and under is not None
        assert over.value == pytest.approx(0.488396, abs=TOL)
        assert under.value == pytest.approx(0.511604, abs=TOL)

    def test_book_at_other_total_is_skipped(self, slate) -> None:
        markets, _ = slate
        game = book_game(
            "a" * 32,
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [
                book_quote("pinnacle", "nfl", totals=[("Over", -110, 48.5), ("Under", -110, 48.5)]),
                book_quote("lowvig", "nfl", totals=[("Under", -105, 47.5), ("Over", -115, 47.5)]),
            ],
        )
        over = fair_for_outcome(markets["kc_buf_total"], 0, game, W)
        assert over is not None
        assert over.books_used == ("lowvig",)
        assert over.value == pytest.approx(0.511604, abs=TOL)
        only_wrong = book_game(
            "a" * 32,
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [book_quote("pinnacle", "nfl", totals=[("Over", -110, 48.5), ("Under", -110, 48.5)])],
        )
        assert fair_for_outcome(markets["kc_buf_total"], 0, only_wrong, W) is None

    def test_outcome_names_are_case_insensitive_and_may_carry_the_line(self, slate) -> None:
        _, games = slate
        market = pm_market(
            "9",
            "nfl",
            "total",
            ["OVER 47.5", "under 47.5"],
            start=T_KC_BUF,
            home="BUF",
            away="KC",
            line=47.5,
        )
        over = fair_for_outcome(market, 0, games[0], W)
        assert over is not None and over.value == pytest.approx(0.5, abs=TOL)

    def test_missing_line_or_bad_names_return_none(self, slate) -> None:
        _, games = slate
        no_line = pm_market(
            "9",
            "nfl",
            "total",
            ["Over", "Under"],
            start=T_KC_BUF,
            home="BUF",
            away="KC",
            line=None,
        )
        assert fair_for_outcome(no_line, 0, games[0], W) is None
        yes_no = pm_market(
            "9", "nfl", "total", ["Yes", "No"], start=T_KC_BUF, home="BUF", away="KC", line=47.5
        )
        assert fair_for_outcome(yes_no, 0, games[0], W) is None


# --------------------------------------------------------------------------- review fixes


class TestDegenerateQuotesAreSkippedPerBook:
    def test_a_bad_moneyline_quote_skips_only_that_book(
        self, slate, caplog: pytest.LogCaptureFixture
    ) -> None:
        markets, _ = slate
        game = book_game(
            "a" * 32,
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [
                book_quote(
                    "pinnacle", "nfl", h2h=[("Kansas City Chiefs", -150), ("Buffalo Bills", 130)]
                ),
                # odds strictly between -100 and +100 are not American odds: ValueError
                book_quote(
                    "draftkings", "nfl", h2h=[("Kansas City Chiefs", -50), ("Buffalo Bills", 50)]
                ),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="app.core.matching"):
            fair = fair_for_outcome(markets["kc_buf_ml"], 0, game, W)
        assert fair is not None
        assert fair.n_books == 1 and fair.books_used == ("pinnacle",)
        assert fair.value == pytest.approx(0.583983, abs=TOL)
        assert any(
            "draftkings" in r.getMessage() and "skipping" in r.getMessage() for r in caplog.records
        )

    def test_shin_skips_an_underround_pair_beside_a_normal_book(
        self, slate, caplog: pytest.LogCaptureFixture
    ) -> None:
        markets, _ = slate
        game = book_game(
            "a" * 32,
            "nfl",
            T_KC_BUF,
            "Buffalo Bills",
            "Kansas City Chiefs",
            [
                book_quote(
                    "pinnacle", "nfl", h2h=[("Kansas City Chiefs", -150), ("Buffalo Bills", 130)]
                ),
                # +105 / +105 sums to less than one: Shin's model has no solution
                book_quote(
                    "lowvig", "nfl", h2h=[("Kansas City Chiefs", 105), ("Buffalo Bills", 105)]
                ),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="app.core.matching"):
            fair = fair_for_outcome(markets["kc_buf_ml"], 0, game, W, method="shin")
        assert fair is not None and fair.books_used == ("pinnacle",)
        assert fair.value == pytest.approx(0.582609, abs=TOL)  # shin == additive for two-way
        assert any(
            "lowvig" in r.getMessage() and "skipping" in r.getMessage() for r in caplog.records
        )
        # power handles the underround, so the same book is kept there
        power = fair_for_outcome(markets["kc_buf_ml"], 0, game, W, method="power")
        assert power is not None and set(power.books_used) == {"pinnacle", "lowvig"}
        # a wrong method is a configuration bug, never swallowed
        with pytest.raises(ValueError):
            fair_for_outcome(markets["kc_buf_ml"], 0, game, W, method="astrology")
