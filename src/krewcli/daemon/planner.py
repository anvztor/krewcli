"""Planner — generate pydantic-graph code for empty bundles.

When a user creates a bundle from cookrew's prompt UI, it starts
with status=open and 0 tasks. krewhub's GraphRunnerController needs
pydantic-graph code (via ``POST /bundles/{id}/graph``) to decompose
the prompt into a dependency DAG of executable tasks.

This module ports the old ``a2a/executors/gateway.py`` planning path
into the daemon architecture: detect empty bundles → run a backend
with the CODEGEN_PROMPT → extract graph code → attach_graph().

The CODEGEN_PROMPT template is the exact production prompt from the
pre-rewrite ``workflows/llm_planner.py``.
"""

from __future__ import annotations

import ast
import logging
import re
from typing import TYPE_CHECKING

from krewcli.backend.protocol import Backend

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)

# Max characters of generated code to log on success/failure.
_CODE_LOG_PREVIEW = 600

# Maximum output from the backend before truncation (128 KB).
_MAX_OUTPUT_CHARS = 131_072

# AST constructs that must not appear in graph code. The krewhub sandbox
# enforces its own allowlist, but catching these early avoids a round-trip.
_FORBIDDEN_AST_NODES = frozenset({
    "Import",
    "ImportFrom",
    "ClassDef",
    "Try",
    "Raise",
    "With",
    "Lambda",
    "Global",
    "Nonlocal",
})

# Allowlist of callable names permitted in graph code. Anything not on
# this list is rejected. This is safer than a denylist because it blocks
# indirect eval/exec tricks (variable indirection, builtins() access).
_ALLOWED_CALLABLES = frozenset({
    # pydantic-graph API
    "GraphBuilder",
    "dispatch_cycle",
    # Python builtins that are safe
    "len",
    "range",
    "isinstance",
    "str",
    "int",
    "float",
    "list",
    "dict",
    "tuple",
    "set",
    "bool",
    "print",
    "min",
    "max",
    "sorted",
    "enumerate",
    "zip",
    "map",
    "filter",
    "any",
    "all",
    "abs",
    "round",
    "sum",
    "hash",
    "id",
    "repr",
    "format",
})

# Matches a fenced code block with optional language tag.
_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_+-]*)\s*\n(.*?)```", re.DOTALL)


# ── Codegen prompt template ──────────────────────────────────────

CODEGEN_PROMPT = """\
You are a workflow planner. Given a user request, generate Python code using \
pydantic-graph's beta GraphBuilder API to decompose the request into executable steps.

The code you produce will be executed by krewhub's sandbox. The sandbox \
injects a strict allowlist into the namespace — anything else (imports, \
classes, try/except, lambdas, attribute access starting with `_`, eval, \
open, getattr, etc.) will be rejected at validation time. Stick to the \
template below verbatim.

## Allowed names in the sandbox namespace
GraphBuilder, StepContext, reduce_list_append, dispatch_cycle,
OrchestratorState, OrchestratorDeps, plus the locals `g`, `graph`, `ctx`,
plus standard literal types (str, int, list, dict, etc.) and basic
builtins (len, range, isinstance, ...).

## Required template

```python
g = GraphBuilder(
    state_type=OrchestratorState,
    deps_type=OrchestratorDeps,
    output_type=str,
)

@g.step
async def step_name(ctx: StepContext[OrchestratorState, OrchestratorDeps, None]) -> str:
    return await dispatch_cycle(
        ctx,
        node_id="step_name",
        task_kind="coder",         # planner | coder | reviewer | tester
        instruction="What this step should accomplish",
        max_iterations=2,
    )

g.add(
    g.edge_from(g.start_node).to(step_a),
    g.edge_from(step_a).to(step_b),
    g.edge_from(step_b).to(g.end_node),
)

