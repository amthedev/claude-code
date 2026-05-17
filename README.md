# Claude Code

FastAPI gateway compatible with the Anthropic Messages API shape used by Claude Code. It exposes local model names like `claude-code-pro`, routes them to OpenRouter models, and keeps Claude Code tool use safe by proxying tool and streaming calls directly upstream.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env` and set `OPENROUTER_API_KEY`.

Run the API:

```bash
claude-gateway
```

Configure Claude Code:

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
export ANTHROPIC_AUTH_TOKEN="local-dev-token"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-code-economy"
export ANTHROPIC_DEFAULT_SONNET_MODEL="claude-code-pro"
export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-code-ultra"
export CLAUDE_CODE_SUBAGENT_MODEL="claude-code-pro"
```

Open the web app:

- Client chat: `http://127.0.0.1:8787/app`
- Admin prototype: `http://127.0.0.1:8787/admin`

## Public Models

- `claude-code-economy`: cheap/default coding path, usually DeepSeek V4 Flash.
- `claude-code-pro`: stronger code/reasoning path, usually DeepSeek V4 Pro.
- `claude-code-ultra`: strong path with optional premium review.
- `claude-code-ui`: frontend/UI path, usually Kimi K2.6.
- `claude-code-auto`: heuristically chooses between the above.

You can override every internal model with environment variables. The defaults in `.env.example` were checked against OpenRouter's public model list on 2026-05-17.

## Cost Guard

The default architecture targets at least 50% savings versus Claude Opus 4.7. It uses Claude Opus 4.7 as the baseline and rejects known internal models whose blended input/output price is above `MAX_COST_RATIO_VS_CLAUDE`. For multi-agent paths it also sums the internal calls conservatively; if the whole pipeline would exceed the target, the gateway falls back to a single budget-safe proxy call.

```bash
export MAX_COST_RATIO_VS_CLAUDE=0.50
export ALLOW_PREMIUM_FALLBACK=false
export ALLOW_DIRECT_EXTERNAL_MODELS=false
```

With the default model set, the main paths stay well under that budget:

- DeepSeek V4 Pro: about 4.4% of Claude Opus 4.7 blended token cost.
- DeepSeek V4 Flash: about 1.1% of Claude Opus 4.7 blended token cost.
- Kimi K2.6: about 14.1% of Claude Opus 4.7 blended token cost.

`claude-code-ultra` improves quality through extra cheap candidates and review instead of calling Claude by default. Set `ALLOW_PREMIUM_FALLBACK=true` only if you intentionally want to permit premium fallback models that still pass the budget guard.

External model ids such as `anthropic/claude-opus-4.7` are not used directly by default. They are routed back into the budget-safe internal model set unless `ALLOW_DIRECT_EXTERNAL_MODELS=true` is explicitly enabled.

## Paid Customer Tokens

For selling API access, use customer-scoped tokens instead of sharing the admin token:

```env
CUSTOMER_ACCOUNTS=cus_live_abc|Cliente|149.90|60000|claude-code-pro|true
CUSTOMER_PROFIT_MARGIN=0.50
USD_TO_BRL=5.50
COST_RESERVE_MULTIPLIER=2.0
QUOTA_DATA_FILE=data/customer_usage.json
```

Format:

```text
token|customer_name|monthly_price_brl|daily_token_limit|allowed_public_model|active
```

When a customer token calls `/v1/messages`, the gateway forces the allowed model, clamps output tokens, reserves daily cost, and blocks requests that would exceed the plan.

## Square Cloud

The repository includes `requirements.txt` and `squarecloud.app`. The configured start command is:

```bash
uvicorn claude_gateway.main:app --host 0.0.0.0 --port 80
```

Set these environment variables in Square Cloud:

```env
OPENROUTER_API_KEY=sk-or-v1-...
GATEWAY_API_KEYS=strong-admin-token
OPENROUTER_SITE_URL=https://your-subdomain.squareweb.app
MAX_COST_RATIO_VS_CLAUDE=0.50
ALLOW_PREMIUM_FALLBACK=false
ALLOW_DIRECT_EXTERNAL_MODELS=false
CUSTOMER_ACCOUNTS=...
```

See [docs/ANALISE_E_DEPLOY.md](docs/ANALISE_E_DEPLOY.md) for the full architecture and operating notes.

## Compatibility Policy

Claude Code relies heavily on streaming and tool calls. For that reason:

- requests with `stream: true` are proxied directly to one selected OpenRouter model;
- requests with `tools`, `tool_choice`, `tool_use`, or `tool_result` are proxied directly;
- non-streaming text-only requests can use the multi-agent pipeline.

This keeps the gateway compatible with Claude Code while still supporting agent debate for normal API calls.

## Useful Endpoints

```http
GET  /health
GET  /v1/models
GET  /v1/budget
POST /v1/messages
GET  /v1/usage
POST /v1/router/debug
POST /v1/agent/run
```

## Smoke Test

```bash
curl -s http://127.0.0.1:8787/health | jq

curl -s http://127.0.0.1:8787/v1/router/debug \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-code-auto","max_tokens":256,"messages":[{"role":"user","content":"Crie um dashboard React bonito"}]}' | jq
```
