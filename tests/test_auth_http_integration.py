"""Integration tests for the full HTTP auth flow."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from krewcli.auth.middleware import JWTAuthMiddleware
from krewcli.auth.routes import auth_routes
from krewcli.auth.service import AuthService

FAKE_SECRET = "integration-http-secret-key-minimum-32-chars!"


async def _dummy_protected(request: Request) -> JSONResponse:
    user = request.state.user
    return JSONResponse({"user_id": user.id})


def _build_full_app() -> Starlette:
    routes = [
        *auth_routes,
        Route("/api/data", _dummy_protected, methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    app.state.auth_service = AuthService(jwt_secret=FAKE_SECRET)
    app.add_middleware(JWTAuthMiddleware)
    return app


@pytest.fixture
async def client():
    transport = ASGITransport(app=_build_full_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
class TestFullHTTPAuthFlow:
    async def test_register_login_access_protected(self, client):
        # Register
        resp = await client.post("/auth/register", json={"email": "flow@b.com", "password": "longpassword"})
        assert resp.status_code == 201
        user_data = resp.json()
        assert user_data["email"] == "flow@b.com"

        # Login
        resp = await client.post("/auth/login", json={"email": "flow@b.com", "password": "longpassword"})
        assert resp.status_code == 200
        token = resp.json()["access_token"]

        # Access /auth/me
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["email"] == "flow@b.com"

        # Access protected resource
        resp = await client.get("/api/data", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert "user_id" in resp.json()

    async def test_protected_rejects_no_token(self, client):
        resp = await client.get("/api/data")
        assert resp.status_code == 401

    async def test_protected_rejects_expired_token(self, client):
        from krewcli.auth.tokens import create_access_token

        token = create_access_token(
            user_id="user_fake",
            secret=FAKE_SECRET,
            expiry_minutes=-1,
        )
        resp = await client.get("/api/data", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    async def test_login_wrong_password(self, client):
        await client.post("/auth/register", json={"email": "wrong@b.com", "password": "longpassword"})
        resp = await client.post("/auth/login", json={"email": "wrong@b.com", "password": "badpassword"})
        assert resp.status_code == 401
