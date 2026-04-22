"""Tests for the sandbox validator module.

Validates pre-execution, environment, and post-execution checks
following Anthropic's managed agents architecture: credentials
never enter the sandbox, working directories are safe, and
agent output doesn't exfiltrate secrets.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from krewcli.daemon.sandbox_validator import (
    SandboxValidator,
    SandboxViolation,
    ValidationResult,
    _merge_results,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def tmp_workdir(tmp_path: Path) -> Path:
    """Create a temporary working directory with a git repo."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')")
    return tmp_path


@pytest.fixture
def validator() -> SandboxValidator:
    return SandboxValidator()


# ── ValidationResult & SandboxViolation ────────────────────────────


class TestValidationResult:
    def test_empty_result_is_valid(self):
        result = ValidationResult()
        assert result.is_valid is True
        assert result.has_critical is False
        assert result.summary() == "All sandbox checks passed."

    def test_empty_tuple_is_valid(self):
        result = ValidationResult(violations=())
        assert result.is_valid is True

    def test_warning_only_is_valid(self):
        result = ValidationResult(violations=(
            SandboxViolation(check="test", message="minor issue", severity="warning"),
        ))
        assert result.is_valid is True
        assert result.has_critical is False

    def test_critical_violation_is_invalid(self):
        result = ValidationResult(violations=(
            SandboxViolation(check="test", message="bad stuff", severity="critical"),
        ))
        assert result.is_valid is False
        assert result.has_critical is True

    def test_error_violation_is_invalid(self):
        result = ValidationResult(violations=(
            SandboxViolation(check="test", message="error stuff", severity="error"),
        ))
        assert result.is_valid is False
        assert result.has_critical is False

    def test_summary_includes_all_violations(self):
        result = ValidationResult(violations=(
            SandboxViolation(check="a", message="msg_a", severity="warning"),
            SandboxViolation(check="b", message="msg_b", severity="critical"),
        ))
        summary = result.summary()
        assert "[WARNING] a: msg_a" in summary
        assert "[CRITICAL] b: msg_b" in summary

    def test_summary_formats_severity_uppercase(self):
        result = ValidationResult(violations=(
            SandboxViolation(check="env_leak", message="leaked", severity="error"),
        ))
        assert "[ERROR]" in result.summary()

    def test_mixed_severities_has_critical_only_for_critical(self):
        result = ValidationResult(violations=(
            SandboxViolation(check="a", message="warn", severity="warning"),
            SandboxViolation(check="b", message="err", severity="error"),
        ))
        assert not result.is_valid
        assert result.has_critical is False

    def test_multiple_critical_violations(self):
        result = ValidationResult(violations=(
            SandboxViolation(check="a", message="crit1", severity="critical"),
            SandboxViolation(check="b", message="crit2", severity="critical"),
        ))
        assert result.has_critical is True
        assert len(result.violations) == 2


class TestSandboxViolation:
    def test_frozen_dataclass(self):
        v = SandboxViolation(check="test", message="msg", severity="warning")
        with pytest.raises(AttributeError):
            v.check = "changed"

    def test_equality(self):
        v1 = SandboxViolation(check="a", message="b", severity="critical")
        v2 = SandboxViolation(check="a", message="b", severity="critical")
        assert v1 == v2

    def test_inequality(self):
        v1 = SandboxViolation(check="a", message="b", severity="critical")
        v2 = SandboxViolation(check="a", message="b", severity="warning")
        assert v1 != v2


