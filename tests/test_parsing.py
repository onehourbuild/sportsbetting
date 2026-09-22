"""Tests for app/core/parsing.py: Polymarket question / line / JSON-list parsing.

The live Gamma formats are unverified (docs/RESEARCH.md), so these tests pin down the
documented shapes, the fallbacks, and that garbage never produces a confident answer.
"""

from __future__ import annotations

import pytest

from app.core.parsing import parse_json_list, parse_market_type, parse_spread, parse_total

# --------------------------------------------------------------------------- parse_market_type


@pytest.mark.parametrize(
    "sports_market_type,expected",
    [
        ("moneyline", "moneyline"),
        ("Moneyline", "moneyline"),
        ("MONEYLINE", "moneyline"),
        ("money_line", "moneyline"),
        ("money line", "moneyline"),
        ("ml", "moneyline"),
        ("h2h", "moneyline"),
        ("spreads", "spread"),
        ("spread", "spread"),
        ("Spreads", "spread"),
        ("handicap", "spread"),
        ("point_spread", "spread"),
        ("run_line", "spread"),
        ("totals", "total"),
        ("total", "total"),
        ("Totals", "total"),
        ("over_under", "total"),
        ("o/u", "total"),
        ("  totals  ", "total"),
    ],
)
def test_sports_market_type_strings(sports_market_type: str, expected: str) -> None:
    assert parse_market_type(sports_market_type, "") == expected
    # a known type wins over any question wording
    assert parse_market_type(sports_market_type, "Chiefs vs. Bills") == expected


@pytest.mark.parametrize(
    "sports_market_type",
    [
        "player_props",
        "props",
        "both_teams_to_score",
        "draw_no_bet",
        "1h_spreads",
        "first_half_total",
        "team_totals",
        "futures",
        "series",
        "nonsense",
    ],
)
def test_unknown_type_strings_are_ignored(sports_market_type: str) -> None:
    assert parse_market_type(sports_market_type, "Spread: Chiefs (-3.5)") is None


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Spread: Chiefs (-3.5)", "spread"),
        ("Chiefs -3.5 vs Bills spread", "spread"),
        ("Will the Chiefs cover -3.5?", "spread"),
        ("Dodgers run line -1.5", "spread"),
        ("O/U 47.5", "total"),
        ("o/u 47.5", "total"),
        ("Over/Under 47.5", "total"),
        ("Over / Under 224.5", "total"),
        ("Total: 47.5", "total"),
        ("Chiefs vs. Bills: Total Points 47.5", "total"),
        ("Will the game go over 47.5 points?", "total"),
        ("Chiefs vs. Bills Moneyline", "moneyline"),
        ("Moneyline: Chiefs vs. Bills", "moneyline"),
        ("Will the Chiefs win?", "moneyline"),
        ("Will the Chiefs beat the Bills?", "moneyline"),
        ("Chiefs vs. Bills", "moneyline"),
        ("Chiefs vs Bills", "moneyline"),
        ("Lakers versus Celtics", "moneyline"),
        ("Twins vs. Tigers", "moneyline"),  # "win" inside "Twins" is not a word match
        ("Twins vs. Tigers O/U 8.5", "total"),
    ],
)
def test_question_heuristics_when_type_missing(question: str, expected: str) -> None:
    assert parse_market_type(None, question) == expected
    assert parse_market_type("", question) == expected
    assert parse_market_type("   ", question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "",
        "Will it rain in Kansas City on Sunday?",
        "Who will be the NFL MVP?",
        "Who will win NBA MVP?",
        "Will the Chiefs win the Super Bowl?",
        "Will the Yankees win the World Series?",
        "Will the Chiefs win the AFC Championship?",
        "Will the Yankees win the series?",
        "Will the Lakers make the playoffs?",
        "1st Half Spread: Chiefs (-1.5)",
        "1H O/U 24.5",
        "First Quarter Moneyline: Lakers vs. Celtics",
        "Q1 Total 55.5",
        "Will Mahomes throw 3+ touchdowns?",
        "Player props: Judge to hit a home run",
        "Team total: Chiefs over 24.5",
        "First five innings: Yankees vs. Dodgers",
        "Alternate spread: Chiefs (-7.5)",
    ],
)
def test_garbage_and_non_full_game_questions_return_none(question: str) -> None:
    assert parse_market_type(None, question) is None


