#!/usr/bin/env python3
"""Build the runtime payload the Framer component fetches.

Reads:
  usdg_all.json        daily network total, one point per day (the chart series)
  usdg_chains.json     per-chain supply per day, written by snapshot_usdg.py
  chains.json          chain list + how each one is read (the file you edit)
  chain_logos_b64.json chain logos, base64

Writes:
  public/usdg-data.json   everything the page needs, versioned

Run:  python3 build_data.py
"""
import datetime as dt
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(BASE, "public")
HISTORY = os.path.join(BASE, "usdg_all.json")
CHAIN_HISTORY = os.path.join(BASE, "usdg_chains.json")
CHAINS = os.path.join(BASE, "chains.json")
LOGOS = os.path.join(BASE, "chain_logos_b64.json")
OUT = os.path.join(PUBLIC, "usdg-data.json")

SCHEMA_VERSION = 1
CHART_DAYS = 365
DAY = 86400
# How far the by-chain sum may sit from the latest recorded total before it is
# worth saying something. A little drift is normal — the chains are read at a
# slightly different moment from the day's aggregate point.
TOTAL_DRIFT_WARN = 0.02


def load_history():
    with open(HISTORY, encoding="utf-8") as f:
        raw = json.load(f)
    # dedupe by UTC day; last write for a day wins
    points = {}
    for d in raw:
        points[int(d["date"])] = round(float(d["totalCirculating"]["peggedUSD"]))
    series = sorted([ts, v] for ts, v in points.items())
    if len(series) < 300:
        sys.exit(f"history looks truncated: only {len(series)} daily points")
    return series


def report_gaps(series):
    """A missing day is drawn as a straight line between the days either side,
    which quietly invents the days in between. Say so rather than let it pass
    unnoticed — `snapshot_usdg.py --backfill` fills them."""
    gaps = [(a, b) for (a, _), (b, _) in zip(series, series[1:]) if b - a > DAY]
    if not gaps:
        return
    missing = sum((b - a) // DAY - 1 for a, b in gaps)
    fmt = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")
    print(f"  ! {missing} missing day(s) in {len(gaps)} gap(s) — the chart interpolates across them:")
    for a, b in gaps[:5]:
        print(f"      {fmt(a)} -> {fmt(b)}")
    print("      run: python3 snapshot_usdg.py --backfill")


def load_icons():
    """chain_logos_b64.json stores '<img src="data:...">'; we want the bare URI."""
    with open(LOGOS, encoding="utf-8") as f:
        raw = json.load(f)
    icons = {}
    for name, markup in raw.items():
        m = re.search(r'src="([^"]+)"', markup)
        icons[name] = m.group(1) if m else markup
    return icons


def load_chain_values():
    """Latest recorded value per chain, newest record wins."""
    if not os.path.exists(CHAIN_HISTORY):
        return {}, None
    with open(CHAIN_HISTORY, encoding="utf-8") as f:
        records = json.load(f)
    values, last_ts = {}, None
    for record in sorted(records, key=lambda r: int(r["date"])):
        values.update(record.get("chains") or {})
        last_ts = int(record["date"])
    return values, last_ts


def value_at(series, ts):
    """Last known value at or before ts."""
    best = series[0][1]
    for t, v in series:
        if t <= ts:
            best = v
        else:
            break
    return best


def change_cards(series, current):
    last_ts = series[-1][0]
    cards = []
    for label, days in (
        ("Past year", 365),
        ("Past 30 days", 30),
        ("Past week", 7),
        ("Past 24 hours", 1),
    ):
        past = value_at(series, last_ts - days * DAY)
        absolute = current - past
        pct = (absolute / past * 100) if past else 0.0
        cards.append([label, round(pct, 2), absolute])
    return cards


def build_chains(cfg, icons, recorded):
    live = [c for c in cfg["chains"] if c.get("enabled")]
    if not live:
        sys.exit("no enabled chains in chains.json")

    missing_icon = [c["name"] for c in live if c["name"] not in icons]
    if missing_icon:
        print(f"  ! no logo for: {', '.join(missing_icon)} (will show a letter tile)")

    never_read = [c["name"] for c in live if c["name"] not in recorded]
    if never_read and recorded:
        print(f"  ! never read yet, using seed value: {', '.join(never_read)}")

    out = []
    for c in live:
        name = c["name"]
        value = recorded.get(name)
        if value is None:
            value = c.get("seed") or 0
        value = round(value)

        # The browser reads the same contracts; it has no use for the explorer
        # fallback (those APIs don't allow cross-origin reads), so keep it out
        # of the payload every visitor downloads.
        read = {k: v for k, v in (c.get("read") or {}).items() if not k.startswith("_")}
        read.pop("explorers", None)
        if read.get("kind") == "evm" and not read.get("contract"):
            read["kind"] = "none"  # can't be read live, whatever it claims

        out.append(
            {
                "name": name,
                "value": value,
                "color": c.get("color") or "#C7E36C",
                "whiteBg": bool(c.get("whiteBg")),
                "icon": icons.get(name, ""),
                "read": read,
            }
        )
    return out


def main():
    series = load_history()
    icons = load_icons()
    recorded, recorded_ts = load_chain_values()
    with open(CHAINS, encoding="utf-8") as f:
        cfg = json.load(f)

    chains = build_chains(cfg, icons, recorded)

    # The by-chain figures are read from the chains themselves, so their sum is
    # the headline figure. The final point of the chart is pinned to it, exactly
    # as the page does after a live read.
    total = sum(c["value"] for c in chains)
    last_ts, last_recorded = series[-1]
    if last_recorded and abs(total - last_recorded) / last_recorded > TOTAL_DRIFT_WARN:
        pct = (total - last_recorded) / last_recorded * 100
        print(
            f"  ! by-chain sum is {pct:+.1f}% from the latest recorded total "
            f"(${total:,} vs ${last_recorded:,}) — stale chain reads, or a chain "
            "added without a snapshot run?"
        )
    series[-1] = [last_ts, total]

    chart = [p for p in series if p[0] >= last_ts - (CHART_DAYS - 1) * DAY]
    report_gaps(chart)

    payload = {
        "schema": SCHEMA_VERSION,
        # Date, not a timestamp: the payload must be reproducible from its inputs,
        # otherwise the daily job commits a diff every run even when nothing
        # changed and the commit history stops meaning anything. Still precise
        # enough to spot a CDN serving a stale file.
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "asOf": dt.datetime.fromtimestamp(last_ts, dt.timezone.utc)
        .strftime("%b %d, %Y")
        .replace(" 0", " "),
        "total": total,
        "cards": change_cards(series, total),
        "series": chart,
        "chains": chains,
    }

    assert sum(c["value"] for c in chains) == total, "by-chain figures must sum to the total"

    os.makedirs(PUBLIC, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    print(f"wrote {OUT} ({os.path.getsize(OUT)/1024:.0f} KB)")
    print(f"  as of      {payload['asOf']}")
    print(f"  total      ${total:,}")
    print(f"  chart      {len(chart)} daily points")
    print(f"  chains     {len(chains)}: {', '.join(c['name'] for c in chains)}")
    if recorded_ts:
        stamp = dt.datetime.fromtimestamp(recorded_ts, dt.timezone.utc).strftime("%Y-%m-%d")
        print(f"  chain data read {stamp}")
    staged = [c["name"] for c in cfg["chains"] if not c.get("enabled")]
    if staged:
        print(f"  staged     {', '.join(staged)} (enabled=false)")


if __name__ == "__main__":
    main()
