"""Tests for auth HTTP route handlers."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.routing import Route

from krewcli.auth.routes import auth_routes
from krewcli.auth.middleware import JWTAuthMiddleware
from krewcli.auth.service import AuthService

FAKE_SECRET = "test-route-secret-key-minimum-32-chars-long!"


def _build_app() -> Starlette:
    app = Starlette(routes=[*auth_routes])
    app.state.auth_service = AuthService(jwt_secret=FAKE_SECRET)
    app.add_middleware(JWTAuthMiddleware)
    return app


@pytest.fixture
def app():
    return _build_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
class TestRegister:
    async def test_register_success(self, client):
        resp = await client.post("/auth/register", json={"email": "a@b.com", "password": "longpassword"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "a@b.com"
        assert "hashed_password" not in data
        assert data["is_active"] is True

    async def test_register_duplicate(self, client):
        await client.post("/auth/register", json={"email": "dup@b.com", "password": "longpassword"})
        resp = await client.post("/auth/register", json={"email": "dup@b.com", "password": "longpassword"})
        assert resp.status_code == 409

    async def test_register_invalid_email(self, client):
        resp = await client.post("/auth/register", json={"email": "bad", "password": "longpassword"})
        assert resp.status_code == 422

    async def test_register_short_password(self, client):
        resp = await client.post("/auth/register", json={"email": "a@b.com", "password": "short"})
        assert resp.status_code == 422

    async def test_register_invalid_json(self, client):
        resp = await client.post("/auth/register", content=b"not json", headers={"content-type": "application/json"})
        assert resp.status_code == 400

    async def test_register_missing_fields(self, client):
        resp = await client.post("/auth/register", json={})
        assert resp.status_code == 422


@pytest.mark.asyncio
class TestLogin:
    async def test_login_success(self, client):
        await client.post("/auth/register", json={"email": "u@b.com", "password": "longpassword"})
        resp = await client.post("/auth/login", json={"email": "u@b.com", "password": "longpassword"})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    async def test_login_bad_credentials(self, client):
        resp = await client.post("/auth/login", json={"email": "no@b.com", "password": "longpassword"})
        assert resp.status_code == 401

    async def test_login_missing_fields(self, client):
        resp = await client.post("/auth/login", json={})
        assert resp.status_code == 400

    async def test_login_invalid_json(self, client):
        resp = await client.post("/auth/login", content=b"not json", headers={"content-type": "application/json"})
        assert resp.status_code == 400


@pytest.mark.asyncio
class TestMe:
    async def test_me_with_token(self, client):
        await client.post("/auth/register", json={"email": "me@b.com", "password": "longpassword"})
        login_resp = await client.post("/auth/login", json={"email": "me@b.com", "password": "longpassword"})
        token = login_resp.json()["access_token"]

        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["email"] == "me@b.com"

    async def test_me_without_token(self, client):
        resp = await client.get("/auth/me")
        assert resp.status_code == 401

    async def test_me_with_bad_token(self, client):
        resp = await client.get("/auth/me", headers={"Authorization": "Bearer invalid"})
        assert resp.status_code == 401
