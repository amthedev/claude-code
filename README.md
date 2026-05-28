# Claude Code Gateway

FastAPI gateway and web app for selling an AI assistant experience with Claude Code compatibility. It exposes public model names like `claude-code-pro`, routes them to your Anthropic-compatible VPS model first, supports optional OpenAI web search.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env` and set your VPS model endpoint:

```env
VPS_MODEL_BASE_URL=https://sua-vps.example.com
VPS_MODEL_ID=local-model
VPS_MODEL_API_FORMAT=anthropic
VPS_MODEL_API_KEY=
RUNPOD_API_KEY=
RUNPOD_POD_ID=
```

For RunPod vLLM templates, `VPS_MODEL_API_KEY` is the vLLM serving key, often
`sk-[pod-id]`. The admin panel's "Ligar VPS" button needs `RUNPOD_API_KEY`,
which is the account API key from RunPod settings, plus `RUNPOD_POD_ID`.

Run the API:

```bash
claude-gateway
```

Configure Claude Code:

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
export ANTHROPIC_AUTH_TOKEN="local-dev-token"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-code-pro"
export ANTHROPIC_DEFAULT_SONNET_MODEL="claude-code-pro"
export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-code-pro"
export CLAUDE_CODE_SUBAGENT_MODEL="claude-code-pro"
```

Open the web app:

- Client chat: `http://127.0.0.1:8787/app`
- Admin gift cards: `http://127.0.0.1:8787/admin`

## Provider Setup

The gateway keeps Claude Code, Cursor, Windsurf, and OpenAI-compatible clients pointed at this public API. Internally, the main model call goes to your VPS:

- Primary Anthropic-compatible: `POST {VPS_MODEL_BASE_URL}/v1/messages`
- Primary OpenAI-compatible/vLLM/RunPod: `POST {VPS_MODEL_BASE_URL}/chat/completions` when `VPS_MODEL_API_FORMAT=openai-chat`
- Public model: `claude-code-pro`
- Actual VPS model id sent upstream: `VPS_MODEL_ID`

```env
VPS_MODEL_BASE_URL=https://sua-vps.example.com
VPS_MODEL_ID=local-model
VPS_MODEL_API_FORMAT=anthropic
VPS_MODEL_API_KEY=
VPS_MODEL_TIMEOUT_SECONDS=55
VPS_MODEL_SLOW_FALLBACK_SECONDS=6
VPS_DISABLE_QWEN_THINKING=true
SIMPLE_REQUEST_MAX_OUTPUT_TOKENS=768
OPENROUTER_EMERGENCY_FALLBACK=false
OPENROUTER_API_KEY=
```

### Same-GPU fast + strong VPS models

To keep everything on one RunPod GPU, run two vLLM servers in the same pod:

- Fast model on port `8000`, for simple and economy requests.
- Strong model on port `8001`, for code, debugging, architecture, reviews, and strong reasoning.

Gateway configuration:

```env
VPS_MODEL_BASE_URL=https://SEU-POD-8000.proxy.runpod.net/v1
VPS_MODEL_ID=qwen25-coder-14b
VPS_MODEL_API_FORMAT=openai-chat
VPS_MODEL_API_KEY=sk-SEU-POD

VPS_FAST_MODEL_BASE_URL=https://SEU-POD-8000.proxy.runpod.net/v1
VPS_FAST_MODEL_ID=qwen25-coder-14b
VPS_FAST_MODEL_API_FORMAT=openai-chat
VPS_FAST_MODEL_API_KEY=sk-SEU-POD

VPS_STRONG_MODEL_BASE_URL=
VPS_STRONG_MODEL_ID=
VPS_STRONG_MODEL_API_FORMAT=
VPS_STRONG_MODEL_API_KEY=
```

Suggested pod command for an L40S:

