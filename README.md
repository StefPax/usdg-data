# usdg-data

Publishes `usdg-data.json` — the figures behind the USDG supply page on our
Framer site.

A scheduled job records one market-cap point a day, rebuilds the payload, and
deploys it to GitHub Pages. The Framer component fetches that file at runtime.
There is no server and no database.

**Published URL:** `https://<org>.github.io/usdg-data/usdg-data.json`

## Layout

| Path | What it is |
|---|---|
| `chains.json` | The chain list. **This is the file you edit.** |
| `chain_logos_b64.json` | Chain logos, base64, keyed by display name |
| `usdg_all.json` | Daily market-cap history (append-only) |
| `build_data.py` | Generates `public/usdg-data.json` from the three above |
| `snapshot_usdg.py` | Records today's point, then runs the build |
| `public/usdg-data.json` | The generated payload. Committed daily by the bot. |

## Adding a chain

1. Add the logo to `chain_logos_b64.json`, keyed by the exact display name:
   `"Arbitrum": "<img src=\"data:image/png;base64,…\" alt=\"Arbitrum\">"`
2. In `chains.json`, set `enabled: true` and fill in the contract address.
3. `python3 build_data.py` — read what it prints, then commit and push.

The Framer site is not touched. The component reads the chain list, colours,
logos and live-read config from the published JSON.

Arbitrum is staged with `enabled: false` and its logo already in place, so it
needs only a contract address and the flag flipped.

## Running it by hand

```bash
python3 build_data.py                  # rebuild from current inputs
python3 snapshot_usdg.py               # fetch today's value, then rebuild
python3 snapshot_usdg.py 1757462400 3201385655   # backfill a specific day
```

The scheduled run can also be triggered from Actions → Daily USDG snapshot →
Run workflow, which takes the same optional backfill arguments.

## Guardrails

These all exit non-zero, so a bad day fails the job instead of quietly
publishing a wrong number:

- `snapshot_usdg.py` refuses a value under $1M or a day-over-day move above 35%.
- `build_data.py` fails if the by-chain figures don't sum to the total, or if
  the residual chain would go negative (which means a snapshot in `chains.json`
  has gone stale).
- The workflow re-validates the payload before deploying.

If a legitimate move trips the 35% guard, re-run with an explicit value:
`python3 snapshot_usdg.py <unix_seconds> <market_cap>`.

## Known gaps

- **Robinhood Chain** has no contract wired, so its figure only moves with the
  daily snapshot.
- **Mantle** reads live but holds 0 USDG until funded, and its `decimals: 6` is
  an assumption inherited from the source build — confirm it before the chain
  carries real balances.
- **Solana is the residual**: it absorbs the difference between the aggregate
  and the other chains' snapshots so the figures always sum to the headline.
  Live reads replace this with the real on-chain number.
