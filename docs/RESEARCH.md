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
  (`basketball/nba`, `baseball/mlb`). Returns `events[]` with
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
