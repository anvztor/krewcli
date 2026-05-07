"""Sandbox validator — security boundary enforcement for task execution.

Validates the execution environment before, during, and after task
execution following Anthropic's managed agents architecture:

  - Credentials never enter the sandbox
  - Working directories are constrained
  - Agent output doesn't exfiltrate secrets
  - Modified files stay within sandbox boundaries

Design principle from https://www.anthropic.com/engineering/managed-agents:
  "Isolate credentials — never store authentication tokens in the
   sandbox where untrusted code executes."

The validator is stateless and can be called at any point in the
harness pipeline. All checks are pure functions over their inputs.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Maximum output length to scan for secrets (256 KB). Outputs exceeding
# this are truncated for pattern matching; the violation itself is flagged.
_MAX_OUTPUT_SCAN_BYTES = 262_144


# ── Data types ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class SandboxViolation:
    """A single sandbox policy violation."""

    check: str
    message: str
    severity: str  # "critical" | "error" | "warning"


@dataclass(frozen=True)
class ValidationResult:
    """Aggregated result from one or more validation checks."""

    violations: tuple[SandboxViolation, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not any(
            v.severity in ("critical", "error") for v in self.violations
        )

    @property
    def has_critical(self) -> bool:
        return any(v.severity == "critical" for v in self.violations)

    def summary(self) -> str:
        if not self.violations:
            return "All sandbox checks passed."
        lines = [
            f"[{v.severity.upper()}] {v.check}: {v.message}"
            for v in self.violations
        ]
        return "\n".join(lines)


def _merge_results(*results: ValidationResult) -> ValidationResult:
    """Combine multiple validation results into one."""
    all_violations: list[SandboxViolation] = []
    for r in results:
        all_violations.extend(r.violations)
    return ValidationResult(violations=tuple(all_violations))


# ── Secret detection patterns ───────────────────────────────────────

# Env var names that indicate credentials.
_SECRET_ENV_NAMES = frozenset({
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITLAB_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "SLACK_TOKEN",
    "SLACK_BOT_TOKEN",
    "DATABASE_URL",
    "DB_PASSWORD",
    "POSTGRES_PASSWORD",
    "MYSQL_PASSWORD",
    "REDIS_PASSWORD",
    "SECRET_KEY",
    "PRIVATE_KEY",
    "ENCRYPTION_KEY",
    "SIGNING_KEY",
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "DOCKER_PASSWORD",
    "HEROKU_API_KEY",
    "STRIPE_SECRET_KEY",
    "SENDGRID_API_KEY",
    "TWILIO_AUTH_TOKEN",
})

# Common secret patterns shared between env value and output checks.
_COMMON_SECRET_PATTERNS = [
    re.compile(r"sk-proj-[a-zA-Z0-9]{10,}"),       # OpenAI project key
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),             # OpenAI API key
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),             # GitHub PAT
    re.compile(r"gho_[a-zA-Z0-9]{36}"),             # GitHub OAuth
    re.compile(r"github_pat_[a-zA-Z0-9_]{20,}"),    # GitHub fine-grained PAT
    re.compile(r"xoxb-[a-zA-Z0-9\-]+"),             # Slack bot token
    re.compile(r"xoxp-[a-zA-Z0-9\-]+"),             # Slack user token
    re.compile(r"AKIA[0-9A-Z]{16}"),                # AWS access key ID
    re.compile(r"glpat-[a-zA-Z0-9\-_]{20,}"),       # GitLab PAT
    re.compile(r"sk_live_[a-zA-Z0-9]{24,}"),        # Stripe live key
    re.compile(r"sk_test_[a-zA-Z0-9]{24,}"),        # Stripe test key
    re.compile(r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----"),
    re.compile(
        r"eyJ[a-zA-Z0-9_-]{10,}\."
        r"eyJ[a-zA-Z0-9_-]{10,}\."
        r"[a-zA-Z0-9_-]{10,}"
    ),  # JWT
]

# Patterns in env var values that look like secrets.
_SECRET_VALUE_PATTERNS = _COMMON_SECRET_PATTERNS

# Patterns in output text that indicate secret exfiltration.
_OUTPUT_SECRET_PATTERNS = _COMMON_SECRET_PATTERNS

# KREWHUB_ env vars that are safe task metadata (not secrets).
# KREWHUB_SESSION_TOKEN holds the daemon's JWT and IS sensitive — but
# it's required by the krewcli-bridge MCP server to call back to
# krewhub when the brain invokes `delegate(...)`. Without it, the
# bridge can't authenticate and the brain has no way to ask the
# operator anything. We allow it through the validator with a noted
# exception: production sandboxes should isolate this token via the
# bridge's stdio (the brain itself never sees it).
_KREWHUB_ALLOWED = frozenset({
    "KREWHUB_TASK_ID",
    "KREWHUB_BUNDLE_ID",
    "KREWHUB_RECIPE_ID",
    "KREWHUB_REPO_URL",
    "KREWHUB_BRANCH",
    "KREWHUB_URL",
    "KREWHUB_SESSION_TOKEN",
    "KREWHUB_PARENT_TAPE_ID",
})

# Paths that must never be used as working directories.
_SENSITIVE_PATHS = frozenset({
    "/etc",
    "/root",
    "/var",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
})


# ── Validator ───────────────────────────────────────────────────────


class SandboxValidator:
    """Stateless sandbox policy enforcer.

    All methods are pure checks — they inspect inputs and return
    a ValidationResult without side effects.
    """

    def validate_workdir(self, working_dir: str) -> ValidationResult:
        """Validate that the working directory is safe for execution."""
        violations: list[SandboxViolation] = []
        path = Path(working_dir)

        # Path traversal check — must run before existence check
        if ".." in working_dir:
            resolved = str(path.resolve()) if path.exists() else "(unresolvable)"
            violations.append(SandboxViolation(
                check="workdir_path_traversal",
                message=(
                    f"Working directory contains path traversal: "
                    f"{working_dir} resolves to {resolved}"
                ),
                severity="critical",
            ))
            return ValidationResult(violations=tuple(violations))

        # Sensitive path check — compare both raw and resolved paths.
        # On macOS /etc resolves to /private/etc, so check both.
        # Always resolve() to catch symlinks pointing into sensitive dirs.
        home = os.path.expanduser("~")
        sensitive = _SENSITIVE_PATHS | {
            os.path.join(home, ".ssh"),
            os.path.join(home, ".gnupg"),
            os.path.join(home, ".aws"),
        }
        raw_normalized = str(path)
        # resolve() follows symlinks so we catch symlink-based escapes.
        try:
            resolved_str = str(path.resolve())
        except (OSError, ValueError):
            resolved_str = raw_normalized

        def _matches_sensitive(check_path: str, sp: str) -> bool:
            return check_path == sp or check_path.startswith(sp + "/")

        is_symlink = raw_normalized != resolved_str

        for sp in sensitive:
            if _matches_sensitive(raw_normalized, sp) or _matches_sensitive(resolved_str, sp):
                violations.append(SandboxViolation(
                    check="workdir_sensitive",
                    message=f"Working directory is in a sensitive path: {raw_normalized}",
                    severity="critical",
                ))
                break

            # When the input is a symlink, also resolve the sensitive
            # path to catch platform-specific symlinks (macOS /etc →
            # /private/etc). Only do this for symlink inputs to avoid
            # false positives on legitimate temp dirs under /var.
            if is_symlink:
                try:
                    sp_resolved = str(Path(sp).resolve())
                except (OSError, ValueError):
                    continue
                if sp_resolved != sp and _matches_sensitive(resolved_str, sp_resolved):
                    violations.append(SandboxViolation(
                        check="workdir_sensitive",
                        message=f"Working directory is in a sensitive path: {raw_normalized}",
                        severity="critical",
                    ))
                    break

        if violations:
            return ValidationResult(violations=tuple(violations))

        # Existence check
        if not path.exists():
            violations.append(SandboxViolation(
                check="workdir_exists",
                message=f"Working directory does not exist: {working_dir}",
                severity="critical",
            ))
            return ValidationResult(violations=tuple(violations))

        # Must be a directory
        if not path.is_dir():
            violations.append(SandboxViolation(
                check="workdir_is_dir",
                message=f"Working directory is not a directory: {working_dir}",
                severity="critical",
            ))
            return ValidationResult(violations=tuple(violations))

        return ValidationResult(violations=tuple(violations))

    def validate_env(self, env: dict[str, str]) -> ValidationResult:
        """Validate that the subprocess environment doesn't leak secrets."""
        violations: list[SandboxViolation] = []

        for name, value in env.items():
            # Check env var name against known secret names
            name_upper = name.upper()
            if name_upper in _SECRET_ENV_NAMES:
                violations.append(SandboxViolation(
                    check="env_secret_leak",
                    message=(
                        f"Secret env var '{name}' must not enter the sandbox. "
                        f"Store credentials outside the execution environment."
                    ),
                    severity="critical",
                ))
                continue

            # Check env var name for generic secret patterns
            if any(
                kw in name_upper
                for kw in ("_SECRET", "_TOKEN", "_PASSWORD", "_PRIVATE_KEY")
            ):
                # Allow only specific KREWHUB_ vars that are task metadata
                if name_upper not in _KREWHUB_ALLOWED:
                    violations.append(SandboxViolation(
                        check="env_secret_leak",
                        message=(
                            f"Env var '{name}' looks like a secret "
                            f"(name contains sensitive keyword)."
                        ),
                        severity="critical",
                    ))
                    continue

            # Check value against known secret patterns. KREWHUB_-allowed
            # vars whose VALUE is itself credential material (the daemon's
            # JWT for KREWHUB_SESSION_TOKEN) are exempt from pattern
            # checks — they're required by the krewcli-bridge MCP server
            # to authenticate `delegate(...)` callbacks to krewhub.
            if name_upper in _KREWHUB_ALLOWED:
                continue
            for pattern in _SECRET_VALUE_PATTERNS:
                if pattern.search(value):
                    violations.append(SandboxViolation(
                        check="env_secret_pattern",
                        message=(
                            f"Env var '{name}' value matches a known "
                            f"secret pattern ({pattern.pattern[:30]}...)."
                        ),
                        severity="critical",
                    ))
                    break

        return ValidationResult(violations=tuple(violations))

    def validate_output(self, output: str) -> ValidationResult:
        """Validate that agent output doesn't contain secrets."""
        if not output:
            return ValidationResult()

        violations: list[SandboxViolation] = []

        # Cap scan length to avoid ReDoS on very large outputs.
        # Scan both head and tail so secrets can't hide past the boundary.
        if len(output) > _MAX_OUTPUT_SCAN_BYTES:
            half = _MAX_OUTPUT_SCAN_BYTES // 2
            scan_text = output[:half] + output[-half:]
            violations.append(SandboxViolation(
                check="output_too_large",
                message=(
                    f"Output exceeds {_MAX_OUTPUT_SCAN_BYTES} bytes "
                    f"({len(output)} bytes). Head and tail were scanned."
                ),
                severity="warning",
            ))
        else:
            scan_text = output

        for pattern in _OUTPUT_SECRET_PATTERNS:
            match = pattern.search(scan_text)
            if match:
                violations.append(SandboxViolation(
                    check="output_secret_pattern",
                    message=(
                        f"Output contains a potential secret "
                        f"(pattern: {pattern.pattern[:30]}..., "
                        f"offset: {match.start()})."
                    ),
                    severity="critical",
                ))

        return ValidationResult(violations=tuple(violations))

    def validate_modified_files(
        self,
        files: list[str],
        working_dir: str,
    ) -> ValidationResult:
        """Validate that modified files are within the sandbox boundary."""
        if not files:
            return ValidationResult()

        violations: list[SandboxViolation] = []
        sandbox_root = Path(working_dir).resolve()

        for file_path in files:
            try:
                resolved = Path(file_path).resolve()
            except (OSError, ValueError):
                violations.append(SandboxViolation(
                    check="file_outside_sandbox",
                    message=f"Cannot resolve file path: {file_path}",
                    severity="critical",
                ))
                continue

            # Check the resolved path is under the sandbox root
            try:
                resolved.relative_to(sandbox_root)
            except ValueError:
                violations.append(SandboxViolation(
                    check="file_outside_sandbox",
                    message=(
                        f"File '{file_path}' resolves to '{resolved}' "
                        f"which is outside the sandbox root '{sandbox_root}'."
                    ),
                    severity="critical",
                ))

        return ValidationResult(violations=tuple(violations))

    # ── Pipeline helpers ────────────────────────────────────────────

    def validate_pre_execution(
        self,
        working_dir: str,
        env: dict[str, str],
    ) -> ValidationResult:
        """Run all pre-execution validation checks."""
        return _merge_results(
            self.validate_workdir(working_dir),
            self.validate_env(env),
        )

    def validate_post_execution(
        self,
        output: str,
        files_modified: list[str],
        working_dir: str,
    ) -> ValidationResult:
        """Run all post-execution validation checks."""
        return _merge_results(
            self.validate_output(output),
            self.validate_modified_files(files_modified, working_dir),
        )
