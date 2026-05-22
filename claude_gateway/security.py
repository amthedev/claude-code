from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import pbkdf2_hmac
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError
from fastapi import HTTPException, Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware

from .config import Settings


_PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must contain at least 8 characters.")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> tuple[bool, bool]:
    if password_hash.startswith("$argon2"):
        try:
            ok = _PASSWORD_HASHER.verify(password_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False, False
        return bool(ok), _PASSWORD_HASHER.check_needs_rehash(password_hash)

    legacy_ok = _verify_legacy_pbkdf2(password, password_hash)
    return legacy_ok, legacy_ok


def _verify_legacy_pbkdf2(password: str, password_hash: str) -> bool:
    try:
        salt, digest = password_hash.split(":", 1)
    except ValueError:
        return False
    candidate = pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(candidate, digest)


def configured_admin_password_hash(settings: Settings) -> str:
    if settings.admin_password_hash:
        return settings.admin_password_hash
    if settings.admin_password:
        return hash_password(settings.admin_password)
    return ""


def verify_admin_login(values: dict[str, Any], settings: Settings) -> None:
    login = str(values.get("login") or "").strip()
    password = str(values.get("password") or "")
    expected_hash = configured_admin_password_hash(settings)
    if not expected_hash:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin password is not configured.",
        )
    ok, _ = verify_password(password, expected_hash)
    if not hmac.compare_digest(login, settings.admin_username) or not ok:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid admin login.")


@dataclass(slots=True)
class RateLimitBucket:
    hits: deque[float]


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, RateLimitBucket] = defaultdict(lambda: RateLimitBucket(deque()))

    def check(self, key: str, *, limit: int, window_seconds: int) -> None:
        if limit <= 0 or window_seconds <= 0:
            return
        now = time.monotonic()
        bucket = self._buckets[key].hits
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Try again later.",
            )
        bucket.append(now)


def rate_limit_key(request: Request, namespace: str) -> str:
    token = request.headers.get("authorization") or request.headers.get("x-api-key") or ""
    if token:
        return f"{namespace}:token:{token[:96]}"
    host = request.client.host if request.client else "unknown"
    return f"{namespace}:ip:{host}"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        response.headers.setdefault("Content-Security-Policy", _content_security_policy())
        return response


class OperationalLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start = time.perf_counter()
        request_id = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:16]}"
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            status_code = response.status_code if response else 500
            if response:
                response.headers.setdefault("X-Request-ID", request_id)
            logging.getLogger("claude_gateway.requests").info(
                "request_id=%s method=%s path=%s status=%s latency_ms=%s auth=%s",
                request_id,
                request.method,
                request.url.path,
                status_code,
                elapsed_ms,
                _auth_kind(request),
            )


def _auth_kind(request: Request) -> str:
    if request.headers.get("authorization"):
        return "bearer"
    if request.headers.get("x-api-key"):
        return "x-api-key"
    if request.headers.get("anthropic-auth-token"):
        return "anthropic-auth-token"
    return "anonymous"


def _content_security_policy() -> str:
    return os.getenv(
        "CONTENT_SECURITY_POLICY",
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self' https://*.squareweb.app; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'",
    )
