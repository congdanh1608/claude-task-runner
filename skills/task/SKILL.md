---
name: task
description: Delegate a task to a worker (Codex, OpenCode, Gemini, Perch, or Relay) through the `task` CLI and follow it to a result. Use when work is more than a known 2-line edit or one known command, or when the user names a worker or model.
---

# task: brain + resumable workers

You are the coordinator. Workers (Codex, OpenCode, Gemini, Perch, and Relay) execute through `task`. A worker session is resumable, so a fix is a reply, not a new task.

`task` is not on PATH: always run it as `~/.claude/scripts/task …` (every `task` below means that path).

## Do it yourself, or delegate
- Yourself: read the files, logs and configs you need to write a correct brief; a known edit of 2 lines or fewer; up to 3 read-only commands with bounded output to answer directly (production SSH is delegated; see below).
- Delegate: everything else (search across files, code changes, tests, ops, deploys, reviews). One task = one goal with its own done criteria.
- A bug fix is never a "known 2-line edit", however small the diff: it goes to a worker under **Bug fixes** below.
- Asked why something was or was not delegated: cite the rule that applied.

## Worker table
| Row (`--row`) | Work | Model | `--effort` |
|---|---|---|---|
| search | search, web research, long docs, screenshots | Flash medium | n/a (in model name) |
| search | light chores, read-only: gather the files and facts a brief needs (when that takes more than ~3 reads), triage a long log or a failed task's RAW log, check a screenshot against the spec, check dependency/SDK versions and changelogs, summarize a spec or report | Flash medium | n/a (in model name) |
| inventory | read-only listing with file:line, no reasoning: list settings, find usages, compare two repos, code map | Flash high | n/a (in model name) |
| mechanical | scoped code with a clear spec, 1–3 files: UI/copy/layout fix at a named spot, small feature, field/endpoint/simple screen | Hydrogen medium (then Luna, DeepSeek) | n/a (in model name) |
| mechanical | write tests for existing code (standalone builds/suites/analyzers go to Luna, below) | Hydrogen medium | n/a (in model name) |
| mechanical | docs, test cases, deploy notes, changelogs, help/guides, scripts with clear input/output, pattern edits, renames | Hydrogen medium | n/a (in model name) |
| — | `task test <worker> [<worker> ...] [--resume] [--verbose]` checks each named worker once by default; `--resume` adds a second turn, even when its switch is off | Named worker | n/a |
| code | multi-file feature or refactor; work that needs running the app to know it is right | Hydrogen medium (then Luna, DeepSeek, Sol) | n/a (in model name) |
| code | debug by running, measuring or reproducing (layout clip, crash); trace a data flow, root cause, feasibility | Hydrogen medium; after a FAIL Carbon low (`--model carbon`) | n/a (in model name) |
| code | deploy, environment setup, multi-branch merges, ops on a server; anything Hydrogen FAILed twice | Luna (`--model luna --row code`) | `low` (ops) / medium (config) |
| review | diff review (code), re-review | Carbon low (then Sol, Luna, DeepSeek) | n/a (in model name) |
| hard | plan or spec review, final branch review | Carbon low (then Sol, Luna, DeepSeek) | n/a (in model name) |
| hard | risky change: prod, migration, payments, auth | Carbon low; for the diff add a Sol `high` review (`--model sol --effort high`) | n/a / `high` |
| — | hard bug: after a Carbon FAIL, or async/concurrency, ordering algebra, sync reconciliation | Sol, named: `--model sol --effort xhigh` (slow: `--timeout 7200`) | `xhigh` |

