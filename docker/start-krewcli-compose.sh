#!/bin/sh
set -eu

home_dir="${HOME:-/home/krewcli}"
codex_dir="${home_dir}/.codex"
host_codex_dir="${HOST_CODEX_DIR:-/host-codex}"

mkdir -p "${codex_dir}"

if [ -f "${host_codex_dir}/auth.json" ]; then
  cp "${host_codex_dir}/auth.json" "${codex_dir}/auth.json"
fi

if [ -f "${host_codex_dir}/config.toml" ]; then
  cp "${host_codex_dir}/config.toml" "${codex_dir}/config.toml"
fi

if [ -z "${OPENAI_API_KEY:-}" ] && [ ! -f "${codex_dir}/auth.json" ]; then
  echo "Missing Codex auth. Set OPENAI_API_KEY or mount ~/.codex into ${host_codex_dir}." >&2
  exit 1
fi

recipe_id="$(
python - <<'PY'
from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


base_url = os.environ.get("KREWCLI_KREWHUB_URL", "http://krewhub:8420").rstrip("/")
api_key = os.environ.get("KREWCLI_API_KEY", "dev-api-key")
repo_url = os.environ.get(
    "KREWCLI_REPO_URL",
    "https://github.com/anvztor/krewcli.git",
)
recipe_name = os.environ.get("KREWCLI_RECIPE_NAME", "").strip()
default_branch = os.environ.get("KREWCLI_DEFAULT_BRANCH", "main")
created_by = os.environ.get("KREWCLI_CREATED_BY", "docker-compose")

if not recipe_name:
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", repo_url)
    recipe_name = match.group(1) if match else repo_url.rstrip("/").rsplit("/", 1)[-1]


def request(path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode()
    req = Request(
        f"{base_url}/api/v1{path}",
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
    )
    with urlopen(req, timeout=10) as response:
        return json.load(response)


for _ in range(60):
    try:
        with urlopen(f"{base_url}/openapi.json", timeout=2) as response:
            if response.status == 200:
                break
    except (HTTPError, URLError):
        time.sleep(2)
else:
    raise SystemExit("KrewHub did not become ready in time")

recipes = request("/recipes")["recipes"]
for recipe in recipes:
    if recipe.get("name") == recipe_name:
        print(recipe["id"])
        sys.exit(0)

created = request(
    "/recipes",
    method="POST",
    payload={
        "name": recipe_name,
        "repo_url": repo_url,
        "default_branch": default_branch,
        "created_by": created_by,
    },
)
print(created["recipe"]["id"])
PY
)"

echo "Starting krewcli for recipe ${recipe_id}" >&2

exec krewcli start \
  --recipe "${recipe_id}" \
  --agent "${KREWCLI_AGENT_NAME:-codex}" \
  --agent-id "${KREWCLI_AGENT_ID:-codex_compose}" \
  --workdir "${KREWCLI_AGENT_WORKDIR:-/workspace}"