```bash
bash -lc 'python -m vllm.entrypoints.openai.api_server --host 0.0.0.0 --port 8001 --model Qwen/Qwen2.5-Coder-14B-Instruct-AWQ --served-model-name qwen25-coder-14b --api-key "$VLLM_API_KEY" --max-model-len 16384 --gpu-memory-utilization 0.62 --trust-remote-code --enable-auto-tool-choice --tool-call-parser hermes'
```

Use one fast code model by default under user load. If you later add a strong
model, keep it on a separate port and only enable `VPS_STRONG_MODEL_*` after a
smoke test on the same GPU.

## Coding Combo

The public model combo is tuned for Claude Code terminal work, but the actual generation now uses your configured VPS model. The router still keeps public identity, token limits, tool behavior, and compatibility rules stable.

- `VPS_MODEL_ID`: the single upstream model used by default.
- OpenRouter fallback: disabled. Requests stay on the configured VPS.
- `gpt-5.4-mini`: optional OpenAI decision/design-director pass when `OPENAI_API_KEY` is configured.

Every request also receives a Claude public response profile while preserving Anthropic-compatible tone, tool-call behavior, and coding ergonomics for Claude Code.

See [docs/CODING_COMBO.md](docs/CODING_COMBO.md) for the full preset and terminal setup.
Use [docs/BENCHMARK.md](docs/BENCHMARK.md) to run the no-credit router benchmark and the low-token live smoke test.

## Public Model

- `claude-code-pro`: Claude Sonnet 4.5 public identity, backed by `VPS_MODEL_ID`.

The visible model name stays stable for clients. The VPS receives `VPS_MODEL_ID`.

## Cost Guard

The gateway still enforces daily plan limits before the upstream call. `MAX_COST_RATIO_VS_CLAUDE` remains available for route estimates and compatibility, while the real primary provider is your VPS.

```bash
export MAX_COST_RATIO_VS_CLAUDE=0.50
export ALLOW_PREMIUM_FALLBACK=false
export ALLOW_DIRECT_EXTERNAL_MODELS=false
```

`claude-code-pro` is the only public model exposed to clients. OpenRouter is disabled for production requests.

## Optional ChatGPT Helper

You can add an OpenAI/ChatGPT key so the internal agent pipeline gets one extra review pass before the final answer:

```env
OPENAI_API_KEY=sk-proj-...
```

By default this helper is blocked for customer chat so paid tokens do not get spent on extra hidden helper calls. `OPENAI_HELPER_FOR_CUSTOMERS` is ignored intentionally.

The default helper model is `gpt-5.4-mini`. You can switch it when needed:

```env
OPENAI_HELPER_MODEL=gpt-5.5
```

## Optional Web Search

When `OPENAI_API_KEY` is configured, Claude can run a lightweight OpenAI Responses API `web_search` pass before the main model only when fresh information is needed or when the request sets `gateway_web_search` to `required`.

```env
ENABLE_WEB_SEARCH=true
WEB_SEARCH_MODEL=gpt-5.5
WEB_SEARCH_CONTEXT_SIZE=low
WEB_SEARCH_FOR_CUSTOMERS=true
WEB_SEARCH_MAX_OUTPUT_TOKENS=900
WEB_SEARCH_TIMEOUT_SECONDS=8
WEB_SEARCH_ALLOWED_DOMAINS=
WEB_SEARCH_BLOCKED_DOMAINS=
```

Accepted request control:

```json
{
  "gateway_web_search": "auto"
}
```

Use `auto` for the default, `required` to force search, and `off` to prevent search. Search results are injected as internal context and the final answer should cite sources as Markdown links when it uses current web data.

## MCP / Claude Desktop Bridge

The project also includes a small MCP server that exposes coding tools over the
Model Context Protocol. This is the bridge you need for Claude Desktop, ChatGPT
Apps, MCP hosts, or Claude Code MCP integrations to inspect a project, apply
patches, run allowed tests, and ask this gateway for extra reasoning.

The Square Cloud hosted API for this project is:

```text
https://your-subdomain.squareweb.app
```