- `--effort` sets Codex reasoning for that task (`medium` = the Codex config default; OpenCode and Gemini models ignore it); Gemini effort is in the model name.
- `--row` picks the model only: pass the row's `--effort` explicitly when the model is a Codex one.
- **Priority with every default worker on: Gemini → Codex → OpenCode** (`chatanywhere` last); the row order above already follows it. A turned-off or out-of-quota worker falls to the next one in its row.
- Hydrogen codes, Carbon reviews diffs, plans and specs (Argon is slow and kept only for `--model argon --pinned`); Luna and DeepSeek are fallbacks and Luna keeps ops, deploy and builds. Every coding brief ends with a self-check that includes "revert changes outside the brief". Flash only reads and writes docs: hand it light read-only chores instead of reading many files in main; never use it as a code fallback.
- Run Argon and Carbon in the background and do not wait on them; Flash and independent DeepSeek tasks can run in parallel with other work.
- A DeepSeek task that runs tests or builds keeps the default `--timeout 1800`; never lower it.
- One brief = one row. Split mixed work: inventory first, then reasoning on its output; data fix and device test apart; BE and FE apart; one task per unrelated bug.
- When a CLI is off (`task off gemini` / `task on gemini`, same for `codex`, `opencode`):

| Off | Code, review, hard, mechanical rows run on | Search, inventory |
|---|---|---|
| Gemini | Codex (Luna, Sol), then OpenCode (code: Luna, DeepSeek, Sol) | Luna, then DeepSeek |
| Codex | Gemini (Hydrogen, Carbon), then OpenCode (Argon last in review) | Flash |
| OpenCode | Gemini, then Codex | Flash |
| Gemini + Codex | OpenCode (DeepSeek) | DeepSeek |
| all three | Claude agents (Claude mode below) | none; ask the user |

- A brief states the contract wherever a value is ambiguous (units, precision, ordering), so review rounds do not oscillate.

Sonnet (`claude-sonnet-5-5-medium`) runs on the `gemini` worker and is pinned-only (shared pool): `task start gemini <id> <brief> --model sonnet --pinned`. The worker name is always `gemini`, never `agy` (`agy` is only the binary behind it). Hydrogen, Argon and Carbon share the `gemini` Antigravity pool, so check `task quota` when a run is long.

### Optional workers (run only when named or switched on)
- `deepseek-free` (`opencode/deepseek-v4.1-flash-free`): requires the `opencode` CLI; runs only when the user names it (`task start opencode <id> <brief> --model deepseek-free`). A non-pinned start that ends QUOTA or fails with a provider error retries once on paid `deepseek`; `--pinned` never hops.
- `perch-deepseek` (`deepseek-flash-starter`): requires the `perch` CLI (signed in); runs only when the user names it with `--pinned` (`task start perch <id> <brief> --model perch-deepseek --pinned`). Never in a row or fallback, and defaults to `--stall 900`.
- `relay` (`relay-sonnet`, `relay-haiku`, `relay-opus`, or a custom gateway model ID): requires the `claude` CLI, `RELAY_BASE_URL` (e.g. `https://<your-gateway>/api`), and a token in Keychain item `task-relay-key` or `TASK_RELAY_KEY_CMD`. Runs only when the user names it with `--pinned` (`task start relay <id> <brief> --model relay-sonnet --pinned`); never in a row or fallback.
- `chatanywhere` (`gpt-6-luna` via `codex` CLI): requires Keychain item `task-chatanywhere-key` (or `TASK_CHATANYWHERE_KEY_CMD`) and optional `CHATANYWHERE_BASE_URL`. Controlled independently via `task on|off chatanywhere`; sits at the tail of `code`, `mechanical`, `search`, and `inventory` rows.

Hard rule for every brief and worker: no secrets and no sensitive files (credentials, `.env`, keystores, key/cert files, service-account JSON) in any brief, or in the working directory given to `perch-deepseek`.

A user-named model uses `--pinned`: no substitute. If it fails or is out of quota, stop and report the reason.

## Claude mode (Gemini, Codex and OpenCode all off)
When `task workers` shows `gemini`, `codex` and `opencode` all off, the Hydrogen/Argon/Carbon/Luna/Sol/DeepSeek rows run as background Claude subagents (`Agent`, `run_in_background: true`, the same brief). Flash rows stay on `task`.

