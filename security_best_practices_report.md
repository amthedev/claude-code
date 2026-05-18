# Security Best Practices Report

## Executive Summary

The project is materially safer after this pass: customer accounts and quotas now use SQLite, passwords use Argon2 with a legacy rehash path, admin login moved out of public JavaScript, security headers are set centrally, login/API endpoints are rate-limited, and MCP write/command tools are disabled unless explicitly enabled.

No codebase can be made impossible to hack. The remaining production risks are mostly operational: rotate default tokens, set a real admin password hash, run behind HTTPS, restrict hosts, and avoid exposing MCP edit tools publicly.

## Critical / High Findings Fixed

### 1. Frontend-Hardcoded Admin Password

- Severity: Critical
- Location: `frontier/admin.html` and `frontier/admin.js`
- Evidence: the previous admin screen embedded the admin login and password in browser-delivered code.
- Impact: anyone opening DevTools or downloading the JS could learn the admin credentials.
- Fix: removed default admin credentials from the HTML and moved admin login verification to `POST /v1/admin/login`, protected by the admin API token. See `frontier/admin.js:255` and `claude_gateway/main.py:179`.
- Residual risk: production must set `ADMIN_PASSWORD_HASH` and replace `GATEWAY_API_KEYS`.

### 2. Weak / Custom Password Hashing

- Severity: High
- Location: previous `claude_gateway/accounts.py` password helpers
- Evidence: password hashing was implemented directly with `hashlib.pbkdf2_hmac`.
- Impact: custom password hashing is harder to tune and audit over time.
- Fix: added Argon2 hashing through `argon2-cffi` with rehash-on-login support for legacy hashes. See `claude_gateway/security.py:20` and `claude_gateway/accounts.py:147`.

### 3. JSON Files Used as Database for Accounts and Quotas

- Severity: High
- Location: previous `claude_gateway/accounts.py` and `claude_gateway/customers.py`
- Evidence: accounts, gift cards, and quota buckets were persisted as JSON files.
- Impact: JSON state is easier to corrupt, race, or manually tamper with; quota tampering can become a billing/security issue.
- Fix: moved accounts, gift cards, and quota tracking to SQLite with parameterized statements and WAL mode. See `claude_gateway/accounts.py:63`, `claude_gateway/accounts.py:118`, and `claude_gateway/customers.py:136`.

### 4. MCP Write and Command Tools Enabled Too Easily

- Severity: High
- Location: `claude_gateway/mcp_server.py`
- Evidence: MCP tools can write files, apply patches, and run commands.
- Impact: if MCP is exposed without strong auth/network controls, an attacker could modify code or run allowed commands.
- Fix: write/patch tools now require `MCP_ENABLE_WRITE_TOOLS=true`; command execution requires `MCP_ENABLE_COMMANDS=true`. See `claude_gateway/mcp_server.py:88`, `claude_gateway/mcp_server.py:118`, and `claude_gateway/mcp_server.py:144`.

## Medium Findings Fixed

### 5. No Central Security Headers

- Severity: Medium
- Location: `claude_gateway/main.py`
- Evidence: no app-level security header middleware was visible.
- Impact: missing CSP/clickjacking/sniffing controls increases XSS and UI-redress blast radius.
- Fix: added central middleware for CSP, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and `Permissions-Policy`. See `claude_gateway/security.py:104` and `claude_gateway/main.py:51`.

### 6. Public OpenAPI Docs on a Sensitive Gateway

- Severity: Medium
- Location: `claude_gateway/main.py`
- Evidence: FastAPI default docs were enabled.
- Impact: public docs can amplify discovery of admin/customer endpoints.
- Fix: disabled `/docs` and `/redoc` by default. See `claude_gateway/main.py:34`.

### 7. No App-Level Rate Limiting for Auth/API

- Severity: Medium
- Location: `claude_gateway/main.py`
- Evidence: login and API routes had no visible throttling.
- Impact: brute force and token/cost exhaustion attempts are cheaper.
- Fix: added in-memory rate limiting for auth and API routes. See `claude_gateway/security.py:77`, `claude_gateway/main.py:133`, and `claude_gateway/main.py:167`.

### 8. Browser Fallback Stored Customer Passwords Locally

- Severity: Medium
- Location: `frontier/client.js` and `frontier/shared.js`
- Evidence: offline fallback could compare and persist customer passwords in local storage objects.
- Impact: XSS or local browser compromise could expose customer passwords.
- Fix: removed client-side login/signup fallback and removed password from local account creation. See `frontier/client.js:445` and `frontier/shared.js`.

## Remaining Operational Requirements

1. Replace `local-dev-token` with a long random `GATEWAY_API_KEYS` value before deployment.
2. Set `ADMIN_PASSWORD_HASH`, not `ADMIN_PASSWORD`, in production.
3. Set `TRUSTED_HOSTS` to your real domain instead of `*`.
4. Put both the gateway and MCP server behind HTTPS.
5. Keep `MCP_ENABLE_WRITE_TOOLS=false` and `MCP_ENABLE_COMMANDS=false` unless MCP is private and authenticated.
6. Consider replacing the in-memory rate limiter with Redis or another shared limiter if you run multiple workers.
7. Continue reducing frontend `innerHTML` usage over time; most current uses escape values, and CSP helps, but DOM-building APIs are safer.

## Verification

- `python -m pytest -q`: 18 passed.
- `python -m ruff check claude_gateway tests`: all checks passed.
