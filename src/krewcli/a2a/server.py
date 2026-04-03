from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard
from starlette.applications import Starlette

from krewcli.auth.service import AuthService


def create_a2a_app(
    agent_card: AgentCard,
    executor: AgentExecutor,
    auth_service: AuthService | None = None,
) -> Starlette:
    """Create and build an A2A Starlette application with optional auth.

    Returns a ready-to-serve ``Starlette`` instance.  When *auth_service*
    is provided the auth HTTP routes are mounted and a JWT middleware
    protects non-public endpoints.
    """

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )

    a2a = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    app = a2a.build()

    if auth_service is not None:
        _attach_auth(app, auth_service)

    return app


def _attach_auth(app: Starlette, auth_service: AuthService) -> None:
    """Mount auth routes, login/register pages, and middleware onto the Starlette app."""
    from krewcli.auth.middleware import JWTAuthMiddleware
    from krewcli.auth.pages import page_routes
    from krewcli.auth.routes import auth_routes

    app.state.auth_service = auth_service

    for route in auth_routes + page_routes:
        app.routes.insert(0, route)

    app.add_middleware(JWTAuthMiddleware)