class TestMergeResults:
    def test_merge_empty(self):
        result = _merge_results()
        assert result.is_valid

    def test_merge_single(self):
        r1 = ValidationResult(violations=(
            SandboxViolation(check="a", message="m", severity="warning"),
        ))
        merged = _merge_results(r1)
        assert len(merged.violations) == 1

    def test_merge_multiple(self):
        r1 = ValidationResult(violations=(
            SandboxViolation(check="a", message="m1", severity="warning"),
        ))
        r2 = ValidationResult(violations=(
            SandboxViolation(check="b", message="m2", severity="critical"),
        ))
        merged = _merge_results(r1, r2)
        assert len(merged.violations) == 2
        assert not merged.is_valid
        assert merged.has_critical

    def test_merge_preserves_order(self):
        r1 = ValidationResult(violations=(
            SandboxViolation(check="first", message="1", severity="warning"),
        ))
        r2 = ValidationResult(violations=(
            SandboxViolation(check="second", message="2", severity="error"),
        ))
        merged = _merge_results(r1, r2)
        assert merged.violations[0].check == "first"
        assert merged.violations[1].check == "second"

    def test_merge_all_valid_is_valid(self):
        r1 = ValidationResult()
        r2 = ValidationResult()
        merged = _merge_results(r1, r2)
        assert merged.is_valid


# ── Working directory validation ────────────────────────────────────


class TestWorkdirValidation:
    def test_valid_workdir(self, tmp_workdir: Path, validator: SandboxValidator):
        result = validator.validate_workdir(str(tmp_workdir))
        assert result.is_valid

    def test_nonexistent_workdir(self, validator: SandboxValidator):
        result = validator.validate_workdir("/nonexistent/path/abc123")
        assert not result.is_valid
        assert any(v.check == "workdir_exists" for v in result.violations)

    def test_workdir_not_directory(self, tmp_path: Path, validator: SandboxValidator):
        file_path = tmp_path / "not_a_dir.txt"
        file_path.write_text("hello")
        result = validator.validate_workdir(str(file_path))
        assert not result.is_valid
        assert any(v.check == "workdir_is_dir" for v in result.violations)

    def test_workdir_path_traversal(self, validator: SandboxValidator):
        result = validator.validate_workdir("/tmp/../../etc/passwd")
        assert not result.is_valid
        assert any(v.check == "workdir_path_traversal" for v in result.violations)

    def test_path_traversal_blocks_immediately(self, validator: SandboxValidator):
        """Path traversal should return early — no other checks should run."""
        result = validator.validate_workdir("/tmp/../../../etc/shadow")
        checks = {v.check for v in result.violations}
        assert checks == {"workdir_path_traversal"}

    def test_path_traversal_embedded(self, validator: SandboxValidator):
        result = validator.validate_workdir("/safe/path/../../../etc")
        assert not result.is_valid
        assert any(v.check == "workdir_path_traversal" for v in result.violations)

    def test_sensitive_path_etc(self, validator: SandboxValidator):
        result = validator.validate_workdir("/etc")
        assert not result.is_valid
        assert any(v.check == "workdir_sensitive" for v in result.violations)

    def test_sensitive_path_root(self, validator: SandboxValidator):
        result = validator.validate_workdir("/root")
        assert not result.is_valid
        assert any(v.check == "workdir_sensitive" for v in result.violations)

    def test_sensitive_path_subdir(self, validator: SandboxValidator):
        """Subdirectories of sensitive paths are also blocked."""
        result = validator.validate_workdir("/etc/nginx/conf.d")
        assert not result.is_valid
        assert any(v.check == "workdir_sensitive" for v in result.violations)

    def test_sensitive_home_ssh(self, validator: SandboxValidator):
        result = validator.validate_workdir(os.path.expanduser("~/.ssh"))
        assert not result.is_valid
        assert any(v.check == "workdir_sensitive" for v in result.violations)

    def test_sensitive_home_gnupg(self, validator: SandboxValidator):
        result = validator.validate_workdir(os.path.expanduser("~/.gnupg"))
        assert not result.is_valid
        assert any(v.check == "workdir_sensitive" for v in result.violations)

    def test_sensitive_home_aws(self, validator: SandboxValidator):
        result = validator.validate_workdir(os.path.expanduser("~/.aws"))
        assert not result.is_valid
        assert any(v.check == "workdir_sensitive" for v in result.violations)

    def test_sensitive_path_var(self, validator: SandboxValidator):
        result = validator.validate_workdir("/var")
        assert not result.is_valid

    def test_sensitive_path_usr(self, validator: SandboxValidator):
        result = validator.validate_workdir("/usr")
        assert not result.is_valid

    @pytest.mark.parametrize("sensitive", ["/bin", "/sbin", "/boot", "/dev", "/proc", "/sys"])
    def test_all_sensitive_system_paths(self, sensitive: str, validator: SandboxValidator):
        result = validator.validate_workdir(sensitive)
        assert not result.is_valid
        assert any(v.check == "workdir_sensitive" for v in result.violations)

    def test_sensitive_ssh_subdir_blocked(self, validator: SandboxValidator):
        result = validator.validate_workdir(
            os.path.expanduser("~/.ssh/keys"),
        )
        assert not result.is_valid

    def test_symlink_to_sensitive_path_blocked(
        self, tmp_path: Path, validator: SandboxValidator,
    ):
        """A symlink pointing to a sensitive directory must be rejected."""
        link = tmp_path / "sneaky_link"
        link.symlink_to("/etc")
        result = validator.validate_workdir(str(link))
        assert not result.is_valid
        assert any(v.check == "workdir_sensitive" for v in result.violations)

    def test_valid_home_subdir_allowed(self, tmp_workdir: Path, validator: SandboxValidator):
        """Non-sensitive subdirs under home should be allowed."""
        result = validator.validate_workdir(str(tmp_workdir))
        assert result.is_valid


