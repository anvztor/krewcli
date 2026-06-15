"""Secret redaction for link-borne content (orch security).

Child Reports and pipe payloads flow over task links into the
orchestrator's LLM prompt. Before any such content is rendered we strip
credential-shaped strings so a worker can't (accidentally or via
injection) exfiltrate a token up the link into the brain's context —
extending the existing ``KREWHUB_SESSION_TOKEN`` isolation to ALL
link-borne data.
"""

from __future__ import annotations

import re

_REDACTED = "‹redacted›"

# Credential-shaped patterns. Conservative thresholds so we don't redact
# benign data (e.g. 40-char git SHAs stay visible; only 64+ hex blobs,
# which are key-shaped, are scrubbed).
_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),          # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),        # GitHub fine-grained PAT
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),                 # OpenAI-style keys
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),        # Slack tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                    # AWS access key id
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"),  # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),     # Bearer <token>
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\b\s*[:=]\s*\S{8,}"),  # k=v secrets
    re.compile(r"\b[A-Fa-f0-9]{64,}\b"),                    # 64+ hex (keys/hashes)
)


def redact_secrets(text: str, extra: tuple[str, ...] = ()) -> str:
    """Return ``text`` with credential-shaped substrings replaced.

    ``extra`` is a list of known-literal secrets (e.g. the live session
    token) that are replaced verbatim before pattern scrubbing — the
    strongest guarantee, since the token is matched exactly regardless of
    shape.
    """
    if not isinstance(text, str) or not text:
        return text
    out = text
    for secret in extra:
        if secret and len(secret) >= 8:
            out = out.replace(secret, _REDACTED)
    for pat in _PATTERNS:
        out = pat.sub(_REDACTED, out)
    return out