graph = g.build()
```

For parallel branches, fan out and join back via additional g.edge_from(...) calls:

```python
g.add(
    g.edge_from(step_a).to(step_b),
    g.edge_from(step_a).to(step_c),
    g.edge_from(step_b).to(step_d),
    g.edge_from(step_c).to(step_d),
)
```

## Rules
1. ALWAYS define `graph = g.build()` as the last line.
2. Step function names must be valid Python identifiers in snake_case and \
   must match the `node_id=` argument inside the function body exactly.
3. Every step body MUST be a single `return await dispatch_cycle(...)` call \
   with keyword arguments. No other statements inside step bodies.
4. `task_kind` must be one of: planner, coder, reviewer, tester. Pick the \
   one whose role best fits the step.
5. `instruction` is a short human-readable sentence telling the worker \
   agent what this step should accomplish.
6. `max_iterations` must be an integer between 1 and 5.
7. Use 3-7 steps. Don't over-decompose.
8. NEVER use: import, class, try/except, raise, with, lambda, global, \
   eval, exec, open, getattr, setattr, type, super, or any name starting \
   with an underscore. The sandbox will reject the code immediately.
9. Output ONLY the Python code — no markdown fences, no explanations.

## Common patterns
- Feature: scope → implement → write_tests → review
- Bugfix: diagnose → write_failing_test → fix → verify
- Refactor: analyze → plan → implement → update_tests → document

## User Request
{prompt}

## Available Agents
{agents}

Generate the GraphBuilder Python code for this workflow. Output ONLY the Python code.
"""


# ── Code extraction ──────────────────────────────────────────────

def clean_code(raw: str) -> str:
    """Extract graph source from LLM output, tolerating common wrappers.

    LLMs routinely wrap code in markdown fences, prepend prose, or add
    ``from __future__ import annotations``. The krewhub sandbox rejects
    all of those, so this helper strips them.

    Strategy:
        1. If the response has ```-fenced blocks, pick the first one
           that mentions ``g.build()`` or ``graph =``; else first block.
        2. If no fences, treat the whole response as code.
        3. Strip ``from __future__ import ...`` lines.
        4. Trim whitespace.
    """
    if not raw:
        return ""

    fenced_blocks = _FENCE_RE.findall(raw)
    if fenced_blocks:
        preferred = next(
            (b for b in fenced_blocks if "g.build" in b or "graph = " in b or "graph=" in b),
            fenced_blocks[0],
        )
        raw = preferred

    raw = raw.strip()

    raw = re.sub(
        r"^\s*from\s+__future__\s+import\s+[^\n]+\n",
        "",
        raw,
        flags=re.MULTILINE,
    )

    return raw.strip()


def _is_valid_graph_code(code: str) -> bool:
    """Validate that the code looks like pydantic-graph output.

    Performs two checks:
      1. Structural: must contain g.build() or graph assignment.
      2. AST safety: must not contain forbidden constructs (import,
         class, try/except, etc.) that the krewhub sandbox would reject.
    """
    if "g.build()" not in code and "graph = " not in code:
        return False

    try:
        tree = ast.parse(code)
    except SyntaxError:
        logger.debug("planner: generated code has syntax errors")
        return False

    for node in ast.walk(tree):
        node_type = type(node).__name__
        if node_type in _FORBIDDEN_AST_NODES:
            logger.warning(
                "planner: generated code contains forbidden %s node", node_type,
            )
            return False

        # Allowlist-based callable check — only permit known-safe names.
        # This blocks indirect invocations like `f = eval; f("code")`.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in _ALLOWED_CALLABLES:
                logger.warning(
                    "planner: generated code calls non-allowlisted name %s",
                    node.func.id,
                )
                return False

        # Block attribute access starting with underscore (private access)
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            logger.warning(
                "planner: generated code accesses private attribute %s",
                node.attr,
            )
            return False

    return True


# ── Planning execution ───────────────────────────────────────────

async def plan_bundle(
    backend: Backend,
    client: "KrewHubClient",
    bundle_id: str,
    user_prompt: str,
    working_dir: str,
    agents_summary: str = "",
) -> bool:
    """Generate graph code for an empty bundle and attach it via krewhub.

    Returns True if graph was successfully attached, False otherwise.
    """
    import httpx

    codegen_prompt = CODEGEN_PROMPT.format(
        prompt=user_prompt,
        agents=agents_summary or backend.name,
    )

    # Run the backend with the codegen prompt
    session = await backend.execute(codegen_prompt, working_dir)

    output_parts: list[str] = []
    async for msg in session.messages_iter():
        if msg.kind == "agent_reply":
            text = msg.payload.get("text", msg.body)
            output_parts.append(text)

    result = await session.result()
    full_output = "".join(output_parts) or result.full_output or result.summary

    if not full_output.strip():
        logger.warning(
            "planner: %s returned empty output for bundle %s",
            backend.name, bundle_id,
        )
        return False

    # Truncate oversized output to prevent downstream issues
    if len(full_output) > _MAX_OUTPUT_CHARS:
        logger.warning(
            "planner: truncating %s output for bundle %s (%d → %d chars)",
            backend.name, bundle_id, len(full_output), _MAX_OUTPUT_CHARS,
        )
        full_output = full_output[:_MAX_OUTPUT_CHARS]

    # Extract and validate code
    code = clean_code(full_output)
    if not _is_valid_graph_code(code):
        logger.warning(
            "planner: %s output does not look like graph code for bundle %s "
            "(no g.build() or graph = assignment)\n--- preview ---\n%s\n--- end ---",
            backend.name, bundle_id, code[:_CODE_LOG_PREVIEW],
        )
        return False

    # Attach to krewhub
    agent_id = f"{backend.name}@planner"
    try:
        await client.attach_graph(bundle_id, code, created_by=agent_id)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        detail = exc.response.text[:200] if exc.response else ""
        logger.error(
            "planner: krewhub rejected graph code for bundle %s "
            "(HTTP %d): %s\n--- code (%d bytes) ---\n%s\n--- end ---",
            bundle_id, status, detail, len(code), code[:_CODE_LOG_PREVIEW],
        )
        return False
    except Exception:
        logger.exception(
            "planner: attach_graph crashed for bundle %s", bundle_id,
        )
        return False

    logger.info(
        "planner: attached graph to bundle %s via %s (%d bytes)",
        bundle_id, backend.name, len(code),
    )
    return True