For Claude Desktop on this machine, install the local stdio MCP bridge:

```bash
python3 scripts/install_claude_desktop_mcp.py \
  --gateway-url "https://your-subdomain.squareweb.app" \
  --gateway-token "replace-with-a-customer-or-api-token"
```

Restart Claude Desktop after installing. The installer merges a `claude-code-api`
entry into `~/Library/Application Support/Claude/claude_desktop_config.json`
without deleting existing preferences.

MCP does not replace Claude Desktop's built-in model. It gives Claude Desktop
tools that call this project's API, especially `ask_claude_api`,
`think_with_gateway`, `coworking`, `list_gateway_models`, and `gateway_status`.
The `coworking` MCP tool provides a coworking-style coding session through your
own hosted API, without depending on Claude Desktop's native paid Cowork feature.
Use a customer/API token generated in the Admin screen. `GATEWAY_API_KEYS` is an
admin-only emergency token and cannot call model endpoints when
`ALLOW_ADMIN_MODEL_ACCESS=false`.

For Claude Code or another HTTP MCP host, you can run the same server as a local
Streamable HTTP endpoint:

```bash
export MCP_WORKSPACE_ROOT="$PWD"
export MCP_GATEWAY_BASE_URL="https://your-subdomain.squareweb.app"
export MCP_GATEWAY_TOKEN="replace-with-a-customer-or-api-token"
export MCP_TRANSPORT="streamable-http"
export MCP_HOST="127.0.0.1"
export MCP_PORT="8000"
export MCP_ENABLE_WRITE_TOOLS="true"
export MCP_ENABLE_COMMANDS="true"
claude-mcp
```

By default it starts a Streamable HTTP MCP endpoint at `http://localhost:8000/mcp`.
For Claude Code you can add it with:

```bash
claude mcp add --transport http claude-gateway-tools http://localhost:8000/mcp
```

Tools exposed:

- `analyze_project`: summarize the workspace and configuration.
- `list_files` and `read_file`: inspect project files under `MCP_WORKSPACE_ROOT`.
- `write_file` and `apply_patch`: edit files inside the workspace.
- `run_tests`: run only exact commands allowed by `MCP_ALLOWED_COMMANDS`.
- `gateway_status`, `list_gateway_models`, `think_with_gateway`, and
  `ask_claude_api`: use the backing gateway/OpenRouter API.
- `coworking`: run a pair-programming, review, debug, or planning coworking
  session through the hosted API.

For production, keep `MCP_ENABLE_WRITE_TOOLS=false` and `MCP_ENABLE_COMMANDS=false`
unless the MCP endpoint is private, authenticated, and behind HTTPS.

## Security Baseline

Use SQLite for server-side accounts, gift cards, and customer usage:

```env
ACCOUNT_DATA_FILE=data/gateway.sqlite3
QUOTA_DATA_FILE=data/gateway.sqlite3
```

Admin UI login is checked by the backend. On the first `/admin` access, create
the admin username/password in the web panel. The backend stores only an Argon2
hash in `ACCOUNT_DATA_FILE`; no admin panel password is needed in Square Cloud
environment variables.

Then configure the operational security settings:

```env
GATEWAY_API_KEYS=replace-with-a-long-random-emergency-token
ALLOW_ADMIN_MODEL_ACCESS=false
TRUSTED_HOSTS=your-domain.example
TRUST_PROXY_HEADERS=true
CORS_ALLOWED_ORIGINS=https://your-domain.example
AUTH_RATE_LIMIT=10
API_RATE_LIMIT=120
```

To manage the Square Cloud app, open `https://your-domain.example/admin`.
The admin panel uses the hosted same-origin API by default, so you do not need
to type a localhost URL or paste the emergency API token.

To sell upgrades through Mercado Pago Checkout Pro, set the backend token and
public hosted URL. Keep the token only on the server:

