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
- Run `python3 scripts/check-sensitive.py --all --history` with `PLUGIN_DENYLIST` (file with one forbidden literal per line) and `PLUGIN_NAME_DIRS` (directories whose subfolder names must never appear) set.
- Releases go only through `scripts/release.sh`, which refuses to run without those two variables and scans the whole history before it commits.

## Documentation

- [Worker Configuration and Management](docs/workers.md): switches, model mappings, fallback tables, and optional workers.
- [Relay Worker Setup](docs/relay.md): configuring external Claude API gateways, credentials, model aliases, and limits.
