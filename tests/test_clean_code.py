"""Unit tests for llm_planner._clean_code.

LLMs wrap code output in all kinds of ways — fenced blocks with
language tags, fenced blocks without tags, prose preamble, reflexive
`from __future__ import annotations` at the top, trailing "hope that
helps!" lines. The krewhub sandbox rejects all of these as invalid
Python for its allowlist. _clean_code is the last hop before we POST
the code to /bundles/{id}/graph, so it has to be tolerant of every
common wrapper shape.
"""

from __future__ import annotations

from krewcli.workflows.llm_planner import _clean_code


MINIMAL_GRAPH = """\
g = GraphBuilder(state_type=S, deps_type=D, output_type=str)

@g.step
async def root(ctx) -> str:
    return await dispatch_cycle(ctx, node_id="root", task_kind="coder",
                                 instruction="do it", max_iterations=2)

g.add(g.edge_from(g.start_node).to(root), g.edge_from(root).to(g.end_node))
graph = g.build()
"""


class TestRawPassthrough:
    def test_plain_code_is_returned_unchanged(self):
        assert _clean_code(MINIMAL_GRAPH).strip() == MINIMAL_GRAPH.strip()

    def test_empty_input_returns_empty(self):
        assert _clean_code("") == ""

    def test_whitespace_only_returns_empty(self):
        assert _clean_code("   \n\t  \n") == ""


class TestFencedBlockExtraction:
    def test_python_fence_is_unwrapped(self):
        wrapped = f"```python\n{MINIMAL_GRAPH}\n```"
        result = _clean_code(wrapped)
        assert "```" not in result
        assert "g.build()" in result
        assert "GraphBuilder" in result

    def test_unlabeled_fence_is_unwrapped(self):
        wrapped = f"```\n{MINIMAL_GRAPH}\n```"
        result = _clean_code(wrapped)
        assert "```" not in result
        assert "g.build()" in result

    def test_fence_with_prose_preamble_is_unwrapped(self):
        wrapped = (
            "Here's the workflow for your request:\n\n"
            f"```python\n{MINIMAL_GRAPH}\n```\n\n"
            "Let me know if you need adjustments!"
        )
        result = _clean_code(wrapped)
        assert "```" not in result
        assert "Here's the workflow" not in result
        assert "Let me know" not in result
        assert "g.build()" in result

    def test_multiple_fences_prefer_graph_block(self):
        """When the LLM emits multiple fenced blocks (e.g. a commentary
        block plus the actual code), prefer the one containing
        g.build() / graph = assignment."""
        wrapped = (
            "```python\n# Some example\nprint('hello')\n```\n\n"
            "And here's the real thing:\n\n"
            f"```python\n{MINIMAL_GRAPH}\n```\n"
        )
        result = _clean_code(wrapped)
        assert "g.build()" in result
        assert "print('hello')" not in result

    def test_fence_with_extra_language_chars(self):
        """```py``` or ```python3``` variants — the regex accepts any
        alphanumeric language tag."""
        wrapped = f"```py\n{MINIMAL_GRAPH}\n```"
        assert "g.build()" in _clean_code(wrapped)


class TestFutureImportStripping:
    def test_leading_future_import_is_stripped(self):
        code = "from __future__ import annotations\n\n" + MINIMAL_GRAPH
        result = _clean_code(code)
        assert "from __future__" not in result
        assert "g.build()" in result
        # The rest of the graph code must survive intact.
        assert "GraphBuilder" in result
        assert "dispatch_cycle" in result

    def test_future_import_inside_fenced_block_is_stripped(self):
        code = (
            "```python\n"
            "from __future__ import annotations\n\n"
            + MINIMAL_GRAPH
            + "```"
        )
        result = _clean_code(code)
        assert "from __future__" not in result
        assert "g.build()" in result

    def test_multiple_future_imports_all_stripped(self):
        code = (
            "from __future__ import annotations\n"
            "from __future__ import division\n\n"
            + MINIMAL_GRAPH
        )
        result = _clean_code(code)
        assert "from __future__" not in result
        assert "g.build()" in result

    def test_non_future_imports_are_preserved(self):
        """Regular imports still pass through — the sandbox will reject
        them at validation time, but _clean_code shouldn't hide them.
        Hiding them would mask a real bug in the LLM's output."""
        code = "import os\n\n" + MINIMAL_GRAPH
        result = _clean_code(code)
        assert "import os" in result  # sandbox will reject, not us


class TestIdempotency:
    def test_clean_of_clean_is_same(self):
        once = _clean_code(MINIMAL_GRAPH)
        twice = _clean_code(once)
        assert once.strip() == twice.strip()

    def test_clean_of_fenced_is_same_as_clean_of_bare(self):
        bare = _clean_code(MINIMAL_GRAPH)
        fenced = _clean_code(f"```python\n{MINIMAL_GRAPH}\n```")
        assert bare == fenced
