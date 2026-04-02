from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Literal

DiagramFormat = Literal["mermaid", "tree"]

DEFAULT_EXCLUDED_NAMES = frozenset(
    {
        ".DS_Store",
        ".git",
        ".jj",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
    }
)


@dataclass(frozen=True, slots=True)
class RepoNode:
    name: str
    is_dir: bool
    children: tuple["RepoNode", ...] = ()

    @property
    def label(self) -> str:
        return f"{self.name}/" if self.is_dir else self.name


def build_repo_diagram(
    root: str | Path,
    *,
    format: DiagramFormat = "mermaid",
    max_depth: int = 3,
    include_hidden: bool = False,
    excluded_names: Iterable[str] = DEFAULT_EXCLUDED_NAMES,
) -> str:
    tree = build_repo_tree(
        root,
        max_depth=max_depth,
        include_hidden=include_hidden,
        excluded_names=excluded_names,
    )
    if format == "mermaid":
        return render_mermaid_diagram(tree)
    if format == "tree":
        return render_tree_diagram(tree)
    raise ValueError(f"Unsupported diagram format: {format}")


def build_repo_tree(
    root: str | Path,
    *,
    max_depth: int = 3,
    include_hidden: bool = False,
    excluded_names: Iterable[str] = DEFAULT_EXCLUDED_NAMES,
) -> RepoNode:
    if max_depth < 0:
        raise ValueError("max_depth must be non-negative")

    root_path = Path(root).resolve()
    if not root_path.exists():
        raise FileNotFoundError(f"Repository root does not exist: {root_path}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"Repository root is not a directory: {root_path}")

    excluded = set(excluded_names)
    return _build_repo_node(
        root_path,
        depth=0,
        max_depth=max_depth,
        include_hidden=include_hidden,
        excluded_names=excluded,
    )


def render_tree_diagram(node: RepoNode) -> str:
    lines = [node.label]
    for index, child in enumerate(node.children):
        _append_tree_lines(
            lines,
            child,
            prefix="",
            is_last=index == len(node.children) - 1,
        )
    return "\n".join(lines)


def render_mermaid_diagram(node: RepoNode) -> str:
    node_lines = ["flowchart TD"]
    edge_lines: list[str] = []
    ids = count()

    def visit(current: RepoNode, parent_id: str | None = None) -> None:
        current_id = f"n{next(ids)}"
        node_lines.append(f'    {current_id}["{_escape_label(current.label)}"]')
        if parent_id is not None:
            edge_lines.append(f"    {parent_id} --> {current_id}")
        for child in current.children:
            visit(child, current_id)

    visit(node)
    return "\n".join([*node_lines, *edge_lines])


def _build_repo_node(
    path: Path,
    *,
    depth: int,
    max_depth: int,
    include_hidden: bool,
    excluded_names: set[str],
) -> RepoNode:
    children: tuple[RepoNode, ...] = ()
    if path.is_dir() and depth < max_depth:
        try:
            entries = [
                entry
                for entry in path.iterdir()
                if _should_include(entry, include_hidden=include_hidden, excluded_names=excluded_names)
            ]
        except OSError:
            entries = []
        entries.sort(key=_repo_sort_key)
        children = tuple(
            _build_repo_node(
                entry,
                depth=depth + 1,
                max_depth=max_depth,
                include_hidden=include_hidden,
                excluded_names=excluded_names,
            )
            for entry in entries
        )
    return RepoNode(name=path.name or str(path), is_dir=path.is_dir(), children=children)


def _should_include(path: Path, *, include_hidden: bool, excluded_names: set[str]) -> bool:
    if path.name in excluded_names:
        return False
    if not include_hidden and path.name.startswith("."):
        return False
    return True


def _repo_sort_key(path: Path) -> tuple[bool, str]:
    return (not path.is_dir(), path.name.lower())


def _append_tree_lines(lines: list[str], node: RepoNode, *, prefix: str, is_last: bool) -> None:
    branch = "└── " if is_last else "├── "
    lines.append(f"{prefix}{branch}{node.label}")
    child_prefix = f"{prefix}{'    ' if is_last else '│   '}"
    for index, child in enumerate(node.children):
        _append_tree_lines(
            lines,
            child,
            prefix=child_prefix,
            is_last=index == len(node.children) - 1,
        )


def _escape_label(label: str) -> str:
    return label.replace("\\", "\\\\").replace('"', '\\"')
