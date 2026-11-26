# How Tasks Are Assigned to Models

`task` routes work through nine task rows (`search`, `inventory`, `mechanical`, `code`, `deep`, `review`, `hard`, `critical`, `escalate`). Each row defines an ordered list of model aliases in `scripts/task.py`, ordered by default priority: **Gemini -> Codex -> OpenCode** (`chatanywhere` last).

## Task Rows

| Row | Kind of work |
| --- | --- |
| `search` | Search, web research, long docs, screenshots, and light read-only chores (gathering facts for a brief, triaging logs, checking dependency versions/changelogs, summarizing specs). |
| `inventory` | Read-only listings with `file:line` and no reasoning (listing settings, finding usages, comparing two repos, code maps). |
| `mechanical` | Docs, deploy notes, changelogs, scripts with clear input/output, pattern edits, renames, writing tests for existing code, deploys and ops (Hydrogen low, Codex low). |
| `code` | Scoped code with a clear spec (1–3 files), multi-file features or refactors, work that needs running the app (Hydrogen medium, Codex medium). |
| `deep` | Hard code: many files, debugging by reproducing/measuring, root cause, no test to lean on (Hydrogen high, Codex high). |
| `review` | Code diff reviews, re-reviews and final branch reviews (Carbon low, Codex medium). |
| `hard` | Spec and plan review by a model other than the writer (Neon medium, Codex medium). |
| `critical` | Writing specs and plans, and risky changes (prod, migration, payments, auth) (Carbon high, Codex high). |
| `escalate` | Hard bugs (async, concurrency, sync) and any task Carbon FAILed. Neon high is costly, so it is never a default (Codex high). |

## Row-to-Model Table

