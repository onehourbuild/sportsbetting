# Research notes (2026-09-18)

Collected from open-source clients (Polymarket `agent-skills`, `agents`,
`py-clob-client`, GoPolymarket `polymarket-go-sdk`, ivanzzeth
`polymarket-go-gamma-client`, The Odds API `samples-python`, pseudo-r
`Public-ESPN-API`) and web searches. The build environment could not reach the
live APIs, so everything below is from source code and docs, not from live
responses. See **Unverified** at the bottom.

## Polymarket Gamma API (keyless)

Base: `https://gamma-api.polymarket.com`

- `GET /events?tag_slug=nfl&active=true&closed=false&limit=100&offset=0`
  Also `tag_slug=nba`, `tag_slug=mlb`. Other filters: `series_id`,
  `start_date_min`, `start_date_max`, `end_date_min`, `sports_market_types`
  (repeatable), `order=start_date&ascending=true`. Paginate with `offset`
  until fewer than `limit` results come back.
- `GET /events/slug/{slug}` — one event.
- `GET /sports` — sport metadata `{sport, tags[], series, resolution}`.
- `GET /sports/market-types` — `{marketTypes: [...]}`.
- `GET /teams?league=nfl` — `{id, name, league, abbreviation, alias, record}`.
- `GET /markets?slug=...`, `GET /markets/{id}`.

Event fields: `id, slug, title, startDate, endDate, active, closed, archived,
liquidity, volume, markets[], tags[], negRisk, competitionState`.
Sports event slug pattern seen in docs: `nfl-lac-buf-2025-01-26`
(league-team-team-date). Do **not** rely on which team is home from the slug;
use market outcomes plus team resolution.

Market fields (subset): `id, question, slug, conditionId, clobTokenIds
(JSON-encoded string list), outcomes (JSON-encoded string list),
outcomePrices (JSON-encoded string list), bestBid, bestAsk, spread,
lastTradePrice, gameStartTime, sportsMarketType, line, acceptingOrders,
enableOrderBook, closed, active, endDate, orderPriceMinTickSize,
orderMinSize, liquidityNum, volumeNum, negRisk, takerBaseFee, makerBaseFee,
teamAID, teamBID, umaResolutionStatus, resolvedBy`.

`outcomes`, `outcomePrices`, `clobTokenIds` arrive as **strings containing
JSON arrays** — `json.loads` them. Index i of each aligns.

Expected `sportsMarketType` values: `moneyline`, `spreads`, `totals` (plus
others we ignore). For `spreads` the `line` is a float and the `question`
usually names the team and signed line, e.g. `Spread: Chiefs (-3.5)`. For
`totals` outcomes are `Over`/`Under` and `line` is the total. After
resolution, `outcomePrices` becomes `["1","0"]` or `["0","1"]` and `closed`
is true.

## Polymarket CLOB API (keyless reads)

Base: `https://clob.polymarket.com`

- `GET /book?token_id=…` → `{market, asset_id, bids:[{price,size}],
  asks:[{price,size}], tick_size, min_order_size, neg_risk, hash, timestamp}`
  (prices and sizes are strings).
- `POST /books` body `[{"token_id": "…"}, …]` → list of books (≤500).
- `GET /price?token_id=…&side=BUY` → `{price}` (best ask for BUY).
- `GET /midpoint?token_id=…` → `{mid}`; `GET /spread?token_id=…`.
- `GET /fee-rate?token_id=…` — per-token fee rate (`py-clob-client`
  `get_fee_rate_bps`). Shape unverified; treat as optional.

## Polymarket fees (effective July 2026)

- Takers on **sports** markets pay `shares × 0.05 × p × (1 − p)`; max
  $1.25 per 100 shares at p = 0.50. Makers pay 0. 15% of sports taker fees are
  rebated to makers.
- Other categories: politics/finance/tech 0.04 (max $1.00/100), crypto 0.07
  (max $1.75/100). We only care about sports.
- Effective buy cost per share at price `a`: `a + 0.05 × a × (1 − a)`.

## The Odds API v4

Base: `https://api.the-odds-api.com/v4`

