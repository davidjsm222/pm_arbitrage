# Cross-venue prediction-market arbitrage monitor

This is a read-only Kalshi and Polymarket US monitor. It discovers markets from
each venue's native REST API, matches likely equivalent contracts, streams both
native order books, and displays fee-adjusted top-of-book and depth economics.
It never places, modifies, or cancels orders.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Use Kalshi production credentials and Polymarket US developer credentials:

```dotenv
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi-private-key.pem
POLYMARKET_US_API_KEY_ID=your-key-id
POLYMARKET_US_SECRET_KEY=your-base64-secret
```

Discovery uses public REST endpoints. Credentials are read only when opening
the authenticated market-data WebSockets.

## Discover native markets

```bash
.venv/bin/python -m cross_venue_arb.stage1 discover --limit 20
```

Stream one or more native venue books:

```bash
.venv/bin/python -m cross_venue_arb.stage1 stream \
  --kalshi KALSHI-MARKET-TICKER \
  --polymarket polymarket-us-market-slug
```

Monitor explicit pairs without rebuilding the matcher cache:

```bash
.venv/bin/python -m cross_venue_arb.stage1 edges \
  --pair example KALSHI-MARKET-TICKER polymarket-us-market-slug
```

## Build the matcher cache

```bash
.venv/bin/python -m cross_venue_arb.matcher
```

The matcher independently fetches all active native markets, applies the
configured liquidity floor, generates bounded entity candidates, checks hard
resolution constraints, and writes a complete SQLite snapshot to
`matcher_cache.sqlite3`. Native Kalshi tickers and Polymarket US slugs are the
cache keys. Existing false-pair exclusions survive subsequent rebuilds.

```bash
.venv/bin/python -m cross_venue_arb.matcher --list-exclusions
.venv/bin/python -m cross_venue_arb.matcher \
  --unflag KALSHI-MARKET-TICKER polymarket-us-market-slug
```

## Run the dashboard

```bash
./arbmonitor
```

The dashboard subscribes every cached high-confidence pair, normalizes both
venue books to executable YES bid/ask ladders, and applies the existing fee,
depth, staleness, and persistence gates. Press Enter or `d` for pair details,
`f` to exclude a false match, `x` to review exclusions, `r` to force a re-sort,
and `q` to quit.

The application remains strictly observational. No trading endpoint is called.