# ── Environment validation ──────────────────────────────────────────


class TestEnvironmentValidation:
    def test_clean_env_is_valid(self, validator: SandboxValidator):
        env = {
            "KREWHUB_TASK_ID": "task-123",
            "KREWHUB_BUNDLE_ID": "bundle-456",
            "PATH": "/usr/bin:/bin",
        }
        result = validator.validate_env(env)
        assert result.is_valid

    def test_empty_env_is_valid(self, validator: SandboxValidator):
        result = validator.validate_env({})
        assert result.is_valid

    # ── Known secret env var names ──

    @pytest.mark.parametrize("secret_name", [
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
    ])
    def test_known_secret_env_name_rejected(
        self, secret_name: str, validator: SandboxValidator,
    ):
        env = {secret_name: "some-value"}
        result = validator.validate_env(env)
        assert not result.is_valid
        assert any(v.check == "env_secret_leak" for v in result.violations)

    def test_secret_env_name_case_insensitive(self, validator: SandboxValidator):
        """Env var names are uppercased before matching."""
        env = {"github_token": "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}
        result = validator.validate_env(env)
        assert not result.is_valid

    # ── Generic keyword patterns in env names ──

    @pytest.mark.parametrize("name", [
        "MY_APP_SECRET",
        "AUTH_TOKEN",
        "DB_PASSWORD",
        "TLS_PRIVATE_KEY",
        "CUSTOM_SECRET_KEY",
    ])
    def test_generic_secret_keyword_in_env_name(
        self, name: str, validator: SandboxValidator,
    ):
        env = {name: "harmless-value"}
        result = validator.validate_env(env)
        assert not result.is_valid
        assert any(v.check == "env_secret_leak" for v in result.violations)

    # ── KREWHUB_ allowlist ──

    @pytest.mark.parametrize("name", [
        "KREWHUB_TASK_ID",
        "KREWHUB_BUNDLE_ID",
        "KREWHUB_RECIPE_ID",
        "KREWHUB_REPO_URL",
        "KREWHUB_BRANCH",
    ])
    def test_krewhub_allowed_vars(self, name: str, validator: SandboxValidator):
        env = {name: "value-123"}
        result = validator.validate_env(env)
        assert result.is_valid

    def test_krewhub_secret_keyword_rejected(self, validator: SandboxValidator):
        """KREWHUB_ prefix must not bypass secret keyword detection."""
        env = {"KREWHUB_SECRET_TOKEN": "some-value"}
        result = validator.validate_env(env)
        assert not result.is_valid
        assert any(v.check == "env_secret_leak" for v in result.violations)

    def test_krewhub_password_rejected(self, validator: SandboxValidator):
        env = {"KREWHUB_DB_PASSWORD": "pass123"}
        result = validator.validate_env(env)
        assert not result.is_valid

    # ── Secret patterns in env values ──

    @pytest.mark.parametrize("value,desc", [
        ("sk-proj-abcdef1234567890", "OpenAI project key"),
        ("sk-abcdef1234567890abcdef", "OpenAI API key"),
        ("ghp_" + "a" * 36, "GitHub PAT"),
        ("gho_" + "b" * 36, "GitHub OAuth token"),
        ("github_pat_" + "c" * 22, "GitHub fine-grained PAT"),
        ("xoxb-123-456-abc", "Slack bot token"),
        ("xoxp-123-456-def", "Slack user token"),
        ("AKIA1234567890ABCDEF", "AWS access key ID"),
        ("glpat-" + "d" * 20, "GitLab PAT"),
        ("sk_live_" + "e" * 24, "Stripe live key"),
        ("sk_test_" + "f" * 24, "Stripe test key"),
    ])
    def test_secret_value_patterns_in_env(
        self, value: str, desc: str, validator: SandboxValidator,
    ):
        env = {"SOME_HARMLESS_VAR": value}
        result = validator.validate_env(env)
        assert not result.is_valid, f"Failed to detect {desc} in env value"
        assert any(v.check == "env_secret_pattern" for v in result.violations)

    def test_private_key_in_env_value(self, validator: SandboxValidator):
        env = {"CONFIG": "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."}
        result = validator.validate_env(env)
        assert not result.is_valid
        assert any(v.check == "env_secret_pattern" for v in result.violations)

    def test_jwt_in_env_value(self, validator: SandboxValidator):
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        )
        env = {"SOME_VAR": jwt}
        result = validator.validate_env(env)
        assert not result.is_valid
        assert any(v.check == "env_secret_pattern" for v in result.violations)

    def test_multiple_secrets_in_env_all_reported(self, validator: SandboxValidator):
        env = {
            "AWS_SECRET_ACCESS_KEY": "key1",
            "GITHUB_TOKEN": "ghp_" + "x" * 36,
            "DB_PASSWORD": "pass",
        }
        result = validator.validate_env(env)
        assert not result.is_valid
        assert len(result.violations) >= 3

    def test_safe_values_not_flagged(self, validator: SandboxValidator):
        env = {
            "HOME": "/home/user",
            "LANG": "en_US.UTF-8",
            "EDITOR": "vim",
            "KREWHUB_TASK_ID": "task-abc",
            "NODE_ENV": "production",
        }
        result = validator.validate_env(env)
        assert result.is_valid


