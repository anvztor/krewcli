from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from krewcli.agents.base import AgentDeps, AgentRunResult, CommandResult
from krewcli.agents.models import TaskResult

logger = logging.getLogger(__name__)


@dataclass
class ClaudeStreamAgent:
    """Claude Code CLI wrapper using --output-format stream-json.

    Matches the multica pattern: streams structured JSON from stdout,
    uses --permission-mode bypassPermissions for headless execution,
    and inherits the host environment (including auth from keychain
    or ANTHROPIC_API_KEY).
    """

    name: str = "Claude"

    async def run(self, prompt: str, *, deps: AgentDeps) -> AgentRunResult:
        args = [
            "claude",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "bypassPermissions",
            "-p", prompt,
        ]

        env = {**os.environ}

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=deps.working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError:
            return AgentRunResult(output=TaskResult(
                summary="Claude CLI not found on PATH",
                success=False,
                blocked_reason="Claude CLI not found on PATH",
            ))

        output_text = ""
        is_error = False
        error_text = ""

        try:
            # Stream structured JSON from stdout
            while True:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=600
                )
                if not line:
                    break

                text = line.decode().strip()
                if not text:
                    continue

                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "assistant":
                    # Extract text blocks from assistant messages
                    message = msg.get("message", {})
                    for block in message.get("content", []):
                        if block.get("type") == "text" and block.get("text"):
                            output_text += block["text"]

                elif msg_type == "result":
                    # Final result message
                    result_text = msg.get("result", "")
                    if result_text:
                        output_text = result_text
                    is_error = msg.get("is_error", False)
                    if is_error:
                        error_text = result_text

        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            return AgentRunResult(output=TaskResult(
                summary="Claude CLI timed out after 10 minutes",
                success=False,
                blocked_reason="Claude CLI timed out",
            ))

        await process.wait()

        from krewcli.agents.base import _list_changed_files, _read_git_value
        from krewcli.agents.models import CodeRefResult

        changed_files = await _list_changed_files(deps.working_dir)
        repo_url = deps.repo_url or await _read_git_value(
            ["git", "config", "--get", "remote.origin.url"], deps.working_dir
        )
        commit_sha = await _read_git_value(
            ["git", "rev-parse", "HEAD"], deps.working_dir
        )

        code_refs = []
        if repo_url and commit_sha and changed_files:
            code_refs.append(CodeRefResult(
                repo_url=repo_url,
                branch=deps.branch,
                commit_sha=commit_sha,
                paths=changed_files,
            ))

        success = process.returncode == 0 and not is_error
        summary = output_text if output_text else (
            "Claude completed successfully" if success else
            error_text or "Claude failed"
        )

        return AgentRunResult(output=TaskResult(
            summary=summary,
            full_output=output_text,
            files_modified=changed_files,
            code_refs=code_refs,
            success=success,
            blocked_reason=None if success else (error_text or summary),
        ))


def create_claude_agent() -> ClaudeStreamAgent:
    """Create a Claude Code CLI wrapper using stream-json output."""
    return ClaudeStreamAgent()