def test_period_scoped_question_beats_known_type_string() -> None:
    assert parse_market_type("spreads", "1H Spread: Chiefs (-1.5)") is None
    assert parse_market_type("totals", "1st Half O/U 24.5") is None
    assert parse_market_type("moneyline", "Will the Chiefs win the Super Bowl?") is None
    # real single games that merely mention the round are still games
    assert parse_market_type("moneyline", "Super Bowl LXI: Chiefs vs. Eagles") == "moneyline"
    assert parse_market_type(None, "World Series Game 3: Yankees vs. Dodgers") == "moneyline"
    assert parse_market_type(None, "AFC Championship: Chiefs vs. Bills") == "moneyline"


def test_heuristic_priority_is_spread_then_total_then_moneyline() -> None:
    assert parse_market_type(None, "Chiefs vs. Bills: Spread (-3.5)") == "spread"
    assert parse_market_type(None, "Chiefs vs. Bills: O/U 47.5") == "total"
    assert parse_market_type(None, "Will the Chiefs win by 3.5 points? Spread") == "spread"


# --------------------------------------------------------------------------- parse_spread

NFL_OUTCOMES = ["Chiefs", "Bills"]


@pytest.mark.parametrize(
    "question,line,outcomes,league,expected",
    [
        ("Spread: Chiefs (-3.5)", -3.5, NFL_OUTCOMES, "nfl", (-3.5, "KC")),
        ("Spread: Chiefs (-3.5)", None, NFL_OUTCOMES, "nfl", (-3.5, "KC")),
        ("Spread: Chiefs (-3.5)", 3.5, NFL_OUTCOMES, "nfl", (-3.5, "KC")),  # question wins
        ("Chiefs -3.5", None, NFL_OUTCOMES, "nfl", (-3.5, "KC")),
        ("Kansas City Chiefs (+3.5)", None, NFL_OUTCOMES, "nfl", (3.5, "KC")),
        ("Chiefs -3.5 vs Bills", None, NFL_OUTCOMES, "nfl", (-3.5, "KC")),
        ("Bills +3.5 vs Chiefs", None, NFL_OUTCOMES, "nfl", (3.5, "BUF")),
        ("Bills +3.5 vs. Chiefs", None, NFL_OUTCOMES, "nfl", (3.5, "BUF")),
        ("Chiefs vs. Bills: Bills +3.5", None, NFL_OUTCOMES, "nfl", (3.5, "BUF")),
        ("Spread: Kansas City Chiefs (-3.5)", None, NFL_OUTCOMES, "nfl", (-3.5, "KC")),
        ("Spread: Chiefs (- 3.5)", None, NFL_OUTCOMES, "nfl", (-3.5, "KC")),
        ("Spread: Chiefs (−3.5)", None, NFL_OUTCOMES, "nfl", (-3.5, "KC")),  # unicode minus
        ("Spread: Chiefs (–3.5)", None, NFL_OUTCOMES, "nfl", (-3.5, "KC")),  # en dash
        ("Cowboys +7", None, ["Cowboys", "Eagles"], "nfl", (7.0, "DAL")),
        ("Spread: Eagles (-4.5)", -4.5, ["Cowboys", "Eagles"], "nfl", (-4.5, "PHI")),
        ("Spread: Celtics (-6.5)", -6.5, ["Lakers", "Celtics"], "nba", (-6.5, "BOS")),
        ("Spread: Nuggets (-3.5)", -3.5, ["Warriors", "Nuggets"], "nba", (-3.5, "DEN")),
        ("Spread: Dodgers (-1.5)", -1.5, ["Yankees", "Dodgers"], "mlb", (-1.5, "LAD")),
        ("Run line: Yankees (+1.5)", None, ["Yankees", "Dodgers"], "mlb", (1.5, "NYY")),
        ("Spread: White Sox (-1.5)", None, ["White Sox", "Cubs"], "mlb", (-1.5, "CWS")),
        ("Spread: Trail Blazers (+8.5)", None, ["Trail Blazers", "Lakers"], "nba", (8.5, "POR")),
        ("Will the Chiefs cover -3.5?", None, ["Yes", "No"], "nfl", (-3.5, "KC")),
        ("Spread: Chiefs (-3.5)", None, ["Yes", "No"], "nfl", (-3.5, "KC")),
        ("Spread: Chiefs (-3.5)", None, [], "nfl", (-3.5, "KC")),
        # date-like fragments are not spreads
        ("Chiefs vs. Bills 2026-09-20: Spread (-3.5)", None, NFL_OUTCOMES, "nfl", (-3.5, "KC")),
        ("Spread (-3.5): Chiefs vs Bills", None, NFL_OUTCOMES, "nfl", (-3.5, "KC")),
        # no signed number in the question: fall back to `line`
        ("Spread: Chiefs", -3.5, NFL_OUTCOMES, "nfl", (-3.5, "KC")),
        ("Spread: Chiefs", -3.5, ["Yes", "No"], "nfl", (-3.5, "KC")),
        ("Bills spread", 3.5, NFL_OUTCOMES, "nfl", (3.5, "BUF")),
        ("Spread: KC", -3.5, NFL_OUTCOMES, "nfl", (-3.5, "KC")),
        ("Chiefs vs. Bills: Spread", -3.5, NFL_OUTCOMES, "nfl", (-3.5, "KC")),  # first outcome
        ("Spread", -3.5, NFL_OUTCOMES, "nfl", (-3.5, "KC")),  # first outcome
        ("Spread", -1.5, ["Dodgers", "Yankees"], "mlb", (-1.5, "LAD")),
        ("", -3.5, NFL_OUTCOMES, "nfl", (-3.5, "KC")),
    ],
)
def test_parse_spread(question, line, outcomes, league, expected) -> None:
    result = parse_spread(question, line, outcomes, league)
    assert result is not None
    assert result[0] == pytest.approx(expected[0])
    assert result[1] == expected[1]
    assert isinstance(result[0], float)


