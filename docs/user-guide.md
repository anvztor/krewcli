# KrewCLI User Guide

Everything an operator needs to install, configure, authenticate, and
troubleshoot `krewcli`. For the internals of how the CLI routes tasks,
see [`architecture.md`](architecture.md).

## Install

Requires Python 3.12+.

```bash
# from the repo root
uv sync              # or: pip install -e .
krewcli --help
```

The `krewcli` entry point is defined in `pyproject.toml` and wraps
`krewcli.cli:main`.

## Quickstart

```bash
# 1. authenticate against krewauth (opens a browser)
krewcli login

# 2. (optional) detect agents on PATH and launch a gateway
krewcli onboard

# 3. or join an existing cookbook/recipe directly
krewcli join --recipe REC_ID --cookbook CB_ID
```

Once the gateway is up, KrewHub can dispatch A2A invocations to the
local CLIs it detected (`claude`, `codex`, `bub`).

## Configuration Files

All persistent state lives under `~/.krewcli/` (created on first run
with `0700` permissions). Each file is written with `0600`.

| File | Purpose | Written by |
| --- | --- | --- |
| `~/.krewcli/token` | JWT from `krewcli login`. Reloaded on 401 for long-running daemons. | `krewcli login` |
| `~/.krewcli/wallet` | Hex-encoded EOA private key used for SIWE flows. | `krewcli wallet create` / `wallet import` |
| `~/.krewcli/session_key` | secp256k1 session key for ERC-4337 smart-account ops. | `krewcli session-key create` |

### Managing stored credentials

```bash
# inspect
krewcli wallet address
krewcli session-key address

# re-authenticate (overwrites ~/.krewcli/token)
krewcli login

# reset — safe to delete manually
rm ~/.krewcli/token
rm ~/.krewcli/wallet
rm ~/.krewcli/session_key
```

> Back up `~/.krewcli/wallet`. Losing that key means losing the on-chain
> identity tied to it.

## Environment Variables

### KrewCLI settings (`KREWCLI_*`)

All settings are read by `pydantic-settings` with the prefix
`KREWCLI_`. Defaults come from `src/krewcli/config.py`.

| Variable | Default | What it does |
| --- | --- | --- |
| `KREWCLI_KREWHUB_URL` | `http://127.0.0.1:8420` | KrewHub control-plane base URL. |
| `KREWCLI_KREW_AUTH_URL` | `http://127.0.0.1:8421` | KrewAuth base URL used by `login`. |
| `KREWCLI_API_KEY` | `dev-api-key` | Legacy fallback auth. Only used when no JWT is present. |
| `KREWCLI_AGENT_HOST` | `127.0.0.1` | Host the local A2A gateway binds to. |
| `KREWCLI_AGENT_PORT` | `9999` | Default port for the gateway (overridable with `--port`). |
| `KREWCLI_HEARTBEAT_INTERVAL` | `15` | Seconds between presence heartbeats to KrewHub. |
| `KREWCLI_TASK_POLL_INTERVAL` | `5` | Seconds between polls in legacy task-worker mode. |
| `KREWCLI_DEFAULT_RECIPE_ID` | `""` | Implicit `--recipe` when unset on the command line. |
| `KREWCLI_DEFAULT_COOKBOOK_ID` | `""` | Implicit `--cookbook` when unset on the command line. |
| `KREWCLI_JWT_SECRET` | `""` | Enables auth on the local A2A gateway. Must be ≥ 32 chars. If empty/short, auth is disabled with a warning. |
| `KREWCLI_TOKEN_EXPIRY_MINUTES` | `30` | JWT expiry for the local gateway. |
| `KREWCLI_HOOK_LISTENER_PORT` | `9998` | Port used by the hook listener (if enabled). |
| `KREWCLI_VERIFY_SSL` | `true` | Set to `false` to disable TLS verification against KrewHub. |
| `KREWCLI_STREAM_EVENTS` | `1` | Set to `0` to disable live event streaming from spawned agents to KrewHub. |
| `KREWCLI_CODEX_DISABLE_ROLLOUT_WATCHER` | unset | Set to `1`/`true`/`yes` to disable the Codex rollout tailer. |

ERC-8004 chain settings (GOAT Testnet3 by default):

