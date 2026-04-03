from __future__ import annotations

from starlette.requests import Request

from krewcli.auth.service import AuthService


def get_auth_service(request: Request) -> AuthService:
    """Retrieve the AuthService instance from app state."""
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise RuntimeError("AuthService not configured on app.state")
    return service
