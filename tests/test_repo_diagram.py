from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from krewcli.cli import main
from krewcli.repo_diagram import build_repo_tree, render_mermaid_diagram, render_tree_diagram


def _build_sample_repo(base: Path) -> Path:
    repo = base / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "src" / "krewcli").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".venv").mkdir()
    (repo / "docs" / "architecture.md").write_text("# docs\n", encoding="utf-8")
    (repo / "src" / "krewcli" / "cli.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "tests" / "test_cli.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    return repo


def test_build_repo_tree_excludes_tooling_dirs_and_sorts_directories(tmp_path):
    repo = _build_sample_repo(tmp_path)

    tree = build_repo_tree(repo, max_depth=1)

    assert tree.label == "repo/"
    assert [child.label for child in tree.children] == [
        "docs/",
        "src/",
        "tests/",
        "README.md",
    ]


def test_render_tree_diagram_formats_nested_structure(tmp_path):
    repo = _build_sample_repo(tmp_path)

    tree = build_repo_tree(repo, max_depth=2)
    output = render_tree_diagram(tree)

    assert output.splitlines() == [
        "repo/",
        "├── docs/",
        "│   └── architecture.md",
        "├── src/",
        "│   └── krewcli/",
        "├── tests/",
        "│   └── test_cli.py",
        "└── README.md",
    ]


def test_render_mermaid_diagram_uses_mermaid_flowchart(tmp_path):
    repo = _build_sample_repo(tmp_path)

    tree = build_repo_tree(repo, max_depth=2)
    output = render_mermaid_diagram(tree)

    assert output.startswith("flowchart TD")
    assert '["repo/"]' in output
    assert '["docs/"]' in output
    assert '["README.md"]' in output
    assert "-->" in output
    assert ".git" not in output
    assert ".venv" not in output


def test_repo_diagram_cli_supports_tree_output(tmp_path):
    repo = _build_sample_repo(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["repo-diagram", "--root", str(repo), "--format", "tree", "--max-depth", "2"],
    )

    assert result.exit_code == 0
    assert "repo/" in result.output
    assert "├── docs/" in result.output
    assert ".git" not in result.output
