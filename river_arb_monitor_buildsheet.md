# Cross-Venue Arb Monitor — Build Sheet

> ⚠️ NO TRADING CALLS IN THIS REPO. Monitor only. No create_order, no order
> placement of any kind, until this line is deliberately removed and replaced
> with an explicit go-ahead. v1 is a measured characterization of cross-venue
> edge, not a trading system. Legging risk is unmodeled and out of scope until
> that changes on purpose.

**Scope.** Kalshi and Polymarket US only. Both USD-settled, no crypto rail on the execution path. Monitor first, trade later. The v1 deliverable is not profit, it is a measured characterization of how much real, capturable edge exists across the two venues once you are honest about fills, fees, and staleness.

**Stack.** Python, asyncio, River SDK for feeds, an in-memory book store, and a durable log (SQLite or Parquet) for the analysis layer. A small FastAPI + React dashboard is optional and only worth it after the core loop works.

**Before you write code.** Two things to pull that this sheet cannot verify for you.
1. River's exact SDK method names for orderbook subscription, market metadata, and market matching. This sheet refers to them by function. Confirm the real calls in their docs.
2. The live fee schedules. Kalshi's fee coefficient varies by market and changes. Polymarket has historically run a zero trading fee but the US-regulated entity may differ. Do not hardcode a number you have not confirmed this week.

---

## The arb structure (read this first, everything else depends on it)

Prediction-market contracts are binary. A YES contract trades in [0, 1], settles at 1 if the event happens and 0 if it does not. NO is the complement. Buying NO at price `n` is the same trade as selling YES at `1 − n`.

The lock is a Dutch book across venues. To capture it you buy the cheap side on one venue and the expensive side on the other so that exactly one leg pays out 1 dollar no matter what happens.

Concretely, for the same event on venue A and venue B:

- Buy YES on A at its ask, `ask_yes_A`. You pay `ask_yes_A` per contract.
- Buy NO on B, which costs `ask_no_B = 1 − bid_yes_B` per contract.
- One of the two legs settles at 1. Guaranteed payout is 1 dollar per pair.

So the gross profit per contract pair is

```
gross = 1 − ask_yes_A − ask_no_B
      = 1 − ask_yes_A − (1 − bid_yes_B)
      = bid_yes_B − ask_yes_A
```

And the net, after each venue's fee:

```
net_per_pair = (bid_yes_B − ask_yes_A) − fee_A − fee_B
```

An arb exists when `bid_yes_B − ask_yes_A > fee_A + fee_B`. In plain terms, the price you can sell YES for on B minus the price you buy YES for on A has to clear the combined fees.

Always check both directions on every pair. The mirror is buy YES on B, buy NO on A, arb when `bid_yes_A − ask_yes_B > fees`.

**Why this formulation matters.** It falls straight out of executable prices, buy at the ask, sell at the bid, so it forces you past the mid-price mistake automatically. If you had written the naive `mid_A − mid_B` version, you would be pricing a trade nobody can fill.

---

## Architecture

One async event loop. Per venue, a WebSocket consumer feeds a shared in-memory book store keyed by River ID. On every book update for a market, recompute the edge for that market's cross-venue pair and run it through the gates. Survivors get written to the log.

```
River WS (Kalshi side) ─┐
                        ├─> book store {river_id: {venue: top_levels, ts}} ─> gate pipeline ─> log
River WS (Poly US side)─┘
```

Book store entry per market holds, per venue, the top N levels of both sides and a last-update timestamp. The timestamp is not optional. It is how Gate 5 kills stale-quote false positives.

If River exposes a single unified feed keyed by River ID, use it and skip the two-consumer split. Confirm in their docs.

---

## The five gates

### Gate 1 — Same event?

River's unified ID claims two markets are the same event. Do not trust it blindly for arb purposes. An arb only exists if the resolution criteria are identical. Same source of truth, same resolution date, same handling of edge cases and ties. Two markets that look identical but resolve on different rules are not an arb, they are a hidden bet on the discrepancy.

Implementation. Pull the resolution metadata for both legs via River's market metadata and compare the fields programmatically, resolution source, close and settle timestamps, and the rule text where available. Flag any mismatch and exclude the pair from tradeable, but keep it in a separate "matched by River, resolves differently" bucket.

That bucket is not waste. It is your best feedback to River. Every place their IDs claim equivalence that does not hold is a concrete bug in their core product, and handing a founder a list of those is the thing that makes you memorable.

### Gate 2 — Real prices?

Compute the edge off executable top-of-book, `ask_yes` and `bid_yes` per venue, never the mid. This gate is really just "use the formula above instead of the naive one," but it is worth naming as a checkpoint because it is the single most common way these monitors lie to you.

### Gate 3 — Survives fees?

Subtract both venues' fees before calling anything an arb.

Kalshi charges a per-contract fee that is a curve, not a flat rate. The standard form is

```
fee = ceil(coef * contracts * price * (1 − price))
```

with `coef` commonly around 0.07 but varying by market. The `price * (1 − price)` term is the important part. It peaks at price 0.5 and goes toward zero in the tails. So the same nominal spread is a real arb on a 90/10 market and a mirage on a coin-flip. Compute the fee at the actual execution price, per contract, per level.

Polymarket US: confirm the live schedule. If it is zero, the fee asymmetry itself is a finding worth noting, the Kalshi leg carries essentially all the friction, which shifts where arbs survive toward the tails.

### Gate 4 — Enough size?

Top-of-book edge on three contracts is not worth a trade. Walk both books to find capturable size.

