# Automacoes seguras GitHub + Square Cloud

Este projeto deve operar como uma API normal e auditavel: tokens oficiais,
deploy autorizado, webhooks registrados e nenhuma execucao remota escondida.

## Variaveis obrigatorias

```env
BASE_URL=https://your-subdomain.squareweb.app
API_TOKEN=use-a-customer-token-or-admin-emergency-token
GATEWAY_API_KEYS=long-random-emergency-admin-token
ALLOW_ADMIN_MODEL_ACCESS=false
TRUSTED_HOSTS=your-subdomain.squareweb.app
CORS_ALLOWED_ORIGINS=https://your-subdomain.squareweb.app
MERCADO_PAGO_ACCESS_TOKEN=APP_USR-...
MERCADO_PAGO_WEBHOOK_SECRET=secret-from-webhooks-panel
MERCADO_PAGO_PUBLIC_URL=https://your-subdomain.squareweb.app
```

`BASE_URL` e `API_TOKEN` sao convencoes para clientes e automacoes externas.
No backend, o token e validado por `GATEWAY_API_KEYS`, sessoes admin ou tokens
de cliente gerados pelo painel.

## Deploy autorizado

1. Conecte o repositorio pelo GitHub App/integracao oficial da Square Cloud.
2. Selecione a branch `main` no painel da Square Cloud.
3. Deixe o GitHub Actions apenas para CI em `.github/workflows/ci.yml`.
4. Configure os secrets operacionais dentro da Square Cloud, nao no GitHub e nao no codigo.

## Webhook Mercado Pago

Registre a URL HTTPS:

```text
https://your-subdomain.squareweb.app/v1/billing/mercadopago/webhook
```

No painel de Webhooks do Mercado Pago, habilite o evento de pagamentos e copie
a chave secreta gerada para `MERCADO_PAGO_WEBHOOK_SECRET`. Quando esse segredo
esta configurado, o backend valida `x-signature`, `x-request-id`, timestamp e
HMAC-SHA256 antes de consultar a API oficial do Mercado Pago.

## Padrao de automacao permitido

- Use `Authorization: Bearer ...` ou `X-API-Key` para clientes autorizados.
- Chame apenas a URL base oficial do backend.
- Registre dominios confiaveis em `TRUSTED_HOSTS` e `CORS_ALLOWED_ORIGINS`.
- Mantenha logs operacionais com `X-Request-ID`.
- Use GitHub Actions com `permissions: contents: read` para deploy.

## Evitar sempre

- Auto-modificacao obscura.
- Download e execucao silenciosa de binarios.
- Bypass de autenticacao ou rotas internas sem token.
- Tecnicas anti-deteccao.
- Segredos hardcoded no repositorio, frontend ou logs.
