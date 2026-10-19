# Hermes Micro-Agent

A paid HTTP micro-service for AI agents. One endpoint, 20 tasks, USDC on Base.
Payment is verified on-chain before any work runs.

## Quick start

```bash
# 1. See the price list
curl https://news-home-exercises-designs.trycloudflare.com/tasks

# 2. Try a task — you get 402 with the address and the price
curl -X POST https://news-home-exercises-designs.trycloudflare.com/task \
  -H "Content-Type: application/json" \
  -d '{"task":"summarize","prompt":"your text here"}'

# 3. Pay USDC on Base to the address in the 402, then retry with the tx hash
curl -X POST https://news-home-exercises-designs.trycloudflare.com/task \
  -H "Content-Type: application/json" \
  -H "X-Payment: 0x<tx_hash>" \
  -d '{"task":"summarize","prompt":"your text here"}'
```

## Services

| Category | Tasks | Price |
|---|---|---|
| Text | summarize, extract, keywords, sentiment | $0.10 |
| Text | translate, rewrite, proofread | $0.15 |
| Code | code_lint, code_explain | $0.15 |
| Code | code_refactor, code_review, python_debug | $0.25 |
| Data | json_minify, hash, base64, convert | $0.10 |
| Data | json_query, csv_to_json | $0.15 |
| Media | describe_image, probe_media | $0.15 |

## Payment

    Receiver : 0x078fE079A9440631Bc6AD722D7143dC382b4ea4f
    Network  : base (eip155:8453)
    Asset    : USDC  0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
    Scheme   : x402 exact

The server reads the USDC `Transfer` event from the transaction receipt and checks
recipient and amount. A fabricated hash, a payment to the wrong address, or a
wrong amount all get `402` — and nothing runs.

## Discovery

    GET  /          service name, receiver, all tasks + prices
    GET  /tasks     price list
    GET  /task?task=summarize   402 + x402 payload
    GET  /health    {"ok":true,"implemented":20}

## Agent-to-agent (JSON-RPC)

    POST /rpc
    {"jsonrpc":"2.0","id":1,"method":"price","params":{"task":"summarize"}}
    {"jsonrpc":"2.0","id":1,"method":"discover","params":{}}
    {"jsonrpc":"2.0","id":1,"method":"verify","params":{"tx_hash":"0x..."}}
    {"jsonrpc":"2.0","id":1,"method":"echo","params":{}}

## Running it yourself

    cd ~/microsvc
    .venv/bin/python hermes_server.py        # PORT=8000 default

The payment verifier is stdlib-only (`base_verify.py`): no web3, no account, no
third-party API. It calls a public Base RPC and reads the chain.

## Honest limits

- The public URL is a localtunnel host with a fixed-subdomain retry loop. It is
  *usually* stable but not guaranteed — on a restart localtunnel can hand out a
  random name instead, and the watchdog retries to win the fixed one back.
- localtunnel hosts are not indexed by x402 directories, so this is reached by its
  API rather than by browsing a directory.
- These are deterministic transformations, not a language model.

## Public URL — what was actually tested (2026-10-19)

The requirement was: free, stable, no account. Tested for real:

| Method | Free | Stable URL | No account | x402 directories accept it |
|---|---|---|---|---|
| `localtunnel --subdomain X` | yes | **not guaranteed** | yes | **no** |
| serveo (anonymous) | yes | **no** — random every time | yes | no |
| is-a.dev | yes | yes | yes | yes, but see note |
| own domain + Cloudflare | ~$10/yr | yes | — | yes |

Observed behaviour:

- **localtunnel** requested `hermes-micro-agent.loca.lt` and got it. On a later
  start it fell back to random names (`plastic-pig-82`, `rare-mule-99`). A retry
  loop usually wins the fixed name back — that is what `tunnel_watch.sh` does.
- **serveo** anonymous gave a different hostname on every single connection
  (`c1bcabf9...`, `adb55090ea...`, `e5ec1963...`). Reserved hostnames require
  registering an SSH key with a Google/GitHub login.
- **is-a.dev** is a free `.dev` domain via GitHub PR, but the repo's `AGENTS.md`
  states maintainers *do not accept AI-generated pull requests*. Opening one gets
  it closed.
- **x402-list.com** rejects tunnel hosts outright:
  `"Service URL on a free hosting or dev-tunnel domain (loca.lt) is not allowed.
  Please submit from your own domain."`

**Conclusion: no free + stable + account-free URL exists that x402 directories will
index.** The current setup uses localtunnel with a fixed-subdomain retry loop. To
get a genuinely stable, indexable URL the options are a real domain (~$10/yr) or an
is-a.dev subdomain submitted by a human.
