#!/usr/bin/env python3
"""Record one day of USDG supply, read from the chains themselves.

    python3 snapshot_usdg.py                      read every chain, record today
    python3 snapshot_usdg.py <unix_secs> <value>  record one day's total by hand
    python3 snapshot_usdg.py --backfill           fill missing past days
    python3 snapshot_usdg.py --dry-run            read and report, write nothing

Each run reads every enabled chain's USDG contract on that chain (see
chain_reader.py) and writes two files:

    usdg_chains.json   per-chain supply, one record per day
    usdg_all.json      the network total, one point per day (the chart's series)

The total is the sum of the chains, so the headline figure and the by-chain
figures cannot disagree.

A chain that can't be read keeps its last known value for the day, marked
`stale` in the record. That avoids a fake dip in the chart from one flaky
endpoint, but the day is refused outright if the chains that *did* answer
don't cover most of the network — a total assembled mostly from yesterday
is not today's total.

--backfill is the one place an aggregator is still used: past days can't be
read from a chain without archive nodes, so missing days are filled from
DefiLlama's history. It never overwrites a day that already exists, and it
checks itself against days we already have before writing anything.

Exits non-zero on failure so the scheduled job surfaces the problem instead of
silently publishing a stale page.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.request

from chain_reader import ReadError, load_chains, read_all

BASE = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(BASE, "usdg_all.json")
CHAIN_HISTORY = os.path.join(BASE, "usdg_chains.json")

# DefiLlama stablecoin id for Global Dollar — backfill only.
LLAMA_URL = "https://stablecoins.llama.fi/stablecoin/286"

SANITY_MIN = 1_000_000
MAX_DAILY_SWING = 0.35  # 35% day-over-day; anything larger needs a human
MIN_FRESH_COVERAGE = 0.80  # fresh reads must cover this much of yesterday's total

DAY = 86400
fmt_day = lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- file helpers


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def totals_by_day(history):
    return {int(d["date"]): float(d["totalCirculating"]["peggedUSD"]) for d in history}


def put_total(history, ts, value):
    """Insert or correct one day in the aggregate history. Returns 'added'/'updated'."""
    for d in history:
        if int(d["date"]) == ts:
            d["totalCirculating"]["peggedUSD"] = value
            history.sort(key=lambda d: int(d["date"]))
            return "updated"
    history.append({"date": str(ts), "totalCirculating": {"peggedUSD": value}})
    history.sort(key=lambda d: int(d["date"]))
    return "added"


def previous_total(history, ts):
    """Most recent recorded total strictly before ts."""
    best = None
    for day, value in sorted(totals_by_day(history).items()):
        if day < ts:
            best = value
    return best


def last_chain_values(chain_history):
    """Latest known value per chain, from the most recent day each appeared."""
    out = {}
    for record in sorted(chain_history, key=lambda r: int(r["date"])):
        out.update(record.get("chains") or {})
    return out


def check_swing(history, ts, value):
    prev = previous_total(history, ts)
    if prev and abs(value - prev) / prev > MAX_DAILY_SWING:
        sys.exit(
            f"refusing to record a {abs(value-prev)/prev:.0%} day-over-day move "
            f"({prev:,.0f} -> {value:,.0f}) on {fmt_day(ts)}. "
            "Re-run with an explicit value if this is real."
        )


def rebuild():
    subprocess.run([sys.executable, os.path.join(BASE, "build_data.py")], check=True)


# ---------------------------------------------------------------- record a day


def record_today(dry_run=False):
    cfg, chains = load_chains()
    if not chains:
        sys.exit("no enabled chains in chains.json")

    print(f"reading {len(chains)} chains from their own endpoints…")
    values, sources, failures = read_all(chains)

    history = load_json(HISTORY, [])
    chain_history = load_json(CHAIN_HISTORY, [])
    known = last_chain_values(chain_history)
    seeds = {c["name"]: c.get("seed") for c in chains}

    # Carry forward anything that didn't answer, so one flaky endpoint doesn't
    # dent the chart. A chain with no previous value and no seed can't be
    # carried and is fatal — a total missing a whole chain is simply wrong.
    day = {}
    stale = []
    for chain in chains:
        name = chain["name"]
        if name in values:
            day[name] = round(values[name])
            continue
        fallback = known.get(name, seeds.get(name))
        if fallback is None:
            sys.exit(
                f"{name} could not be read and has no previous value or seed "
                f"({failures.get(name)}). Set a seed in chains.json or fix the endpoint."
            )
        day[name] = round(fallback)
        stale.append(name)

    total = sum(day.values())

    # Guard against publishing a total mostly assembled from yesterday.
    prev = previous_total(history, int(dt.datetime.now(dt.timezone.utc).timestamp()))
    if stale and prev:
        fresh = sum(v for n, v in day.items() if n not in stale)
        coverage = fresh / prev if prev else 0
        print(f"  carried forward: {', '.join(stale)} ({coverage:.0%} of the total read fresh)")
        if coverage < MIN_FRESH_COVERAGE:
            sys.exit(
                f"only {coverage:.0%} of the network was read this run "
                f"(need {MIN_FRESH_COVERAGE:.0%}); refusing to record a partial total. "
                f"Failures: {'; '.join(f'{k}: {v}' for k, v in failures.items())}"
            )

    if total < SANITY_MIN:
        sys.exit(f"refusing to record implausible total: {total}")

    ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    ts -= ts % DAY  # start of the UTC day, so one point is exactly one day
    check_swing(history, ts, total)

    print(f"\n{fmt_day(ts)}  total ${total:,}")
    if dry_run:
        print("(dry run — nothing written)")
        return

    action = put_total(history, ts, total)
    record = {"date": str(ts), "chains": day}
    if stale:
        record["stale"] = stale
    chain_history = [r for r in chain_history if int(r["date"]) != ts] + [record]
    chain_history.sort(key=lambda r: int(r["date"]))

    save_json(HISTORY, history)
    save_json(CHAIN_HISTORY, chain_history)
    print(f"{action} {fmt_day(ts)} ({len(history)} daily points, {len(chain_history)} per-chain)")
    rebuild()


def record_manual(ts, value):
    """Record one day's total by hand, for a day the guards refused."""
    value = round(float(value))
    ts = int(ts) - int(ts) % DAY
    if value < SANITY_MIN:
        sys.exit(f"refusing to record implausible total: {value}")
    history = load_json(HISTORY, [])
    action = put_total(history, ts, value)
    save_json(HISTORY, history)
    print(f"{action} {fmt_day(ts)} = {value:,} ({len(history)} points)")
    rebuild()


