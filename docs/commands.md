# KrewCLI Commands

User-facing reference for every `krewcli` subcommand, organized by the
workflow the user is most likely in.

> For how the pieces fit together internally, see
> [`architecture.md`](architecture.md). This document is oriented toward
> operators running `krewcli` against a KrewHub deployment.

## Global Options

All subcommands share the bootstrap behavior in `krewcli.cli:main`:

- logging is configured at `INFO`
- `Settings` are loaded from environment variables prefixed `KREWCLI_`
  (see [Environment Variables](#environment-variables))
- a JWT is loaded from `~/.krewcli/token` if present
- a `KrewHubClient` is built and passed to every subcommand

If `KrewHub` returns `401`, the CLI prints:

```text
Authentication failed (401). Run 'krewcli login' to refresh your session.
```

If the host is unreachable, it prints a connect error. If TLS verification
fails, it suggests `KREWCLI_VERIFY_SSL=false`.

---

## Workflow 1 — First-Time Setup

These commands establish your identity on this machine. Run them once
before bringing any agent online.

### `krewcli login`

Authenticate with KrewAuth via the device-authorization flow.

```text
krewcli login
```

**What it does**

1. `POST /auth/device/request` to `KREWCLI_KREW_AUTH_URL`
2. Opens `<auth>/auth/login?device_code=...` in your browser
3. Polls `POST /auth/device/token` every 3 seconds until approved
4. Saves the returned JWT to `~/.krewcli/token`

**Flags**: none.

**Expected output**

```text
  Open: http://127.0.0.1:8421/auth/login?device_code=ABCD-EFGH
  Code: ABCD-EFGH

  Waiting for approval (expires in 5 min)...

  Logged in as @alice
  Account: acct_01HX...
  Wallet: 0xAbC...
  Session expires: 2026-04-17T18:30:00Z
  JWT saved to ~/.krewcli/token
```

**Failure modes**

- Timeout: `Error: Timed out waiting for approval.`
- Code expired: `Error: Code expired.`
- Connect error: `Error: Could not connect to krewauth at <url>`

### `krewcli wallet create`

Generate an Ethereum EOA and save it locally.

```text
krewcli wallet create
```

Writes the private key to `~/.krewcli/wallet`. Prints the address.

### `krewcli wallet import <private_key>`

Import an existing EOA key.

```text
krewcli wallet import 0xabc123...
```

Writes the key to `~/.krewcli/wallet`. Fails with `Invalid private key.`
if the key cannot be parsed by `eth_account`.

### `krewcli wallet address`

Print the saved wallet address.

```text
krewcli wallet address
```

Exits with status `1` and `No wallet found. Run 'krewcli wallet create' first.`
if the file is missing.

### `krewcli session-key create`

Generate a session key for ERC-4337 smart-account actions. Saved to
`~/.krewcli/session_key`.

```text
krewcli session-key create
```

**Expected output**

```text
Session key created: 0xSessionKeyAddr...
Saved to ~/.krewcli/session_key
Request approval: human must call addSessionKey() on the smart account
```

### `krewcli session-key address`

Print the session-key address. Exits with status `1` if missing.

---

## Workflow 2 — Bring Agents Online

After logging in, choose one path:

- [`onboard`](#krewcli-onboard) — guided setup, recommended first run
- [`join`](#krewcli-join) — advanced, multi-agent gateway
- [`start`](#krewcli-start) — legacy alias

### `krewcli onboard`

Interactive workspace bootstrap and gateway launch. Picks a cookbook,
clones it, adds recipes as submodules, detects local agent CLIs, and
starts the gateway.

```text
krewcli onboard
krewcli onboard --cookbook-name my-project --owner alice
krewcli onboard --cookbook CB_ID --agents claude,codex
```

**Flags**

| Flag | Default | Purpose |
| --- | --- | --- |
| `--cookbook` | — | Reuse an existing cookbook (skips creation) |
| `--cookbook-name` | `my-cookbook` | Name when creating a new cookbook |
| `--owner` | `cli_user` | Owner ID on cookbook creation |
| `--port` | `9999` | Local A2A gateway port |
| `--workdir` | `~/krew` | Root working directory |
| `--agents` | — | Comma-separated agent types; skips interactive select |
| `--max-concurrent` | `1` | Max concurrent tasks per agent |

**Steps executed**

1. Create or reuse a cookbook on KrewHub
2. Clone the cookbook repo into `<workdir>/<cookbook-name>`
3. Multi-select recipes → added as git submodules
4. Commit and push submodules; KrewHub auto-indexes
5. Detect agent CLIs on `PATH` (multi-select if `--agents` not set)
6. Start the gateway app with `/agents/{name}` routes
7. Register agents and start heartbeats

**Expected output**

```text
Created cookbook: cb_01HX...
Cloning cookbook to /Users/alice/krew/my-cookbook

Select recipes (space to toggle, enter to confirm):
  [x] web-frontend
  [ ] api-server

Selected 1 recipe(s)
  Added submodule: web-frontend
  Pushed to krewhub (indexing triggered)
  Submodules synced

Select agents (detected on PATH):
  [x] claude
  [x] codex

Gateway agents: claude, codex
  /agents/claude -> claude CLI
  /agents/codex -> codex CLI
  Registered Claude Stream (claude_alice_1234)
  Registered Codex (codex_alice_1234)

Onboarding complete:
  Cookbook: cb_01HX...
  Workspace: /Users/alice/krew/my-cookbook
  Recipes: web-frontend
  Agents: claude, codex
  Gateway: http://127.0.0.1:9999
  KrewHub: http://127.0.0.1:8420

Gateway ready. Waiting for tasks. Press Ctrl+C to stop.
```

**When to use this instead of `join`**

- First run on a new machine
- You want the cookbook directory laid out and submodules wired
- You want guided recipe and agent selection

### `krewcli join`

Bring agents online as an A2A gateway. Two modes are supported.

```text
# multi-agent gateway (recommended)
krewcli join --recipe ID --cookbook CB
krewcli join --recipe ID --agents claude,codex --max-concurrent 2

# interactive — omit --recipe/--cookbook to be prompted
krewcli join

# legacy single-agent modes
krewcli join --recipe ID --agent claude
krewcli join --recipe ID --provider anthropic
krewcli join --recipe ID --framework anthropic
krewcli join --recipe ID --endpoint http://my-agent:8080
krewcli join --recipe ID --orchestrator --provider anthropic
```

**Flags — gateway mode**

| Flag | Default | Purpose |
| --- | --- | --- |
| `--recipe` | interactive | Recipe ID to join |
| `--cookbook` | `KREWCLI_DEFAULT_COOKBOOK_ID` | Cookbook ID |
| `--port` | `9999` | A2A server port |
| `--agent-id` | `gw_<pid>` | Override agent ID prefix |
| `--workdir` | `.` | Working directory for agents |
| `--agents` | auto-detect | Comma-separated agent types |
| `--max-concurrent` | `1` | Max concurrent tasks per agent type |

**Flags — legacy single-agent mode** (mutually exclusive; choose one)

| Flag | Purpose |
| --- | --- |
| `--agent {claude,codex,bub}` | Run one local CLI as a single A2A endpoint |
| `--provider {anthropic,openai}` | Direct LLM executor |
| `--model NAME` | Override model (used with `--provider` or `--framework`) |
| `--framework {anthropic,openai}` | pydantic-ai framework agent |
| `--endpoint URL` | Proxy to a remote A2A agent |
| `--orchestrator` | Run the planner/orchestrator executor |

If any legacy flag is present, `join` resolves to `_run_agent()` instead
of gateway mode.

**Expected output — gateway mode**

```text
Starting A2A gateway
  Recipe: recipe_01HX...
  Cookbook: cb_01HX...
  Work dir: /Users/alice/krew/my-cookbook
  Port: 9999
  Max concurrent per agent: 1
  KrewHub: http://127.0.0.1:8420
```

Followed by uvicorn logs and an SSE-watcher that bridges hub-delivered
A2A invocations into local agents.

**Expected output — legacy mode**

```text
Bringing agent online (legacy single-agent mode)
  Mode: cli:claude
  Agent: Claude Stream (claude_12345)
  Recipe: recipe_01HX...
  A2A: http://127.0.0.1:9999
```

**Interactive fallback**

If `--recipe` or `--cookbook` is omitted in gateway mode, `join` fetches
cookbooks over the JWT session and presents:

1. a single-select for cookbook
2. a multi-select for recipes (first selection becomes `--recipe`)
3. a multi-select for agents on `PATH`

`krewcli login` must have run first; otherwise you get:

```text
Usage error: No session. Run 'krewcli login' first.
```

### `krewcli start`

Legacy alias for `join`. Identical flags, immediately delegates.

```text
krewcli start --recipe ID --agent claude
```

Prefer `join` or `onboard` in new scripts.

---

## Workflow 3 — Work With Tasks

These commands operate against tasks and bundles already created on
KrewHub. Useful for one-shot execution, debugging, or manual milestones.

### `krewcli list-tasks --recipe <recipe_id>`

List open and claimed tasks for a recipe, grouped by bundle.

```text
krewcli list-tasks --recipe recipe_01HX...
```

**Expected output**

```text
Bundle: bundle_01HX... [open]
  Prompt: Implement dark-mode toggle on settings page
    [ ] task_01HX...: Add toggle component
    [>] task_01HX...: Wire theme context (claude_alice_1234)
    [x] task_01HX...: Update Settings.test.tsx
```

Status glyphs:

| Glyph | Meaning |
| --- | --- |
| `[ ]` | open |
| `[>]` | claimed |
| `[~]` | working |
| `[x]` | done |
| `[!]` | blocked |
| `[-]` | cancelled |
| `[?]` | unknown |

### `krewcli claim <task_id> --recipe <recipe_id>`

Claim and execute a single task end-to-end. Useful for replaying or
debugging one task outside the gateway.

```text
krewcli claim task_01HX... --recipe recipe_01HX... --agent claude
```

**Flags**

| Flag | Default | Purpose |
| --- | --- | --- |
| `--recipe` | required | Recipe ID containing the task |
| `--agent` | `claude` | One of `claude`, `codex`, `bub` |
| `--agent-id` | `<agent>_<pid>` | Override agent ID |
| `--workdir` | `.` | Working directory |

**Execution**

1. Loads recipe context (`repo_url`, `branch`)
2. Starts a `HeartbeatLoop`
3. Runs `TaskRunner.claim_and_execute(task_id)`

**Expected output**

```text
Task task_01HX... completed: Added ThemeToggle and theme context
# or
Task task_01HX... blocked: tests failed — see output
# or
Task task_01HX... failed or could not be claimed
```

### `krewcli milestone <task_id> --body <text> [--fact ...]`

Post a milestone event to a task. Typically used by humans or scripts
to record progress that an agent didn't capture.

```text
krewcli milestone task_01HX... --body "Manual QA passed" \
  --fact "verified on Chrome 124" \
  --fact "dark toggle persists across reload"
```

**Flags**

| Flag | Default | Purpose |
| --- | --- | --- |
| `--body` | required | Markdown or text body of the event |
| `--fact` | repeatable | One claim per use; attached as structured facts |
| `--agent-id` | `cli_user` | Actor ID recorded on the event |

**Expected output**

```text
Milestone posted: evt_01HX...
```

---

## Workflow 4 — Diagnostics

### `krewcli status`

Print the agent registry — which backends are known to this build and
what capabilities they advertise.

```text
krewcli status
```

**Expected output**

```text
  claude: Claude Stream
    capabilities: code, implement, fix, test, review
  codex: Codex
    capabilities: code, implement, fix, test
  bub: Bub
    capabilities: code
```

Does not check `PATH`. For "is this CLI installed?", use `which claude`
or run `krewcli onboard` (which filters by `PATH`).

### `krewcli repo-diagram`

Render a structure diagram of a local repository.

```text
krewcli repo-diagram --root . --format mermaid --max-depth 3
krewcli repo-diagram --root ./my-app --format tree --include-hidden
```

**Flags**

| Flag | Default | Purpose |
| --- | --- | --- |
| `--root` | `.` | Directory to diagram |
| `--format` | `mermaid` | `mermaid` or `tree` |
| `--max-depth` | `3` | Recursion depth (≥ 0) |
| `--include-hidden` | `false` | Include dotfiles/dirs |

**Expected output (`--format tree`)**

```text
my-app
├── src
│   ├── cli.py
│   └── config.py
└── tests
    └── test_cli.py
```

**Expected output (`--format mermaid`)**

```text
graph TD
  my-app --> src
  src --> cli.py
  src --> config.py
  my-app --> tests
  tests --> test_cli.py
```

---

## Environment Variables

All are loaded by `pydantic-settings` under the `KREWCLI_` prefix. Full
list from `src/krewcli/config.py`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `KREWCLI_KREWHUB_URL` | `http://127.0.0.1:8420` | KrewHub API base URL |
| `KREWCLI_KREW_AUTH_URL` | `http://127.0.0.1:8421` | KrewAuth base URL (used by `login`) |
| `KREWCLI_API_KEY` | `dev-api-key` | Fallback `X-API-Key` when no JWT present |
| `KREWCLI_AGENT_PORT` | `9999` | Default A2A port |
| `KREWCLI_AGENT_HOST` | `127.0.0.1` | Bind host for A2A server |
| `KREWCLI_HEARTBEAT_INTERVAL` | `15` | Seconds between presence pings |
| `KREWCLI_TASK_POLL_INTERVAL` | `5` | Seconds between task polls |
| `KREWCLI_DEFAULT_RECIPE_ID` | `""` | Default `--recipe` |
| `KREWCLI_DEFAULT_COOKBOOK_ID` | `""` | Default `--cookbook` |
| `KREWCLI_VERIFY_SSL` | `true` | Set `false` for self-signed KrewHub |
| `KREWCLI_ERC8004_CHAIN_ID` | `48816` | GOAT Testnet3 chain |
| `KREWCLI_ERC8004_RPC_URL` | `https://rpc.testnet3.goat.network` | On-chain RPC |
| `KREWCLI_ERC8004_IDENTITY_REGISTRY` | `0x5560...5522` | Identity registry address |
| `KREWCLI_ERC8004_REPUTATION_REGISTRY` | `0xd914...f964` | Reputation registry address |

## On-Disk State

`krewcli` writes to `~/.krewcli/` only:

| File | Written by | Purpose |
| --- | --- | --- |
| `~/.krewcli/token` | `login` | JWT for KrewHub and KrewAuth |
| `~/.krewcli/wallet` | `wallet create`/`import` | EOA private key |
| `~/.krewcli/session_key` | `session-key create` | ERC-4337 session key |

Delete any of these to reset that piece of state. Deleting `token`
forces a re-login; deleting `wallet` orphans the smart-account identity
associated with that EOA.

## Quick Reference

```text
# first run
krewcli login
krewcli onboard

# routine — bring gateway up
krewcli join --recipe <id> --cookbook <id>

# poke at tasks
krewcli list-tasks --recipe <id>
krewcli claim <task_id> --recipe <id>
krewcli milestone <task_id> --body "note"

# diagnostics
krewcli status
krewcli repo-diagram --root .

# identity (rare)
krewcli wallet create
krewcli session-key create
```
