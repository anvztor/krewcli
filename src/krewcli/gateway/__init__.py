"""Gateway package — agent identity, registration, and ERC-8004 setup.

After the Managed Agents rewrite, the gateway package retains only
the identity/registration/worktree utilities used by the daemon.
The old lifecycle, task_handler, and task_executor modules are replaced
by krewcli.daemon.
"""

from krewcli.gateway.identity import (
    _gateway_agent_metadata,
    _get_owner_label,
    _make_agent_id,
)

__all__ = [
    "_gateway_agent_metadata",
    "_get_owner_label",
    "_make_agent_id",
]