```env
MERCADO_PAGO_ACCESS_TOKEN=APP_USR-...
MERCADO_PAGO_WEBHOOK_SECRET=secret-from-mercado-pago-webhooks-panel
MERCADO_PAGO_WEBHOOK_TOLERANCE_SECONDS=600
MERCADO_PAGO_PUBLIC_URL=https://your-domain.example
```

The customer app creates a Mercado Pago checkout preference for the plan and
redirects the user to pay. Mercado Pago calls
`/v1/billing/mercadopago/webhook`; when the payment status is `approved`, the
backend validates `x-signature` when `MERCADO_PAGO_WEBHOOK_SECRET` is set, then
upgrades the account automatically. Register the HTTPS webhook URL in Mercado
Pago's Webhooks panel and store the generated secret as an environment variable;
do not hardcode it or pass it through the browser. Current app plans are:

```text
Pro         R$ 65,00   claude-code-pro
5X          R$ 125,00  claude-code-pro
20X         R$ 280,00  claude-code-pro
30X         R$ 390,00  claude-code-pro
```

The app sets security headers, disables public OpenAPI docs, rate-limits login
and API calls, stores customer passwords with Argon2, and keeps MCP write/command
tools disabled unless explicitly enabled.

## Paid Customer Tokens

For selling API access, use customer-scoped tokens instead of sharing the admin token:

```env
CUSTOMER_ACCOUNTS=sk-live-abc|Cliente|149.90|60000|claude-code-pro|true
CUSTOMER_PROFIT_MARGIN=0.50
USD_TO_BRL=5.50
COST_RESERVE_MULTIPLIER=2.0
QUOTA_DATA_FILE=data/customer_usage.json
ACCOUNT_DATA_FILE=data/accounts.json
```

`CUSTOMER_PROFIT_MARGIN` has a hard safety floor of `0.50`: even if it is configured lower by mistake, paid plans reserve at most 50% of the customer's payment for API cost.

Format:

```text
token|customer_name|monthly_price_brl|daily_token_limit|allowed_public_model|active
```

When a customer token calls `/v1/messages`, the gateway forces the allowed model, clamps output tokens, reserves daily cost, and blocks requests that would exceed the plan.

The Admin still generates gift cards for direct sales through `/v1/admin/gift-cards`.
Customers can also create a free account without a gift card through `/v1/auth/signup`;
free accounts use the economy model with a 1,600-token daily limit. After signup or
redemption, the account receives its own `sk-...` API token and can use the
chat/API with server-side limits.

## Temporary Public Trial

For a controlled public QA window, enable a time-boxed trial instead of
`ALLOW_UNAUTHENTICATED`. New signups without a gift card receive a customer token
and a temporary Max-style account until the configured end time. Existing free
accounts are promoted when they log in or use `/v1/auth/me` during the window.
Paid and gift-card accounts are never downgraded or overwritten.

```env
PUBLIC_TRIAL_ENABLED=true
PUBLIC_TRIAL_END_AT=2026-05-23T18:00:00Z
PUBLIC_TRIAL_PLAN_ID=ultra
PUBLIC_TRIAL_DAILY_LIMIT=1200000
PUBLIC_TRIAL_LABEL=Teste grátis 24h
```

When `PUBLIC_TRIAL_ENABLED=false` or `PUBLIC_TRIAL_END_AT` is in the past, trial
accounts are automatically returned to the normal `Grátis` plan on the next
account/API access.

## OpenAI / Codex Compatibility

The gateway also exposes OpenAI-compatible entry points for tools that accept a custom base URL:

```http
POST /v1/responses
POST /v1/chat/completions
```

For Codex CLI, configure a user-level `~/.codex/config.toml` provider that uses the Responses API:

```toml
model = "claude-code-pro"
model_provider = "claude_gateway"

[model_providers.claude_gateway]
name = "Claude Code Gateway"
base_url = "https://your-subdomain.squareweb.app/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
```

Then set the customer's token:

```bash
export OPENAI_API_KEY="sk-..."
```

