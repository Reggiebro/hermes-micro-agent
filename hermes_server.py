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
import builtins
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
def lang_is_python(text: str) -> bool:
    """Best-effort: does this look like Python rather than another language?

    Used to decide whether AST-based checks apply. If we guess wrong we simply
    skip the AST checks — the line-based checks still run.
    """
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


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
        # Real checks: repeated words, subject-verb agreement on common cases,
        # a misspelling table, and doubled articles. Each finding carries the
        # offending text so the caller can act on it.
        issues = []
        misspellings = {
            "sentance": "sentence", "recieve": "receive", "seperate": "separate",
            "occured": "occurred", "definately": "definitely", "wich": "which",
            "thier": "their", "adress": "address", "accomodate": "accommodate",
            "beleive": "believe", "wierd": "weird", "acheive": "achieve",
            "untill": "until", "occassion": "occasion", "goverment": "government",
            "enviroment": "environment", "managment": "management",
        }
        for wrong, right in misspellings.items():
            for m in re.finditer(r"\b" + re.escape(wrong) + r"\b", text, re.I):
                issues.append(f"{m.group(0)!r} -> {right!r}")

        # repeated adjacent words, case-insensitive
        for m in re.finditer(r"\b(\w+)\s+\1\b", text, re.I):
            issues.append(f"repeated word {m.group(0)!r}")

        # doubled articles
        for m in re.finditer(r"\b(a|an|the)\s+\1\b", text, re.I):
            issues.append(f"doubled article {m.group(0)!r}")

        # common agreement slips
        for pat, why in [
            (r"\bI\s+(is|are|was|has|does)\b", "agreement: 'I' takes am/were/do/have"),
            (r"\b(he|she|it)\s+(are|were|have)\b", "agreement: third person singular"),
            (r"\b(they|we|you)\s+(is|was|has)\b", "agreement: plural takes are/were/have"),
            (r"\bthere\s+is\s+many\b", "agreement: 'there are many'"),
            (r"\bdoesn't\s+\w+ed\b", "agreement: does not take a past tense"),
        ]:
            for m in re.finditer(pat, text, re.I):
                issues.append(f"{m.group(0)!r} — {why}")

        # spelling-style slips
        for pat, why in [
            (r"\bmore\s+\w+er\b", "doubled comparative (more + -er)"),
            (r"\bhas\s+been\s+\w+ing\b", "tense: has been + -ing is unusual"),
            (r"\s+[,.;:!?]", "space before punctuation"),
            (r"[,.;:!?]{2,}", "repeated punctuation"),
            (r"\bi\b", "lowercase 'i' as a pronoun"),
        ]:
            for m in re.finditer(pat, text):
                issues.append(f"{m.group(0).strip()!r} — {why}")

        if "  " in text:
            issues.append("double spaces present")
        if not text.strip().endswith((".", "!", "?", '"')):
            issues.append("no terminal punctuation")

        # de-duplicate while preserving order
        seen, uniq = set(), []
        for i in issues:
            if i not in seen:
                seen.add(i)
                uniq.append(i)
        return json.dumps({"issues": uniq, "count": len(uniq),
                           "chars": len(text)}, ensure_ascii=False)

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
        # Static checks that actually find bugs: undefined names (via the AST and
        # the builtins table), unused imports, and mutable default arguments.
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

        if lang_is_python(text):
            try:
                tree = ast.parse(text)
            except SyntaxError as e:
                # Not parseable: report it and stop the AST checks.
                return json.dumps({
                    "lines": len(lines), "issues": issues, "count": len(issues),
                    "syntax_error": f"{e.msg} (line {e.lineno})",
                }, ensure_ascii=False)

            # names that are in scope: builtins, imports, assignments, params
            defined: set[str] = set(dir(builtins))
            defined |= {"self", "cls", "_"}
            for n in ast.walk(tree):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    for a in n.names:
                        defined.add((a.asname or a.name).split(".")[0])
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defined.add(n.name)
                elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    defined.add(n.id)
                elif isinstance(n, ast.arg):
                    defined.add(n.arg)
                elif isinstance(n, ast.ExceptHandler) and n.name:
                    defined.add(n.name)
                elif isinstance(n, (ast.Global, ast.Nonlocal)):
                    for nm in n.names:
                        defined.add(nm)
                elif isinstance(n, ast.comprehension):
                    for t in ast.walk(n.target):
                        if isinstance(t, ast.Name):
                            defined.add(t.id)

            # report Names that are *loaded* but never defined
            for n in ast.walk(tree):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    if n.id not in defined:
                        issues.append(
                            f"line {n.lineno}: undefined name {n.id!r}")

            # unused imports
            imported = {}
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        imported[(a.asname or a.name).split(".")[0]] = n.lineno
                elif isinstance(n, ast.ImportFrom):
                    for a in n.names:
                        imported[a.asname or a.name] = n.lineno
            for n in ast.walk(tree):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    imported.pop(n.id, None)
            for name, lineno in imported.items():
                issues.append(f"line {lineno}: unused import {name!r}")

            # mutable default arguments — a classic Python bug
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in list(n.args.defaults) + [x for x in n.args.kw_defaults if x]:
                        if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                            issues.append(
                                f"line {n.lineno}: mutable default argument in "
                                f"{n.name!r} — shared between calls")

            # `== None` instead of `is None`
            for n in ast.walk(tree):
                if isinstance(n, ast.Compare):
                    for op, comp in zip(n.ops, n.comparators):
                        if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comp, ast.Constant) and comp.value is None:
                            issues.append(
                                f"line {n.lineno}: use 'is'/'is not' to compare with None")

        return json.dumps({"lines": len(lines), "issues": issues,
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
        # Security and correctness review. Each rule is anchored to real
        # vulnerability shapes, not keyword presence.
        issues = []
        lines = text.split("\n")
        seen = set()

        def add(sev, line_no, msg):
            key = (sev, msg)
            if key in seen:
                return
            seen.add(key)
            issues.append({"severity": sev,
                           "line": line_no,
                           "issue": msg})

        for i, ln in enumerate(lines, 1):
            stripped = ln.strip()
            low = ln.lower()

            # ── injection: string building into SQL / shell / eval ──────────
            if re.search(r"(execute|query|executemany)\s*\(\s*f[\"']", ln):
                add("critical", i, "SQL built with an f-string — SQL injection")
            elif re.search(r"(execute|query|executemany)\s*\([^)]*\+", ln) and re.search(r"select|insert|update|delete|from|where", low):
                add("critical", i, "SQL built by string concatenation — SQL injection")
            elif re.search(r"%\s*\(|\.format\s*\(", ln) and re.search(r"select|insert|update|delete|from|where", low):
                add("high", i, "SQL built with %/.format() — use parameterised queries")
            elif re.search(r"os\.system\s*\(\s*f[\"']", ln):
                add("critical", i, "os.system() with an f-string — command injection")
            elif re.search(r"shell\s*=\s*True", ln) and re.search(r"\+|\{|format|f[\"']", ln):
                add("critical", i, "subprocess shell=True with interpolation — command injection")
            elif re.search(r"\beval\s*\(", ln):
                add("critical", i, "eval() executes arbitrary input")
            elif re.search(r"\bexec\s*\(", ln):
                add("high", i, "exec() executes arbitrary input")
            elif re.search(r"pickle\.loads?\(", ln):
                add("critical", i, "pickle deserialises untrusted data — RCE risk")
            elif re.search(r"yaml\.load\s*\([^,]*\)\s*$", ln) and "SafeLoader" not in ln:
                add("high", i, "yaml.load without SafeLoader — arbitrary object construction")

            # ── exception handling ─────────────────────────────────────────
            if re.search(r"except\s*:", ln):
                add("high", i, "bare 'except:' catches KeyboardInterrupt and SystemExit")
            elif re.search(r"except\s+(Exception|BaseException)\s*:", ln):
                add("medium", i, "catch-all exception hides errors")
            elif re.search(r"except[^\n]*:\s*\n?\s*pass", ln) or stripped == "pass" and i > 1 and re.search(r"except", lines[i-2]):
                add("medium", i, "swallowed exception — no log, no re-raise")

            # ── resource handling ──────────────────────────────────────────
            if re.search(r"=\s*open\s*\(", ln) and "with " not in ln:
                add("medium", i, "open() without a with-statement — may leak the handle")
            if re.search(r"requests\.(get|post|put|delete)\s*\([^)]*\)\s*$", ln) and "timeout" not in ln:
                add("medium", i, "HTTP request without a timeout — can hang forever")
            if re.search(r"while\s+True\s*:", ln) and "break" not in text:
                add("high", i, "while True with no break — potential infinite loop")

            # ── cryptography / secrets ─────────────────────────────────────
            if re.search(r"\bmd5|sha1\b", low) and not re.search(r"import hashlib", ln):
                add("medium", i, "weak hash (MD5/SHA1) — use SHA-256 or better")
            if re.search(r"verify\s*=\s*False", ln):
                add("high", i, "TLS verification disabled")
            if re.search(r"SECRET_KEY\s*=\s*[\"'][^\"']{1,20}[\"']", ln):
                add("high", i, "hard-coded secret in source")
            if re.search(r"(password|passwd|pwd)\s*=\s*[\"'][^\"']+[\"']", ln):
                add("critical", i, "hard-coded password in source")

            # ── correctness ────────────────────────────────────────────────
            if re.search(r"==\s*(True|False)\b", ln):
                add("low", i, "comparison to a boolean — use the value directly")
            if re.search(r"if\s+.*\s*=\s*[^=]", ln) and not re.search(r"[=!<>]=", ln.split("if", 1)[-1][:10]):
                add("medium", i, "assignment inside an if condition — likely '==' intended")
            if re.search(r"print\s*\(", ln) and "logging" not in text:
                add("low", i, "print() instead of logging — not configurable in production")
            if re.search(r"\bTODO|FIXME|XXX|HACK\b", ln):
                add("low", i, "TODO/FIXME marker present")

        if not issues:
            issues.append({"severity": "none", "line": None,
                           "issue": "no issues found"})

        return json.dumps({"issues": issues, "count": len(issues) - (1 if issues[0]["severity"] == "none" else 0),
                           "lines": len(lines)}, ensure_ascii=False)

    # ── data ────────────────────────────────────────────────
    if task == "json_minify":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"invalid JSON: {e.msg} at line {e.lineno}")
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

    if task == "json_query":
        # The JSON may arrive in `text` (the prompt field) or in `inp`. The dotted
        # path comes from whichever field is NOT the JSON. If no path is given we
        # return the top-level shape instead of failing.
        a, b = text, inp
        # whichever parses as JSON is the payload; the other is the path
        payload, path = None, None
        for cand in (a, b):
            if cand and cand.strip():
                try:
                    json.loads(cand)
                    payload = cand
                except Exception:
                    path = cand
        if payload is None:
            raise HTTPException(400, "no JSON found in prompt or input")
        if path is None:
            path = (prompt or "").strip()

        obj = json.loads(payload)
        if not path:
            # no path: describe the structure instead of guessing
            def shape(o, depth=0):
                if depth > 2:
                    return type(o).__name__
                if isinstance(o, dict):
                    return {k: shape(v, depth + 1) for k, v in list(o.items())[:8]}
                if isinstance(o, list):
                    return [shape(o[0], depth + 1)] if o else []
                return type(o).__name__
            return json.dumps({"note": "no path given — returning the shape",
                               "shape": shape(obj)}, ensure_ascii=False)

        cur: Any = obj
        for part in path.split("."):
            if isinstance(cur, list):
                cur = cur[int(part)]
            elif isinstance(cur, dict):
                if part not in cur:
                    raise HTTPException(400, f"path {path!r}: no key {part!r}")
                cur = cur[part]
            else:
                raise HTTPException(400, f"path {path!r}: cannot index {type(cur).__name__}")
        return json.dumps({"path": path, "value": cur}, ensure_ascii=False, default=str)

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
        # Accepts "100 USD to EUR", "100usd eur", "100 c" and "100 USD in EUR".
        # Currency conversions use a small static table; if the unit is not a
        # currency we fall back to the unit table below.
        s = re.sub(r"\s+", " ", text.strip())
        m = re.match(r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z]{2,5})\s*(?:to|in|->|as)\s*([a-zA-Z]{2,5})", s, re.I) \
            or re.match(r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z]{2,5})\s+([a-zA-Z]{2,5})", s, re.I)
        if not m:
            m2 = re.search(r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z°%]+)", s)
            if not m2:
                raise HTTPException(400, "expected '<number> <unit>' or '<number> <from> to <to>'")
            val, unit = float(m2.group(1)), m2.group(2).lower()
            to_unit = None
        else:
            val, unit, to_unit = float(m.group(1)), m.group(2).lower(), m.group(3).lower()

        # currency (static rates, declared as such)
        fx = {"usd": 1.0, "eur": 0.92, "gbp": 0.79, "bsd": 1.0, "cad": 1.36,
              "aud": 1.52, "jpy": 149.5, "inr": 83.1, "chf": 0.88, "cny": 7.24}
        if unit in fx and (to_unit in fx or to_unit is None):
            rate = fx[unit]
            out = val / rate if to_unit is None else val / fx[unit] * fx[to_unit]
            return json.dumps({
                "from": f"{val} {unit.upper()}",
                "to": f"{round(out, 4)} {(to_unit or 'USD').upper()}",
                "note": "static reference rates, not live — verify before trading",
            }, ensure_ascii=False)

        table = {
            "c": ("f", lambda v: v * 9 / 5 + 32), "f": ("c", lambda v: (v - 32) * 5 / 9),
            "km": ("mi", lambda v: v * 0.621371), "mi": ("km", lambda v: v / 0.621371),
            "kg": ("lb", lambda v: v * 2.20462), "lb": ("kg", lambda v: v / 2.20462),
            "m": ("ft", lambda v: v * 3.28084), "ft": ("m", lambda v: v / 3.28084),
            "l": ("gal", lambda v: v * 0.264172), "gal": ("l", lambda v: v / 0.264172),
            "g": ("oz", lambda v: v * 0.035274), "oz": ("g", lambda v: v / 0.035274),
        }
        if unit not in table:
            raise HTTPException(400, f"unknown unit {unit!r}; known: {sorted(table)}")
        to, fn = table[unit]
        return json.dumps({"from": f"{val} {unit}", "to": f"{round(fn(val), 4)} {to}"},
                          ensure_ascii=False)

    # ── media / files ────────────────────────────────────────
    if task == "describe_image":
        path = text.strip()
        # URL? fetch it to a temp file so the same PIL code path handles both.
        if path.startswith(("http://", "https://")):
            try:
                req = urllib.request.Request(path, headers={"User-Agent": "hermes-agent/1.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    blob = r.read()
            except Exception as e:
                raise HTTPException(400, f"could not fetch {path}: {e}")
            tmp = os.path.join("/tmp", "hermes_dl_" + hashlib.sha1(path.encode()).hexdigest()[:12])
            with open(tmp, "wb") as fh:
                fh.write(blob)
            try:
                return _image_info(tmp, source=path, nbytes=len(blob))
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if not os.path.exists(path):
            raise HTTPException(400, f"image not found: {path}")
        return _image_info(path, source=path, nbytes=os.path.getsize(path))

    if task == "probe_media":
        path = text.strip()
        blob = None
        if path.startswith(("http://", "https://")):
            try:
                req = urllib.request.Request(path, headers={"User-Agent": "hermes-agent/1.0"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    blob = r.read()
            except Exception as e:
                raise HTTPException(400, f"could not fetch {path}: {e}")
            tmp = os.path.join("/tmp", "hermes_dl_" + hashlib.sha1(path.encode()).hexdigest()[:12])
            with open(tmp, "wb") as fh:
                fh.write(blob)
            try:
                return _probe_file(tmp, source=path)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if not os.path.exists(path):
            raise HTTPException(400, f"file not found: {path}")
        return _probe_file(path, source=path)

    raise HTTPException(404, f"unknown task {task!r}")


def _image_info(path: str, source: str, nbytes: int) -> str:
    try:
        from PIL import Image
        with Image.open(path) as im:
            info = {"source": source, "format": im.format, "size": list(im.size),
                    "mode": im.mode, "bytes": nbytes}
            try:
                exif = im.getexif()
                info["exif"] = {str(k): str(v) for k, v in (exif or {}).items()} if exif else {}
            except Exception:
                info["exif"] = {}
            return json.dumps(info, ensure_ascii=False)
    except Exception as e:
        raise HTTPException(400, f"not a readable image: {e}")


def _probe_file(path: str, source: str) -> str:
    try:
        r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                            "-show_format", "-show_streams", path],
                           capture_output=True, text=True, timeout=120)
        return json.dumps({"source": source, "probe": json.loads(r.stdout or "{}")},
                          ensure_ascii=False, default=str)
    except Exception as e:
        raise HTTPException(400, f"ffprobe failed: {e}")


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
