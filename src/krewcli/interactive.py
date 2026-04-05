"""Interactive prompt helpers for CLI selection."""

from __future__ import annotations

import click


def prompt_multi_select(
    label: str,
    items: list[tuple[str, str]],
) -> list[int]:
    """Display a numbered list and prompt for comma-separated selection or 'all'.

    Args:
        label: Section header (e.g., "Recipes", "Agents")
        items: List of (display_name, identifier) tuples

    Returns:
        List of selected 0-based indices.
    """
    if not items:
        return []

    click.echo(f"\n{label}:")
    for i, (display, ident) in enumerate(items, 1):
        click.echo(f"  [{i}] {display}  ({ident})")

    raw = click.prompt(
        "Select (comma-separated, or 'all')",
        default="all",
        type=str,
    )

    raw = raw.strip()
    if raw.lower() == "all":
        return list(range(len(items)))

    selected: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            num = int(part)
        except ValueError:
            click.echo(f"  Ignoring invalid input: {part}")
            continue
        if 1 <= num <= len(items):
            selected.append(num - 1)
        else:
            click.echo(f"  Ignoring out-of-range: {num}")

    if not selected:
        click.echo("  No valid selection, defaulting to all.")
        return list(range(len(items)))

    return sorted(set(selected))


def prompt_single_select(
    label: str,
    items: list[tuple[str, str]],
) -> int:
    """Display a numbered list and prompt for a single selection.

    Returns:
        Selected 0-based index.
    """
    if not items:
        raise click.UsageError(f"No {label.lower()} available to select.")

    if len(items) == 1:
        click.echo(f"\n{label}: {items[0][0]} ({items[0][1]}) [auto-selected]")
        return 0

    click.echo(f"\n{label}:")
    for i, (display, ident) in enumerate(items, 1):
        click.echo(f"  [{i}] {display}  ({ident})")

    while True:
        raw = click.prompt("Select", type=str)
        try:
            num = int(raw.strip())
            if 1 <= num <= len(items):
                return num - 1
        except ValueError:
            pass
        click.echo(f"  Please enter a number between 1 and {len(items)}.")