# ── Output validation ───────────────────────────────────────────────


class TestOutputValidation:
    def test_clean_output_is_valid(self, validator: SandboxValidator):
        result = validator.validate_output("Task completed successfully.")
        assert result.is_valid

    def test_empty_output_is_valid(self, validator: SandboxValidator):
        result = validator.validate_output("")
        assert result.is_valid

    def test_none_like_empty_output(self, validator: SandboxValidator):
        """Empty string short-circuits to valid."""
        result = validator.validate_output("")
        assert result.is_valid
        assert len(result.violations) == 0

    # ── Secret pattern detection in output ──

    def test_output_openai_project_key(self, validator: SandboxValidator):
        result = validator.validate_output("key: sk-proj-abcdef1234567890")
        assert not result.is_valid
        assert any(v.check == "output_secret_pattern" for v in result.violations)

    def test_output_openai_api_key(self, validator: SandboxValidator):
        result = validator.validate_output(
            "Using sk-abcdef1234567890abcdef for auth",
        )
        assert not result.is_valid

    def test_output_github_pat(self, validator: SandboxValidator):
        result = validator.validate_output(
            "Token: ghp_" + "a" * 36,
        )
        assert not result.is_valid

    def test_output_github_oauth(self, validator: SandboxValidator):
        result = validator.validate_output(
            "OAuth: gho_" + "b" * 36,
        )
        assert not result.is_valid

    def test_output_github_fine_grained_pat(self, validator: SandboxValidator):
        result = validator.validate_output(
            "PAT: github_pat_" + "c" * 22,
        )
        assert not result.is_valid

    def test_output_slack_bot_token(self, validator: SandboxValidator):
        result = validator.validate_output("token=xoxb-123-456-abcdef")
        assert not result.is_valid

    def test_output_slack_user_token(self, validator: SandboxValidator):
        result = validator.validate_output("token=xoxp-123-456-abcdef")
        assert not result.is_valid

    def test_output_aws_access_key(self, validator: SandboxValidator):
        result = validator.validate_output("AKIA1234567890ABCDEF is the access key")
        assert not result.is_valid

    def test_output_gitlab_pat(self, validator: SandboxValidator):
        result = validator.validate_output("glpat-" + "d" * 20)
        assert not result.is_valid

    def test_output_stripe_live_key(self, validator: SandboxValidator):
        result = validator.validate_output("sk_live_" + "e" * 24)
        assert not result.is_valid

    def test_output_stripe_test_key(self, validator: SandboxValidator):
        result = validator.validate_output("sk_test_" + "f" * 24)
        assert not result.is_valid

    def test_output_private_key_block(self, validator: SandboxValidator):
        result = validator.validate_output(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAK\n-----END RSA PRIVATE KEY-----",
        )
        assert not result.is_valid

    def test_output_generic_private_key(self, validator: SandboxValidator):
        result = validator.validate_output(
            "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----",
        )
        assert not result.is_valid

    def test_output_jwt(self, validator: SandboxValidator):
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        result = validator.validate_output(jwt)
        assert not result.is_valid
        assert any(v.check == "output_secret_pattern" for v in result.violations)

    def test_output_secret_embedded_in_longer_text(self, validator: SandboxValidator):
        """Secrets buried in verbose output should still be detected."""
        output = (
            "Step 1: Cloned the repository\n"
            "Step 2: Configured auth with AKIA1234567890ABCDEF\n"
            "Step 3: Deployed successfully\n"
        )
        result = validator.validate_output(output)
        assert not result.is_valid

    def test_output_multiple_secrets_all_flagged(self, validator: SandboxValidator):
        output = (
            "key1: sk-proj-abcdef1234567890\n"
            "key2: AKIA1234567890ABCDEF\n"
        )
        result = validator.validate_output(output)
        assert not result.is_valid
        assert len(result.violations) >= 2

    def test_output_safe_base64_not_flagged(self, validator: SandboxValidator):
        """Normal base64 strings that don't match secret patterns should pass."""
        result = validator.validate_output(
            "The hash is: dGhpcyBpcyBhIHRlc3Q=",
        )
        assert result.is_valid

    def test_output_safe_code_snippet(self, validator: SandboxValidator):
        result = validator.validate_output(
            "def calculate_total(items):\n"
            "    return sum(item.price for item in items)\n",
        )
        assert result.is_valid

    def test_oversized_output_warns(self, validator: SandboxValidator):
        """Output exceeding _MAX_OUTPUT_SCAN_BYTES gets a warning."""
        from krewcli.daemon.sandbox_validator import _MAX_OUTPUT_SCAN_BYTES
        large_output = "x" * (_MAX_OUTPUT_SCAN_BYTES + 1000)
        result = validator.validate_output(large_output)
        # Should still be valid (warning, not error) when no secrets found
        assert result.is_valid
        assert any(v.check == "output_too_large" for v in result.violations)

    def test_oversized_output_still_detects_secrets_in_prefix(
        self, validator: SandboxValidator,
    ):
        """Secrets in the scanned prefix are still detected."""
        from krewcli.daemon.sandbox_validator import _MAX_OUTPUT_SCAN_BYTES
        # Put a secret at the start, then pad to exceed the limit
        output = "sk-proj-abcdef1234567890" + "x" * _MAX_OUTPUT_SCAN_BYTES
        result = validator.validate_output(output)
        assert not result.is_valid
        assert any(v.check == "output_secret_pattern" for v in result.violations)

    def test_oversized_output_detects_secrets_in_tail(
        self, validator: SandboxValidator,
    ):
        """Secrets in the tail of oversized output are also detected."""
        from krewcli.daemon.sandbox_validator import _MAX_OUTPUT_SCAN_BYTES
        # Pad to exceed the limit, then put a secret at the end
        output = "x" * _MAX_OUTPUT_SCAN_BYTES + "sk-proj-abcdef1234567890"
        result = validator.validate_output(output)
        assert not result.is_valid
        assert any(v.check == "output_secret_pattern" for v in result.violations)


