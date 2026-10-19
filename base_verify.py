#!/usr/bin/env python3
"""
On-chain USDC verification on Base mainnet — stdlib only (urllib + json).

Why not web3.py: the public Base endpoints return logs in mixed shapes (hex str
vs bytes) and occasionally refuse get_transaction for a mined tx. Hand-rolling the
receipt read over raw JSON-RPC is both simpler and more predictable here, and it
was verified against a real transfer.

Trustless: we read the chain through a public RPC and check the USDC Transfer
event ourselves. No third-party API, no signed token from a payment processor.

USDC (Base mainnet): 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
Transfer topic:       0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# stored WITHOUT the 0x prefix, because _as_hex() strips it before comparing
TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

RPCS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.blockscout.com/api/eth-rpc",
]


def _rpc(method: str, params: list, timeout: int = 25) -> Any:
    """Call a public Base RPC.

    A JSON-RPC `null` result is a legitimate answer (a transaction that does not
    exist) — it is not an RPC failure. Only a missing "result" key or a transport
    error counts as failure, so we keep trying the remaining endpoints.
    """
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    last: Any = None
    for url in RPCS:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
                if "result" in d:
                    return d["result"]
                if "error" in d:
                    last = d["error"]
        except Exception as e:  # noqa: BLE001 - try the next endpoint
            last = e
    raise RuntimeError(f"no Base RPC reachable: {last}")


def _receipt(tx_hash: str) -> Optional[dict]:
    """Fetch a tx receipt, or None if the tx is not known to the chain."""
    rc = _rpc("eth_getTransactionReceipt", [tx_hash])
    return rc if isinstance(rc, dict) else None


def _as_hex(value: Any) -> str:
    """Normalise a log field (hex str, bytes, or already-hex) to a lowercase hex string
    with NO 0x prefix."""
    if isinstance(value, (bytes, bytearray)):
        return value.hex().lower()
    s = str(value).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    return s


def _addr_from_topic(topic: Any) -> Optional[str]:
    """Last 40 hex chars of a topic == the address it encodes."""
    h = _as_hex(topic)
    if len(h) < 40:
        return None
    return "0x" + h[-40:]


def verify_payment(tx_hash: str, recipient: str, min_cents: int) -> Optional[dict]:
    """
    Confirm a USDC transfer of at least `min_cents` into `recipient` in `tx_hash`.

    Returns {"cents", "sender", "block"} or None.

    Requires all of: the tx is known, it succeeded, and at least one USDC Transfer
    log sent >= min_cents to `recipient`. Anything else is a refusal.
    """
    if not isinstance(tx_hash, str) or not tx_hash.startswith("0x") or len(tx_hash) != 66:
        return None
    if not isinstance(recipient, str) or not recipient.startswith("0x"):
        return None
    recipient = recipient.lower()

    receipt = _receipt(tx_hash)
    if not receipt or receipt.get("status") != "0x1":
        return None

    best: Optional[dict] = None
    for log in receipt.get("logs", []):
        if (log.get("address") or "").lower() != USDC_BASE.lower():
            continue
        topics = log.get("topics") or []
        if len(topics) < 3 or _as_hex(topics[0]) != TRANSFER_TOPIC:
            continue
        to = _addr_from_topic(topics[2])
        if to is None or to != recipient:
            continue
        try:
            value = int(_as_hex(log.get("data", "0x0")), 16)
        except (ValueError, TypeError):
            continue
        if value >= min_cents and (best is None or value > best["cents"]):
            best = {
                "cents": value,
                "sender": _addr_from_topic(topics[1]),
                "block": int(receipt["blockNumber"], 16),
            }
    return best


def block_number() -> int:
    return int(_rpc("eth_blockNumber", []), 16)


def receiver() -> str:
    return os.environ.get("MICROSVC_RECEIVER",
                          "0x078fE079A9440631Bc6AD722D7143dC382b4ea4f")


if __name__ == "__main__":
    print(f"Base block: {block_number()}")
    print(f"USDC: {USDC_BASE}")
    print("stdlib verifier ready")
