# `task` Command Reference

The `task` CLI (`~/.claude/scripts/task`, backed by `scripts/task.py`) runs worker CLIs (`codex`, `opencode`, `agy` for Gemini, `perch`, and `relay`) in resumable foreground sessions with watchdog supervision and automatic fallback.

For worker configuration, model tables, and switches, see [docs/workers.md](workers.md). For how rows resolve to models, overrides, and quota behaviour, see [docs/models.md](models.md). For external Claude API gateway setup, see [docs/relay.md](relay.md).

---

## Exit Codes and Task States

### Exit codes

`task start`, `task reply`, and `task status` map the final task state directly to the process exit code:

| Exit code | Meaning |
| --- | --- |
| `0` | `DONE` (or command succeeded) |
| `1` | `FAIL` (worker failed, was killed by watchdog, or `task test` reported a failure) |
| `2` | Usage or configuration error (invalid flags, unknown task ID, `BLOCKED`, or missing optional script) |
| `3` | `NEED-INPUT` (worker needs clarification or approval before continuing) |
| `4` | `QUOTA` (worker hit a rate limit or quota error) |
| `5` | `RUNNING` (task is still active) |

### Task states

| State | Meaning |
| --- | --- |
| `RUNNING` | The worker process is currently executing. |
| `DONE` | The worker finished and reported `STATE: DONE`. |
| `NEED-INPUT` | The worker paused and requested input via `task reply`. |
| `FAIL` | The worker crashed, could not be spawned, returned an invalid result, or was killed by the watchdog. |
| `QUOTA` | The worker hit a rate or usage limit. |

---

## Walk-through: Start, Status, Reply

1. **Write the brief file first.** Keep task briefs in a scratch file such as `.task/<id>.md`. Follow the 8-item structure in [skills/task/brief-template.md](../skills/task/brief-template.md) (`task` appends the `STATE / ANSWER / EVIDENCE / MISSING` contract footer automatically).

2. **Start the task** in an isolated git worktree (`--write`) using a task row (`--row code`):

```bash
~/.claude/scripts/task start <id> .task/<id>.md --row code --write
```

`task start` runs in the foreground until the worker exits and prints the status block (or `NEED-INPUT` with exit code `3`).

3. **Check status later** (if you need to re-read the report or timing):

```bash
~/.claude/scripts/task status <id>
```

4. **Send a follow-up** when the task ends in `NEED-INPUT` (up to 2 replies per task):

```bash
~/.claude/scripts/task reply <id> "Proceed with option A and run the unit tests."
```

---

## Subcommands

### `task start`

Start a new task in the foreground and wait for the worker to exit.

**Syntax**

```text
task start [<worker>] <id> <brief-file>
  [--model MODEL] [--effort {low,medium,high,xhigh}]
  [--row {code,hard,inventory,mechanical,review,search}]
  [--pinned] [--write] [--cwd CWD]
  [--timeout TIMEOUT] [--stall STALL] [--cmd-stall CMD_STALL]
  [--watch WATCH]
```

You can invoke `start` in two forms:
- **Row form**: `task start <id> <brief-file> --row <row>` — picks the first enabled worker/model in `<row>` and falls back automatically if one is off or quota-blocked.
- **CLI (`<worker>`) form**: `task start <worker> <id> <brief-file> [...]` — targets a specific CLI (`codex`, `opencode`, `gemini`, `perch`, or `relay`).

**Flags**

| Flag | Default | Meaning |
| --- | --- | --- |
| `--row` | Derived from `--model` or `<worker>` | Task row (`code`, `hard`, `inventory`, `mechanical`, `review`, `search`). Required when neither `<worker>` nor `--model` is given. |
| `--model` | Resolved from `--row` or `<worker>` | Model alias (e.g. `luna`, `sol`, `flash`, `hydrogen`, `argon`, `carbon`, `deepseek`, `deepseek-free`, `chatanywhere`, `perch-deepseek`, `sonnet`, `relay-sonnet`, `relay-haiku`, `relay-opus`) or full model ID. |
| `--effort` | Codex config default (`None`) | Codex reasoning effort: `low`, `medium`, `high`, or `xhigh`. |
| `--pinned` | `false` | Disable fallback; fail with exit code `2` if the requested worker/model is off. Requires both `<worker>` and `--model`. Required for `perch-deepseek` and `relay-*`. |
| `--write` | `false` | Create a new git worktree at `<repo>/.worktrees/<id>` on branch `task/<id>`. Requires `--cwd` (or current directory) to be inside a git repository. |
| `--cwd` | Current directory | Working directory for the worker. |
| `--timeout` | `1800` | Maximum run duration in seconds (positive integer). |
| `--stall` | Per-alias (`180`, `300`, or `900`) | Seconds without output/watch-file activity before killing the worker (`900` for `perch-deepseek`; `300` for `sol`, `chatanywhere`, `hydrogen`, `argon`, `carbon`, `relay-*`; `180` for others). |
| `--cmd-stall` | `1800` | Watchdog threshold in seconds for a stalled command/subprocess (positive integer). |
| `--watch` | `[]` | File path to monitor for activity to reset the stall timer. May be passed multiple times; relative paths resolve against the work directory. |