| Row | `subagent_type` |
|---|---|
| code: ops, deploy, build, merges, docs, test setup; mechanical | `task-runner:haiku-worker` |
| code: normal code, tests, read-only reasoning; hard: hard bug, risky change | `task-runner:sonnet-worker` |
| hard: review of a diff | `task-runner:sonnet-reviewer` |
| hard: write or review a spec or plan | `task-runner:opus-worker` |

Same loop limits: a FAIL moves one step up (`haiku-worker` → `sonnet-worker`), then stop and ask. Keep deep debugging off `opus-worker`. Any CLI back on (`task on <cli>`) → the normal table.

## Run
Every `task start` / `task reply` is its own Bash call with `run_in_background: true` and `description` = `{cli} - {model short} - {id}` (so `/tasks` shows one named row per worker). Never use shell `&`, `nohup`, `disown` or several workers in one command.
1. Pick a short kebab-case id. Write the brief from `brief-template.md` to `.task/<id>.md` with the Write tool, then start with a plain `~/.claude/scripts/task start ...`; include its estimated duration. For a `--row` start, first run `~/.claude/scripts/task resolve <row>` and use its output plus ` - <id>` as the Bash description. In any Bash call, `rm`/`cp`/`mv`/redirect targets are literal paths, not `$VAR`. If `start` cannot read the brief, check it exists before retrying.
2. A foreground start is for an estimated ≤5-minute task (review, SSH check, small fix) only when main has nothing else to do meanwhile. Set the Bash timeout to ≤10 minutes and `task --timeout` below the Bash timeout. Long or parallel tasks stay background.
   Background means the Bash tool with `run_in_background: true`; shell `&` hides the worker from `/tasks` and loses the completion notification. Parallel workers = several Bash calls in the same turn, one per worker.
3. A retry or escalation uses `<id>-<new model short>` (e.g. `fix-login-crash-sol`). The display name is `{cli} - {model short} - {id}`. Set the Bash tool description to this display name for every `task start` and `task reply` call; for `--row` starts, use the output of `task resolve <row>` plus ` - <id>`. Start with `task start <id> <brief-file> --row search|inventory|code|review|hard|mechanical [--write] [--cwd DIR] [--timeout SEC] [--stall SEC] [--cmd-stall SEC] [--watch PATH ...]`. Or name a worker and model: `task start <cli> <id> <brief-file> --model M [--row ROW] [--pinned]`, where `M` can be a model short name (`luna`, `sol`, `chatanywhere`, `deepseek`, `deepseek-free`, `perch-deepseek`, `relay-sonnet`, `relay-haiku`, `relay-opus`, `flash`, `hydrogen`, `argon`, `carbon`, `sonnet`) or a full model ID. `--pinned` keeps a named model fixed and blocks if it is off. `--write` runs in a git worktree `.worktrees/<id>` where the worker commits; read-only tasks run in the repo.
   Independent tasks (no shared files, no order between them) start in the same turn as parallel background calls; tasks touching the same files run one after another. `gemini` worker tasks (Flash, Sonnet) may run in parallel; if parallel `gemini` runs fail or corrupt each other, set `TASK_AGY_SERIAL=1` to serialize them (waits up to `TASK_LOCK_WAIT`, default 600 s, then FAILs).
