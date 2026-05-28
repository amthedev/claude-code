# Claude Code production readiness

Este checklist prepara o projeto para vender acesso real sem depender de configuração manual frágil.

## Deploy

1. Defina variáveis de ambiente:

```env
OPENROUTER_API_KEY=sk-or-v1-...
OPENAI_API_KEY=sk-proj-...
GATEWAY_API_KEYS=um-token-admin-forte
OPENROUTER_SITE_URL=https://SEU-SUBDOMINIO.squareweb.app
OPENROUTER_APP_NAME=Claude Code
ENABLE_WEB_SEARCH=true
WEB_SEARCH_MODEL=gpt-5.5
WEB_SEARCH_CONTEXT_SIZE=low
WEB_SEARCH_FOR_CUSTOMERS=true
WEB_SEARCH_TIMEOUT_SECONDS=8
MAX_COST_RATIO_VS_CLAUDE=0.50
ALLOW_PREMIUM_FALLBACK=false
ALLOW_DIRECT_EXTERNAL_MODELS=false
ACCOUNT_DATA_FILE=data/gateway.sqlite3
QUOTA_DATA_FILE=data/gateway.sqlite3
CORS_ALLOWED_ORIGINS=https://SEU-SUBDOMINIO.squareweb.app
TRUSTED_HOSTS=SEU-SUBDOMINIO.squareweb.app
EXPOSE_OPENAPI=false
```

3. Entre em `/admin`, crie a senha admin e confira o checklist operacional no painel Gateway.
4. Ative Mercado Pago antes de vender upgrades pagos.

## Critérios de pronto

- `/health` retorna apenas `{"status":"ok"}` publicamente.
- `/v1/admin/health` mostra OpenRouter, pesquisa web, CORS, OpenAPI privado e banco persistente como prontos.
- O chat responde com pesquisa web em `Auto` apenas para pedidos atuais, e com fontes quando usar dados da internet.
- O toggle `Web` força pesquisa; `Off` impede pesquisa.
- Gift cards, contas, compras e uso diário persistem no mesmo SQLite.
- Planos pagos reservam no máximo 50% do valor recebido para custo de API, mesmo se `CUSTOMER_PROFIT_MARGIN` for configurado abaixo disso por engano.
- Admin usa senha/hash no backend e `GATEWAY_API_KEYS` forte.

## Smoke tests

```bash
curl https://SEU-SUBDOMINIO.squareweb.app/health

curl -s https://SEU-SUBDOMINIO.squareweb.app/v1/budget \
  -H "Authorization: Bearer um-token-admin-forte" | jq

curl -s https://SEU-SUBDOMINIO.squareweb.app/v1/router/debug \
  -H "Authorization: Bearer um-token-admin-forte" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-code-pro","gateway_web_search":"auto","max_tokens":128,"messages":[{"role":"user","content":"Qual a versão atual do Node.js hoje?"}]}' | jq

curl -s https://SEU-SUBDOMINIO.squareweb.app/v1/messages \
  -H "Authorization: Bearer um-token-admin-forte" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-code-pro","gateway_web_search":"required","max_tokens":256,"messages":[{"role":"user","content":"Pesquise uma notícia atual de tecnologia e cite fontes."}]}' | jq
```

## Riscos e rollback

- Pesquisa web aumenta custo e latência. Desative com `ENABLE_WEB_SEARCH=false` se houver pico de custo.
- Para falha de OpenAI, o gateway continua respondendo e instrui o modelo a não inventar dados atuais.
- Para problema de marca/copy, o rebrand público fica em `frontier/*`; os aliases `claude-code-*` continuam apenas como compatibilidade técnica.
- Para rollback de deploy, volte ao commit anterior e mantenha o SQLite `data/gateway.sqlite3`.

## Validação local

```bash
python3 -m pytest -q
python3 -m ruff check claude_gateway tests
node --check frontier/shared.js
node --check frontier/client.js
node --check frontier/admin.js
```