# ── File boundary validation ────────────────────────────────────────


class TestFileValidation:
    def test_modified_files_within_workdir(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        files = [
            str(tmp_workdir / "src" / "main.py"),
            str(tmp_workdir / "src" / "new_file.py"),
        ]
        result = validator.validate_modified_files(files, str(tmp_workdir))
        assert result.is_valid

    def test_modified_file_outside_workdir(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        files = [
            str(tmp_workdir / "src" / "main.py"),
            "/etc/passwd",
        ]
        result = validator.validate_modified_files(files, str(tmp_workdir))
        assert not result.is_valid
        assert any(v.check == "file_outside_sandbox" for v in result.violations)

    def test_symlink_escape(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        link = tmp_workdir / "escape_link"
        link.symlink_to("/etc")
        files = [str(link / "passwd")]
        result = validator.validate_modified_files(files, str(tmp_workdir))
        assert not result.is_valid
        assert any(v.check == "file_outside_sandbox" for v in result.violations)

    def test_empty_file_list_is_valid(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        result = validator.validate_modified_files([], str(tmp_workdir))
        assert result.is_valid

    def test_path_traversal_in_file_path(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        """File paths with ../ that escape sandbox are caught."""
        files = [str(tmp_workdir / "src" / ".." / ".." / "etc" / "passwd")]
        result = validator.validate_modified_files(files, str(tmp_workdir))
        assert not result.is_valid
        assert any(v.check == "file_outside_sandbox" for v in result.violations)

    def test_multiple_files_outside_all_reported(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        files = ["/etc/passwd", "/root/.bashrc", "/var/log/syslog"]
        result = validator.validate_modified_files(files, str(tmp_workdir))
        assert not result.is_valid
        assert len(result.violations) == 3

    def test_mix_of_inside_and_outside(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        files = [
            str(tmp_workdir / "src" / "main.py"),
            "/etc/passwd",
            str(tmp_workdir / "README.md"),
        ]
        result = validator.validate_modified_files(files, str(tmp_workdir))
        assert not result.is_valid
        violations_outside = [
            v for v in result.violations if v.check == "file_outside_sandbox"
        ]
        assert len(violations_outside) == 1

    def test_deeply_nested_inside_workdir(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        deep = tmp_workdir / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        files = [str(deep / "file.txt")]
        result = validator.validate_modified_files(files, str(tmp_workdir))
        assert result.is_valid

    def test_sibling_directory_outside_sandbox(
        self, tmp_path: Path, validator: SandboxValidator,
    ):
        """A sibling directory of the workdir is outside the sandbox."""
        workdir = tmp_path / "project"
        sibling = tmp_path / "other_project"
        workdir.mkdir()
        sibling.mkdir()
        files = [str(sibling / "file.py")]
        result = validator.validate_modified_files(files, str(workdir))
        assert not result.is_valid


# ── Full pipeline validation ────────────────────────────────────────


class TestFullPipeline:
    def test_validate_pre_execution_clean(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        env = {
            "KREWHUB_TASK_ID": "task-123",
            "KREWHUB_BUNDLE_ID": "bundle-456",
        }
        result = validator.validate_pre_execution(
            working_dir=str(tmp_workdir),
            env=env,
        )
        assert result.is_valid

    def test_validate_pre_execution_fails_with_secrets(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        env = {
            "KREWHUB_TASK_ID": "task-123",
            "AWS_SECRET_ACCESS_KEY": "secret123",
        }
        result = validator.validate_pre_execution(
            working_dir=str(tmp_workdir),
            env=env,
        )
        assert not result.is_valid

    def test_pre_execution_bad_workdir_and_bad_env(
        self, validator: SandboxValidator,
    ):
        """Both workdir and env failures are reported together."""
        result = validator.validate_pre_execution(
            working_dir="/nonexistent/path/xyz",
            env={"GITHUB_TOKEN": "ghp_" + "x" * 36},
        )
        assert not result.is_valid
        checks = {v.check for v in result.violations}
        assert "workdir_exists" in checks
        assert "env_secret_leak" in checks

    def test_validate_post_execution_clean(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        result = validator.validate_post_execution(
            output="Task completed successfully.",
            files_modified=[str(tmp_workdir / "src" / "main.py")],
            working_dir=str(tmp_workdir),
        )
        assert result.is_valid

    def test_validate_post_execution_secret_in_output(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        result = validator.validate_post_execution(
            output="Found key: sk-proj-1234567890abcdef",
            files_modified=[],
            working_dir=str(tmp_workdir),
        )
        assert not result.is_valid

    def test_post_execution_file_escape_and_secret_output(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        """Both output secrets and file escapes are reported together."""
        result = validator.validate_post_execution(
            output="Here: ghp_" + "a" * 36,
            files_modified=["/etc/shadow"],
            working_dir=str(tmp_workdir),
        )
        assert not result.is_valid
        checks = {v.check for v in result.violations}
        assert "output_secret_pattern" in checks
        assert "file_outside_sandbox" in checks

    def test_post_execution_clean_output_with_file_escape(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        result = validator.validate_post_execution(
            output="All good.",
            files_modified=["/var/log/attack.log"],
            working_dir=str(tmp_workdir),
        )
        assert not result.is_valid
        assert any(v.check == "file_outside_sandbox" for v in result.violations)

    def test_post_execution_empty_output_and_files(
        self, tmp_workdir: Path, validator: SandboxValidator,
    ):
        result = validator.validate_post_execution(
            output="",
            files_modified=[],
            working_dir=str(tmp_workdir),
        )
        assert result.is_valid


# ── Statelessness ───────────────────────────────────────────────────


class TestStatelessness:
    """Validator is stateless — repeated calls with same input give same result."""

    def test_repeated_calls_same_result(self, validator: SandboxValidator):
        env = {"KREWHUB_TASK_ID": "t1", "PATH": "/usr/bin"}
        r1 = validator.validate_env(env)
        r2 = validator.validate_env(env)
        assert r1.is_valid == r2.is_valid
        assert len(r1.violations) == len(r2.violations)

    def test_failure_does_not_taint_next_call(self, validator: SandboxValidator):
        bad = validator.validate_env({"GITHUB_TOKEN": "tok"})
        assert not bad.is_valid
        good = validator.validate_env({"PATH": "/usr/bin"})
        assert good.is_valid

    def test_different_validators_same_result(self):
        v1 = SandboxValidator()
        v2 = SandboxValidator()
        env = {"AWS_SECRET_ACCESS_KEY": "key"}
        assert v1.validate_env(env).is_valid == v2.validate_env(env).is_valid
