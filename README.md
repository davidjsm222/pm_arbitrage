# River arb monitor — stages 1–3

The monitor connects to River, subscribes to three candidate cross-venue pairs,
keeps the last valid full book in memory, and calculates both executable
top-of-book directions after taker fees. It does not place orders, run gates,
or walk depth.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export RIVER_KEY_ID='<Settings -> API Keys UUID>'
export RIVER_PRIVATE_KEY='<base64 private key shown once>'
```

Search Kalshi and Polymarket independently for active, unexpired markets:

```bash
.venv/bin/python -m river_arb_monitor.stage1 discover
```

The confirmed-live test IDs are hardcoded in `river_arb_monitor/stage1.py`.
After a future discovery run, update that tuple before streaming:

```bash
.venv/bin/python -m river_arb_monitor.stage1 stream
```

For a one-off check without editing that list:

```bash
.venv/bin/python -m river_arb_monitor.stage1 stream --river-id 123 --river-id 456
```

Invalid (`is_valid=false`) crossed-book frames are counted and dropped. The
last valid book stays in the store until the documented fresh snapshot arrives.

Print both one-contract top-of-book edge directions for each pair:

```bash
.venv/bin/python -m river_arb_monitor.stage1 edges --duration 20
```

Fee defaults were verified on 2026-07-11: Kalshi `KXMENWORLDCUP` uses taker
coefficient `0.07` and multiplier `1`; Polymarket US uses taker coefficient
`0.06`. Venue-specific published rounding rules are applied to each one-contract
calculation.
