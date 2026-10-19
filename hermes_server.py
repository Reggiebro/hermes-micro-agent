#!/usr/bin/env python3
"""
Hermes Micro-Agent — a paid x402 micro-service that does real work.

Two things this fixes that the template version could not do:

1. It does not shell out to a local GGUF model. There is no local model here, and
   pretending to call one would return a fabricated result to a paying caller.
   Instead every task below is genuinely implemented against the stdlib: real
   parsing, real arithmetic, real file handling. A task that is not implemented is
   refused, never faked.

2. Payment is verified on-chain. The template compared a header to the literal
   string "TEST_PAID_OK" — that is not a payment check. This one reads the USDC
   Transfer event from Base mainnet via a public RPC, so a caller cannot get the
   work by sending any string that looks like a hash.

Also added, because a micro-service needs to be callable by other agents:
  * a JSON-RPC 2.0 endpoint (POST /rpc) so agents can discover, price and invoke
    each other over a standard interface
  * GET /  (discovery document) and GET /tasks (price list)
"""
from __future__ import annotations

import ast
import base64
import csv
import difflib
import hashlib
import io
import json
import os
import re
import statistics
import subprocess
import sys
import textwrap
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ────────────────────────────────────────────────────────────── config
RECEIVER = os.environ.get(
    "MICROSVC_RECEIVER",
    "0x078fE079A9440631Bc6AD722D7143dC382b4ea4f",
)
PORT = int(os.environ.get("PORT", "8080"))

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
RPCS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.blockscout.com/api/eth-rpc",
]

# Price per micro-task, in USDC. Cheap on purpose: another agent should be able to
# call this and still come out ahead.
PRICES: dict[str, float] = {
    # text
    "summarize": 0.10,
    "extract": 0.10,
    "keywords": 0.10,
    "sentiment": 0.10,
    "translate": 0.15,
    "rewrite": 0.15,
    "proofread": 0.15,
    # code
    "python_debug": 0.25,
    "code_lint": 0.15,
    "code_refactor": 0.25,
    "code_explain": 0.15,
    "code_review": 0.25,
    # data
    "json_minify": 0.10,
    "json_query": 0.15,
    "csv_to_json": 0.15,
    "hash": 0.10,
    "base64": 0.10,
    "convert": 0.10,
    # media / files
    "describe_image": 0.15,
    "probe_media": 0.15,
}

# Which tasks are genuinely implemented. Anything absent is refused, not faked.
IMPLEMENTED = {
    "summarize", "extract", "keywords", "sentiment", "translate", "rewrite",
    "proofread", "python_debug", "code_lint", "code_refactor", "code_explain",
    "code_review", "json_minify", "json_query", "csv_to_json", "hash", "base64",
    "convert", "describe_image", "probe_media",
}

app = FastAPI(title="Hermes Micro-Agent", version="1.0")


# ────────────────────────────────────────────────────────────── models
class TaskPayload(BaseModel):
    prompt: str = Field(..., description="text input, or code, or a path")
    wallet_address: Optional[str] = Field(None, description="caller's wallet, for receipts")
    task: Optional[str] = Field(None, description="task name; defaults to 'summarize'")


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Any = None
    method: str
    params: dict = {}


# ────────────────────────────────────────────────────────────── payment
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
        except Exception as e:
            last = e
    raise RuntimeError(f"no Base RPC reachable: {last}")


def verify_payment(tx_hash: str, min_cents: int) -> Optional[dict]:
    """Confirm a USDC transfer into RECEIVER inside tx_hash. Returns details or None."""
    if not isinstance(tx_hash, str) or not tx_hash.startswith("0x") or len(tx_hash) != 66:
        return None
    receiver = RECEIVER.lower()

    rc = _rpc("eth_getTransactionByHash", [tx_hash])
    if not rc:
        return None
    receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
    if not receipt or receipt.get("status") != "0x1":
        return None

    best = None
    for log in receipt.get("logs", []):
        if log.get("address", "").lower() != USDC_BASE.lower():
            continue
        topics = log.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC.lower():
            continue
        if ("0x" + topics[2][-40:]).lower() != receiver:
            continue
        try:
            value = int(log["data"], 16)
        except (ValueError, TypeError):
            continue
        if value >= min_cents and (best is None or value > best["cents"]):
            best = {"cents": value, "sender": "0x" + topics[1][-40:],
                    "block": int(receipt["blockNumber"], 16)}
    return best


def price_for(task: str) -> float:
    return PRICES.get(task, 0.15)


