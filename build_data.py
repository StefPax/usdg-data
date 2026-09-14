#!/usr/bin/env python3
"""Build the runtime payload the Framer component fetches.

Reads:
  usdg_all.json        daily market-cap history (seeded from DefiLlama, extended
                       once a day by snapshot_usdg.py)
  chains.json          chain list + how to read each one live (the file you edit)
  chain_logos_b64.json chain logos, base64

Writes:
  public/usdg-data.json   everything the page needs, versioned

Run:  python3 build_data.py
"""
import base64
import datetime as dt
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(BASE, "public")
HISTORY = os.path.join(BASE, "usdg_all.json")
CHAINS = os.path.join(BASE, "chains.json")
LOGOS = os.path.join(BASE, "chain_logos_b64.json")
OUT = os.path.join(PUBLIC, "usdg-data.json")

SCHEMA_VERSION = 1
CHART_DAYS = 365


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


def load_icons():
    """chain_logos_b64.json stores '<img src="data:...">'; we want the bare URI."""
    with open(LOGOS, encoding="utf-8") as f:
        raw = json.load(f)
    icons = {}
    for name, markup in raw.items():
        m = re.search(r'src="([^"]+)"', markup)
        icons[name] = m.group(1) if m else markup
    return icons


def value_at(series, ts):
    """Last known value at or before ts."""
    best = series[0][1]
    for t, v in series:
        if t <= ts:
            best = v
        else:
            break
    return best


def change_cards(series):
    last_ts, current = series[-1]
    cards = []
    for label, days in (
        ("Past year", 365),
        ("Past 30 days", 30),
        ("Past week", 7),
        ("Past 24 hours", 1),
    ):
        past = value_at(series, last_ts - days * 86400)
        absolute = current - past
        pct = (absolute / past * 100) if past else 0.0
        cards.append([label, round(pct, 2), absolute])
    return cards


def build_chains(cfg, icons, total):
    live = [c for c in cfg["chains"] if c.get("enabled")]
    if not live:
        sys.exit("no enabled chains in chains.json")

    missing_icon = [c["name"] for c in live if c["name"] not in icons]
    if missing_icon:
        print(f"  ! no logo for: {', '.join(missing_icon)} (will show a letter tile)")

    residuals = [c for c in live if c.get("residual")]
    if len(residuals) > 1:
        sys.exit("only one chain may be marked residual")

    fixed_total = sum(c["snapshot"] or 0 for c in live if not c.get("residual"))
    out = []
    for c in live:
        if c.get("residual"):
            # absorbs the difference so the by-chain figures sum to the aggregate
            value = total - fixed_total
            if value < 0:
                sys.exit(
                    f"residual chain {c['name']} would be negative — "
                    "a snapshot in chains.json is probably stale"
                )
        else:
            value = c["snapshot"] or 0

        read = dict(c.get("read") or {})
        read.pop("_note", None)
        # a chain with no contract wired can't be read live, whatever it claims
        if read.get("kind") == "evm" and not read.get("contract"):
            read["kind"] = "none"

        out.append(
            {
                "name": c["name"],
                "value": value,
                "color": c.get("color") or "#C7E36C",
                "whiteBg": bool(c.get("whiteBg")),
                "icon": icons.get(c["name"], ""),
                "read": read,
            }
        )
    return out


def main():
    series = load_history()
    icons = load_icons()
    with open(CHAINS, encoding="utf-8") as f:
        cfg = json.load(f)

    last_ts, total = series[-1]
    chart = [p for p in series if p[0] >= last_ts - (CHART_DAYS - 1) * 86400]
    chains = build_chains(cfg, icons, total)

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
        "cards": change_cards(series),
        "series": chart,
        "chains": chains,
    }

    assert sum(c["value"] for c in chains) == total, "by-chain figures must sum to the total"

    os.makedirs(PUBLIC, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    size = os.path.getsize(OUT)
    print(f"wrote {OUT} ({size/1024:.0f} KB)")
    print(f"  as of      {payload['asOf']}")
    print(f"  total      ${total:,}")
    print(f"  chart      {len(chart)} daily points")
    print(f"  chains     {len(chains)}: {', '.join(c['name'] for c in chains)}")
    staged = [c["name"] for c in cfg["chains"] if not c.get("enabled")]
    if staged:
        print(f"  staged     {', '.join(staged)} (enabled=false)")


if __name__ == "__main__":
    main()