- `GET /sports?apiKey=…` (free, no credit cost).
- `GET /sports/{sport_key}/odds?apiKey=…&regions=us,eu&markets=h2h,spreads,totals&oddsFormat=american&dateFormat=iso`
  or `…&bookmakers=pinnacle,betonlineag,lowvig,circasports,draftkings,fanduel`.
- Sport keys: `americanfootball_nfl`, `basketball_nba`, `baseball_mlb`.
- Response: list of `{id, sport_key, sport_title, commence_time (ISO),
  home_team, away_team, bookmakers:[{key, title, last_update,
  markets:[{key: h2h|spreads|totals, last_update, outcomes:[{name, price,
  point?}]}]}]}`. Spread `outcomes[].name` is the team, `point` is the
  team's spread; totals `name` is `Over`/`Under`, `point` is the total.
- Headers: `x-requests-remaining`, `x-requests-used`, `x-requests-last`.
- **Cost:** `markets × regions` per call; with `bookmakers=` every 10
  bookmakers count as 1 region. Free tier = 500 credits/month. 3 markets ×
  1 region = 3 credits per league per refresh, 9 for all three leagues.
- Bookmaker keys (docs): `pinnacle` (eu), `betonlineag`, `lowvig`,
  `bovada`, `draftkings`, `fanduel`, `betmgm`, `williamhill_us` (Caesars),
  `circasports` (us2), `betrivers`, `pointsbetus`, `unibet_eu`.
  Whether Pinnacle is still on the free plan in 2026 is disputed; if it is
  missing from responses the consensus simply uses the books that answer.
- Team names are full names: `Kansas City Chiefs`, `Los Angeles Clippers`,
  `Athletics`.

## ESPN scoreboard (keyless, single book)

- `GET https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=YYYYMMDD`
  (`basketball/nba`, `baseball/mlb`). `dates` is the **US Eastern** calendar date: a
  West-coast night game that starts 02:10Z on the 20th is listed under the 19th. The
  scan requests ET yesterday, today and tomorrow and de-duplicates by event id.
  Returns `events[]` with
  `competitions[0].competitors[] {homeAway, team{displayName,
  abbreviation, shortDisplayName}, score}`, `status.type.completed`, and
  `competitions[0].odds[] {provider{name}, details ("KC -3.5"), overUnder,
  homeTeamOdds{moneyLine, spreadOdds}, awayTeamOdds{…}}`.
- Send a non-browser User-Agent. Odds vanish for finished games.

## PWA on phones

- iOS 16.4+: manifest + `display: standalone` + service worker + HTTPS →
  Add to Home Screen gives a standalone app. iOS 26 makes every home-screen
  site open as a web app. Push requires home-screen install; not needed v1.
- Provide `apple-touch-icon` 180 px, `theme-color`, and a 192/512 icon set.

## Hosting (for the README)

- Fly.io: shared-cpu-1x ≈ $2/month + 1 GB volume ≈ $0.15/month, auto-stop.
  No permanent free tier in 2026.
- Render free web services have ephemeral disks (SQLite would be wiped).
- Railway: $5 one-time trial credit, Hobby $5/month.

## Unverified (could not hit live APIs from the build environment)

1. Exact `sportsMarketType` strings and the sign convention of `line` for
   spreads. Parser reads the `question` text for `(±x.x)` and a team name and
   falls back to `line`. Unparseable markets are listed on Diagnostics.
2. Whether Gamma nests `markets[]` with all listed fields under
   `/events?tag_slug=` responses (the Go SDK types say yes).
3. `GET /fee-rate` response shape. Fee rate defaults to 0.05 from Settings.
4. Whether Pinnacle appears in free-tier Odds API responses.
5. Polymarket `outcomes` for moneyline: short nickname (`Chiefs`) vs full
   name. Team resolution accepts city, nickname, full name, and abbreviation.
6. Which Polymarket outcome / `teamAID` is the home team (the client assumes
   `[away, home]`; matching is order-insensitive so only display names are at
   risk).
