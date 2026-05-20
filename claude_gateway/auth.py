from __future__ import annotations

import hmac
import ipaddress
import re
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from .accounts import AccountStore
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

    if _is_trusted_admin_request(request, settings):
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

    admin_session = AccountStore(settings).admin_session_for_token(token)
    if admin_session:
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

    customer = AccountStore(settings).customer_plan_for_token(token)
    if customer:
        if not customer.active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Customer account is inactive.",
            )
        return AuthContext(token=token, kind="customer", customer=customer)

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid gateway token.")


def require_gateway_auth(request: Request, settings: Settings) -> AuthContext:
    return authenticate_request(request, settings)


def _is_trusted_admin_request(request: Request, settings: Settings) -> bool:
    if not request.url.path.startswith("/v1/admin/"):
        return False
    if not settings.admin_trusted_ips:
        return False
    client_ip = _client_ip(request, settings)
    if not client_ip:
        return False
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for allowed in settings.admin_trusted_ips:
        try:
            if ip in ipaddress.ip_network(allowed, strict=False):
                return True
        except ValueError:
            continue
    return False


def _client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:
        for header in ("cf-connecting-ip", "true-client-ip", "x-real-ip", "x-client-ip"):
            value = request.headers.get(header, "").strip()
            if value:
                return _clean_ip(value)

        forwarded = request.headers.get("forwarded", "")
        match = re.search(r"for=(?:\"?)(\[?[A-Fa-f0-9:.]+\]?)(?:\"?)", forwarded)
        if match:
            return _clean_ip(match.group(1))

        forwarded_for = request.headers.get("x-forwarded-for", "")
        first_ip = forwarded_for.split(",", 1)[0].strip()
        if first_ip:
            return _clean_ip(first_ip)
    return _clean_ip(request.client.host if request.client else "")


def _clean_ip(value: str) -> str:
    raw = value.strip().strip("[]")
    if raw.count(":") == 1 and "." in raw:
        raw = raw.rsplit(":", 1)[0]
    return raw


def client_ip_for_debug(request: Request, settings: Settings) -> dict[str, object]:
    detected = _client_ip(request, settings)
    trusted = False
    if detected:
        try:
            ip = ipaddress.ip_address(detected)
            trusted = any(
                ip in ipaddress.ip_network(allowed, strict=False)
                for allowed in settings.admin_trusted_ips
            )
        except ValueError:
            trusted = False
    return {
        "detected_ip": detected,
        "trusted": trusted,
        "configured_trusted_ips": list(settings.admin_trusted_ips),
        "trust_proxy_headers": settings.trust_proxy_headers,
        "headers_seen": {
            name: request.headers.get(name)
            for name in (
                "cf-connecting-ip",
                "true-client-ip",
                "x-real-ip",
                "x-client-ip",
                "x-forwarded-for",
                "forwarded",
            )
            if request.headers.get(name)
        },
    }
