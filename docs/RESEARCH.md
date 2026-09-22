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
    recognizable on Diagnostics instead of hiding in the generic
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

## Verified live 2026-09-18 (first run against the real APIs)

The list above was written from source code and docs. These are observations from real
responses, and they supersede the matching "Unverified" entries.

- **Gamma `/events?tag_slug=` works as described**: events nest `markets[]` with the listed
  fields (item 2), `outcomes` / `outcomePrices` / `clobTokenIds` arrive as JSON-encoded
  strings, and the optional `order` / `sports_market_types` filters were accepted — no
  fallback was triggered (item 13). NFL returned 3,281 parseable markets and MLB 332.
- **`sportsMarketType` (item 1, item 10).** Confirmed `moneyline`, `spreads`, `totals`. The
  live slate also carries many types the app deliberately ignores — `first_half_spreads`,
  `second_half_spreads`, `q1_spreads`, `team_totals`, `first_touchdowns`,
  `receiving_yards`, `baseball_player_hits`, `baseball_player_total_bases` and more — plus
  a large tail with `sportsMarketType: null` (season futures, award and novelty markets).
  These land on Diagnostics as "unsupported market type", which is **expected**, not a
  parser failure: a normal NFL scan lists ~15k of them.
- **`takerBaseFee` (item 7) is `1000` on sports markets.** Read as basis points that is
  0.10, inside the plausible band, so it is accepted and applied — while Polymarket's fee
  docs state a 0.05 coefficient for sports. The unit remains unverified and the two
  disagree. Left as-is deliberately: too high a fee understates edge, which can only hide
  an opportunity, never invent one. Every override is on Diagnostics.
- **ESPN requires a recognized HTTP-client User-Agent** (new, item 20 below).
- **ESPN's odds block moved** (new, item 21 below) — items 12 and 19 are superseded.
- **NBA out of season** (mid-September): 509 events, every one a future or novelty market,
  so zero game markets. Correct behavior, but worth knowing before calling it a bug.

20. `site.api.espn.com` returns `403 Access Denied` for a custom User-Agent and serves
    requests naming a known client (`curl/…`, `python-httpx/…`, `python-requests/…`,
    `okhttp/…`, `Go-http-client/…`). A browser UA is also refused. `USER_AGENT` therefore
    leads with the httpx token. If ESPN data ever disappears again, test the User-Agent
    first — the failure is a clean 403 on every call, visible on Diagnostics as an error.
21. ESPN's scoreboard odds are now `odds[0].{moneyline, pointSpread, total}`, each with
    `{home, away}` or `{over, under}`, each of those with `{open, close}` holding string
    `odds` and (for spreads/totals) a string `line` — `"+123"`, `"+1.5"`, `"o8.5"`. The
    flat `homeTeamOdds.moneyLine`, `homeTeamOdds.spreadOdds`, `overOdds` and `underOdds`
    fields are absent. Both shapes are read; `close` wins over `open`, legacy wins over
    nested. `details` is the spread for NFL/NBA but the **moneyline** for MLB, so it is no
    longer used when `pointSpread` is present and is bounded by `MAX_SPREAD_POINTS`.
22. Still unverified: settlement from `outcomePrices` after a real resolution, CLV capture
    from a real closing snapshot, and every Odds API code path (no key was available).

## Verified live 2026-09-20 (wallet import and Kalshi)

- `GET https://data-api.polymarket.com/trades?user=<wallet>&limit=500&offset=N` is public
  and keyless, returns a JSON list newest first, and pages by `offset`. Each row:
  `proxyWallet`, `side` (BUY|SELL), `asset` (CLOB token id), `conditionId`, `size`
  (shares), `price`, `timestamp` (epoch seconds), `title`, `slug`, `eventSlug`,
  `outcome`, `outcomeIndex`, `transactionHash`, plus profile fields. `takerOnly=false`
  returned the same 336 rows as the default for a live wallet. Same host rate-limits
  fast loops (see the back test's backoff); the importer makes one request per 500 fills.
- Kalshi public API (no key): `GET https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXNFLGAME&status=open`
  lists game markets (ticker `KXNFLGAME-26SEP21NYGLAR-LAR`). The list endpoint returned
  `null` prices; `GET /markets/{ticker}` carries them as strings in `yes_bid_dollars`,
  `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`, sizes in `*_size_fp`, and
  `GET /markets/{ticker}/orderbook?depth=5` gives `orderbook_fp.yes_dollars` /
  `no_dollars` as `[price, size]` pairs. `close_time` is not kickoff (three days after).

## Verified live 2026-09-21 (Polymarket US is a separate exchange)

Polymarket ships two products and the app only knows about one of them.

- **polymarket.com** blocks US residents from trading and shows an interstitial pointing
  at polymarket.us. Identity there is an on-chain proxy wallet; fills are public on
  `data-api.polymarket.com/trades?user=<wallet>`.
- **polymarket.us** is the US-regulated exchange. There is **no wallet**: accounts hold
  cash, and programmatic access is by API key. A .us profile page contains no `0x`
  address at all.
- Public .us market data is **keyless**: `https://gateway.polymarket.us/v1/events?limit=`,
  `/v1/events/slug/<slug>`, `/v1/markets?limit=`, `/v1/search?q=`. Slugs look like
  `nfl-nyg-lar-2026-09-21` (event) and `asc-nfl-nyg-lar-2026-09-21-pos-6pt5` (market).
  An event embeds its markets with `outcomes`, `outcomePrices`, `bestBidQuote`,
  `bestAskQuote`, `feeCoefficient`, `orderPriceMinTickSize`, `minimumTradeQty`,
  `gameStartTime`, `status` and `sportsMarketType`. One NFL game carried 814 markets.
- **The .us taker fee coefficient is 0.0695**, on all 814 markets of the game checked,
  against 0.05 on .com. `prefs.taker_fee_rate` defaults to the .com value.
- Authenticated .us API (`https://api.polymarket.us`) needs headers `X-PM-Access-Key`,
  `X-PM-Timestamp` (ms, within 30s of server time) and `X-PM-Signature` (base64 Ed25519
  over `timestamp + method + path`). `GET /v1/portfolio/activities` returns trades with
  `marketSlug`, `createTime`, `price`, `qty`, `costBasis`, `realizedPnl` and
  `isAggressor` (maker vs taker), paged by `nextCursor` / `eof`; filter with
  `types=ACTIVITY_TYPE_TRADE`. Keys are created by the account holder at
  `polymarket.us/developer` after identity verification and shown once.
- Caution: .us outcome labels contradict each other. The market titled "Los Angeles Rams
  wins by over 6.5 points" has `question` "Will the New York Giants cover 6.5…" and
  `outcomes` `["-6.50","+6.50"]`. Resolve the side by price against a known reference,
  not by the sign in the label.



## Unverified: the .us signing payload

`signature_for` signs `timestamp + method + path` with the path alone, no query string,
because that is what the live notes of 2026-09-21 record. Whether the API includes the
query string could not be settled here -- this environment cannot reach
`api.polymarket.us`, and the account holder is the only person who can mint a key.

This is a safe assumption to be wrong about. A bad signature fails authentication loudly
and immediately; unlike a mis-parsed price it cannot quietly cost money. If the first live
call returns 401, signing `path + "?" + urlencode(params)` is the next thing to try.
