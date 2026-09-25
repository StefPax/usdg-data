#!/usr/bin/env python3
"""Read USDG supply from each chain itself.

No aggregator sits in the middle: every figure below comes from the chain that
issued the tokens. Order of preference per chain:

  1. the chain's own JSON-RPC endpoint  -> totalSupply() on the USDG contract
  2. the chain's own block explorer API -> same figure, via its indexer

RPC first because it is the chain answering directly; the explorer is the same
chain's data one step removed, which makes it a good fallback but not a first
choice.

Every EVM read also verifies the contract on-chain before trusting the number:
it calls symbol() and decimals() and checks them against what chains.json
expects. A wrong address returns the wrong symbol, and a wrong `decimals` is
the one config mistake that silently inflates a figure by orders of magnitude
(6 vs 18 is a factor of a trillion). Both are caught here rather than on the
page.

Used by snapshot_usdg.py. Import read_all(), or run this file directly to see
what every chain currently reports:

    python3 chain_reader.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CHAINS = os.path.join(BASE, "chains.json")

TIMEOUT = 30
EXPECTED_SYMBOL = "USDG"

# ERC-20 function selectors
SEL_TOTAL_SUPPLY = "0x18160ddd"
SEL_DECIMALS = "0x313ce567"
SEL_SYMBOL = "0x95d89b41"


class ReadError(Exception):
    """A chain could not be read, with the reason worth showing a human."""


# ---------------------------------------------------------------- transport


def _post_json(url, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "usdg-snapshot/2"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "usdg-snapshot/2"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _rpc_call(url, method, params):
    res = _post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if isinstance(res, dict) and res.get("error"):
        raise ReadError(str(res["error"].get("message") or res["error"]))
    if not isinstance(res, dict) or "result" not in res:
        raise ReadError("no result in RPC response")
    return res["result"]


def _eth_call(url, contract, selector):
    out = _rpc_call(url, "eth_call", [{"to": contract, "data": selector}, "latest"])
    if not out or out == "0x":
        raise ReadError(f"empty response for {selector} (is {contract} a token contract?)")
    return out


# ---------------------------------------------------------------- ABI decoding


def _decode_uint(hexstr):
    return int(hexstr, 16)


def _decode_string(hexstr):
    """Decode an ABI-encoded string return value.

    Well-behaved tokens return a dynamic string (offset, length, bytes). A few
    older ones return a fixed bytes32 instead, so handle both rather than
    rejecting a contract over its ABI vintage.
    """
    raw = bytes.fromhex(hexstr[2:] if hexstr.startswith("0x") else hexstr)
    if len(raw) == 32:  # bytes32: right-padded with NULs
        return raw.rstrip(b"\x00").decode("utf-8", "replace").strip()
    if len(raw) >= 64:
        offset = int.from_bytes(raw[:32], "big")
        if offset + 32 <= len(raw):
            length = int.from_bytes(raw[offset : offset + 32], "big")
            start = offset + 32
            if length <= len(raw) - start:
                return raw[start : start + length].decode("utf-8", "replace").strip()
    raise ReadError("could not decode string return value")


def _scaled(raw_units, decimals):
    """Raw integer units -> whole tokens, without losing precision on the way.

    float(raw)/10**d would round a multi-billion supply in the cents, so split
    the integer and fractional parts and only use float for the remainder.
    """
    div = 10**decimals
    return raw_units // div + (raw_units % div) / div


# ---------------------------------------------------------------- per-chain reads


def _read_evm_rpc(rpc, cfg, name):
    """Read and verify an ERC-20 USDG contract through one RPC endpoint."""
    contract = cfg["contract"]

    symbol = _decode_string(_eth_call(rpc, contract, SEL_SYMBOL))
    if symbol.upper() != EXPECTED_SYMBOL:
        raise ReadError(
            f"contract {contract} reports symbol {symbol!r}, expected "
            f"{EXPECTED_SYMBOL!r} — wrong address for {name}?"
        )

    on_chain_decimals = _decode_uint(_eth_call(rpc, contract, SEL_DECIMALS))
    configured = cfg.get("decimals")
    if configured is not None and int(configured) != on_chain_decimals:
        raise ReadError(
            f"chains.json says decimals={configured} but the contract reports "
            f"{on_chain_decimals}; fix chains.json before trusting this figure"
        )

    raw = _decode_uint(_eth_call(rpc, contract, SEL_TOTAL_SUPPLY))
    return _scaled(raw, on_chain_decimals), on_chain_decimals


def _read_evm_explorer(api, cfg, name):
    """Fallback: the same chain's own block explorer (Blockscout API v2).

    GET /api/v2/tokens/{address} -> {symbol, decimals, total_supply, ...}
    where total_supply is raw integer units as a string.
    """
    url = api.rstrip("/") + "/api/v2/tokens/" + cfg["contract"]
    data = _get_json(url)
    if not isinstance(data, dict):
        raise ReadError("unexpected explorer response")

    symbol = str(data.get("symbol") or "").strip()
    if symbol and symbol.upper() != EXPECTED_SYMBOL:
        raise ReadError(
            f"explorer reports symbol {symbol!r} for {cfg['contract']}, "
            f"expected {EXPECTED_SYMBOL!r} — wrong address for {name}?"
        )

    if data.get("decimals") is None or data.get("total_supply") is None:
        raise ReadError("explorer response has no decimals/total_supply")

    decimals = int(data["decimals"])
    configured = cfg.get("decimals")
    if configured is not None and int(configured) != decimals:
        raise ReadError(
            f"chains.json says decimals={configured} but the explorer reports {decimals}"
        )

    return _scaled(int(str(data["total_supply"])), decimals), decimals


def _read_solana(rpc, cfg, name):
    """getTokenSupply already returns a decimal-adjusted amount for the mint."""
    res = _rpc_call(rpc, "getTokenSupply", [cfg["mint"]])
    value = (res or {}).get("value") or {}
    decimals = value.get("decimals")
    amount = value.get("amount")
    if amount is not None and decimals is not None:
        return _scaled(int(amount), int(decimals)), int(decimals)
    # older nodes only give the pre-scaled figure
    ui = value.get("uiAmount")
    if ui is None:
        ui = value.get("uiAmountString")
    if ui is None:
        raise ReadError("no supply in getTokenSupply response")
    return float(ui), decimals


def read_chain(chain):
    """Read one chain. Returns (value, source, decimals). Raises ReadError.

    Tries every endpoint the chain lists before giving up, so one flaky
    provider doesn't cost a day's data.
    """
    name = chain["name"]
    cfg = chain.get("read") or {}
    kind = cfg.get("kind")
    attempts = []

    if kind == "evm":
        if not cfg.get("contract"):
            raise ReadError("no contract address configured")
        for rpc in cfg.get("rpcs") or []:
            attempts.append(("rpc " + rpc, lambda r=rpc: _read_evm_rpc(r, cfg, name)))
        for api in cfg.get("explorers") or []:
            attempts.append(("explorer " + api, lambda a=api: _read_evm_explorer(a, cfg, name)))
    elif kind == "solana":
        if not cfg.get("mint"):
            raise ReadError("no mint address configured")
        for rpc in cfg.get("rpcs") or []:
            attempts.append(("rpc " + rpc, lambda r=rpc: _read_solana(r, cfg, name)))
    elif kind == "none":
        raise ReadError("live reads disabled for this chain (kind: none)")
    else:
        raise ReadError(f"unknown read kind {kind!r}")

    if not attempts:
        raise ReadError("no endpoints configured")

    problems = []
    for label, call in attempts:
        try:
            value, decimals = call()
        except ReadError as e:
            problems.append(f"{label}: {e}")
            continue
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
            problems.append(f"{label}: {type(e).__name__}: {e}")
            continue

        if value is None or value < 0:
            problems.append(f"{label}: implausible value {value!r}")
            continue
        return value, label, decimals

    # A symbol/decimals mismatch is a config bug, not a flaky endpoint: surface
    # it first so it isn't buried under a list of connection errors.
    mismatch = [p for p in problems if "expected" in p or "decimals=" in p]
    raise ReadError("; ".join(mismatch or problems))


def load_chains(path=CHAINS):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg, [c for c in cfg["chains"] if c.get("enabled")]


def read_all(chains, verbose=True):
    """Read every chain given. Returns (values, sources, failures)."""
    values, sources, failures = {}, {}, {}
    for chain in chains:
        name = chain["name"]
        try:
            value, source, _ = read_chain(chain)
        except ReadError as e:
            failures[name] = str(e)
            if verbose:
                print(f"  ! {name}: {e}")
            continue
        values[name] = value
        sources[name] = source
        if verbose:
            print(f"  ok {name}: {value:,.0f} USDG  ({source})")
    return values, sources, failures


def main():
    _, chains = load_chains()
    print(f"reading {len(chains)} chains from their own endpoints…")
    values, _, failures = read_all(chains)
    if values:
        print(f"\ntotal: ${sum(values.values()):,.0f} across {len(values)} chains")
    if failures:
        print(f"could not read: {', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