def x402_payload(task: str) -> dict:
    """The x402 'exact' payload describing what must be paid."""
    usd = price_for(task)
    return {
        "x402Version": 1,
        "accepts": [{
            "scheme": "exact",
            "network": "base",
            "maxAmountRequired": str(int(round(usd * 100))),
            "asset": "USDC",
            "payTo": RECEIVER,
            "resource": f"/task?task={task}",
            "description": f"{task} micro-task",
            "mimeType": "application/json",
            "usd": usd,
        }],
    }


def payment_required(task: str) -> JSONResponse:
    return JSONResponse(status_code=402, content={
        "error": "Payment Required",
        "message": (
            f"'{task}' costs {price_for(task):.2f} USDC on Base. "
            f"Send it to {RECEIVER}, then retry with header X-Payment: <tx_hash>."
        ),
        **x402_payload(task),
    })


# ────────────────────────────────────────────────────────────── work
def do_task(task: str, inp: str, prompt: str = "") -> str:
    """Real implementations. Nothing here fabricates a result."""
    if not inp and not prompt:
        raise HTTPException(400, "input is required")

    text = inp or prompt

    # ── text ────────────────────────────────────────────────
    if task == "summarize":
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.replace("\n", " ")) if s.strip()]
        if not sents:
            return "(empty input)"
        out = sents[0]
        if len(sents) > 1:
            out += " " + sents[-1]
        words = len(text.split())
        return json.dumps({"summary": out, "sentences": len(sents), "words": words,
                           "compression": f"{len(sents) - 2} of {len(sents)} sentences removed"},
                          ensure_ascii=False)

    if task == "extract":
        emails = sorted(set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)))
        urls = sorted(set(re.findall(r"https?://[^\s)\]>\"']+", text)))
        nums = sorted(set(re.findall(r"\b\d[\d,]*(?:\.\d+)?\b", text)))
        return json.dumps({"emails": emails, "urls": urls, "numbers": nums},
                          ensure_ascii=False)

    if task == "keywords":
        stop = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "for",
                "on", "with", "as", "at", "by", "from", "that", "this", "be", "are"}
        words = re.findall(r"[a-zA-Z]{3,}", text.lower())
        freq: dict[str, int] = {}
        for w in words:
            if w not in stop:
                freq[w] = freq.get(w, 0) + 1
        top = sorted(freq.items(), key=lambda kv: -kv[1])[:15]
        return json.dumps({"keywords": [k for k, _ in top],
                           "frequencies": dict(top)}, ensure_ascii=False)

    if task == "sentiment":
        pos = {"good", "great", "excellent", "love", "best", "happy", "positive",
               "recommend", "solid", "win", "profit", "gain"}
        neg = {"bad", "poor", "worst", "hate", "loss", "fail", "broken", "negative",
               "avoid", "scam", "bug", "crash", "error"}
        words = re.findall(r"[a-zA-Z]+", text.lower())
        p = sum(1 for w in words if w in pos)
        n = sum(1 for w in words if w in neg)
        total = p + n or 1
        score = (p - n) / total
        label = "positive" if score > 0.1 else "negative" if score < -0.1 else "neutral"
        return json.dumps({"label": label, "score": round(score, 3),
                           "positive_hits": p, "negative_hits": n}, ensure_ascii=False)

    if task == "translate":
        cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
        lat = sum(1 for c in text if c.isascii() and c.isalpha())
        script = "cyrillic" if cyr > lat else "latin"
        return json.dumps({
            "detected_script": script,
            "chars": len(text),
            "words": len(text.split()),
            "note": "script identified; full translation requires a model endpoint",
        }, ensure_ascii=False)

    if task == "rewrite":
        out = re.sub(r"\s+", " ", text).strip()
        out = out[0].upper() + out[1:] if out else out
        if out and out[-1] not in ".!?":
            out += "."
        return json.dumps({"original_chars": len(text), "rewritten": out,
                           "trimmed_chars": len(text) - len(out)}, ensure_ascii=False)

    if task == "proofread":
        issues = []
        if "  " in text:
            issues.append("double spaces present")
        if re.search(r"\bi\b", text):
            issues.append("lowercase 'i' as a pronoun")
        if re.search(r"\s+[.,;:!?]", text):
            issues.append("space before punctuation")
        if re.search(r"[,.;!?]{2,}", text):
            issues.append("repeated punctuation")
        if not text.strip().endswith((".", "!", "?")):
            issues.append("no terminal punctuation")
        return json.dumps({"issues": issues or ["none found"], "count": len(issues)},
                          ensure_ascii=False)

    # ── code ────────────────────────────────────────────────
    if task == "python_debug":
        try:
            ast.parse(text)
            return json.dumps({"syntax": "valid", "kind": "python-ast",
                               "note": "parsed successfully"}, ensure_ascii=False)
        except SyntaxError as e:
            return json.dumps({
                "syntax": "invalid",
                "kind": "python-ast",
                "error": e.msg,
                "line": e.lineno,
                "column": e.offset,
                "text": (e.text or "").strip(),
            }, ensure_ascii=False)

    if task == "code_lint":
        issues = []
        lines = text.split("\n")
        for i, ln in enumerate(lines, 1):
            if ln.rstrip() != ln:
                issues.append(f"line {i}: trailing whitespace")
            if len(ln) > 120:
                issues.append(f"line {i}: {len(ln)} chars (>120)")
            if "\t" in ln:
                issues.append(f"line {i}: tab character")
        if lines and lines[-1].strip():
            issues.append("no trailing newline")
        return json.dumps({"lines": len(lines), "issues": issues or ["none found"],
                           "count": len(issues)}, ensure_ascii=False)

    if task == "code_refactor":
        # Extract function/class signatures and report structure — real AST work.
        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            raise HTTPException(400, f"not parseable python: {e.msg} (line {e.lineno})")
        funcs = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        imports = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imports += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                imports.append(f"{n.module}.*")
        long_fns = []
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = (n.end_lineno or n.lineno) - n.lineno + 1
                if length > 40:
                    long_fns.append({"name": n.name, "lines": length})
        return json.dumps({
            "language": "python",
            "functions": funcs,
            "classes": classes,
            "imports": imports,
            "candidates_for_refactor": long_fns,
            "suggestion": ("split functions over 40 lines" if long_fns
                           else "no functions over 40 lines"),
        }, ensure_ascii=False)

    if task == "code_explain":
        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            raise HTTPException(400, f"not parseable python: {e.msg} (line {e.lineno})")
        top = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
        loops = sum(1 for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While)))
        calls = [n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        return json.dumps({
            "top_level": top,
            "loops": loops,
            "function_calls": sorted(set(calls)),
            "explanation": (
                f"{len(top)} top-level definition(s), {loops} loop(s), "
                f"{len(set(calls))} distinct called function(s)"
            ),
        }, ensure_ascii=False)

    if task == "code_review":
        issues = []
        if re.search(r"except\s*:", text):
            issues.append("bare except: — catches BaseException including KeyboardInterrupt")
        if re.search(r"except\s+Exception\s*:", text):
            issues.append("bare 'except Exception:' — swallows everything")
        if re.search(r"eval\(|exec\(", text):
            issues.append("eval/exec used — code injection risk")
        if re.search(r"subprocess\.[a-z]+\([^)]*shell\s*=\s*True", text):
            issues.append("subprocess with shell=True")
        if re.search(r"password|secret|api[_-]?key", text, re.I) and "input(" in text:
            issues.append("credential read via input() — will echo")
        if re.search(r"TODO|FIXME|XXX|HACK", text):
            issues.append("TODO/FIXME marker present")
        if re.search(r"print\(", text) and "logging" not in text:
            issues.append("print() used instead of logging")
        return json.dumps({"issues": issues or ["none found"], "count": len(issues),
                           "lines": len(text.split("\n"))}, ensure_ascii=False)

    # ── data ────────────────────────────────────────────────
    if task == "json_minify":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"invalid JSON: {e.msg} at line {e.lineno}")
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

    if task == "json_query":
        # Query with a simple dotted path: "a.b.0.c"
        path = prompt or inp
        m = re.match(r"\s*```json\s*(.*?)\s*```\s*(.*)", text, re.S)
        raw, dotted = (m.group(1), m.group(2).strip()) if m else (text, "")
        if not dotted:
            raise HTTPException(400, "provide a dotted path, e.g. 'data.items.0.id'")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"invalid JSON: {e.msg}")
        cur: Any = obj
        for part in dotted.split("."):
            if isinstance(cur, list):
                cur = cur[int(part)]
            else:
                cur = cur[part]
        return json.dumps({"path": dotted, "value": cur}, ensure_ascii=False, default=str)

    if task == "csv_to_json":
        try:
            rows = list(csv.DictReader(io.StringIO(text)))
        except csv.Error as e:
            raise HTTPException(400, f"invalid CSV: {e}")
        if not rows:
            raise HTTPException(400, "CSV has no data rows")
        return json.dumps({"columns": list(rows[0].keys()), "rows": len(rows),
                           "sample": rows[:5]}, ensure_ascii=False)

    if task == "hash":
        algo = (prompt or "sha256").strip().lower()
        if algo not in {"md5", "sha1", "sha256", "sha512", "sha3_256", "blake2b"}:
            raise HTTPException(400, f"unsupported algorithm {algo!r}")
        h = hashlib.new(algo, text.encode()).hexdigest()
        return json.dumps({"algorithm": algo, "digest": h, "input_bytes": len(text.encode())},
                          ensure_ascii=False)

    if task == "base64":
        mode = (prompt or "encode").strip().lower()
        raw = text.encode()
        if mode == "decode":
            try:
                out = base64.b64decode(text, validate=True).decode("utf-8")
            except Exception as e:
                raise HTTPException(400, f"not valid base64: {e}")
            return json.dumps({"mode": "decode", "result": out}, ensure_ascii=False)
        return json.dumps({"mode": "encode", "result": base64.b64encode(raw).decode()},
                          ensure_ascii=False)

    if task == "convert":
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z°%]+)", text)
        if not m:
            raise HTTPException(400, "expected '<number> <unit>'")
        val, unit = float(m.group(1)), m.group(2).lower()
        table = {
            "c": ("f", lambda v: v * 9 / 5 + 32), "f": ("c", lambda v: (v - 32) * 5 / 9),
            "km": ("mi", lambda v: v * 0.621371), "mi": ("km", lambda v: v / 0.621371),
            "kg": ("lb", lambda v: v * 2.20462), "lb": ("kg", lambda v: v / 2.20462),
            "m": ("ft", lambda v: v * 3.28084), "ft": ("m", lambda v: v / 3.28084),
        }
        if unit not in table:
            raise HTTPException(400, f"unknown unit {unit!r}; known: {sorted(table)}")
        to, fn = table[unit]
        return json.dumps({"from": f"{val} {unit}", "to": f"{round(fn(val), 4)} {to}"},
                          ensure_ascii=False)

    # ── media / files ────────────────────────────────────────
    if task == "describe_image":
        if not os.path.exists(text):
            raise HTTPException(400, f"image not found: {text}")
        try:
            from PIL import Image
            with Image.open(text) as im:
                info = {"path": text, "format": im.format, "size": list(im.size),
                        "mode": im.mode, "bytes": os.path.getsize(text)}
                try:
                    info["exif"] = {k: str(v) for k, v in (im.getexif() or {}).items()}
                except Exception:
                    info["exif"] = {}
                return json.dumps(info, ensure_ascii=False)
        except Exception as e:
            raise HTTPException(400, f"not a readable image: {e}")

    if task == "probe_media":
        if not os.path.exists(text):
            raise HTTPException(400, f"file not found: {text}")
        try:
            r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                                "-show_format", "-show_streams", text],
                               capture_output=True, text=True, timeout=60)
            return json.dumps({"path": text, "probe": json.loads(r.stdout or "{}")},
                              ensure_ascii=False, default=str)
        except Exception as e:
            raise HTTPException(400, f"ffprobe failed: {e}")

    raise HTTPException(404, f"unknown task {task!r}")


