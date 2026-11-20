# task-runner

Resumable worker tasks for Claude Code: a brain session delegates coding, research, review, and mechanical chores to worker CLIs (`codex`, `opencode`, `agy` for Gemini) through a unified `task` command, with watchdog supervision, session resume, and automatic fallback.

## Requirements

- Python 3.9+
- `git`
- At least one worker CLI: `codex`, `opencode`, or `agy` (Gemini CLI)
- Optional: `perch`, `claude` (for relay)
- Developed on macOS; macOS Keychain is used only by optional workers (`relay`, `chatanywhere`). Linux and other environments are untested, but support `TASK_RELAY_KEY_CMD` and `TASK_CHATANYWHERE_KEY_CMD` to supply credentials from any command.

## Installation

Add the marketplace repository and install the plugin in Claude Code:

```bash
/plugin marketplace add congdanh1608/claude-task-runner
/plugin install task-runner@task-runner
```

To update to the latest release:

```bash
/plugin marketplace update task-runner
```

To uninstall:

```bash
/plugin uninstall task-runner@task-runner
```

## First Run

The plugin installs launcher scripts into `~/.claude/scripts/`.
After `/plugin install`, start a new Claude Code session (or reload plugins) so the SessionStart hook creates `~/.claude/scripts/task`.

Check the status of configured worker CLIs and model switches:

```bash
~/.claude/scripts/task workers
```

Test that a worker CLI can run and produce output:

```bash
~/.claude/scripts/task test <worker>
```

For example, test the Gemini Flash model or Codex Luna:

```bash
~/.claude/scripts/task test flash
~/.claude/scripts/task test luna
```

See [docs/workers.md](docs/workers.md) for full testing and configuration options.

### Make `task` easy to type

The hook installs the launcher at `~/.claude/scripts/task`, but `~/.claude/scripts` is not on `PATH` by default (the plugin never edits your shell rc files). Claude itself calls `~/.claude/scripts/task` by full path, so adding it to `PATH` is only for typing `task` directly in your own terminal.

The plugin cache lives at `~/.claude/plugins/cache/<marketplace>/task-runner/<version>/` (on Windows: `%USERPROFILE%\.claude\plugins\cache\<marketplace>\task-runner\<version>\`).

- **macOS (zsh is the default shell) and Linux (bash)**: append the following line to `~/.zshrc` (macOS) or `~/.bashrc` (Linux/bash), open a new terminal, and run `task workers`:

  ```bash
  export PATH="$HOME/.claude/scripts:$PATH"
  ```

  Alternatively, add an alias instead of editing `PATH`:

  ```bash
  alias task="$HOME/.claude/scripts/task"
  ```

- **Windows (untested)**: `scripts/install-launchers.sh` generates `~/.claude/scripts/task` as a `#!/bin/bash` script, so use **WSL** or **Git Bash** (untested) with the same `export PATH="$HOME/.claude/scripts:$PATH"` line in that shell's rc file (`~/.bashrc` or `~/.zshrc`). Native **PowerShell** and **CMD** are not supported (and untested): in addition to the bash launcher, `scripts/task.py` imports POSIX-only APIs (`fcntl`, `os.killpg`, `signal.SIGHUP`, and `ps`), so running `python3 <plugin cache>\scripts\task.py ...` directly in native Windows Python is not a working fallback.

Check that `task` is on your `PATH`:

```bash
command -v task
```

## Quick command reference

| Command | What it does |
| --- | --- |
| `task workers` | Show `on`/`off` switches for all worker CLIs and model aliases |
| `task on <name>` / `task off <name>` | Enable or disable a worker CLI or model alias |
| `task test <worker> [--resume]` | Smoke-test a worker (and optionally session resume) |
| `task start <id> <brief> --row <row>` | Start a task in the foreground and wait for its status report |
| `task reply <id> <message>` | Send a follow-up message to a `NEED-INPUT` session |
| `task status <id>` | Print a task's current state, report, timing, and raw log path |
| `task list` | List all recorded tasks and their states |
| `task quota [--md]` | Show quota signals and limits for all configured workers |
| `task clean sessions` | Delete transcripts and file-history for archived Claude sessions |

Full syntax, flags and exit codes: [docs/commands.md](docs/commands.md). Which model each row uses: [docs/models.md](docs/models.md).

## Built-in Skills

The plugin registers three skills:

- `task`: Delegate tasks to worker CLIs with automatic fallback, watchdog supervision, and resumable sessions.
- `quota`: Check real-time quota signals and limits across all configured workers and models.
- `skill-updates`: Check upstream updates for user-level copied skills and marketplace plugins.

## Optional Integrations

Integrations with external tools or plugins (such as custom statuslines, progress ledgers, test runners, or superpowers workflows) are strictly optional and only activate if those plugins are installed.

## State and Logs

- Task registries and resumable session states: `~/.claude/state/tasks`
- Worker output and raw execution logs: `~/.claude/logs/task-runner`
- Worker switch configuration: `~/.claude/state/task-workers.json`

## Relay

`relay` runs the Claude CLI through an external Claude API gateway; you bring your own gateway URL and API token (see [docs/relay.md](docs/relay.md)); it only runs when you name it.

## Before you commit or release

- Never commit keys, tokens, passwords, private URLs, or gateway hosts.
- Never include personal, client, or project names, hostnames, IPs, emails, or absolute machine paths.
- Commit only with a GitHub noreply identity (`<id>+<user>@users.noreply.github.com`) and write in English only.
- Enable the pre-commit hook in each clone: `git config core.hooksPath .githooks`.
- Run `python3 scripts/check-sensitive.py --all --history` with `PLUGIN_DENYLIST` (file with one forbidden literal per line) and `PLUGIN_NAME_DIRS` (directories (globs allowed, for example `/work/clients/*`) whose immediate subfolder names must never appear anywhere in the repo or its history; `PLUGIN_NAME_ALLOW` is an optional comma-separated list of names to ignore (such as this repo's own name)) set.
- Releases go only through `scripts/release.sh`, which refuses to run without those two variables and scans the whole history before it commits.

## Documentation

- [Command Reference](docs/commands.md): full `task` subcommand syntax, flags, exit codes, walk-through, and troubleshooting.
- [Model Assignment](docs/models.md): how task rows map to model aliases, fallback order, overrides, and quota handling.
- [Worker Configuration and Management](docs/workers.md): switches, model mappings, fallback tables, and optional workers.
- [Relay Worker Setup](docs/relay.md): configuring external Claude API gateways, credentials, model aliases, and limits.
