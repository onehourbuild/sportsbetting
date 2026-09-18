# FIXTURES — the synthetic slate every fixture file must agree on

Fixed "now" for tests and demo: **2026-09-19T15:00:00Z**. All fixture files
(`fixtures/gamma_events_*.json`, `fixtures/clob_books.json`,
`fixtures/oddsapi_*.json`, `fixtures/espn_scoreboard_*.json`) describe the
same games with the same teams and start times so matching succeeds in demo
mode. Ids are synthetic. Prices below are chosen so the expected edges hold
with min_edge 0.02, taker fee 0.05, power de-vig, default book weights.

Bookmakers in the Odds API fixtures: `pinnacle`, `betonlineag`, `lowvig`,
`draftkings`, `fanduel`. Give every book the same line unless a row says
otherwise; vary prices by a few cents so the consensus is not degenerate
(the "Books" column is the pinnacle price; other books within ±5 cents).

| # | League | Away @ Home | Start (UTC) | Books (pinnacle) | Polymarket markets (best ask per outcome) | Expected |
|---|---|---|---|---|---|---|
| 1 | nfl | KC Chiefs @ BUF Bills | 2026-09-20T20:25:00Z | ML KC -150 / BUF +130; spread KC -3.5 (-110/-110); total 47.5 (-110/-110) | ML ["Chiefs","Bills"] asks 0.55 / 0.46; spread "Spread: Chiefs (-3.5)" line -3.5 ["Chiefs","Bills"] asks 0.50 / 0.52; total "O/U 47.5" line 47.5 ["Over","Under"] asks 0.49 / 0.53 | Chiefs ML edge ≈ +0.021 (opp); spread/total no opp |
| 2 | nfl | DAL Cowboys @ PHI Eagles | 2026-09-20T17:00:00Z | ML PHI -200 / DAL +170; spread PHI -4.5; total 44.5 | ML ["Cowboys","Eagles"] asks 0.34 / 0.66; spread line -4.5 team PHI asks 0.51/0.51; total 44.5 asks 0.50/0.50 | no opp (Eagles fair 0.651 < eff 0.674; Cowboys fair 0.349 vs eff 0.351) |
| 3 | nba | LAL Lakers @ BOS Celtics | 2026-10-22T23:30:00Z | ML BOS -240 / LAL +195; spread BOS -6.5; total 224.5 | ML ["Lakers","Celtics"] asks 0.31 / 0.66; spread line -6.5 team BOS asks 0.50/0.52; total 224.5 asks 0.50/0.51 | Celtics ML edge ≈ +0.022 (opp) |
| 4 | nba | GSW Warriors @ DEN Nuggets | 2026-10-23T02:00:00Z | ML DEN -130 / GSW +110; spread DEN -2.5; total 231.5 (O -105 / U -115) | ML ["Warriors","Nuggets"] asks 0.48 / 0.55; spread **line -3.5** team DEN asks 0.48/0.54 (line mismatch on purpose); total 231.5 asks 0.47 / 0.49 | no opp; spread market appears in Diagnostics as "no book at line" |
| 5 | mlb | NYY Yankees @ LAD Dodgers | 2026-09-20T02:10:00Z | ML LAD -140 / NYY +120; run line LAD -1.5 (+120) / NYY +1.5 (-140); total 8.5 | ML ["Yankees","Dodgers"] asks 0.40 / 0.58; spread line -1.5 team LAD asks 0.44/0.58; total 8.5 asks 0.50/0.52 | Yankees ML edge ≈ +0.026 (opp) |
| 6 | mlb | ATH Athletics @ SEA Mariners | 2026-09-20T01:40:00Z | ML SEA -165 / ATH +140; total 7.5 (-110/-110) | ML ["Athletics","Mariners"] asks 0.41 / 0.62; total "O/U 7.5" asks 0.55 / 0.45 | Under 7.5 edge ≈ +0.038 (opp) |
| 7 | mlb | BAL Orioles @ TOR Blue Jays | 2026-09-17T23:07:00Z | (finished; not in Odds API fixture) | ML ["Orioles","Blue Jays"] **closed: true**, outcomePrices ["0","1"] | used for settlement tests: a demo bet on Orioles settles as lost |

Polymarket event slugs: `nfl-kc-buf-2026-09-20`, `nfl-dal-phi-2026-09-20`,
`nba-lal-bos-2026-10-22`, `nba-gsw-den-2026-10-22`, `mlb-nyy-lad-2026-09-19`,
`mlb-ath-sea-2026-09-19`, `mlb-bal-tor-2026-09-17`. Market ids `5xxxxx`,
event ids `1xxxx`, token ids 70+ digit numeric strings, condition ids `0x…`.
Odds API game ids are 32-char hex. ESPN ids are 9-digit numbers.

CLOB fixture (`clob_books.json`): a JSON object keyed by token id with
`{bids:[{price,size}], asks:[{price,size}], tick_size:"0.01", min_order_size:"5"}`
as strings, matching the CLOB `/books` response items (`asset_id` also set).
Best ask must equal the ask in the table; include 2–3 deeper levels per side
with sizes 100–2000 shares so `walk_asks` has something to walk.

ESPN fixture: NFL scoreboard for 2026-09-20 with games 1 and 2 and ESPN BET
odds (`details` "KC -3.5", `overUnder` 47.5, moneylines), plus an MLB
scoreboard for 2026-09-17 with game 7 completed, score BAL 2 – TOR 5.
