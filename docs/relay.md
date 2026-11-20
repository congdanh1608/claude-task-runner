# Relay Worker Setup

relay is for using an external Claude API token/gateway you already have.

The `relay` worker executes the Claude CLI through an external Claude API gateway. It runs only when you explicitly name it with `--pinned` in a task brief (`task start relay <id> <brief> --model relay-sonnet --pinned`). It is in no automated row and is never a fallback target.

## Setup Steps

Setting up `relay` requires 3 steps:

### 1. Set the Gateway URL

Export `RELAY_BASE_URL` in your shell profile (such as `~/.zshenv`, `~/.bashrc`, or `~/.zshrc`):

```bash
export RELAY_BASE_URL="https://<your-gateway>/api"
```

Replace `https://<your-gateway>/api` with your actual gateway base URL.

### 2. Store the API Token

Store the gateway authentication token securely so it never enters shell history or command-line arguments:

#### On macOS (Keychain)

Use `security add-generic-password` to prompt for the password interactively:

```bash
security add-generic-password -U -s task-relay-key -a "$USER" -w
```

When prompted, paste `<your-token>`. `task.py` retrieves the token using:

```bash
security find-generic-password -s task-relay-key -w
```

#### On Linux or other environments (`TASK_RELAY_KEY_CMD`)

Set `TASK_RELAY_KEY_CMD` to a command that outputs your token to stdout:

```bash
export TASK_RELAY_KEY_CMD="secret-tool lookup service task-relay-key"
# or: export TASK_RELAY_KEY_CMD="pass show api/relay"
# or: export TASK_RELAY_KEY_CMD="op read op://private/relay/credential"
```

`task.py` executes this command to obtain the token when running the worker.

### 3. Turn on the Relay Worker

Enable the relay worker in `task`:

```bash
~/.claude/scripts/task on relay
```

## Model Aliases

When dispatching tasks to `relay`, use one of the built-in model aliases:

| Alias | Upstream Model | Description |
|---|---|---|
| `relay-sonnet` (default) | `claude-sonnet-5-5` | General coding and tasks |
| `relay-opus` | `claude-opus-5-5` | Deep reasoning |
| `relay-haiku` | `claude-haiku-5-5` | Fast lightweight chores |

You can also pass any model ID supported by your gateway that contains letters, digits, dots, underscores, colons, or hyphens:

```bash
~/.claude/scripts/task start relay <id> <brief> --model <custom-model-id> --pinned
```

## Testing

Verify your gateway URL and token configuration before running real tasks:

```bash
~/.claude/scripts/task test relay-sonnet
```

(or `~/.claude/scripts/task test relay`).

`task test` runs in an isolated temporary directory and works even when `relay` is turned off.

## Disabling or Removing

To temporarily disable relay:

```bash
~/.claude/scripts/task off relay
```

To permanently remove credentials and configuration:

1. Delete the Keychain entry (macOS):
   ```bash
   security delete-generic-password -s task-relay-key
   ```
2. Remove `RELAY_BASE_URL` (and `TASK_RELAY_KEY_CMD` if used) from your shell startup file.

## Limits and Security

- **Gateway visibility**: The operator of your external API gateway can read every prompt, brief, and response. Never put secrets, passwords, API keys, or confidential credentials into a task brief.
- **Sandboxing**: `relay` runs Claude CLI inside an isolated configuration directory (`~/.claude/state/relay-config`), with restricted tool permissions (`Read`, `Grep`, `Glob`, and `Edit`/`Write` when `--write` is enabled; shell/Bash is disabled), safe mode enabled, and denied access to sensitive file patterns (`.env*`, `**/*.pem`, `**/*.keystore`, `**/*service-account*.json`).
- **No fallback**: If the gateway is unreachable, returns an error, or the token expires, the worker halts immediately with `FAIL` or `QUOTA`. It does not fall back to other worker CLIs.
