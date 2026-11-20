# Brief template

Every brief has these 8 items. `task` appends the result contract (STATE / ANSWER / EVIDENCE / MISSING) itself; do not repeat it.

Every brief states an estimated duration. Commands over 2 minutes must print progress at least every 60 seconds and write output to a log passed via `--watch`; do not run them silently.

1. **Estimated duration; goal and done criteria**: give the duration, then state what must be true when finished, checkably.
2. **Allowed scope**: what the worker may touch ("anything the project's own scripts do", or named paths).
3. **Stop list** (5 items at most): what it must not do, with the machine named ("install nothing on the dev machine"). Every brief also forbids `git stash`, `clean`, `checkout`, `restore` and `reset` on the user's working tree: uncommitted work lives there.
4. **Independent steps**: a step it cannot do becomes `skipped: <reason>`; the rest continue.
5. **Inputs already known**: files, command outputs, prior findings, the error from an earlier task.
6. **Facts**: it finds them itself in code and docs (point at the project's CLAUDE.md / AGENTS.md / runbook) before asking.
7. **Order**: preflight (list ALL conflicts and missing prerequisites) → do → self-review (read own `git diff` against every done criterion, revert out-of-scope edits, run related tests/analyzer, fix; list what was checked and what is unsure in EVIDENCE).
8. **Prod-changing step**: report the exact plan as NEED-INPUT and wait for `task reply`.

## Filled example (generic)

```
Goal: the nightly export fails with "unknown column". Find the cause and fix it in <repo>.
Done when: `<script> --dry-run` exits 0 and the unit tests for the export module pass.

Scope: files under <repo>/src/export and its tests. Anything <repo>'s own scripts do is allowed.
Stop list:
- no schema or data change on any live database (plan it as NEED-INPUT)
- install nothing on this machine
- no change outside <repo>

Steps (independent; if one cannot be done write `skipped: <reason>` and go on):
1. Reproduce with `<script> --dry-run`.
2. Find the cause.
3. Fix it and add a test.
4. Run the test suite named in <repo>/CLAUDE.md.

Known: the error appears since the last migration (migrations/0042); the log is in .task/export.log.
Find build and test commands yourself in <repo>/CLAUDE.md before asking.
Order: preflight (list ALL conflicts and missing prerequisites in one go) → fix → self-review (read own `git diff` against every done criterion, revert out-of-scope edits, run related tests/analyzer, fix; list what was checked and what is unsure in EVIDENCE).
Applying the fix to production: stop with NEED-INPUT and the exact plan.
```