@pytest.mark.parametrize(
    "question,line,outcomes,league",
    [
        ("Will it rain?", None, ["Yes", "No"], "nfl"),
        ("Will it rain?", -3.5, ["Yes", "No"], "nfl"),  # line but no team anywhere
        ("Spread", -3.5, ["Yes", "No"], "nfl"),
        ("Chiefs vs. Bills: Spread", None, NFL_OUTCOMES, "nfl"),  # no number, no line
        ("Spread: Ravens (-3.5)", None, NFL_OUTCOMES, "nfl"),  # team is not an outcome
        ("Spread: Ravens", -3.5, NFL_OUTCOMES, "nfl"),
        ("Chiefs vs. Bills 2026-09-20", None, NFL_OUTCOMES, "nfl"),  # date is not a line
        ("Nothing to see here +5", None, [], "nfl"),  # signed number but no team anywhere
        ("", None, NFL_OUTCOMES, "nfl"),
        ("Spread: Chiefs (-3.5)", None, NFL_OUTCOMES, "nhl"),  # unknown league
    ],
)
def test_parse_spread_returns_none(question, line, outcomes, league) -> None:
    assert parse_spread(question, line, outcomes, league) is None


def test_parse_spread_ignores_percentages_and_dates_as_lines() -> None:
    assert parse_spread("Chiefs -3.5 (Sept 20, 2026)", None, NFL_OUTCOMES, "nfl") == (-3.5, "KC")
    assert parse_spread("Chiefs +55% to win", 3.5, NFL_OUTCOMES, "nfl") == (3.5, "KC")


# --------------------------------------------------------------------------- parse_total


@pytest.mark.parametrize(
    "question,line,expected",
    [
        ("O/U 47.5", None, 47.5),
        ("o/u 47.5", None, 47.5),
        ("O/U: 47.5", None, 47.5),
        ("Over/Under 47.5", None, 47.5),
        ("Over / Under 224.5", None, 224.5),
        ("Over-Under 44.5", None, 44.5),
        ("Over/Under: 224.5", None, 224.5),
        ("Total: 47.5", None, 47.5),
        ("Total 8", None, 8.0),
        ("Total Points: 47.5", None, 47.5),
        ("Total Runs: 8.5", None, 8.5),
        ("47.5 points", None, 47.5),
        ("Chiefs vs. Bills: Total Points 47.5", None, 47.5),
        ("Chiefs vs. Bills O/U 47.5", None, 47.5),
        ("Yankees vs. Dodgers: O/U 8.5 runs", None, 8.5),
        ("Over 47.5?", None, 47.5),
        ("Will the total be over 231.5 points?", None, 231.5),
        ("Chiefs vs Bills 2026-09-20 O/U 47.5", None, 47.5),
        ("O/U 47.5", 48.5, 47.5),  # question wins over line
        ("Chiefs vs Bills", 47.5, 47.5),  # fall back to line
        ("", 7.5, 7.5),
        ("Will the game go over?", 47.5, 47.5),
        ("O/U 231.5", 231.5, 231.5),
    ],
)
def test_parse_total(question: str, line: float | None, expected: float) -> None:
    result = parse_total(question, line)
    assert result == pytest.approx(expected)
    assert isinstance(result, float)


