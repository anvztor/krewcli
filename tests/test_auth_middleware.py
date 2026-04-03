"""Tests for JWT auth middleware."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from krewcli.auth.middleware import JWTAuthMiddleware, _is_public
from krewcli.auth.service import AuthService

FAKE_SECRET = "test-middleware-secret-key-minimum-32-chars!"


async def _protected_endpoint(request: Request) -> JSONResponse:
    user = getattr(request.state, "user", None)
    if user is None:
        return JSONResponse({"user": None})
    return JSONResponse({"user": user.to_safe_dict()})


def _build_app() -> Starlette:
    app = Starlette(routes=[
        Route("/protected", _protected_endpoint, methods=["GET"]),
        Route("/auth/login", _protected_endpoint, methods=["POST"]),
        Route("/auth/register", _protected_endpoint, methods=["POST"]),
        Route("/.well-known/agent.json", _protected_endpoint, methods=["GET"]),
    ])
    app.state.auth_service = AuthService(jwt_secret=FAKE_SECRET)
    app.add_middleware(JWTAuthMiddleware)
    return app


@pytest.fixture
async def client():
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestIsPublic:
    def test_login_is_public(self):
        assert _is_public("/auth/login") is True

    def test_register_is_public(self):
        assert _is_public("/auth/register") is True

    def test_well_known_is_public(self):
        assert _is_public("/.well-known/agent.json") is True

    def test_protected_path(self):
        assert _is_public("/protected") is False

    def test_auth_me_is_not_public(self):
        assert _is_public("/auth/me") is False


@pytest.mark.asyncio
class TestMiddleware:
    async def test_protected_route_no_token(self, client):
        resp = await client.get("/protected")
        assert resp.status_code == 401
        assert "Missing" in resp.json()["error"]

    async def test_protected_route_with_valid_token(self, client):
        service = _build_app().state.auth_service
        from krewcli.auth.models import UserCreate
        user = service.register(UserCreate(email="mw@b.com", password="longpassword"))
        token = service.login("mw@b.com", "longpassword")

        # Need a fresh app with the same service to have the user
        app = _build_app()
        app.state.auth_service.register(UserCreate(email="mw@b.com", password="longpassword"))
        tok = app.state.auth_service.login("mw@b.com", "longpassword")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/protected", headers={"Authorization": f"Bearer {tok}"})
        assert resp.status_code == 200
        assert resp.json()["user"]["email"] == "mw@b.com"

    async def test_protected_route_with_bad_token(self, client):
        resp = await client.get("/protected", headers={"Authorization": "Bearer badtoken"})
        assert resp.status_code == 401

    async def test_public_login_no_token_needed(self, client):
        resp = await client.post("/auth/login")
        assert resp.status_code != 401 or "Missing" not in resp.json().get("error", "")

    async def test_public_register_no_token_needed(self, client):
        resp = await client.post("/auth/register")
        assert resp.status_code != 401 or "Missing" not in resp.json().get("error", "")

    async def test_well_known_no_token_needed(self, client):
        resp = await client.get("/.well-known/agent.json")
        assert resp.status_code != 401 or "Missing" not in resp.json().get("error", "")
