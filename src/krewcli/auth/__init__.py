from __future__ import annotations

from krewcli.auth.dependencies import get_auth_service
from krewcli.auth.middleware import JWTAuthMiddleware
from krewcli.auth.models import User, UserCreate, TokenPayload
from krewcli.auth.pages import page_routes
from krewcli.auth.password import hash_password, verify_password
from krewcli.auth.routes import auth_routes
from krewcli.auth.service import AuthService, AuthError
from krewcli.auth.token_store import save_token, load_token, clear_token
from krewcli.auth.tokens import create_access_token, decode_access_token, TokenError

__all__ = [
    "User",
    "UserCreate",
    "TokenPayload",
    "hash_password",
    "verify_password",
    "create_access_token",
    "decode_access_token",
    "TokenError",
    "AuthService",
    "AuthError",
    "auth_routes",
    "page_routes",
    "JWTAuthMiddleware",
    "get_auth_service",
    "save_token",
    "load_token",
    "clear_token",
]