# ---------------------------------------------------------------- backfill


def llama_history():
    """Daily aggregate history from DefiLlama, as {day_ts: total}."""
    req = urllib.request.Request(LLAMA_URL, headers={"User-Agent": "usdg-snapshot/2"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)

    per_day = {}
    for chain in (data.get("chainBalances") or {}).values():
        for point in chain.get("tokens") or []:
            try:
                ts = int(point["date"])
            except (KeyError, TypeError, ValueError):
                continue
            ts -= ts % DAY
            amount = (point.get("circulating") or {}).get("peggedUSD")
            if amount:
                per_day[ts] = per_day.get(ts, 0.0) + float(amount)

    if not per_day:
        sys.exit("could not read any history from DefiLlama (response shape changed?)")
    return per_day


def backfill(limit_days=400):
    """Fill days missing from the aggregate history. Never overwrites a day."""
    history = load_json(HISTORY, [])
    if not history:
        sys.exit("no existing history to backfill into")
    have = totals_by_day(history)
    first, last = min(have), max(have)

    print(f"have {len(have)} days, {fmt_day(first)} -> {fmt_day(last)}")
    missing = [t for t in range(first, last + DAY, DAY) if t not in have]
    if not missing:
        print("no gaps in the recorded range")
        return
    print(f"missing {len(missing)} day(s): {', '.join(fmt_day(t) for t in missing[:12])}"
          + (" …" if len(missing) > 12 else ""))

    print("fetching history from DefiLlama…")
    llama = llama_history()

    # Sanity-check the parse against days we already have before trusting it for
    # days we don't. A shape change upstream would otherwise write junk history.
    overlap = [t for t in have if t in llama][-30:]
    if len(overlap) < 5:
        sys.exit("not enough overlap with existing history to trust the backfill")
    worst = max(abs(llama[t] - have[t]) / have[t] for t in overlap if have[t])
    print(f"  cross-check on {len(overlap)} known days: worst difference {worst:.2%}")
    if worst > 0.05:
        sys.exit(
            f"DefiLlama disagrees with recorded history by up to {worst:.1%} — "
            "not backfilling from a source that doesn't match what we already have"
        )

    filled, absent = [], []
    for ts in missing:
        if ts in llama and llama[ts] >= SANITY_MIN:
            put_total(history, ts, round(llama[ts]))
            filled.append(ts)
        else:
            absent.append(ts)

    if filled:
        save_json(HISTORY, history)
        for ts in filled:
            print(f"  filled {fmt_day(ts)} = {round(llama[ts]):,}")
        print(f"backfilled {len(filled)} day(s); {len(history)} points total")
        rebuild()
    else:
        print("nothing to fill — DefiLlama has no data for the missing days")
    if absent:
        print(f"  still missing (no upstream data): {', '.join(fmt_day(t) for t in absent)}")


# ---------------------------------------------------------------- entry point


def main():
    args = sys.argv[1:]
    if not args:
        record_today()
    elif args == ["--dry-run"]:
        record_today(dry_run=True)
    elif args == ["--backfill"]:
        backfill()
    elif len(args) == 2 and not args[0].startswith("-"):
        record_manual(args[0], args[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
