"""Gateway identity helpers — owner resolution and agent ID construction."""

from __future__ import annotations

from krewcli.agents.registry import AGENT_REGISTRY


def _get_owner_label() -> str:
    """Resolve a human-readable owner from the stored JWT."""
    try:
        from krewcli.auth.token_store import load_token
        import jwt as _pyjwt
        token = load_token()
        if not token:
            return "local"
        payload = _pyjwt.decode(token, options={"verify_signature": False})
        return payload.get("username") or payload.get("sub", "local")
    except Exception:
        return "local"


def _make_agent_id(name: str, owner: str) -> str:
    """Stable agent_id: name@owner (not port-dependent)."""
    return f"{name}@{owner}"


def _gateway_agent_metadata(name: str) -> tuple[str, list[str]]:
    """Look up display name and capabilities for a registered agent."""
    entry = AGENT_REGISTRY.get(name, {})
    display_name = entry.get("display_name", name)
    capabilities = entry.get("capabilities", [])
    return display_name, capabilities