For the buy-YES-on-A leg you consume A's ask ladder, prices rising as you go deeper. For the buy-NO-on-B leg you consume B's NO-ask ladder, which is the mirror of B's YES-bid ladder, YES bids falling as you go deeper. For each additional pair `k`, compute the marginal net using the level prices at that depth and the per-contract Kalshi fee at those prices. Accumulate pairs while cumulative net stays positive.

Report two numbers, capturable size N (the count of pairs before the edge goes negative) and total locked profit across those N pairs. Rank on what comes next, not on either of these raw.

### Gate 5 — Still there?

Two failure modes to catch. Staleness and evaporation.

Staleness. If one venue's book has not updated in X seconds while the other is moving, the apparent edge is probably a dead quote on the stale side. Use the per-venue update timestamps. If either side is older than your freshness threshold, do not flag.

Evaporation. Only promote an opportunity to "confirmed" if it survives some minimum persistence, a handful of consecutive book updates or T milliseconds. Log both first-seen and last-seen timestamps so you can measure how long real opportunities actually live. That lifetime distribution is itself part of the deliverable and it is the number that tells you whether trading this is even feasible given round-trip latency.

---

## Ranking: return on locked capital

Do not rank survivors by raw edge. Both legs lock collateral until the event resolves, which can be days or months, so the honest metric is return on the capital you tie up, annualized.

```
capital_locked_per_pair ≈ ask_yes_A + ask_no_B   (what you pay upfront, close to 1 dollar)
rolc = net_per_pair / capital_locked_per_pair
annualized = rolc * (365 / days_to_resolution)
```

Use simple annualization, not compounding. You cannot assume you can redeploy the capital the instant it frees up, so `rolc * 365 / days` is the defensible figure. Note the caveat rather than compounding an assumption you cannot support.

This is the column that separates a trader's tool from a coder's tool. A 70 bps edge locked for 90 days is worse than a 40 bps edge locked for 6. Ranking by edge gets that backwards.

**Sizing note for when you trade it.** True locked arb is not a Kelly problem. Kelly sizes edge with variance. A confirmed lock is near-riskless, so you size to the min of available depth and your capital, full size, not fractional Kelly. The real risk here is legging, one leg fills and the other moves before you complete, leaving you directional. That is a separate model about how much you let work before both legs confirm, and it is the reason v1 stays monitor-only.

---

## The opportunity log

Every confirmed survivor writes one row. This table is the whole deliverable, so capture enough to reconstruct the decision later.

| field | meaning |
|---|---|
| `river_id` | matched market id |
| `event_label` | human-readable market name |
| `direction` | which venue you buy YES on |
| `first_seen`, `last_seen` | for lifetime measurement |
| `edge_bps` | net edge in basis points at detection |
| `capturable_size` | pairs before edge goes negative |
| `locked_profit` | total net across capturable size |
| `capital_locked` | upfront cost across the size |
| `annualized_rolc` | the ranking metric |
| `days_to_resolution` | at detection |
| `fee_A`, `fee_B` | so you can attribute where friction landed |
| `resolution_match` | Gate 1 result, true or flagged |
| `stale_flag` | Gate 5 result |

Also log the rejects, or at least count them per gate. The rejection breakdown is the insight, "of everything that looked like an arb, X percent died at fees, Y percent at staleness," and you cannot report that if you only keep survivors.

---

## The analysis layer (the actual deliverable)

Run the monitor for a few weeks, then produce the summary. This is what goes in the email to Oscar and Antonin and what you talk about in an interview.

Answer four questions with the log.
1. How often do genuine, fee-and-depth-survivable arbs appear, per day, per market category.
2. How big, distribution of capturable size and annualized RoLC.
3. How long they live, the lifetime distribution from first-seen to last-seen, which tells you whether they are capturable at real latency.
4. Where the fake ones die, the per-gate rejection breakdown.

Hold both outcomes as valid before you start. If real arbs are common and persistent, you have a trading tool and a great artifact. If they are rare and mostly die at the fee or persistence gate, your finding is that the retail-visible edge is largely noise once modeled honestly, which is the more mature result and the stronger interview story. Either way the artifact is the same shape, an honest characterization of cross-venue efficiency built on River's own infrastructure, with a resolution-mismatch bug list attached for them.

---

## Build order

Ship it in layers so each stage works before the next.

1. **Connect and store.** One venue's WebSocket into the book store, printing top-of-book with timestamps. Prove the feed and the freshness tracking work.
2. **Both venues, one pair.** Add the second feed, key by River ID, get both books live for a single matched market.
3. **Edge math, top of book.** Implement the two-direction net formula with real fee functions. No gates yet, just print the number. Sanity-check by hand against the live books.
4. **Gates 3 and 5.** Fee curve and staleness/persistence. This is where most of the false positives should disappear. Watch them disappear.
5. **Gate 4, depth walk.** Add capturable size and locked profit.
6. **Gate 1, resolution check.** Add the metadata comparison and start the mismatch bucket.
7. **Log and let it run.** Write survivors and reject counts. Leave it running across markets for the study window.
8. **Analysis layer.** The four questions, plus the mismatch list for River.

Stages 1 through 5 are a weekend if the SDK cooperates. The value is in 6 through 8.

---

## What this hands you beyond the resume line

A resolution-mismatch bug list for River, which is real feedback on their core product.

Twenty minutes of interview conversation you have earned rather than read, on executable pricing, fee curves, legging risk, and capital efficiency, all from something you built and measured.

And, if the numbers are there, a monitor you can actually trade off later, at which point the legging model in the sizing note becomes the next thing to build.