7. Units of `takerBaseFee`. The client reads basis points but believes the result only
   inside a plausible sports band (`0.01 <= bps / 10000 <= 0.2`). A fraction (`0.05`),
   a percent (`5`) or a flag (`1`) would otherwise divide down to a near-zero rate that
   silently removes the taker fee and manufactures edge. Anything outside the band is
   logged at WARNING and the fee-rate preference applies instead; every override the
   parser saw is recorded on `PolymarketClient.taker_fee_overrides` for Diagnostics.
8. Whether `GET /markets/{id}` nests `events[]` with tags (the client falls back
   to the slug prefix, then to the league remembered from `events()`, then to the
   `league=` hint the scan passes from the stored `Game.league`).
9. Whether omitting the `closed` param on `/events` returns both open and
   closed events (the client re-filters closed events itself).
10. `parse_market_type` treats an unknown non-empty `sportsMarketType` as ignored
    rather than falling back to question heuristics; if live Gamma uses an
    unexpected string every market will show on Diagnostics as
    "unsupported market type" until `_MARKET_TYPE_ALIASES` in
    `app/core/parsing.py` is extended.
11. ESPN scoreboard date values may omit seconds (`"2026-09-20T20:25Z"`);
    `parse_iso_utc` handles both.
12. ESPN abbreviations that differ from canonical keys (GS, WSH, SA, NY, UTAH,
    CHW, AZ, OAK) are handled in `to_book_games` by falling back to
    `matching.team_key`.
13. `/events` is requested with `order=id&ascending=true` and the documented
    `sports_market_types=moneyline&sports_market_types=spreads&sports_market_types=totals`
    filter (repeated query key). Neither has been exercised live. Both are optimisations
    (the client re-filters market types and de-duplicates events itself), so a 4xx on the
    first page now makes `PolymarketClient._events_page` retry once without them, log at
    WARNING and set `PolymarketClient.events_filters_dropped` for the rest of that
    client's life, instead of costing every league every market.
14. Whether a series / round winner can arrive with an empty `sportsMarketType` and a
    game-like title ("NBA Finals: Thunder vs. Pacers"). The parser rejects series
    nouns (finals, series, playoffs, semifinals, wild card, ALDS/ALCS/NLDS/NLCS, "best
    of") without a game number when the type is missing, and requires a
    `gameStartTime` for any type inferred from the question; "Championship" and
    "Super Bowl" are treated as single games.
15. Whether in-play markets keep `acceptingOrders: true` and stay in the
    `closed=false` slate (assumed yes: Polymarket trades in-game). The scan therefore
    skips any market whose `gameStartTime` (or the matched book's `commence_time`) is
    at or before the scan time, whatever the flags say.
16. ESPN's scoreboard date bucketing is assumed to follow US Eastern time (see above);
    if it is UTC after all, the three-day window still covers it.
17. Whether every Gamma market carries `acceptingOrders`. Older payload variants are
    reported to carry only `enableOrderBook` / `active`, and treating an absent flag as
    False made a scan store quotes for everything and price nothing. The client now falls
    back to those two fields and, when nothing in the payload says the market is tradable
    (and it is not closed), reports it as unparseable with the distinct reason
    "acceptingOrders missing (enableOrderBook/active off)" so the payload change is
    recognisable on Diagnostics instead of hiding in the generic
    "market not accepting orders" list.
18. Whether an event's `startDate` ever equals kickoff. It is generally the listing /
    creation timestamp, so it is NOT used as a `game_start` fallback any more: a market
    without `gameStartTime` keeps `game_start = None` (matching falls back to its unique
    home/away pair rule and the scan uses the matched book's `commence_time`). The old
    fallback backdated the whole game and made the scan treat it as already started.
19. ESPN per-side prices: `homeTeamOdds.spreadOdds` / `awayTeamOdds.spreadOdds` and
    `overOdds` / `underOdds` on the odds block, assumed to be American prices like
    `moneyLine`. ESPN is the only book when there is no Odds API key, so a spread or
    total is contributed only when both sides carry a real price; assuming -110/-110
    de-vigged to exactly 0.5 whatever the real price was and turned any side asking
    below ~0.475 into a fabricated opportunity. Moneylines are unaffected.
