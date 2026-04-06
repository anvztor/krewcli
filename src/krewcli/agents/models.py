from __future__ import annotations

from pydantic import BaseModel, Field


class FactRefResult(BaseModel):
    claim: str
    source_url: str | None = None
    source_title: str | None = None
    confidence: float | None = None


class CodeRefResult(BaseModel):
    repo_url: str
    branch: str
    commit_sha: str
    paths: list[str]


class TaskResult(BaseModel):
    """Structured output from an agent after completing a task."""
    summary: str = Field(description="Brief summary of work done")
    full_output: str = Field(default="", description="Full untruncated agent output")
    files_modified: list[str] = Field(default_factory=list, description="Files changed")
    facts: list[FactRefResult] = Field(default_factory=list, description="Facts discovered")
    code_refs: list[CodeRefResult] = Field(default_factory=list, description="Code references")
    success: bool = Field(default=True, description="Whether the task succeeded")
    blocked_reason: str | None = Field(default=None, description="Reason if blocked")
