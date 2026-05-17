from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from .config import Settings


def extract_bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return request.headers.get("x-api-key") or request.headers.get("anthropic-auth-token")


def require_gateway_auth(request: Request, settings: Settings) -> None:
    if settings.allow_unauthenticated:
        return

    if not settings.gateway_api_keys:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Gateway auth is enabled but GATEWAY_API_KEYS is empty.",
        )

    token = extract_bearer_token(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token.")

    for expected in settings.gateway_api_keys:
        if hmac.compare_digest(token, expected):
            return

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid gateway token.")
