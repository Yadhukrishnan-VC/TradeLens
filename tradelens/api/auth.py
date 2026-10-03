"""Bearer-token auth for the API.

One shared secret (API_TOKEN) sent as `Authorization: Bearer <token>`. Everything is protected except
the health probe and the static dashboard files (the dashboard asks for the token and then sends it with
every API call). This protects a single-user deployment; it is not a multi-user login system.

With API_TOKEN unset, auth is OFF (the local-development default) and a warning is logged.
Never expose an unauthenticated instance beyond localhost: it can approve orders and flip the kill switch.
"""
from __future__ import annotations

import hmac
import logging
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("tradelens.auth")
MIN_TOKEN_LEN = 24
PUBLIC_EXACT = {"/health", "/"}
PUBLIC_PREFIX = "/ui"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or path == PUBLIC_PREFIX or path.startswith(PUBLIC_PREFIX + "/")


def install_auth(app: FastAPI, token: str) -> bool:
    """Returns True if auth was enabled. Must be called BEFORE the CORS middleware is added so that
    CORS stays outermost and browser preflight (OPTIONS) requests are answered without a token."""
    if not token:
        log.warning("API_TOKEN is not set: the API is OPEN. Fine on localhost, never on a network.")
        return False
    if len(token) < MIN_TOKEN_LEN:
        raise RuntimeError(f"API_TOKEN must be at least {MIN_TOKEN_LEN} characters. Generate one with: python -m tradelens token")
    expected = token.encode()

    @app.middleware("http")
    async def _require_token(request: Request, call_next):  # noqa: ANN202
        if request.method == "OPTIONS" or is_public(request.url.path):
            return await call_next(request)
        scheme, _, cred = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(cred.strip().encode(), expected):
            return await call_next(request)
        return JSONResponse({"detail": "missing or invalid API token"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})

    return True