For OpenAI-compatible clients that still use Chat Completions, use:

```text
Base URL: https://your-subdomain.squareweb.app/v1
API Key: sk-...
Model: claude-code-pro
```

## Square Cloud

The repository includes `requirements.txt` and `squarecloud.app`. The configured start command is:

```bash
uvicorn claude_gateway.main:app --host 0.0.0.0 --port 80
```

Set these environment variables in Square Cloud:

```env
VPS_MODEL_BASE_URL=https://your-vps.example.com
VPS_MODEL_ID=local-model
VPS_MODEL_API_FORMAT=anthropic
VPS_MODEL_API_KEY=
OPENROUTER_EMERGENCY_FALLBACK=false
OPENROUTER_API_KEY=
OPENAI_API_KEY=sk-proj-...
GATEWAY_API_KEYS=strong-admin-token
MERCADO_PAGO_ACCESS_TOKEN=APP_USR-...
MERCADO_PAGO_WEBHOOK_SECRET=secret-from-mercado-pago-webhooks-panel
MERCADO_PAGO_PUBLIC_URL=https://your-subdomain.squareweb.app
OPENROUTER_SITE_URL=https://your-subdomain.squareweb.app
OPENROUTER_APP_NAME=Claude Code
ENABLE_WEB_SEARCH=true
MAX_COST_RATIO_VS_CLAUDE=0.50
ALLOW_PREMIUM_FALLBACK=false
ALLOW_DIRECT_EXTERNAL_MODELS=false
CUSTOMER_ACCOUNTS=...
```

For authorized deploy without adding Square Cloud tokens to GitHub, connect this
repository through Square Cloud's GitHub integration and select the `main`
branch in the Square Cloud dashboard. GitHub Actions stays as CI only; runtime
secrets remain in Square Cloud environment variables.

See [docs/ANALISE_E_DEPLOY.md](docs/ANALISE_E_DEPLOY.md) and [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md) for the full architecture and launch checklist.

## Compatibility Policy

Claude Code relies heavily on streaming and tool calls. For that reason:

- the gateway defaults to fast mode in code, independent of environment flags;
- customer/API tokens are forced through the fast path for their first 10 daily requests;
- hidden reasoning/think mode is disabled before payloads reach the model backend;
- simple `stream: true` requests are proxied directly to the VPS model;
- `stream: true` requests use the direct VPS path by default so terminal output starts faster;
- set `ENABLE_STREAM_AGENT_ORCHESTRATION=true` only if you prefer slower streamed answers with the internal multi-agent pipeline;
- requests with `tools`, `tool_choice`, `tool_use`, or `tool_result` are proxied directly;
- text-only requests use heavier routing only after an explicit stronger reasoning request and a high-risk prompt such as production/auth/payment/security/database work.

This keeps the gateway compatible with Claude Code, Cursor, Windsurf, and OpenAI-compatible clients while still supporting agent debate for normal API calls.

## Useful Endpoints

```http
GET  /health
GET  /v1/models
GET  /v1/budget
POST /v1/messages
POST /v1/responses
POST /v1/chat/completions
GET  /v1/usage
POST /v1/router/debug
POST /v1/agent/run
POST /v1/auth/signup
POST /v1/auth/login
GET  /v1/admin/gift-cards
POST /v1/admin/gift-cards
GET  /v1/admin/accounts
```

For Claude Code terminal usage, keep `MAX_REQUEST_OUTPUT_TOKENS` and
`TOOL_REQUEST_OUTPUT_TOKENS` at `16000` or higher. Large file-creation tool calls
can otherwise be truncated before the `Write` tool receives `file_path` and
`content`.

## Smoke Test

```bash
curl -s http://127.0.0.1:8787/health | jq

curl -s http://127.0.0.1:8787/v1/router/debug \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-code-pro","max_tokens":256,"messages":[{"role":"user","content":"Crie um dashboard React bonito"}]}' | jq
```
