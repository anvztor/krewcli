# Introduction to KrewCLI

`krewcli` is the operator-side command-line tool for the Krew platform. It
turns your machine into an **A2A (agent-to-agent) gateway** that exposes
your local coding agents — `claude`, `codex`, `bub` — to a KrewHub
deployment so tasks dispatched on the hub can be claimed and executed
locally.

In practice, `krewcli`:

- authenticates you against KrewAuth via a browser device flow and caches
  the JWT for reuse
- clones a **cookbook** (git repo of recipes) and wires selected
  **recipes** in as submodules
- detects agent CLIs on your `PATH` and launches a local A2A server
  (default `http://127.0.0.1:9999`) with a route per agent
  (`/agents/claude`, `/agents/codex`, …)
- streams agent output (stdout, tool calls, milestones) back to KrewHub as
  task events over SSE
- optionally manages an on-chain identity (EOA wallet + ERC-4337 session
  key) for agent minting and ERC-8004 registry operations

If you want the deeper picture of how tasks flow through the gateway and
back to KrewHub, read [`architecture.md`](architecture.md) next. For the
full reference of every subcommand and flag, see
[`commands.md`](commands.md). For day-to-day configuration and
troubleshooting, see [`user-guide.md`](user-guide.md).

## Prerequisites

Before you install `krewcli`, make sure you have:

- **Python 3.12 or newer** (`python3 --version`)
- **Git** on your `PATH` — used to clone cookbooks and manage recipe
  submodules
- **A reachable KrewHub + KrewAuth deployment.** For local dev the
  defaults work out of the box:
  - KrewHub at `http://127.0.0.1:8420`
  - KrewAuth at `http://127.0.0.1:8421`
- **At least one supported agent CLI** installed and logged in on your
  host — `krewcli` does not ship agents, it wraps whatever is on your
  `PATH`:
  - [`claude`](https://docs.anthropic.com/claude/docs/claude-code) —
    inherits `ANTHROPIC_API_KEY` or Claude login
  - `codex` — reads the global `~/.codex/auth.json`
  - `bub` — optional
- (Optional) **`uv`** for dependency management — `pip install -e .`
  works as a fallback.
- (Optional, on-chain flows only) a funded test-net account on GOAT
  Testnet3 — used only if you run agent mint / ERC-8004 registry
  operations. Off-chain task execution does not need this.

Verify your agents are visible to `krewcli`:

```bash
which claude codex bub
```

## Installation

```bash
# clone the monorepo or the krewcli subtree, then:
cd krewcli

# recommended
uv sync
# or, with plain pip
pip install -e .

# sanity check
krewcli --help
```

The console entry point is declared in `pyproject.toml`
(`krewcli = "krewcli.cli:main"`).

On first run, `krewcli` creates `~/.krewcli/` (mode `0700`) to hold your
JWT, EOA wallet, and session key. Each file inside is written with mode
`0600`. See [user-guide.md#configuration-files](user-guide.md#configuration-files)
for what lives there and how to reset it.

## Quickstart

The minimum path from install to a running gateway is three commands:

```bash
# 1. authenticate — opens a browser, saves JWT to ~/.krewcli/token
krewcli login

# 2. guided bootstrap — pick a cookbook, select recipes and agents,
#    and launch the local A2A gateway
krewcli onboard
```

`krewcli onboard` will:

1. create (or reuse) a cookbook on KrewHub
2. clone it to `~/krew/<cookbook-name>`
3. prompt you to multi-select recipes (added as git submodules)
4. prompt you to multi-select agents detected on `PATH`
5. register the gateway with KrewHub and start heartbeating
6. sit in the foreground — `Ctrl+C` to stop

Expected output (abridged):

```text
Created cookbook: cb_01HX...
Cloning cookbook to /Users/alice/krew/my-cookbook
Selected 1 recipe(s): web-frontend
Gateway agents: claude, codex
  Registered Claude Stream (claude_alice_1234)
  Registered Codex         (codex_alice_1234)

Gateway ready. Waiting for tasks. Press Ctrl+C to stop.
```

Once the gateway is up, KrewHub can dispatch tasks to `/agents/claude`,
`/agents/codex`, etc., and the local CLIs will execute them against the
cloned recipe repo.

### Skipping the interactive bootstrap

If you already know your cookbook and recipe IDs, skip `onboard` and go
straight to `join`:

```bash
krewcli join --recipe REC_ID --cookbook CB_ID
```

Or set them in the environment once and drop the flags:

```bash
export KREWCLI_DEFAULT_COOKBOOK_ID=cb_XXXXX
export KREWCLI_DEFAULT_RECIPE_ID=rec_XXXXX
krewcli join
```

### Running a single task for debugging

To claim and execute one specific task end-to-end without bringing the
gateway up:

```bash
krewcli list-tasks --recipe REC_ID
krewcli claim TASK_ID --recipe REC_ID --agent claude
```

## Next Steps

- **Wire up your config** — environment variables, stored credentials,
  TLS, and local-gateway auth: [`user-guide.md`](user-guide.md).
- **Look up a specific command** — every flag, every subcommand:
  [`commands.md`](commands.md).
- **Understand the runtime** — how `cli.py`, the A2A gateway, the SSE
  bridge, and `TaskRunner` fit together: [`architecture.md`](architecture.md).
