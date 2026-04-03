"""Tests for the HTML login and register pages."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from krewcli.auth.pages import page_routes


@pytest.fixture()
def client():
    from starlette.applications import Starlette
    from starlette.routing import Route

    app = Starlette(routes=list(page_routes))
    return TestClient(app)


class TestLoginPage:
    def test_get_login_returns_html(self, client: TestClient):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_login_page_contains_form(self, client: TestClient):
        resp = client.get("/login")
        body = resp.text
        assert '<form id="login-form">' in body
        assert 'type="email"' in body
        assert 'type="password"' in body
        assert "Sign in" in body

    def test_login_page_links_to_register(self, client: TestClient):
        resp = client.get("/login")
        assert 'href="/register"' in resp.text

    def test_login_page_posts_to_auth_login(self, client: TestClient):
        resp = client.get("/login")
        assert "'/auth/login'" in resp.text or '"/auth/login"' in resp.text


class TestRegisterPage:
    def test_get_register_returns_html(self, client: TestClient):
        resp = client.get("/register")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_register_page_contains_form(self, client: TestClient):
        resp = client.get("/register")
        body = resp.text
        assert '<form id="register-form">' in body
        assert 'type="email"' in body
        assert 'id="password"' in body
        assert 'id="confirm"' in body
        assert "Create account" in body

    def test_register_page_links_to_login(self, client: TestClient):
        resp = client.get("/register")
        assert 'href="/login"' in resp.text

    def test_register_page_posts_to_auth_register(self, client: TestClient):
        resp = client.get("/register")
        assert "'/auth/register'" in resp.text or '"/auth/register"' in resp.text

    def test_register_page_validates_password_match(self, client: TestClient):
        resp = client.get("/register")
        assert "Passwords do not match" in resp.text


class TestMiddlewarePublicPaths:
    def test_login_and_register_are_public(self):
        from krewcli.auth.middleware import _is_public

        assert _is_public("/login")
        assert _is_public("/register")
        assert _is_public("/auth/login")
        assert _is_public("/auth/register")
        assert not _is_public("/auth/me")
        assert not _is_public("/rpc")
