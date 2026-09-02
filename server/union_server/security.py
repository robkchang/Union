"""Browser-side security: sessions, current user, CSRF, rate limiting,
password hashing. Replaces DittoRoo's flask-login / flask-wtf / flask-limiter."""
from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request

_ph = PasswordHasher()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(hashed: str, pw: str) -> bool:
    try:
        return _ph.verify(hashed, pw)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


class LoginRequired(Exception):
    """Raised by page routes; the app turns it into a redirect to /login."""

    def __init__(self, next_url: str):
        self.next_url = next_url


def current_user(request: Request) -> dict | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = request.app.state.db.get_user_by_id(int(uid))
    if not user:
        request.session.clear()
    return user


def require_user_page(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise LoginRequired(str(request.url.path))
    return user


def require_user_json(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, {"error": "login_required"})
    return user


def login(request: Request, user: dict) -> None:
    request.session.clear()
    request.session["uid"] = user["id"]
    request.session["csrf"] = secrets.token_urlsafe(32)


def logout(request: Request) -> None:
    request.session.clear()


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


async def verify_csrf(request: Request) -> None:
    """Dependency for browser POSTs: token in the `X-CSRF-Token` header or a
    `csrf_token` form field must match the session."""
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token")
    if not supplied and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        form = await request.form()
        supplied = form.get("csrf_token")
    if not expected or not supplied or not secrets.compare_digest(expected, str(supplied)):
        raise HTTPException(403, {"error": "csrf", "detail": "CSRF token missing or invalid."})


def flash(request: Request, message: str, category: str = "info") -> None:
    msgs = request.session.get("flash") or []
    msgs.append([category, message])
    request.session["flash"] = msgs


def pop_flashes(request: Request) -> list[list[str]]:
    msgs = request.session.pop("flash", None) or []
    return msgs


class RateLimiter:
    """Sliding-window limiter keyed by (bucket, client)."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)

    def _prune(self, bucket: str, client: str, window: float) -> deque:
        now = time.monotonic()
        q = self._hits[(bucket, client)]
        while q and q[0] < now - window:
            q.popleft()
        return q

    def check(self, bucket: str, client: str, limit: int, window: float) -> None:
        """Record a hit; 429 if the client already has `limit` hits in `window`."""
        q = self._prune(bucket, client, window)
        if len(q) >= limit:
            raise HTTPException(429, {"error": "rate_limited", "detail": f"Too many requests; try again in {int(window)}s."})
        q.append(time.monotonic())

    def peek(self, bucket: str, client: str, limit: int, window: float) -> None:
        """429 if the client is already over the limit, without recording a hit."""
        if len(self._prune(bucket, client, window)) >= limit:
            raise HTTPException(429, {"error": "rate_limited", "detail": f"Too many failed attempts; try again in {int(window)}s."})


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
