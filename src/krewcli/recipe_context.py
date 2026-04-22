"""Recipe context resolution — extract repo_url and branch from a recipe."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient


async def load_recipe_context(
    client: "KrewHubClient",
    recipe_id: str,
) -> tuple[str, str]:
    """Fetch the recipe and return (repo_url, branch).

    Falls back to empty strings if the recipe doesn't have them.
    """
    data = await client.get_recipe(recipe_id)
    # get_recipe returns the full API response: {"recipe": {...}, ...}
    recipe = data.get("recipe", data)
    repo_url = recipe.get("repo_url", "") or ""
    branch = recipe.get("default_branch", recipe.get("branch", "")) or ""
    return repo_url, branch