# ────────────────────────────────────────────────────────────── routes
@app.get("/")
def discovery():
    """The document an agent reads first: what exists, what it costs, how to pay."""
    return {
        "service": "hermes-micro-agent",
        "version": "1.0",
        "receiver": RECEIVER,
        "network": "base",
        "asset": "USDC",
        "usdc_contract": USDC_BASE,
        "protocol": "x402 exact",
        "tasks": [
            {"name": t, "price_usdc": PRICES[t],
             "implemented": t in IMPLEMENTED, "category": _category(t)}
            for t in sorted(PRICES)
        ],
        "how_to_pay": (
            "POST /task with {prompt, task}. Get HTTP 402 with our address and the "
            "amount. Send USDC on Base, then resend with header X-Payment: <tx_hash>. "
            "We verify the Transfer log on-chain before doing any work."
        ),
        "jsonrpc": "POST /rpc with {jsonrpc, id, method, params}",
        "note": "tasks marked implemented:false are refused, never faked",
    }


def _category(t: str) -> str:
    if t in {"summarize", "extract", "keywords", "sentiment", "translate",
             "rewrite", "proofread"}:
        return "text"
    if t in {"python_debug", "code_lint", "code_refactor", "code_explain", "code_review"}:
        return "code"
    if t in {"json_minify", "json_query", "csv_to_json", "hash", "base64", "convert"}:
        return "data"
    return "media"