**Example**

```bash
~/.claude/scripts/task start fix-export .task/fix-export.md --row code --write --watch .task/export.log
```

**Output and exit code**

Prints `<worker> - <alias> - <id> | <STATE>` followed by `FALLBACK:` (if switched), `HINT:` (on `QUOTA`), `ANSWER:`, `EVIDENCE:`, `MISSING:`, and `RAW: <log-path>`. Exits with `0` (`DONE`), `3` (`NEED-INPUT`), `1` (`FAIL`), `4` (`QUOTA`), `5` (`RUNNING`), or `2` (`BLOCKED` / usage error).

---

### `task reply`

Send a follow-up message to an existing worker session (maximum 2 replies per task).

**Syntax**

```text
task reply <id> <message> [--timeout TIMEOUT] [--stall STALL] [--cmd-stall CMD_STALL] [--watch WATCH]
```

**Flags**

| Flag | Default | Meaning |
| --- | --- | --- |
| `--timeout` | Half of task's `timeout` (min `1`, default `900`) | Maximum run duration in seconds (positive integer). |
| `--stall` | Per-alias default (`180`, `300`, or `900`) | Seconds without activity before watchdog kill (positive integer). |
| `--cmd-stall` | Task's saved `cmd_stall` (`1800`) | Watchdog command-stall threshold in seconds (positive integer). |
| `--watch` | Appended to task's `watch_paths` | Additional path(s) to monitor for activity. Repeatable. |

**Example**

```bash
~/.claude/scripts/task reply fix-export "Use the staging schema and re-run the dry run."
```

**Output and exit code**

Prints the updated status block (same format as `task start`) and exits with the new state's code (`0`, `1`, `3`, `4`, `5`), or `2` if the task is still `RUNNING`, has no session ID, or has reached the 2-reply limit.

---

### `task status`

Print a task's current state, report fields, timing (when available), and raw log path.

**Syntax**

```text
task status <id>
```

**Example**

```bash
~/.claude/scripts/task status fix-export
```

**Output and exit code**

Prints `<worker> - <alias> - <id> | <STATE>`, report fields (`ANSWER`, `EVIDENCE`, `MISSING`, or `PARTIAL`/`FILES` on watchdog salvage), `timing: first output <X>s, total <Y>s` (if recorded), and `RAW: <log-path>`. Exits with the task state's exit code (`0`, `1`, `3`, `4`, `5`), or `2` if `<id>` does not exist.

---

### `task list`

List all recorded tasks in the state directory.

**Syntax**

```text
task list
```

**Example**

```bash
~/.claude/scripts/task list
```

**Output and exit code**

Prints one line per task: `<worker> - <alias> - <id> | <STATE> | <updated-utc-timestamp>`. Exits `0`.

---

### `task resolve`

Show which worker CLI and model alias would be selected for a given row based on current switches and quota state.

**Syntax**

```text
task resolve {code,hard,inventory,mechanical,review,search}
```

**Example**

```bash
~/.claude/scripts/task resolve code
```

**Output and exit code**

Prints `<worker> - <alias>` and exits `0`, or exits `2` if every candidate in that row is off or quota-blocked.

---

### `task workers`

Display the current `on`/`off` switch state for all worker CLIs and model aliases.

**Syntax**

```text
task workers
```

**Example**

```bash
~/.claude/scripts/task workers
```

**Output and exit code**

Prints each CLI (`codex`, `opencode`, `gemini`, `perch`, `relay`) and its indented model aliases with `on` or `off` (noting that `chatanywhere` has its own switch not tied to `codex`). Exits `0`.

---

### `task on`

Enable a worker CLI or model alias in the switch file.

**Syntax**

```text
task on <name>
```

Turning a CLI on enables all of its model aliases except `chatanywhere` (which has its own switch).

**Example**

```bash
~/.claude/scripts/task on luna
```

**Output and exit code**

Prints nothing on success and exits `0`. Prints an error to stderr and exits `2` if `<name>` is unknown.

---

### `task off`

Disable a worker CLI or model alias in the switch file.

**Syntax**

```text
task off <name>
```

Turning a CLI off disables all of its model aliases except `chatanywhere` (which has its own switch).

**Example**

```text
~/.claude/scripts/task off argon
```

**Output and exit code**

Prints nothing on success and exits `0`. Prints an error to stderr and exits `2` if `<name>` is unknown.

---

### `task test`

Run a quick smoke test against one or more named workers in an isolated temporary directory.

**Syntax**

