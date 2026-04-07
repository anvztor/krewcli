"""Canonical hook event vocabulary + cross-agent tool name map.

Single source of truth for the protocol every agent's normalizer
must produce. Lifted verbatim from vibe-island's bridge binary so
our consumers can render hook events from any agent identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# Canonical hook event names. Locked. Every per-source normalizer
# MUST emit one of these strings as `hook_event_name`.
SESSION_START: Final = "SessionStart"
SESSION_END: Final = "SessionEnd"
STOP: Final = "Stop"
STOP_FAILURE: Final = "StopFailure"
SUBAGENT_STOP: Final = "SubagentStop"
BEFORE_AGENT: Final = "BeforeAgent"
USER_PROMPT_SUBMIT: Final = "UserPromptSubmit"
PRE_TOOL_USE: Final = "PreToolUse"
POST_TOOL_USE: Final = "PostToolUse"
POST_TOOL_USE_FAILURE: Final = "PostToolUseFailure"
PERMISSION_REQUEST: Final = "PermissionRequest"
NOTIFICATION: Final = "Notification"

CANONICAL_EVENTS: frozenset[str] = frozenset({
    SESSION_START, SESSION_END, STOP, STOP_FAILURE, SUBAGENT_STOP,
    BEFORE_AGENT, USER_PROMPT_SUBMIT, PRE_TOOL_USE, POST_TOOL_USE,
    POST_TOOL_USE_FAILURE, PERMISSION_REQUEST, NOTIFICATION,
})

# Map each canonical hook event to a krewhub `EventType` enum value.
# These are the only event types that can land in the events table
# from a hook source.
EVENT_TO_KREWHUB_TYPE: dict[str, str] = {
    SESSION_START: "session_start",
    SESSION_END: "session_end",
    STOP: "session_end",
    STOP_FAILURE: "session_end",
    SUBAGENT_STOP: "session_end",
    BEFORE_AGENT: "session_start",
    USER_PROMPT_SUBMIT: "prompt",
    PRE_TOOL_USE: "tool_use",
    POST_TOOL_USE: "tool_use",
    POST_TOOL_USE_FAILURE: "tool_use",
    PERMISSION_REQUEST: "tool_use",
    NOTIFICATION: "agent_reply",
}

# Cross-agent tool name normalization. Different agents call the same
# operation different things; we resolve everything to Claude's set
# (verbatim from vibe-island's bridge-binary-all.txt, extended with
# codex rollout tool names from a real rollout-*.jsonl).
TOOL_NAME_MAP: dict[str, str] = {
    # cursor / vscode-style
    "run_in_terminal": "Bash",
    "create_file": "Write",
    "search_replace": "Edit",
    "read_file": "Read",
    "grep_code": "Grep",
    "search_file": "Glob",
    "search_web": "WebSearch",
    "fetch_content": "WebFetch",
    # opencode lowercase
    "bash": "Bash",
    "read": "Read",
    "edit": "Edit",
    "write": "Write",
    "grep": "Grep",
    "glob": "Glob",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "task": "Task",
    "todowrite": "TodoWrite",
    # codex rollout tool names
    "exec_command": "Bash",
    "apply_patch": "Edit",
    "shell": "Bash",
    "update_plan": "TodoWrite",
    "view_image": "Read",
    "container.exec": "Bash",
}


def canonicalize_tool_name(raw: str | None) -> str:
    if not raw:
        return ""
    return TOOL_NAME_MAP.get(raw.lower(), raw)


def krewhub_event_type(canonical_event: str) -> str:
    """Translate a canonical hook event to a krewhub EventType value."""
    return EVENT_TO_KREWHUB_TYPE.get(canonical_event, "tool_use")


@dataclass
class CanonicalHookEvent:
    """The shape every normalizer must produce.

    This mirrors vibe-island's `base()` envelope verbatim. The bridge
    forwarder serializes this into a `payload` dict on the krewhub
    event so consumers can read it back without losing structure.
    """

    hook_event_name: str
    source: str
    session_id: str = ""
    cwd: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    prompt: str = ""
    last_assistant_message: str = ""
    env: dict[str, str] = field(default_factory=dict)
    ppid: int | None = None
    tty: str | None = None
    extra: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "hook_event_name": self.hook_event_name,
            "_source": self.source,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "prompt": self.prompt,
            "last_assistant_message": self.last_assistant_message,
            "_env": self.env,
            "_ppid": self.ppid,
            "_tty": self.tty,
            **self.extra,
        }
