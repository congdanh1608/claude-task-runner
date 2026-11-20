---
name: skill-updates
description: Check user-level copied skills and marketplace plugins for upstream updates, and apply or bootstrap them.
---

# Skill updates

Check user-level copied skills and marketplace plugin updates.

1. In one Bash call, run `~/.claude/scripts/skill-updates.py check` and show both tables.
2. Ask which skills the user wants updated. Run `~/.claude/scripts/skill-updates.py apply <skill>` for each selected skill.
3. For `new: not in manifest`, ask the user for the upstream repository URL (or whether it is local-only), then run `~/.claude/scripts/skill-updates.py bootstrap <skill> --repo URL [--path DIR] [--branch BRANCH]` or `~/.claude/scripts/skill-updates.py bootstrap <skill> --local`.
4. After the user edits a skill, no setup is needed; edits are detected and kept by `apply`.
5. Show marketplace update commands from the table. Run them only if the user says to.

## Conflict resolution (worker)

On conflict, main puts the base, local, and upstream paths in a Luna brief. Luna merges compatible changes into a staged directory, then runs `~/.claude/scripts/skill-updates.py apply <skill> --from <staged-dir>` and reports one line. If behavior truly contradicts, return `NEED-INPUT` with both versions and a recommendation for the user.