```text
task test <worker> [<worker> ...] [--resume] [--verbose]
```

Valid `<worker>` names: `luna`, `sol`, `chatanywhere`, `flash`, `hydrogen`, `argon`, `carbon`, `sonnet`, `codex`, `opencode`, `gemini`, `perch`, `perch-deepseek`, `deepseek-free`, `relay-sonnet`, `relay-haiku`, `relay-opus`, `relay`.

**Flags**

| Flag | Default | Meaning |
| --- | --- | --- |
| `--resume` | `false` | Run a second turn (`reply`) to verify session resume. |
| `--verbose` | `false` | Print each worker's full JSON report. |

**Example**

```bash
~/.claude/scripts/task test flash luna --resume
```

**Output and exit code**

Prints one line per worker (`ok`, `SKIP` on quota, or `FAIL`) with timings and switch state, followed by `SUMMARY ok=<N> fail=<N> skip=<N>`. Exits `0` if `fail == 0`, `1` if any worker failed, or `2` on invalid arguments.

---

### `task quota`

Show quota signals and limits for all configured workers (delegates to `scripts/quota.py`).

**Syntax**

```text
task quota [--md]
```

**Flags**

| Flag | Default | Meaning |
| --- | --- | --- |
| `--md` | `false` | Format output as a Markdown table instead of plain text. |

**Example**

```bash
~/.claude/scripts/task quota --md
```

**Output and exit code**

Prints the quota table to stdout and exits `0`.

---

### `task usage`

> [!NOTE]
> `task usage` is **only available when the optional `daily-usage.py` script is installed** (in the plugin scripts directory or `~/.claude/scripts/daily-usage.py`). Otherwise it prints `task usage: usage reports are not part of this plugin` to stderr and exits `2`.

Show daily Claude and worker usage since the last mark.

**Syntax**

```text
task usage [--mark] [--plain]
```

**Flags**

| Flag | Default | Meaning |
| --- | --- | --- |
| `--mark` | `false` | Set the usage mark to the current time. |
| `--plain` | `false` | Print plain text instead of a Markdown table. |

**Example**

```bash
~/.claude/scripts/task usage --plain
```

**Output and exit code**

Prints the usage report (or updates the mark) when `daily-usage.py` is present; exits `2` when the optional script is not installed.

---

### `task clean sessions`

Delete transcripts of Claude Code sessions archived ("deleted") in the VS Code Claude Code extension, along with their companion directories and file-history, while keeping synced per-host tombstones (delegates to `scripts/clean-sessions.py`).

**Syntax**

```text
task clean sessions
```

**Example**

```bash
~/.claude/scripts/task clean sessions
```

**Output and exit code**

Prints a summary of removed transcripts/history and exits `0` (or non-zero if `clean-sessions.py` encounters an error).

---

## Where Things Live

- **Task state registry**: `$TASK_STATE_DIR` (default `~/.claude/state/tasks`), storing `<id>.json` per task.
- **Worker switch file**: `~/.claude/state/task-workers.json` (in the parent directory of `$TASK_STATE_DIR`), alongside `task-chatanywhere-quota.json`.
- **Logs and salvage reports**: `$TASK_LOG_DIR` (default `~/.claude/logs/task-runner`), storing `<id>.log`, `<id>.log.err`, `<id>.log.report` (written on watchdog timeout/stall), and the optional `agy.lock` serial-mode lock.
- **Write worktrees**: `<repo>/.worktrees/<id>` on branch `task/<id>` when `--write` is passed to `task start`.

---

## Troubleshooting

- **`BLOCKED: <model> is off` or `all workers for row <row> are off`**: Run `task workers` to inspect disabled CLIs/models and re-enable one with `task on <name>`.
- **`QUOTA` (exit code `4`)**: The worker hit a rate or usage limit. Disable that alias with `task off <alias>` (as suggested by `HINT: task off <alias>`) so row-based tasks fall back to another worker. Note that `chatanywhere` is automatically skipped for the rest of the UTC day after a `QUOTA` result.
- **Watchdog killed the task (`killed: timeout ...` or `killed: stall ...`)**: Inspect the `RAW:` log and `<id>.log.report` salvage file for partial output and modified files. For slow or quiet commands, increase `--timeout`, `--stall`, or `--cmd-stall`, or pass `--watch <path>` pointing to a log file the command updates regularly.
- **`cannot run worker CLI: ...` (exit code `1`)**: The requested CLI (`codex`, `opencode`, `agy`, `perch`, or `claude`) is missing from `PATH` or failed to start. Verify installation and run `task test <worker>`.
- **`relay configuration missing: RELAY_BASE_URL is not set` or `relay authentication key unavailable in Keychain`**: Set `RELAY_BASE_URL` and configure the Keychain entry or `TASK_RELAY_KEY_CMD` before using `relay` (see [docs/relay.md](relay.md)).
