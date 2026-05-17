from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from .config import Settings
from .customers import CustomerPlan, parse_customer_accounts


@dataclass(frozen=True, slots=True)
class AuthContext:
    token: str
    kind: str
    customer: CustomerPlan | None = None

    @property
    def is_customer(self) -> bool:
        return self.customer is not None


def extract_bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return request.headers.get("x-api-key") or request.headers.get("anthropic-auth-token")


def authenticate_request(request: Request, settings: Settings) -> AuthContext:
    if settings.allow_unauthenticated:
        return AuthContext(token="", kind="admin")

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
            return AuthContext(token=token, kind="admin")

    try:
        customer_accounts = parse_customer_accounts(settings)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    for expected, customer in customer_accounts.items():
        if hmac.compare_digest(token, expected):
            if not customer.active:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Customer account is inactive.",
                )
            return AuthContext(token=token, kind="customer", customer=customer)

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid gateway token.")


def require_gateway_auth(request: Request, settings: Settings) -> AuthContext:
    return authenticate_request(request, settings)