| Variable | Default |
| --- | --- |
| `KREWCLI_ERC8004_CHAIN_ID` | `48816` |
| `KREWCLI_ERC8004_RPC_URL` | `https://rpc.testnet3.goat.network` |
| `KREWCLI_ERC8004_IDENTITY_REGISTRY` | `0x556089008Fc0a60cD09390Eca93477ca254A5522` |
| `KREWCLI_ERC8004_REPUTATION_REGISTRY` | `0xd9140951d8aE6E5F625a02F5908535e16e3af964` |

### Hook-side env keys (`KREWHUB_*`)

Spawned agents (Codex rollout watcher, hook adapters) route events back
to KrewHub using these keys. The gateway populates them per spawn — you
only need to set them yourself when running a hook binary outside the
gateway.

| Variable | Purpose |
| --- | --- |
| `KREWHUB_URL` | KrewHub base URL (defaults to `http://127.0.0.1:8420`). |
| `KREWHUB_API_KEY` | Legacy API key for `/api/v1/hooks/ingest` etc. |
| `KREWHUB_JWT` | Bearer JWT; preferred over `KREWHUB_API_KEY` when set. |
| `KREWHUB_TASK_ID` | If present, events POST to `/api/v1/tasks/{task_id}/events`. |
| `KREWHUB_RECIPE_ID` | Recipe scope for milestone events. |
| `KREWHUB_BUNDLE_ID` | Bundle scope for planner events. |

### Upstream agent CLIs

`krewcli` does **not** inject provider credentials into `claude` or
`codex` — they use whatever auth is already on the host.

- `claude` inherits `ANTHROPIC_API_KEY` or the Claude keychain/login.
- `codex` reads `$CODEX_HOME` (default `~/.codex`); `krewcli`
  intentionally unsets `CODEX_HOME` in spawn env so the global auth
  file is used.
- The in-process planner endpoint also honors `ANTHROPIC_BASE_URL` +
  `ANTHROPIC_AUTH_TOKEN` for gateway/proxy setups.

## Authentication Setup

KrewCLI uses two independent layers:

1. **Off-chain auth** — JWT from `krewcli login`. This is what almost
   every runtime flow uses (task claim, events, heartbeats, A2A).
2. **On-chain identity** — wallet + session key for ERC-4337
   smart-account operations (agent minting, registry writes).

You only need layer 2 if you plan to run on-chain flows. Layer 1 is
sufficient for task execution.

### Layer 1: JWT via device flow

```bash
krewcli login
```

Flow (implemented in `cli_wallet.py`):

1. `POST {KREWCLI_KREW_AUTH_URL}/auth/device/request` → `device_code`,
   `user_code`, `expires_in`.
2. Your browser opens `/auth/login?device_code=<user_code>`.
3. Approve with passkey or wallet.
4. CLI polls `/auth/device/token` until `status == "approved"`.
5. JWT is written to `~/.krewcli/token`.

`KrewHubClient` sends `Authorization: Bearer <jwt>` and falls back to
`X-API-Key: $KREWCLI_API_KEY` only when no JWT is found. On any 401 the
client re-reads the token file and retries once, so a fresh
`krewcli login` is enough to unstick a long-running daemon.

### Layer 2: wallet + session key

Only needed for on-chain flows (agent mint, ERC-8004 identity, etc).

```bash
# create or import an EOA wallet (stored at ~/.krewcli/wallet)
krewcli wallet create
krewcli wallet import 0x<private_key_hex>
krewcli wallet address

# create a session key (stored at ~/.krewcli/session_key)
krewcli session-key create
krewcli session-key address
```

When both a JWT and a session key are present, `krewcli join` will:

1. Look up your smart account via `GET /auth/account/info`.
2. Request session-key approval for each gateway agent against the
   ERC-8004 identity registry (`register(string)` selector only).
3. Build one-click mint UserOperations (`mint_agents.build_one_click_userop`).
4. Submit them to `POST /auth/mint-ops/submit`.
5. Prompt you to click **Mint** in cookrew.

Off-chain operation keeps working while minting is pending.

### Local gateway auth

If you expose the A2A gateway to other callers, set
`KREWCLI_JWT_SECRET` to a random ≥ 32-char value. Without it, the
gateway logs `auth is DISABLED` and accepts unauthenticated A2A calls.

```bash
export KREWCLI_JWT_SECRET=$(openssl rand -hex 32)
krewcli join --recipe REC_ID --cookbook CB_ID
```

## Command Overview

