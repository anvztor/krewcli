"""krewcli bridge — vibe-island-style hook collection.

Mirrors the vibe-island-bridge architecture: one canonical hook
protocol, one entrypoint, per-source normalizers. Every supported
agent (claude / codex / gemini / cursor / opencode / droid) lands
here via its own per-agent hook config, gets normalized into the
canonical shape, and is forwarded to krewhub via the existing
`POST /api/v1/tasks/{task_id}/events` endpoint.
"""
