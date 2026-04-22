"""Tests for planner code extraction and AST validation.

Validates that the planner:
  - Extracts code from fenced blocks correctly
  - Rejects code containing forbidden AST constructs
  - Rejects calls to dangerous builtins
  - Rejects private attribute access
  - Accepts valid pydantic-graph code
"""

from __future__ import annotations

from krewcli.daemon.planner import _is_valid_graph_code, clean_code


# ── clean_code ──────────────────────────────────────────────────────


class TestCleanCode:
    def test_strips_markdown_fences(self):
        raw = "```python\ng = 1\ngraph = g\n```"
        assert "```" not in clean_code(raw)

    def test_strips_future_import(self):
        raw = "from __future__ import annotations\ngraph = g.build()"
        assert "__future__" not in clean_code(raw)

    def test_picks_block_with_g_build(self):
        raw = "```\nprint('hello')\n```\n```python\ngraph = g.build()\n```"
        code = clean_code(raw)
        assert "g.build()" in code

    def test_empty_input(self):
        assert clean_code("") == ""

    def test_no_fences_returns_stripped(self):
        raw = "  graph = g.build()  "
        assert clean_code(raw) == "graph = g.build()"


# ── _is_valid_graph_code ────────────────────────────────────────────


class TestIsValidGraphCode:
    def test_valid_graph_code(self):
        code = (
            "g = GraphBuilder(\n"
            "    state_type=OrchestratorState,\n"
            "    deps_type=OrchestratorDeps,\n"
            "    output_type=str,\n"
            ")\n\n"
            "@g.step\n"
            "async def step_a(ctx):\n"
            "    return await dispatch_cycle(ctx, node_id='step_a', "
            "task_kind='coder', instruction='Do work', max_iterations=2)\n\n"
            "g.add(\n"
            "    g.edge_from(g.start_node).to(step_a),\n"
            "    g.edge_from(step_a).to(g.end_node),\n"
            ")\n\n"
            "graph = g.build()\n"
        )
        assert _is_valid_graph_code(code) is True

    def test_rejects_missing_g_build(self):
        code = "x = 1\ny = 2"
        assert _is_valid_graph_code(code) is False

    def test_rejects_import(self):
        code = "import os\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_from_import(self):
        code = "from os import path\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_class_def(self):
        code = "class Hack:\n    pass\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_try_except(self):
        code = "try:\n    x = 1\nexcept:\n    pass\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_raise(self):
        code = "raise Exception('boom')\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_with_statement(self):
        code = "with open('f') as fh:\n    pass\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_lambda(self):
        code = "f = lambda x: x\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_global(self):
        code = "def f():\n    global x\n    pass\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_eval_call(self):
        code = "eval('1+1')\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_exec_call(self):
        code = "exec('x=1')\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_open_call(self):
        code = "open('/etc/passwd')\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_getattr_call(self):
        code = "getattr(obj, 'x')\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_dunder_import_call(self):
        code = "__import__('os')\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_compile_call(self):
        code = "compile('x=1', '<string>', 'exec')\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_delattr_call(self):
        code = "delattr(obj, 'x')\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_vars_call(self):
        """vars() could be used to access builtins indirectly."""
        code = "vars()['eval']('1+1')\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_type_call(self):
        code = "type('X', (), {})\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_allows_safe_builtins(self):
        code = "x = len([])\ny = range(10)\ngraph = g.build()"
        assert _is_valid_graph_code(code) is True

    def test_allows_dispatch_cycle(self):
        code = "dispatch_cycle(ctx, node_id='a', task_kind='coder', instruction='x', max_iterations=1)\ngraph = g.build()"
        assert _is_valid_graph_code(code) is True

    def test_allows_graphbuilder(self):
        code = "g = GraphBuilder(state_type=S, deps_type=D, output_type=str)\ngraph = g.build()"
        assert _is_valid_graph_code(code) is True

    def test_rejects_private_attribute_access(self):
        code = "obj._secret\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_dunder_attribute_access(self):
        code = "obj.__class__\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_rejects_syntax_error(self):
        code = "def (\ngraph = g.build()"
        assert _is_valid_graph_code(code) is False

    def test_accepts_graph_assignment_form(self):
        code = "graph = g.build()\n"
        assert _is_valid_graph_code(code) is True