| Command | When to use |
| --- | --- |
| `krewcli login` | Refresh the stored JWT. |
| `krewcli onboard` | Interactive bootstrap: pick cookbook/recipes, clone repo, add submodules, launch gateway. |
| `krewcli join` | Launch the A2A gateway for an existing cookbook + recipe. Supports legacy single-agent flags. |
| `krewcli claim TASK_ID --recipe REC_ID` | One-shot execution of a single task. |
| `krewcli list-tasks --recipe REC_ID` | Show open/claimed tasks for a recipe. |
| `krewcli milestone TASK_ID --body ...` | Post a milestone event to a task. |
| `krewcli status` | Print the registered agent backends. |
| `krewcli repo-diagram` | Render a mermaid/tree diagram of the repo structure. |
| `krewcli wallet …` | Manage `~/.krewcli/wallet`. |
| `krewcli session-key …` | Manage `~/.krewcli/session_key`. |
| `krewcli start` | Legacy alias for `join`. |

Run any command with `--help` for full options.

## Troubleshooting

### `Authentication failed (401)`
Your JWT expired or was rotated. Run `krewcli login` again; long-running
daemons pick up the new token automatically on the next retry.

### `Cannot connect to KrewHub`
Confirm `KREWCLI_KREWHUB_URL` points at a reachable instance and the
service is running. For local dev this is `http://127.0.0.1:8420`.

### `SSL certificate error connecting to KrewHub`
The message includes `CERTIFICATE_VERIFY_FAILED`. For local/self-signed
setups:

```bash
export KREWCLI_VERIFY_SSL=false
```

### `No session. Run 'krewcli login' first.`
`~/.krewcli/token` is missing or unreadable. Run `krewcli login`.

### `Specify --cookbook or set KREWCLI_DEFAULT_COOKBOOK_ID`
Either pass `--cookbook CB_ID` or export the default:

```bash
export KREWCLI_DEFAULT_COOKBOOK_ID=cb_XXXXX
export KREWCLI_DEFAULT_RECIPE_ID=rec_XXXXX
```

### `No agent CLIs found on PATH (claude, codex, etc).`
Install at least one supported agent CLI and make sure it's on `PATH`
in the shell that launches `krewcli`.

```bash
which claude codex bub
```

### `KREWCLI_JWT_SECRET is not set — auth is DISABLED`
The local A2A gateway is accepting unauthenticated calls. For anything
beyond local dev, set `KREWCLI_JWT_SECRET` to a random ≥ 32-char string.

### `No session key — run 'krewcli session-key create' first`
You have a JWT but no session key. On-chain setup is skipped; off-chain
task execution continues to work. Create a session key if you need
mint/identity flows.

### `No smart account — connect wallet in cookrew first`
Your krewauth account isn't linked to a smart account yet. Finish the
wallet-connect flow in cookrew before re-running `krewcli join`.

### Codex runs but emits no live events
The rollout watcher tails `~/.codex/sessions/`. Check:

- `codex` is authed (`codex auth status` or equivalent).
- `CODEX_HOME` is unset, or points at the directory that holds your
  real `auth.json`.
- `KREWCLI_CODEX_DISABLE_ROLLOUT_WATCHER` is not set to `1`.

### Claude runs but emits no live events
- `KREWCLI_STREAM_EVENTS` must not be `0`.
- `claude --output-format stream-json --verbose` must work standalone.
- For prod KrewHub, the spawn context needs a Bearer JWT; make sure you
  ran `krewcli login` before starting the gateway — the JWT is read
  when the gateway spawns the agent.

### Token rotation on a running daemon
You don't need to restart. `KrewHubClient` re-reads `~/.krewcli/token`
on any 401 and retries once. Just run `krewcli login` in another shell.

### Inspect effective settings
```bash
python -c "from krewcli.config import get_settings; print(get_settings().model_dump())"
```

### Verbose logs
KrewCLI configures `logging.basicConfig(level=INFO, …)` at startup.
Raise the root log level if you need more detail:

```bash
PYTHONLOGLEVEL=DEBUG krewcli join --recipe REC_ID
```

(or edit `krewcli/cli.py`'s `logging.basicConfig` call for a persistent
change.)

## Where To Look Next

- Command dispatch and flow internals: [`architecture.md`](architecture.md).
- Settings definitions: `src/krewcli/config.py`.
- Auth internals: `src/krewcli/auth/` and `src/krewcli/cli_wallet.py`.
- Hook event routing: `src/krewcli/bridge/forwarder.py`.
