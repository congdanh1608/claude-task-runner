# Worker Configuration and Management

`task` delegates work to autonomous worker CLIs (`gemini`, `codex`, `opencode`, `perch`, `relay`). Worker sessions are resumable, meaning follow-up instructions and review fixes continue the existing session without starting over.

All commands run via `~/.claude/scripts/task`.

## Worker Switches

View the state of all worker CLIs and model aliases:

```bash
task workers
```

Turn a worker CLI or individual model on or off:

```bash
task on <name>
task off <name>
```

Turning off a CLI disables all of its associated models (for example, `task off codex` disables both `luna` and `sol`). Switches persist across sessions in `~/.claude/state/task-workers.json`.

Check worker and model quota signals:

```bash
task quota
```

## Testing Workers: `task test`

Verify that a worker CLI is correctly installed and authenticated:

```bash
task test <worker> [<worker> ...] [--resume] [--verbose]
```

- You must explicitly specify worker names (e.g. `task test flash`, `task test luna sol`). It never runs all workers automatically.
- Valid worker names: `luna`, `sol`, `chatanywhere`, `flash`, `hydrogen`, `argon`, `carbon`, `sonnet`, `codex`, `opencode`, `gemini`, `perch`, `perch-deepseek`, `deepseek-free`, `relay-sonnet`, `relay-haiku`, `relay-opus`, `relay`.
- CLI names map to their default model: `codex` -> `luna`, `opencode` -> `deepseek`, `gemini` -> `flash`, `perch` -> `perch-deepseek`, `relay` -> `relay-sonnet`.
- Naming a worker that is currently turned off still runs the test and reports `(switch: off)`. This allows you to verify a worker before enabling it.
- Each test runs in an isolated temporary directory and registry without touching real project tasks.
- By default, a test runs a single turn. Adding `--resume` runs two turns to verify that session resumption works.
- Output displays a single status line per worker (`ok`, `FAIL`, or `SKIP quota`), along with elapsed execution time and a final summary. Pass `--verbose` to print full worker reports.

## Workers and Models

- **`gemini`** (CLI: `agy`):
  - `flash` (`gemini-3.8-flash-high` for `inventory` / `gemini-3.8-flash-medium` for `search`): Documentation, search, repo inventory, read-only chores.
  - `hydrogen` (`gemini-4-hydrogen-medium`): Code changes, mechanical edits, small features, unit tests.
  - `carbon` (`gemini-4-carbon-low`): Code diff reviews, spec reviews, plan reviews.
  - `argon` (`gemini-4-argon-low`): Review fallback.
  - `sonnet` (`claude-sonnet-5-5-medium`): Pinned-only model on Gemini.
- **`codex`** (CLI: `codex`):
  - `luna` (`gpt-6-luna`): Standalone builds, test suites, multi-branch merges, ops, deployments.
  - `sol` (`gpt-6.1-sol`): Hard debugging, complex logic, concurrency.
- **`opencode`** (CLI: `opencode`):
  - `deepseek` (`opencode-go/deepseek-v4.1-flash`): General code and mechanical edits.
  - `deepseek-free` (`opencode/deepseek-v4.1-flash-free`): Free tier on OpenCode.

## Fallback Order

When all workers are turned on, the default dispatch priority across task rows is: **Gemini -> Codex -> OpenCode** (`chatanywhere` last).

When one or more CLIs are turned off:

| Turned Off | Code, Review, Hard, Mechanical Rows Run On | Search, Inventory Rows Run On |
|---|---|---|
| Gemini | Codex (Luna, Sol), then OpenCode (code: Luna, DeepSeek, Sol) | Luna, then DeepSeek |
| Codex | Gemini (Hydrogen, Carbon), then OpenCode (Argon last in review) | Flash |
| OpenCode | Gemini, then Codex | Flash |
| Gemini + Codex | OpenCode (DeepSeek) | DeepSeek |
| All three | Claude subagents (if configured) | Ask user |

If you explicitly name a model with `--pinned` and that model is turned off or fails, `task` stops and reports the reason instead of falling back to another model.

## Optional and Named Workers

The following workers are optional or run only when explicitly named (`--pinned` is enforced only for `perch-deepseek` and `relay-*`):

### `deepseek-free`
- Free tier of DeepSeek via OpenCode (`opencode/deepseek-v4.1-flash-free`), zero-retention.
- Runs only when you explicitly name it (`task start opencode <id> <brief> --model deepseek-free`; add `--pinned` to disable fallback).
- If a non-pinned run encounters quota limits or provider errors, it falls back once to paid `deepseek` (`FALLBACK: deepseek-free quota -> deepseek`).

### `perch-deepseek`
- Perch CLI running the starter model (`deepseek-flash-starter`).
- Runs only when explicitly named with `--pinned` (`task start perch <id> <brief> --model perch-deepseek --pinned`).
- It belongs to no automated row and is never a fallback source or target.
- On quota limits, authentication errors, or missing binaries, it halts immediately without fallback.
- Requires the `perch` CLI installed and authenticated.

### `relay`
- Runs the Claude CLI through an external Claude API gateway.
- Runs only when explicitly named with `--pinned` (`task start relay <id> <brief> --model relay-sonnet --pinned`).
- It belongs to no automated row and is never a fallback source or target.
- Requires `RELAY_BASE_URL` and an authentication token in macOS Keychain or via `TASK_RELAY_KEY_CMD`.
- See [docs/relay.md](relay.md) for setup details.

### `chatanywhere`
- Luna (`gpt-6-luna`) running via Codex CLI with ChatAnywhere free key credentials.
- Serves as the tail fallback worker for `search`, `inventory`, `mechanical`, and `code` rows (never `hard`).
- Controlled by its own independent switch (`task on chatanywhere` / `task off chatanywhere`), independent of the `codex` CLI switch.
- Credentials are read from macOS Keychain item `task-chatanywhere-key` or from the command configured in `TASK_CHATANYWHERE_KEY_CMD`.
- Rate limits: 100 requests/day and 50k points/week. On quota limit, it is skipped for the remainder of the UTC day.
- Test via `task test chatanywhere`.