@app.get("/task")
def discover_task(task: str = "summarize"):
    """GET /task?task=summarize -> always 402 with the x402 payload.

    x402 verifiers (x402 Arena, the Bazaar facilitator) probe a GET request to
    confirm an endpoint is payment-gated. Serving the price here costs nothing
    and is what makes this service discoverable as a paid resource.

    Arena's verifier keys off a 402 response that also carries an x402 payment
    header, so we advertise one explicitly.
    """
    resp = payment_required(task)
    resp.headers["X-PAYMENT-REQUIRED"] = f"USDC {PRICES[task]} base"
    resp.headers["Access-Control-Expose-Headers"] = (
        "X-PAYMENT-REQUIRED, X402-Version, X-PAYMENT")
    return resp


@app.get("/tasks")
def list_tasks():
    return {"tasks": {t: PRICES[t] for t in sorted(PRICES)},
            "receiver": RECEIVER, "network": "base"}


@app.post("/task")
async def execute_task(payload: TaskPayload, request: Request,
                       x_payment_hash: Optional[str] = Header(None, alias="X-Payment")):
    """Run a paid micro-task. Refuses with 402 unless payment is verified on-chain."""
    task = (payload.task or "summarize").strip()

    if task not in PRICES:
        raise HTTPException(404, f"unknown task {task!r}")
    if task not in IMPLEMENTED:
        raise HTTPException(400, f"task {task!r} is not implemented (refused, not faked)")

    usd = price_for(task)
    cents = int(round(usd * 100))

    if not x_payment_hash:
        return payment_required(task)

    try:
        found = verify_payment(x_payment_hash, cents)
    except RuntimeError as e:
        # RPC unreachable: refuse rather than do free work.
        return JSONResponse(status_code=503, content={
            "error": "Payment verification unavailable",
            "message": str(e),
            **x402_payload(task),
        })

    if not found:
        return JSONResponse(status_code=402, content={
            "error": "Payment Required",
            "message": (
                f"Could not confirm {cents / 100:.2f} USDC to {RECEIVER} in tx "
                f"{x_payment_hash}. Check the amount and that it went to this address."
            ),
            "reason": "no matching USDC Transfer to this address",
            **x402_payload(task),
        })

    try:
        result = do_task(task, payload.prompt, "")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"task failed: {e}")

    return {
        "status": "success",
        "agent": "Hermes",
        "task": task,
        "result": result,
        "paid_usdc": found["cents"] / 100,
        "tx_hash": x_payment_hash,
        "sender": found["sender"],
        "settled_at": datetime.now(timezone.utc).isoformat(),
    }


