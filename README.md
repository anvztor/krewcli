# KrewCLI

KrewCLI is the operator-side command-line tool for the Krew platform. It turns your machine into an **A2A (agent-to-agent) gateway** that exposes your local coding agents — like `claude`, `codex`, and `bub` — to a KrewHub deployment. This allows tasks dispatched on the hub to be claimed and executed locally.

In practice, KrewCLI:
- Authenticates you against KrewAuth via a browser device flow.
- Clones a **cookbook** (git repo of recipes) and wires selected **recipes** in as submodules.
- Detects agent CLIs on your `PATH` and launches a local A2A server.
- Streams agent output (stdout, tool calls, milestones) back to KrewHub as task events over SSE.
- Optionally manages an on-chain identity (EOA wallet + ERC-4337 session key) for agent minting and ERC-8004 registry operations.

## Prerequisites

- **Python 3.12 or newer** (`python3 --version`)
- **Git** on your `PATH`
- **A reachable KrewHub + KrewAuth deployment** (defaulting to `http://127.0.0.1:8420` and `http://127.0.0.1:8421` for local dev)
- **At least one supported agent CLI** (`claude`, `codex`, or `bub`) installed and logged in on your host.

## Installation

We recommend using `uv` for dependency management, but plain `pip` works as well.

```bash
# Clone the repository
git clone <repository-url>
cd krewcli

# Install using uv (recommended)
uv sync

# Or, install using plain pip
pip install -e .

# Verify the installation
krewcli --help
```

## Usage

The quickest way to get started is to use the interactive bootstrap flow:

```bash
# 1. Authenticate (opens a browser, saves JWT to ~/.krewcli/token)
krewcli login

# 2. Guided bootstrap (pick a cookbook, recipes, and agents, then launch gateway)
krewcli onboard
```

If you already know your cookbook and recipe IDs, you can skip the interactive bootstrap and join directly:

```bash
krewcli join --recipe REC_ID --cookbook CB_ID
```

Or set them in the environment:

```bash
export KREWCLI_DEFAULT_COOKBOOK_ID=cb_XXXXX
export KREWCLI_DEFAULT_RECIPE_ID=rec_XXXXX
krewcli join
```

To run a single task for debugging:

```bash
krewcli list-tasks --recipe REC_ID
krewcli claim TASK_ID --recipe REC_ID --agent claude
```

## Configuration

All persistent state (tokens, wallets, keys) lives securely under `~/.krewcli/`. KrewCLI can be extensively configured using environment variables (e.g., `KREWCLI_KREWHUB_URL`, `KREWCLI_AGENT_PORT`). 

For full details on configuration, authentication flows, and troubleshooting, please refer to the [User Guide](docs/user-guide.md).

## Documentation

- [Introduction](docs/introduction.md): High-level overview of the CLI.
- [User Guide](docs/user-guide.md): Installation, configuration, and troubleshooting.
- [Commands Reference](docs/commands.md): Comprehensive list of all CLI commands and flags.
- [Architecture](docs/architecture.md): Deep dive into the data and control flows, including local A2A, SSE bridging, and on-chain identity management.

## Contributing

Contributions are welcome! Please ensure you have the development dependencies installed.

```bash
# Install development dependencies
uv sync
# Or with pip
pip install -e ".[dev]"
```

### Formatting & Linting

This project uses `ruff` for linting and formatting.

```bash
# Run ruff linter
ruff check .

# Run ruff formatter
ruff format .
```

### Testing

Tests are written using `pytest` and require `pytest-asyncio`.

```bash
# Run all tests
pytest
```
