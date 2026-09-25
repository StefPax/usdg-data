# usdg-data

Publishes `usdg-data.json` — the figures behind the USDG supply page on our
Framer site.

Every figure is read from the chain that issued the tokens. A scheduled job
reads each chain's USDG contract once a day, records it, rebuilds the payload
and deploys it to GitHub Pages. The Framer component reads that file, then
reads the same contracts again in the visitor's browser. There is no server,
no database, and no aggregator in the path.

**Published URL:** `https://stefpax.github.io/usdg-data/usdg-data.json`

## Layout

| Path | What it is |
|---|---|
| `chains.json` | The chain list and contract addresses. **This is the file you edit.** |
| `chain_logos_b64.json` | Chain logos, base64, keyed by display name |
| `chain_reader.py` | Reads USDG supply from each chain (RPC, then its explorer) |
| `snapshot_usdg.py` | Records the day; also backfills missing days |
| `build_data.py` | Generates `public/usdg-data.json` |
| `test_pipeline.py` | Tests for all of the above. Runs in CI before every snapshot. |
| `usdg_all.json` | Daily network total (the chart series) |
| `usdg_chains.json` | Per-chain supply per day, written by the job |
| `public/usdg-data.json` | The generated payload. Committed daily by the bot. |

## Where the numbers come from

For each enabled chain, in order:

1. **The chain's own JSON-RPC endpoint** — `totalSupply()` on the USDG contract.
2. **The chain's own block explorer** (Blockscout API) — the same figure via its
   indexer, used only if every RPC fails.

Before trusting any EVM figure the reader calls `symbol()` and `decimals()` on
the contract and checks them against `chains.json`. A wrong address reports the
wrong symbol and is refused; a wrong `decimals` is refused too, because 6 vs 18
would overstate a figure by a factor of a trillion. Both fail the run rather
than reaching the page.

The network total is the sum of the chains, so the headline figure and the
by-chain figures cannot disagree.

DefiLlama is used in exactly one place: `--backfill`, for days that predate
this pipeline or were missed. Past days can't be read from a chain without
archive nodes.

## Adding a chain

1. Add the logo to `chain_logos_b64.json`, keyed by the exact display name:
   `"Arbitrum": "<img src=\"data:image/png;base64,…\" alt=\"Arbitrum\">"`
2. In `chains.json`, set `enabled: true` and fill in the contract address.
3. Run the workflow (Actions → Daily USDG snapshot → Run workflow). It reads
   the new chain, verifies the contract really is USDG, and publishes.

The Framer site is not touched — the component reads the chain list, colours,
logos and read config from the published JSON.

Arbitrum is staged with `enabled: false` and its logo already in place, so it
needs only a contract address and the flag flipped.

## Running it

Everything is available from the Actions tab — no terminal needed. Run the
workflow and pick a mode:

| Mode | What it does |
|---|---|
| `snapshot` | Read the chains and record today. Same as the daily run. |
| `backfill` | Fill any days missing from the chart history. Safe to re-run. |
| `dry-run` | Read the chains and report. Writes and publishes nothing. |

`dry-run` is the one to use after changing a contract address: it shows what
each chain reports without touching the published data.

From a terminal the same three are `python3 snapshot_usdg.py`, `… --backfill`
and `… --dry-run`. `python3 snapshot_usdg.py <unix_seconds> <total>` records one
day by hand, for a day the guards refused.

## Guardrails

All of these exit non-zero, so a bad day fails the job instead of quietly
publishing a wrong number:

- A contract whose `symbol()` isn't USDG, or whose `decimals()` disagrees with
  `chains.json`, is refused.
- A chain that can't be read keeps its last known value for the day, marked
  `stale` in `usdg_chains.json` — but the day is refused outright if the chains
  that did answer cover less than 80% of the network. A total mostly assembled
  from yesterday is not today's total.
- A day-over-day move above 35%, or a total under $1M, is refused.
- `--backfill` cross-checks DefiLlama against days already recorded and refuses
  to write anything if they disagree by more than 5%.
- The build fails if the by-chain figures don't sum to the total, and warns if
  the chart has gaps or if the by-chain sum drifts from the recorded total.
- `test_pipeline.py` runs in CI before every snapshot.

If a legitimate move trips the 35% guard, re-run in `snapshot` mode with an
explicit timestamp and value.

## Known gaps

- **Mantle** reads live but holds 0 USDG until funded. Its decimals are now
  verified against the contract on every run, so the earlier "assumed 6
  decimals" risk is closed.
- **Per-chain history starts from the day this pipeline went live.** Before
  that only the network total was recorded, so `usdg_chains.json` can't be
  backfilled. The chart is unaffected — it has always used the total.
