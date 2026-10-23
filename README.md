# Hermes Micro-Agent

A paid HTTP micro-service for AI agents. One endpoint, 20 tasks, USDC on Base.
Payment is verified on-chain before any work runs.

## Free services — no payment, no key

These four are free forever. Call them directly:

```bash
# SHA-256 hash of any text
curl -X POST https://92.5.95.177.sslip.io/free/hash \
  -H "Content-Type: application/json" -d '{"prompt":"hello world"}'

# Base64 encode/decode
curl -X POST https://92.5.95.177.sslip.io/free/base64 \
  -H "Content-Type: application/json" -d '{"prompt":"hello"}'

# Unit conversion (length, weight, temperature, data size)
curl -X POST https://92.5.95.177.sslip.io/free/convert \
  -H "Content-Type: application/json" -d '{"prompt":"10 km to miles"}'

# Minify a JSON document
curl -X POST https://92.5.95.177.sslip.io/free/json_minify \
  -H "Content-Type: application/json" -d '{"prompt":"{  \"a\":  1 }"}'
```

Each returns `{"status":"success","task":"...","result":"...","free":true}`.

## Paid services — $0.10-$0.25 per call

```bash
# 1. See the price list
curl https://92.5.95.177.sslip.io/tasks

# 2. Try a task — you get 402 with the address and the price
curl -X POST https://92.5.95.177.sslip.io/task \
  -H "Content-Type: application/json" \
  -d '{"task":"summarize","prompt":"your text here"}'

# 3. Pay USDC on Base to the address in the 402, then retry with the tx hash
curl -X POST https://92.5.95.177.sslip.io/task \
  -H "Content-Type: application/json" \
  -H "X-Payment: 0x<tx_hash>" \
  -d '{"task":"summarize","prompt":"your text here"}'
```

**Or call a service by name** — one route per task, so a search for the service finds the route:

| Route | Service | Price |
|---|---|---|
| `/summarize` | Summarise text | $0.10 |
| `/extract` | Extract key points | $0.10 |
| `/keywords` | Find keywords | $0.10 |
| `/sentiment` | Detect sentiment | $0.10 |
| `/translate` | Translate | $0.15 |
| `/rewrite` | Rewrite text | $0.15 |
| `/proofread` | Proofread | $0.15 |
| `/code_lint` | Lint Python | $0.15 |
| `/code_explain` | Explain code | $0.15 |
| `/code_refactor` | Refactor code | $0.25 |
| `/code_review` | Review code | $0.25 |
| `/python_debug` | Debug Python | $0.25 |
| `/json_query` | Query JSON | $0.15 |
| `/csv_to_json` | CSV to JSON | $0.15 |
| `/describe_image` | Describe an image | $0.15 |
| `/probe_media` | Probe a media URL | $0.15 |

## Payment

    Receiver : 0x078fE079A9440631Bc6AD722D7143dC382b4ea4f
    Network  : base (eip155:8453)
    Asset    : USDC  0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913

The USDC Transfer is verified on-chain before any work runs. Unpaid or
unverifiable requests get 402 and nothing executes.

## Discovery

- OpenAPI: `https://92.5.95.177.sslip.io/openapi.json`
- x402 manifest: `https://92.5.95.177.sslip.io/.well-known/x402`
- Listed on [x402scan](https://www.x402scan.com/server/4d9a40cb-c0d9-4524-ad62-ac312b7ec7bb)

## JSON-RPC utility gateway (free)

```bash
# Price one task
curl -X POST https://92.5.95.177.sslip.io/rpc \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"price","params":{"task":"summarize"}}'

# Verify a payment
curl -X POST https://92.5.95.177.sslip.io/rpc \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"verify","params":{"tx_hash":"0x...","min_cents":1000}}'
```

## Honest limits

- The paid tasks are deterministic transformations, not model calls. Same input,
  same output.
- `describe_image` and `probe_media` fetch URLs — a failed fetch is reported as
  `[fetch failed: HTTP xxx]`, never invented.
- Unlisted tasks are refused with 400, never faked.
- There is no SLA. The endpoint is a single VPS on a free cloud tier.