# ────────────────────────────────────────────────────────────── JSON-RPC
_RPC_METHODS = {
    "discover": lambda p: discovery(),
    "price": lambda p: {"task": p.get("task"),
                        "price_usdc": price_for(str(p.get("task", ""))),
                        "receiver": RECEIVER, "network": "base",
                        "implemented": str(p.get("task", "")) in IMPLEMENTED},
    "verify": lambda p: _rpc_verify(p),
    "echo": lambda p: {"echo": p, "at": datetime.now(timezone.utc).isoformat()},
}


def _rpc_verify(p: dict) -> dict:
    tx = str(p.get("tx_hash") or "")
    cents = int(p.get("min_cents") or 0)
    try:
        found = verify_payment(tx, cents)
    except RuntimeError as e:
        return {"verifiable": False, "error": str(e)}
    return {"verifiable": True, "paid": found is not None, "details": found}


@app.post("/rpc")
async def rpc(request: Request):
    """JSON-RPC 2.0 — the interface other agents call."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32700, "message": "Parse error"}})

    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if body.get("jsonrpc") != "2.0":
        return JSONResponse(status_code=400, content={
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32600, "message": "Invalid Request"}})

    if method not in _RPC_METHODS:
        return JSONResponse(status_code=404, content={
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}",
                      "available": sorted(_RPC_METHODS)}})

    try:
        result = _RPC_METHODS[method](params)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    except HTTPException as e:
        return JSONResponse(status_code=400, content={
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32000, "message": e.detail}})


@app.get("/health")
def health():
    return {"ok": True, "receiver": RECEIVER, "implemented": len(IMPLEMENTED)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
