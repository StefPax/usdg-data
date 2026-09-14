#!/usr/bin/env python3
"""Record one daily USDG market-cap point, then rebuild the runtime payload.

  python3 snapshot_usdg.py                      fetch today's value from DefiLlama
  python3 snapshot_usdg.py <unix_secs> <value>  record a specific day by hand

Appends (or corrects) the day's total in usdg_all.json, normalised to the start
of the UTC day so one point is exactly one day, then re-runs build_data.py.

Exits non-zero on failure so the scheduled job surfaces the problem instead of
silently publishing a stale page.
"""
import json
import os
import subprocess
import sys
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(BASE, "usdg_all.json")

# DefiLlama stablecoin id for Global Dollar
LLAMA_URL = "https://stablecoins.llama.fi/stablecoin/286"
# Reject obviously wrong reads rather than writing them into the history.
SANITY_MIN = 1_000_000
MAX_DAILY_SWING = 0.35  # 35% day-over-day; anything larger needs a human


def fetch_from_llama():
    req = urllib.request.Request(LLAMA_URL, headers={"User-Agent": "usdg-snapshot/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)

    tokens = data.get("chainBalances") or {}
    latest_ts, latest_val = None, None
    for chain in tokens.values():
        for point in chain.get("tokens", []):
            ts = int(point["date"])
            val = float((point.get("circulating") or {}).get("peggedUSD") or 0)
            if latest_ts is None or ts > latest_ts:
                latest_ts, latest_val = ts, 0.0
            if ts == latest_ts:
                latest_val += val

    # prefer the top-level aggregate when present, it's the authoritative figure
    for key in ("totalCirculating", "circulating"):
        agg = data.get(key)
        if isinstance(agg, dict) and agg.get("peggedUSD"):
            latest_val = float(agg["peggedUSD"])
            break

    if latest_ts is None or not latest_val:
        sys.exit("could not read a market cap from DefiLlama")
    return latest_ts, latest_val


def main():
    if len(sys.argv) == 3:
        ts, val = int(sys.argv[1]), float(sys.argv[2])
    elif len(sys.argv) == 1:
        ts, val = fetch_from_llama()
    else:
        sys.exit("usage: snapshot_usdg.py [<unix_seconds> <market_cap_usd>]")

    val = round(val)
    ts -= ts % 86400  # normalise to the start of the UTC day

    if val < SANITY_MIN:
        sys.exit(f"refusing to record implausible market cap: {val}")

    with open(HISTORY, encoding="utf-8") as f:
        history = json.load(f)

    previous = None
    for d in sorted(history, key=lambda d: int(d["date"])):
        if int(d["date"]) < ts:
            previous = float(d["totalCirculating"]["peggedUSD"])
    if previous and abs(val - previous) / previous > MAX_DAILY_SWING:
        sys.exit(
            f"refusing to record a {abs(val-previous)/previous:.0%} day-over-day move "
            f"({previous:,.0f} -> {val:,.0f}). Re-run with an explicit value if this is real."
        )

    updated = False
    for d in history:
        if int(d["date"]) == ts:
            d["totalCirculating"]["peggedUSD"] = val
            updated = True
            break
    if not updated:
        history.append({"date": str(ts), "totalCirculating": {"peggedUSD": val}})

    history.sort(key=lambda d: int(d["date"]))
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(history, f)

    print(f"{'updated' if updated else 'added'} {ts} = {val:,} ({len(history)} points)")
    subprocess.run([sys.executable, os.path.join(BASE, "build_data.py")], check=True)


if __name__ == "__main__":
    main()