4. Do not poll. On the completion notification run `task status <id>`. Exit codes: `0` DONE, `3` NEED-INPUT, `1` FAIL, `4` QUOTA, `5` RUNNING, `2` usage error. `task list` shows all tasks.
5. DONE: check the EVIDENCE against the done criteria (`git diff --stat` plus the reviewer's verdict; open a full hunk only where the report is unclear; for a worktree, merge it after review). NEED-INPUT: answer from what you know or ask the user, then `task reply <id> "<answer>"` (background, same session), using the task's display name as the Bash description. A prod-changing plan needs the user's approval before you reply.
   A `FAIL` beginning `killed: stall` is a watchdog limit, not a model failure; follow **Watchdog limits**.

## Bug fixes
1. Investigate: read-only worker by the table (Hydrogen; Carbon after a FAIL; Sol `xhigh` for hard bugs). End the report with a `FIX TASK:` section: the failing test to write, the one change to make, the verify command.
2. Fix: a new `--write` task (Hydrogen, row `mechanical` when the `FIX TASK` is bounded, else row `code`) built from that section. Done criteria: the test fails on the old code (output quoted), the fix, the test passes, related tests pass.
3. Review: Carbon on the diff (`--row review`); add a Sol review when the fix is risky. Docs and changelog updates go in the same fix task.
A bug that truly cannot be tested (pure native or visual): the fix brief says so and names the manual check for the user.

## Builds, tests, emulator
- Standalone builds, suites, analyzers and "does it build?" checks go to Luna (`--model luna --row code`). An implementation task stays with its assigned Hydrogen (or fallback) worker, which runs relevant tests/analyzers as done criteria. Deploys and ops go to Luna (`--model luna --row code --effort low`).
- Production SSH may run on any worker on the owner's own paid accounts (Codex, Gemini, OpenCode paid models, Claude mode), including a normal row start when the brief names the SSH check. It never runs on third-party gateway or free workers: `perch-deepseek`, `deepseek-free`, `relay-*`, `chatanywhere`. Pick the worker by the usual table (an ssh check is a short `code` row task; Codex Luna is the default for ops and deploys). Workers may use a documented SSH alias (credential use, never inspection/printing). SSH briefs name the host/alias, purpose, permitted actions and needed non-secret output fields; bound output by bytes and lines. A permission denial is a blocker, never a reason to copy keys or switch provider.
- The brief says: on a build or test failure, read the error and fix it within the brief's scope, at most 2 attempts; still failing → stop with FAIL, the error excerpt (≤30 lines) and a root-cause hypothesis.
- That FAIL escalates to Carbon with a new id (`<id>-carbon`), then Sol (`<id>-sol`) if Carbon also fails, read-only first: the brief carries the excerpt and hypothesis. Main reads the investigator's conclusion, not the logs, then decides the fix task. Sol FAIL → stop and ask the user.

Plugin paths in briefs: resolve the newest cached version first (`ls -d ~/.claude/plugins/cache/<marketplace>/<plugin>/*/ | sort -V | tail -1`) and write the full path; old versions stay in the cache.

## Optional integrations (only when installed)
- If `superpowers` skills are installed, map their "dispatch a subagent" / systematic-debugging / review steps to `task` workers using the Worker table (or Claude mode when all three CLIs are off).
- If `app-testing` is installed, device UI tests (emulator/simulator) can be delegated to a background `task-runner:haiku-worker` subagent following its `mobile-device-test` skill while keeping builds and installs on Luna.
- If `design-tools` is installed, HTML mockups of app screens can follow `design-tools:html-mockup` on a background `task-runner:haiku-worker` subagent.
- If a progress-ledger plugin is installed, batch confirmed state updates into existing Bash calls rather than issuing dummy work.

## Cheaper loops, same reviews
- Implementer brief ends with a self-check before reporting DONE: run the tests, tick every spec item, read its own `git diff`, fix what it finds. Missing self-check evidence on a code diff goes in the review brief, never a reply. For docs/test-case tasks without review, request missing substantive verification in one reply; accept equivalent self-check evidence.
- Review after the self-check: docs, notes and test cases only → the self-check is enough, main checks `git diff --stat`; every code diff → Carbon (`<id>-review`, `--row review`).
- Review brief carries the `base..HEAD` range, the spec/plan path and the changed-file list, so the reviewer does not explore the repo.
- Fix and re-review rounds continue the same session: `task reply <impl-id> "<findings>"` for the fix, `task reply <review-id> "fixed in <sha>, re-check"` for the re-review. A new task id only after the reply limit. Claude mode: `SendMessage` to the same agent instead of a new `Agent`.
Main stays thin: hand artifacts over as file paths, read reports not diffs, never write the code itself.
- Search before reading (`git grep -n`, `-l`) to find the right file and spot, then read whatever the brief needs to be right.
- Use `git diff --stat` / `git log --oneline -n 10` for worker results; the reviewer reads the full diff.

## Limits
- At most 2 `task reply` per task.
- FAIL: except for `killed: stall` below, escalate once with a NEW id (`<id>-<new model short>`, e.g. `fix-login-crash-carbon`): Hydrogen to Carbon, Carbon to Sol, Luna to Sol, Flash to Hydrogen, DeepSeek to Luna. The new brief carries the error and the evidence. A second FAIL: stop and tell the user.
- QUOTA: stop and ask the user with options (check `task quota` first), never switch on your own: **A** another CLI still has quota → `task off <this one>` and rerun the brief; **B** all out → `task off` all three and rerun on the matching Claude agent; **C** pause until the reset (time from `task quota`).
- Switches: `task workers` lists model and CLI state; `task off <name>` and `task on <name>` persist it for any CLI (`codex`, `opencode`, `gemini`, `perch`, `relay`) or model alias (`luna`, `sol`, `chatanywhere`, `deepseek-free`, `deepseek`, `perch-deepseek`, `relay-sonnet`, `relay-haiku`, `relay-opus`, `flash`, `hydrogen`, `argon`, `carbon`, `sonnet`). Turning a CLI off disables all its models. Off models follow the row order; all-off and pinned-off starts block.
- Never re-dispatch work whose result you have not read.

## Briefs
- Project facts (build/test commands, hosts, deploy scripts, protected units) come from the project's own `CLAUDE.md`, `AGENTS.md` or runbook; the brief points the worker there and does not copy them.
- Never put secret values in a brief; name the file or key instead.

## Watchdog limits

Every brief states an estimated duration. For commands over 2 minutes, write output to a log passed with repeatable `--watch` and print progress at least every 60 seconds.

Default `--stall`: 180 seconds for Luna, Flash, and DeepSeek; 300 seconds for Sol, Hydrogen, Argon and Carbon; 900 seconds for Perch. Start timeout defaults to 1800 seconds; reply timeout defaults to half the start timeout. An active command gets `--cmd-stall 1800` seconds without CPU, child, file, or watched-log activity before it is killed.

| Task | normal | `--timeout` |
|---|---:|---:|
| ssh check | <1m | 3m |
| search / read code | 1-5m | 10m |
| review diff | 2-8m | 15m |
| small fix + tests | 2-7m | 15m |
| feature + full suite | 10-25m | 40m |
| Flutter/Android build | 3-8m | 15m (first build 25m) |
| iOS archive | 5-12m | 25m |
| hard bug (Sol xhigh) | 30-100m | 7200 |
| deploy restart/migrate | 1-3m | 8m |
| deploy image build | 5-10m | 20m |
| long script (sweep/crawl/export) | items × per-item | estimate ×1.5 + `--watch <script log>` (required) |
| multi-step | sum | sum |

The table is a starting point, not a rule. Size `--timeout`, `--stall` and `--cmd-stall` to the task: scale up for a large repo, cold cache or first build, a slow host or rate-limited site, a high-effort model; scale down for a tiny known change. Base it on evidence (item count, a previous run's time in `task list`, the project's own timings) and write the chosen values plus a one-line reason in the brief (e.g. `limits: timeout 30m, stall 300s — first Gradle build, no cache`). Keep a hard cap: never omit `--timeout`, and stay within ~2× your estimate.

A `FAIL` beginning `killed: stall` is a watchdog limit, not a model failure. Check `partial:` and `files:`, then rerun once with corrected limits and/or `--watch`, resuming from partial output when supported. Do not escalate. A second stall kill means stop and ask the user.
