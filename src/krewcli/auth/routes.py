"""HTTP auth endpoints: register, login, and current-user lookup."""

from __future__ import annotations

import logging

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from krewcli.auth.dependencies import get_auth_service
from krewcli.auth.models import UserCreate
from krewcli.auth.service import AuthError

logger = logging.getLogger(__name__)


async def handle_register(request: Request) -> JSONResponse:
    """POST /auth/register — create a new user account."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    try:
        user_input = UserCreate(**body)
    except ValidationError as exc:
        errors = [
            {"field": e.get("loc", ()), "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        return JSONResponse({"error": "Validation failed", "details": errors}, status_code=422)

    auth_service = get_auth_service(request)

    try:
        user = auth_service.register(user_input)
    except AuthError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    return JSONResponse(user.to_safe_dict(), status_code=201)


async def handle_login(request: Request) -> JSONResponse:
    """POST /auth/login — authenticate and return a JWT access token."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    email = body.get("email", "")
    password = body.get("password", "")

    if not email or not password:
        return JSONResponse({"error": "email and password are required"}, status_code=400)

    auth_service = get_auth_service(request)

    try:
        token = auth_service.login(email, password)
    except AuthError:
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    return JSONResponse({"access_token": token, "token_type": "bearer"})


async def handle_me(request: Request) -> JSONResponse:
    """GET /auth/me — return the current authenticated user."""
    user = getattr(request.state, "user", None)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return JSONResponse(user.to_safe_dict())


auth_routes = [
    Route("/auth/register", handle_register, methods=["POST"]),
    Route("/auth/login", handle_login, methods=["POST"]),
    Route("/auth/me", handle_me, methods=["GET"]),
]