Each row lists model aliases in priority order (first = preferred). Default `--timeout` is `1800` seconds on `task start` (and half of the task's timeout, minimum `1`s, on `task reply`); default `--cmd-stall` is `1800` seconds.

| Row | Ordered model aliases (CLI: model ID) | Default `--stall` | Default `--effort` |
| --- | --- | --- | --- |
| `search` | 1. `flash` (`gemini`: `gemini-3.8-flash-medium`)<br>2. `luna` (`codex`: `gpt-6-luna`)<br>3. `deepseek` (`opencode`: `opencode-go/deepseek-v4.1-flash`)<br>4. `chatanywhere` (`codex`: `gpt-6-luna`) | `180`s (`flash`, `luna`, `deepseek`)<br>`300`s (`chatanywhere`) | `None` (Codex uses its config default; ignored by Gemini/OpenCode) |
| `inventory` | 1. `flash` (`gemini`: `gemini-3.8-flash-high`)<br>2. `luna` (`codex`: `gpt-6-luna`)<br>3. `deepseek` (`opencode`: `opencode-go/deepseek-v4.1-flash`)<br>4. `chatanywhere` (`codex`: `gpt-6-luna`) | `180`s (`flash`, `luna`, `deepseek`)<br>`300`s (`chatanywhere`) | `None` |
| `mechanical` | 1. `hydrogen-low` (`gemini`: `gemini-4-hydrogen-low`)<br>2. `luna` (`codex`: `gpt-6-luna`)<br>3. `deepseek` (`opencode`: `opencode-go/deepseek-v4.1-flash`)<br>4. `chatanywhere` (`codex`: `gpt-6-luna`) | `300`s (`hydrogen-low`, `chatanywhere`)<br>`180`s (`luna`, `deepseek`) | Codex `low` (Gemini: in the model name; OpenCode: none) |
| `code` | 1. `hydrogen` (`gemini`: `gemini-4-hydrogen-medium`)<br>2. `luna` (`codex`: `gpt-6-luna`)<br>3. `deepseek` (`opencode`: `opencode-go/deepseek-v4.1-flash`)<br>4. `chatanywhere` (`codex`: `gpt-6-luna`) | `300`s (`hydrogen`, `chatanywhere`)<br>`180`s (`luna`, `deepseek`) | Codex `medium` (Gemini: in the model name; OpenCode: none) |
| `review` | 1. `carbon` (`gemini`: `gemini-4-carbon-low`)<br>2. `sol` (`codex`: `gpt-6.1-sol`)<br>3. `luna` (`codex`: `gpt-6-luna`)<br>4. `deepseek` (`opencode`: `opencode-go/deepseek-v4.1-flash`)<br>5. `argon` (`gemini`: `gemini-4-argon-low`) | `300`s (`carbon`, `sol`, `argon`)<br>`180`s (`luna`, `deepseek`) | Codex `medium` (Gemini: in the model name; OpenCode: none) |
| `deep` | 1. `hydrogen-high` (`gemini`: `gemini-4-hydrogen-high`)<br>2. `luna` (`codex`: `gpt-6-luna`)<br>3. `deepseek` (`opencode`: `opencode-go/deepseek-v4.1-flash`)<br>4. `chatanywhere` (`codex`: `gpt-6-luna`) | `300`s (`hydrogen-high`, `chatanywhere`)<br>`180`s (`luna`, `deepseek`) | Codex `high` (Gemini: in the model name; OpenCode: none) |
| `critical` | 1. `carbon-high` (`gemini`: `gemini-4-carbon-high`)<br>2. `sol` (`codex`: `gpt-6.1-sol`)<br>3. `deepseek` (`opencode`: `opencode-go/deepseek-v4.1-flash`) | `300`s (`carbon-high`, `sol`)<br>`180`s (`deepseek`) | Codex `high` (Gemini: in the model name; OpenCode: none) |
| `escalate` | 1. `neon` (`gemini`: `gemini-4-neon-high`)<br>2. `sol` (`codex`: `gpt-6.1-sol`)<br>3. `deepseek` (`opencode`: `opencode-go/deepseek-v4.1-flash`) | `300`s (`neon`, `sol`)<br>`180`s (`deepseek`) | Codex `high` (Gemini: in the model name; OpenCode: none) |
| `hard` | 1. `neon-medium` (`gemini`: `gemini-4-neon-medium`)<br>2. `sol` (`codex`: `gpt-6.1-sol`)<br>3. `luna` (`codex`: `gpt-6-luna`)<br>4. `deepseek` (`opencode`: `opencode-go/deepseek-v4.1-flash`) | `300`s (`neon-medium`, `sol`)<br>`180`s (`luna`, `deepseek`) | Codex `medium` (Gemini: in the model name; OpenCode: none) |

## How `task start` and `task resolve` Pick a Model

1. **Row resolution (`resolve_row`)**: Starting from the preferred alias (the row's first entry, or the alias from `--model`), `task` checks that:
   - Both the CLI switch and the model switch are `on` in `~/.claude/state/task-workers.json` (`chatanywhere` checks only its own independent switch), and
   - The alias is not quota-blocked.
2. **Automatic fallback**: If the preferred alias is off or quota-blocked, `task` scans the row's ordered list and picks the first enabled, non-blocked alias, printing `FALLBACK: <from> off -> <to>`. If every alias in the row is off or quota-blocked, `task start` prints `BLOCKED: all workers for row <row> are off; Claude mode (task skill) or ask the user` and exits with code `2`.
3. **`task resolve <row>`**: Runs the same row lookup in read-only mode and prints `<worker> - <alias>` (exit code `0`), or exits `2` if every candidate in that row is off or quota-blocked.
4. **Overrides**:
   - `--model <alias|model-id>`: Sets the preferred alias for the start (if `--row` is omitted, the alias's default row is used). Without `--pinned`, it still falls back along the row if that model is off.
   - `--pinned`: Requires both `<worker>` and `--model`. Disables all fallback: if the named model is off, `task start` prints `BLOCKED: <model> is off (explicitly named); ask the user` and exits `2`. If it runs and fails or hits `QUOTA`, no substitute is tried.
   - `--effort {low,medium,high,xhigh}`: Passes `-c model_reasoning_effort="<effort>"` to `codex` (and `--effort <effort>` to `relay`). Ignored by `gemini`, `opencode`, and `perch` (Gemini effort is built into the model ID).

## Fallback When a CLI Is Turned Off

When one or more CLIs are disabled via `task off <cli>` (see [docs/workers.md](workers.md)):

| Turned Off | `code`, `deep`, `review`, `hard`, `critical`, `escalate`, `mechanical` Rows Run On | `search`, `inventory` Rows Run On |
| --- | --- | --- |
| Gemini | Codex (`luna`, `sol`), then OpenCode (`code`: `luna`, `deepseek`) | `luna`, then `deepseek` |
| Codex | Gemini (`hydrogen`, `hydrogen-low`, `hydrogen-high`, `carbon`, `carbon-high`, `neon`, `neon-medium`), then OpenCode (`argon` last in `review`) | `flash` |
| OpenCode | Gemini, then Codex | `flash` |
| Gemini + Codex | OpenCode (`deepseek`) | `deepseek` |
| All three | Claude subagents (if configured) | Ask user |

`chatanywhere` has its own switch (`task on chatanywhere` / `task off chatanywhere`) independent of `codex` and sits at the tail of `search`, `inventory`, `mechanical`, and `code` (never `review` or `hard`).

## `QUOTA` Behaviour

When a worker hits a rate or usage limit, `task` finishes with state `QUOTA` (exit code `4`) and prints:

- `<worker> - <alias> - <id> | QUOTA`
- `HINT: task off <alias>` (when an alias is identified)
- `ANSWER:` (the quota/rate-limit reason) and `RAW: <log-path>`

What gets skipped and for how long:

- **`chatanywhere`**: Automatically recorded in `task-chatanywhere-quota.json` and skipped for the remainder of the current UTC day.
- **`deepseek-free` (non-pinned)**: Automatically retries once on paid `deepseek`, printing `FALLBACK: deepseek-free quota -> deepseek`.
- **All other aliases**: Not automatically disabled. They stay `on` in `~/.claude/state/task-workers.json` until you run `task off <alias>` (or wait for the provider's quota reset).

## Named-Only Workers (No Row)

These aliases belong to no automated row and are never selected by `--row` or row fallback:

| Alias | CLI | Model ID | `--pinned` requirement & notes |
| --- | --- | --- | --- |
| `deepseek-free` | `opencode` | `opencode/deepseek-v4.1-flash-free` | Optional `--pinned`; non-pinned retries once on `deepseek` on `QUOTA` or provider error; default `--stall 180`s. |
| `sonnet` | `gemini` | `claude-sonnet-5-5-medium` | Pinned-only usage (`--model sonnet --pinned`); tiny shared pool; default `--stall 180`s. |
| `perch-deepseek` | `perch` | `deepseek-flash-starter` | `--pinned` enforced by `task start`; default `--stall 900`s. |
| `relay-sonnet` | `relay` | `claude-sonnet-5-5` | `--pinned` enforced by `task start`; default `--stall 300`s. |
| `relay-haiku` | `relay` | `claude-haiku-5-5` | `--pinned` enforced by `task start`; default `--stall 300`s. |
| `relay-opus` | `relay` | `claude-opus-5-5` | `--pinned` enforced by `task start`; default `--stall 300`s. |

## Worked Example: `task resolve code`

1. **With all workers `on`**:

   ```bash
   ~/.claude/scripts/task resolve code
   # gemini - hydrogen
   ```

   `hydrogen` (`gemini`) is the first alias in `code` and both `gemini` and `hydrogen` are enabled.

2. **With `gemini` turned `off`** (`task off gemini`):

   ```bash
   ~/.claude/scripts/task resolve code
   # codex - luna
   ```

   `hydrogen` runs on `gemini`, so it is skipped and the next alias in `code`, `luna` (`codex`), is chosen.

3. **With `gemini` and `codex` both `off`** (`task off codex`):

   ```bash
   ~/.claude/scripts/task resolve code
   # opencode - deepseek
   ```

   `luna` and `sol` run on `codex`, so the first enabled alias left in `code` is `deepseek` (`opencode`).

Turning only `codex` off leaves `task resolve code` at `gemini - hydrogen`, because `hydrogen` comes first and runs on `gemini`.
