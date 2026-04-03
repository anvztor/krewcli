"""JWT Bearer-token authentication middleware for Starlette."""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from krewcli.auth.dependencies import get_auth_service
from krewcli.auth.service import AuthError

logger = logging.getLogger(__name__)

PUBLIC_EXACT: frozenset[str] = frozenset({
    "/auth/register", "/auth/login",
    "/login", "/register",
})
PUBLIC_PREFIXES: tuple[str, ...] = ("/.well-known/",)


def _is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or any(path.startswith(p) for p in PUBLIC_PREFIXES)


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """Extract and validate Bearer tokens on protected routes."""

    async def dispatch(self, request: Request, call_next):
        if _is_public(request.url.path):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse({"error": "Missing or invalid authorization header"}, status_code=401)

        token = auth_header[7:]
        auth_service = get_auth_service(request)

        try:
            user = auth_service.authenticate(token)
        except AuthError as exc:
            logger.debug("Auth failed: %s", exc)
            return JSONResponse({"error": "Invalid or expired token"}, status_code=401)

        request.state.user = user
        return await call_next(request)
