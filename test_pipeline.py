#!/usr/bin/env python3
"""Tests for the USDG data pipeline. No network: every endpoint is mocked.

    python3 test_pipeline.py

Covers the parts where a bug would put a wrong number on the page: ABI
decoding, the symbol/decimals verification, endpoint failover, carrying a
chain forward when it can't be read, and the backfill's self-check.
"""
import json
import os
import shutil
import sys
import tempfile
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import chain_reader as cr

PASS, FAIL = [], []


def check(name, got, want):
    if got == want:
        PASS.append(name)
        print(f"  ok   {name}: {got}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def check_raises(name, fn, fragment):
    try:
        fn()
    except BaseException as e:
        if fragment.lower() in str(e).lower():
            PASS.append(name)
            print(f"  ok   {name}: refused — {str(e)[:78]}")
        else:
            FAIL.append(name)
            print(f"  FAIL {name}: wrong error: {e}")
        return
    FAIL.append(name)
    print(f"  FAIL {name}: did not raise")


# ---------------------------------------------------------------- ABI encoding


def enc_uint(n):
    return "0x" + format(n, "064x")


def enc_string(s):
    raw = s.encode()
    body = format(32, "064x") + format(len(raw), "064x") + raw.ljust(32, b"\x00").hex()
    return "0x" + body


def enc_bytes32(s):
    return "0x" + s.encode().ljust(32, b"\x00").hex()


def test_decoding():
    print("\nABI decoding")
    check("dynamic string", cr._decode_string(enc_string("USDG")), "USDG")
    check("bytes32 string", cr._decode_string(enc_bytes32("USDG")), "USDG")
    check("uint", cr._decode_uint(enc_uint(6)), 6)

    # 1,835,509,357.123456 USDG at 6 decimals must survive intact. float division
    # would lose the cents at this magnitude.
    raw = 1_835_509_357_123456
    check("6dp scaling keeps cents", f"{cr._scaled(raw, 6):.6f}", "1835509357.123456")
    check("18dp scaling", cr._scaled(50 * 10**18, 18), 50.0)
    check("zero supply", cr._scaled(0, 6), 0.0)


# ---------------------------------------------------------------- reads


def mock_rpc(supplies, symbol="USDG", decimals=6, fail=()):
    """Fake eth_call/getTokenSupply. `supplies` maps rpc url -> raw units."""

    def _post(url, payload):
        if url in fail:
            raise OSError("connection refused")
        method = payload["method"]
        if method == "getTokenSupply":
            return {"result": {"value": {"amount": str(supplies[url]), "decimals": decimals}}}
        data = payload["params"][0]["data"]
        if data == cr.SEL_SYMBOL:
            return {"result": enc_string(symbol)}
        if data == cr.SEL_DECIMALS:
            return {"result": enc_uint(decimals)}
        if data == cr.SEL_TOTAL_SUPPLY:
            return {"result": enc_uint(supplies[url])}
        raise AssertionError("unexpected selector " + data)

    return _post


def evm_chain(rpcs, explorers=(), decimals=6):
    return {
        "name": "Testnet",
        "read": {
            "kind": "evm",
            "decimals": decimals,
            "contract": "0xabc",
            "rpcs": list(rpcs),
            "explorers": list(explorers),
        },
    }


def test_reads():
    print("\non-chain reads")
    A, B, C = "https://a", "https://b", "https://c"

    cr._post_json = mock_rpc({A: 328_756_086_000000})
    value, source, _ = cr.read_chain(evm_chain([A]))
    check("evm read", f"{value:,.0f}", "328,756,086")
    check("source reported", source.startswith("rpc "), True)

    # two endpoints down, third answers — one flaky provider costs nothing
    cr._post_json = mock_rpc({C: 100_000000}, fail=(A, B))
    value, source, _ = cr.read_chain(evm_chain([A, B, C]))
    check("fails over to 3rd rpc", (value, source), (100.0, "rpc https://c"))

    # wrong address: the contract is real but it isn't USDG
    cr._post_json = mock_rpc({A: 999_000000}, symbol="USDC")
    check_raises("wrong symbol refused", lambda: cr.read_chain(evm_chain([A])), "expected 'USDG'")

    # the Mantle scenario: config says 6, contract says 18
    cr._post_json = mock_rpc({A: 50 * 10**18}, decimals=18)
    check_raises(
        "decimals mismatch refused", lambda: cr.read_chain(evm_chain([A], decimals=6)), "decimals"
    )

    # ...and with the config corrected it reads correctly
    cr._post_json = mock_rpc({A: 50 * 10**18}, decimals=18)
    value, _, dec = cr.read_chain(evm_chain([A], decimals=18))
    check("correct 18dp config reads", (value, dec), (50.0, 18))

    cr._post_json = mock_rpc({}, fail=(A,))
    check_raises("all rpcs down", lambda: cr.read_chain(evm_chain([A])), "connection refused")

    check_raises(
        "no contract configured",
        lambda: cr.read_chain({"name": "X", "read": {"kind": "evm", "rpcs": [A]}}),
        "no contract",
    )

    # solana
    cr._post_json = mock_rpc({A: 627_509_882_000000}, decimals=6)
    value, _, _ = cr.read_chain({"name": "Solana", "read": {"kind": "solana", "mint": "m", "rpcs": [A]}})
    check("solana read", f"{value:,.0f}", "627,509,882")

    print("\nexplorer fallback (used only when every RPC fails)")
    cr._post_json = mock_rpc({}, fail=(A,))
    cr._get_json = lambda url: {"symbol": "USDG", "decimals": 6, "total_supply": "328756086000000"}
    value, source, _ = cr.read_chain(evm_chain([A], explorers=["https://exp"]))
    check("explorer fallback", f"{value:,.0f}", "328,756,086")
    check("explorer source reported", source.startswith("explorer"), True)

    cr._get_json = lambda url: {"symbol": "WETH", "decimals": 18, "total_supply": "1"}
    check_raises(
        "explorer symbol checked too",
        lambda: cr.read_chain(evm_chain([A], explorers=["https://exp"])),
        "expected 'USDG'",
    )


# ---------------------------------------------------------------- snapshot


def sandbox():
    """A throwaway copy of the repo so tests never touch real history."""
    tmp = tempfile.mkdtemp(prefix="usdg-test-")
    for f in ("chains.json", "chain_logos_b64.json", "build_data.py", "chain_reader.py",
              "snapshot_usdg.py"):
        shutil.copy(os.path.join(BASE, f), tmp)
    # a small but valid aggregate history: 400 days ending 2026-09-08
    end = 1757289600  # 2026-09-08
    hist = [
        {"date": str(end - i * 86400), "totalCirculating": {"peggedUSD": 3_200_000_000 - i * 1_000_000}}
        for i in range(400)
    ]
    with open(os.path.join(tmp, "usdg_all.json"), "w") as f:
        json.dump(hist, f)
    return tmp


def run_snapshot(tmp, values, argv=()):
    """Run record_today in `tmp` with chain reads stubbed to `values`."""
    import importlib

    sys.path.insert(0, tmp)
    for mod in ("chain_reader", "snapshot_usdg", "build_data"):
        sys.modules.pop(mod, None)
    reader = importlib.import_module("chain_reader")
    reader.read_all = lambda chains, verbose=True: (
        {c["name"]: values[c["name"]] for c in chains if values.get(c["name"]) is not None},
        {},
        {c["name"]: "stubbed failure" for c in chains if values.get(c["name"]) is None},
    )
    snap = importlib.import_module("snapshot_usdg")
    snap.rebuild = lambda: None
    try:
        snap.record_today()
        return "ok"
    except SystemExit as e:
        return str(e)
    finally:
        sys.path.remove(tmp)


FULL = {
    "X Layer": 1_835_509_357,
    "Solana": 627_509_882,
    "Ethereum": 378_670_250,
    "Robinhood Chain": 328_756_086,
    "Ink": 21_117_562,
    "Mantle": 0,
}


def test_snapshot():
    print("\ndaily snapshot")
    tmp = sandbox()
    out = run_snapshot(tmp, FULL)
    chains_file = json.load(open(os.path.join(tmp, "usdg_chains.json")))
    hist = json.load(open(os.path.join(tmp, "usdg_all.json")))
    latest = max(hist, key=lambda d: int(d["date"]))
    check("records all chains", out, "ok")
    check("per-chain file written", len(chains_file[-1]["chains"]), 6)
    check("total is the sum", latest["totalCirculating"]["peggedUSD"], sum(FULL.values()))
    check("no stale marker when all read", "stale" in chains_file[-1], False)
    shutil.rmtree(tmp)

    # one small chain down -> carried forward and marked, day still recorded
    tmp = sandbox()
    run_snapshot(tmp, FULL)
    partial = dict(FULL, Ink=None)
    out = run_snapshot(tmp, partial)
    rec = json.load(open(os.path.join(tmp, "usdg_chains.json")))[-1]
    check("small chain down: still records", out, "ok")
    check("carried chain marked stale", rec.get("stale"), ["Ink"])
    check("carried value kept", rec["chains"]["Ink"], 21_117_562)
    shutil.rmtree(tmp)

    # the biggest chain down -> below coverage, refuse rather than publish a dip
    tmp = sandbox()
    run_snapshot(tmp, FULL)
    out = run_snapshot(tmp, dict(FULL, **{"X Layer": None}))
    check("major chain down: refused", "refusing to record a partial total" in out, True)
    shutil.rmtree(tmp)

    # a chain never read and with no seed is fatal, not silently dropped
    tmp = sandbox()
    cfg = json.load(open(os.path.join(tmp, "chains.json")))
    for c in cfg["chains"]:
        if c["name"] == "Ink":
            c["seed"] = None
    json.dump(cfg, open(os.path.join(tmp, "chains.json"), "w"))
    out = run_snapshot(tmp, dict(FULL, Ink=None))
    check("unreadable + unseeded is fatal", "no previous value or seed" in out, True)
    shutil.rmtree(tmp)

    # implausible jump is refused
    tmp = sandbox()
    out = run_snapshot(tmp, dict(FULL, **{"X Layer": 99_000_000_000}))
    check("huge swing refused", "day-over-day" in out, True)
    shutil.rmtree(tmp)


# ---------------------------------------------------------------- backfill


def test_backfill():
    print("\nbackfill (repairs gaps from DefiLlama history)")
    import importlib

    tmp = sandbox()
    # carve the real-world gap into the history: drop 5 consecutive days
    path = os.path.join(tmp, "usdg_all.json")
    hist = json.load(open(path))
    by_day = {int(d["date"]): d for d in hist}
    end = max(by_day)
    dropped = [end - i * 86400 for i in range(1, 6)]
    json.dump([d for t, d in by_day.items() if t not in dropped], open(path, "w"))

    sys.path.insert(0, tmp)
    for mod in ("chain_reader", "snapshot_usdg", "build_data"):
        sys.modules.pop(mod, None)
    snap = importlib.import_module("snapshot_usdg")
    snap.rebuild = lambda: None

    # upstream that matches what we already have -> trusted
    good = {t: float(d["totalCirculating"]["peggedUSD"]) for t, d in by_day.items()}
    snap.llama_history = lambda: good
    snap.backfill()
    filled = {int(d["date"]) for d in json.load(open(path))}
    check("gap filled", all(t in filled for t in dropped), True)
    check("values match upstream", json.load(open(path)) and
          {int(d["date"]): d["totalCirculating"]["peggedUSD"] for d in json.load(open(path))}[dropped[0]],
          round(good[dropped[0]]))

    # a second run has nothing to do (idempotent)
    before = open(path).read()
    snap.backfill()
    check("second run is a no-op", open(path).read() == before, True)

    # upstream that disagrees with recorded history -> refuse, don't write junk
    json.dump([d for t, d in by_day.items() if t not in dropped], open(path, "w"))
    snap.llama_history = lambda: {t: v * 1.5 for t, v in good.items()}
    try:
        snap.backfill()
        check("mismatched source refused", "did not refuse", "refused")
    except SystemExit as e:
        check("mismatched source refused", "disagrees" in str(e), True)
    check("nothing written on refusal",
          {int(d["date"]) for d in json.load(open(path))}.isdisjoint(dropped), True)

    sys.path.remove(tmp)
    shutil.rmtree(tmp)


def main():
    for t in (test_decoding, test_reads, test_snapshot, test_backfill):
        try:
            t()
        except Exception:
            FAIL.append(t.__name__)
            traceback.print_exc()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
