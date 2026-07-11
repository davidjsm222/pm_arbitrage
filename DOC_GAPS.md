# River Markets documentation gaps

Checked against the public documentation and `rivermarkets==0.4.0` from PyPI
on 2026-07-10.

## Orderbook SDK surface conflicts with the published package

- Page: `https://docs.rivermarkets.com/ws-api-reference/orderbooks`
- The minimal SDK client uses `client.ws.orderbooks()` and then
  `stream.subscribe(river_ids)`. The current PyPI package exposes no `client.ws`;
  it exposes `client.realtime.orderbooks(river_ids)` instead.
- The same page reads `msg.data.best_bid_price`, but 0.4.0 parses WebSocket
  envelopes with an open generic `Message` model and leaves `data` as a plain
  `dict`, so attribute access fails. The Python SDK page correctly uses
  `msg.data["best_bid_price"]`.
- Page also in conflict: `https://docs.rivermarkets.com/sdk/python` documents
  the installed `.realtime.orderbooks(river_ids)` surface.
- Live observation on 2026-07-10: on the first subscription, river IDs `1383`
  and `691214` delivered an `update` as their first book-data frame rather than
  a `snapshot`. Two other active search results (`691222`, `691240`) delivered
  no book-data frame during 30 seconds. After active books were warmed and the
  socket reconnected, explicit snapshots arrived. The page says a cached book
  gets a snapshot immediately, while an uncached book gets `pending` followed
  by a snapshot; consumers should be told that an update may arrive first and
  that active market status does not guarantee book-feed availability.
- Live `exchange_timestamp` values arrived without a UTC offset, for example
  `2026-07-11T02:19:57.086000`, while the page's payload example includes
  `+00:00`. The page should state whether offset-less wire timestamps are
  guaranteed to be UTC; otherwise standard ISO-8601 parsers produce naive
  datetimes and freshness comparisons can be unsafe.

## Async client lifecycle is undocumented

- Page: `https://docs.rivermarkets.com/sdk/python`
- `AsyncRiverMarkets` owns an `httpx.AsyncClient`, but 0.4.0 exposes no public
  `aclose()` or async context-manager method and the page gives no cleanup
  guidance. Short-lived commands must reach through two private wrappers to
  close the underlying client cleanly.

## Generic-asset matching semantics are unspecified

- Pages:
  - `https://docs.rivermarkets.com/api-reference/generic-assets/list-generic-assets`
  - `https://docs.rivermarkets.com/api-reference/generic-assets/get-generic-asset`
- `owner=platform` exposes platform-curated approved baskets and the detail
  route resolves their member markets, but the docs do not define whether
  members are guaranteed to share identical resolution criteria/outcomes, how
  cross-exchange outcome orientation is represented, or what review/maintenance
  standard “approved” implies. Those guarantees are needed before using a
  basket as an equivalence assertion.

## River ID concept wording implies cross-exchange identity

- Page: `https://docs.rivermarkets.com/concepts/river-ids`
- “Reference the same market consistently regardless of which exchange it
  trades on” can be read as one ID spanning equivalent listings. In practice,
  each exchange listing has its own River ID. The page should state that IDs
  normalize identifier format but do not assert cross-venue equivalence.

## Polymarket US is not distinguishable in market search

- Pages:
  - `https://docs.rivermarkets.com/api-reference/markets/search-markets`
  - `https://docs.rivermarkets.com/api-reference/overview`
- The overview says Polymarket US is available, while market search documents
  only `exchange_name=POLYMARKET`. The result schema has no documented venue or
  jurisdiction field that distinguishes Polymarket US listings from other
  Polymarket listings.

## Fee schedules are external, time-varying, and absent from River metadata

- River pages:
  - `https://docs.rivermarkets.com/api-reference/markets/search-markets`
  - `https://docs.rivermarkets.com/ws-api-reference/orderbooks`
- External source: `https://kalshi.com/docs/kalshi-fee-schedule.pdf`, effective
  July 7, 2026.
- Kalshi publishes a time-versioned per-series table rather than one permanent
  global rule. `KXMENWORLDCUP` is explicitly listed with maker multiplier `1`
  and taker multiplier `1`; its current taker fee therefore uses the general
  `0.07 × C × p × (1-p)` curve. River's documented market and orderbook models
  expose no applicable fee coefficient, multiplier, schedule version, or fee
  rounding metadata. The monitor must currently maintain and periodically
  revalidate this external mapping instead of deriving fees from River data.
- Polymarket US's official schedule at `https://docs.polymarket.us/fees` is
  confirmed, not unknown: effective July 1, 2026, taker theta is `0.06` with
  fees rounded to the nearest cent using banker's rounding. A zero-fee default
  is no longer valid.
- Future Kalshi finance expansion warning: Kalshi's February 5, 2026 fee
  schedule explicitly assigned the reduced coefficient `0.035` to S&P 500
  (`INX*`) and Nasdaq-100 (`NASDAQ100*`) markets, so `0.07` has not been
  platform-wide. This is out of scope for the current sports-only universe.
  The July 7, 2026 schedule is structured differently and lists `KXINXY` and
  `KXNASDAQ100Y` in its per-series multiplier table, so the effective schedule
  must be rechecked at expansion time rather than treating `0.035` as another
  permanent global constant.