@pytest.mark.parametrize(
    "question,line",
    [
        ("Chiefs vs Bills", None),
        ("Will it rain?", None),
        ("", None),
        ("Chiefs vs Bills 2026-09-20", None),  # date is not a total
        ("Total", None),
        ("Total: 2026", None),  # four digits is not a total
        ("over the moon", None),
    ],
)
def test_parse_total_returns_none(question: str, line: float | None) -> None:
    assert parse_total(question, line) is None


def test_parse_total_line_types() -> None:
    assert parse_total("", 47) == 47.0
    assert parse_total("", "47.5") == 47.5  # type: ignore[arg-type]  # Gamma sends strings
    assert parse_total("", "n/a") is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- parse_json_list


@pytest.mark.parametrize(
    "value,expected",
    [
        ('["Chiefs","Bills"]', ["Chiefs", "Bills"]),
        ('["Yes", "No"]', ["Yes", "No"]),
        ('["0.55","0.45"]', ["0.55", "0.45"]),  # strings stay strings
        ('["1","0"]', ["1", "0"]),
        ("[]", []),
        ('["' + "7" * 77 + '", "1"]', ["7" * 77, "1"]),  # 70+ digit token ids
        ('"[\\"a\\", \\"b\\"]"', ["a", "b"]),  # double-encoded
        (["a", "b"], ["a", "b"]),
        (("a", "b"), ["a", "b"]),
        ([], []),
        (None, []),
        ("", []),
        ("   ", []),
        ("not json", []),
        ('{"a": 1}', []),
        ('"just a string"', []),
        ("42", []),
        (42, []),
        ("[1, 2", []),  # malformed
    ],
)
def test_parse_json_list(value, expected) -> None:
    result = parse_json_list(value)
    assert result == expected
    assert isinstance(result, list)


def test_parse_json_list_returns_a_copy() -> None:
    original = ["a"]
    result = parse_json_list(original)
    assert result == original and result is not original


# --------------------------------------------------------------------------- review fixes


@pytest.mark.parametrize(
    "question",
    [
        "NBA Finals: Thunder vs. Pacers",
        "World Series: Yankees vs. Dodgers",
        "Thunder vs. Pacers (Series)",
        "Yankees vs. Dodgers - ALDS",
        "NLCS: Dodgers vs. Braves",
        "NL Wild Card: Padres vs. Braves",
        "Eastern Conference Finals: Celtics vs. Knicks",
        "Playoffs: Lakers vs. Nuggets",
        "Best of 7: Celtics vs. Knicks",
        "Western Conference Semifinals: Thunder vs. Nuggets",
    ],
)
def test_series_titled_like_games_are_not_games_without_a_type(question: str) -> None:
    """With no `sportsMarketType`, a series / round winner wearing a game title must not be
    priced as a moneyline against game 1's book odds."""
    assert parse_market_type("", question) is None
    assert parse_market_type(None, question) is None
    # an explicit game type from Gamma still wins over the wording
    assert parse_market_type("moneyline", question) == "moneyline"


def test_numbered_series_games_are_games() -> None:
    assert parse_market_type(None, "World Series Game 3: Yankees vs. Dodgers") == "moneyline"
    assert parse_market_type("", "NBA Finals Game 7: Thunder vs. Pacers") == "moneyline"
    assert parse_market_type("", "NBA Finals Game 7: O/U 210.5") == "total"
    assert parse_market_type("", "ALCS Game 2 Spread: Yankees (-1.5)") == "spread"
