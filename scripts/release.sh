#!/usr/bin/env bash
# Release task-runner: test, validate, sensitive-scan (tree+history), bump, changelog, commit, tag, push.
# Usage: scripts/release.sh patch|minor|major "<changelog line>" [--dry-run] [--allow-no-denylist]
set -euo pipefail

fail() { echo "release: $1 failed: $2" >&2; exit 1; }

USAGE='usage: release.sh patch|minor|major "<changelog line>" [--dry-run] [--allow-no-denylist]'
BUMP=""
LINE=""
DRY=0
ALLOW_NO_DENYLIST=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --allow-no-denylist) ALLOW_NO_DENYLIST=1 ;;
    --*) fail args "unknown flag $a" ;;
    *)
      if [ -z "$BUMP" ]; then
        BUMP="$a"
      elif [ -z "$LINE" ]; then
        LINE="$a"
      else
        fail args "$USAGE"
      fi
      ;;
  esac
done

case "$BUMP" in
  patch|minor|major) ;;
  *) fail args "$USAGE" ;;
esac
[ -n "$LINE" ] || fail args "$USAGE"

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

# Step 1: branch and clean-tree check
branch=$(git rev-parse --abbrev-ref HEAD)
[ "$branch" = "main" ] || fail git "must be on main (current: $branch)"
[ -z "$(git status --porcelain)" ] || fail git "working tree is not clean"

# Verify PLUGIN_DENYLIST and PLUGIN_NAME_DIRS are set and readable
check_env_gate() {
  if [ -z "${PLUGIN_DENYLIST:-}" ] || [ ! -r "$PLUGIN_DENYLIST" ]; then
    return 1
  fi
  if [ -z "${PLUGIN_NAME_DIRS:-}" ]; then
    return 1
  fi
  local IFS=":"
  local d
  for d in $PLUGIN_NAME_DIRS; do
    [ -n "$d" ] || continue
    [ -d "$d" ] && [ -r "$d" ] || return 1
  done
  return 0
}

if ! check_env_gate; then
  if [ "$ALLOW_NO_DENYLIST" = 1 ]; then
    echo "release: WARNING: --allow-no-denylist set; PLUGIN_DENYLIST or PLUGIN_NAME_DIRS is unset or unreadable!" >&2
  else
    fail sensitive "PLUGIN_DENYLIST and PLUGIN_NAME_DIRS must be set and readable (or pass --allow-no-denylist)"
  fi
fi

# Step 2: unit tests
python3 -m unittest discover -s scripts/tests -p 'test_*.py' || fail tests "unit tests failed"

# Step 3: validate plugin
claude plugin validate . || fail validate "claude plugin validate failed"

# Step 4: sensitive scan (tree + history)
python3 scripts/check-sensitive.py --all --history || fail sensitive "check-sensitive refused"

# Compute next version without modifying files yet
NEWVER=$(python3 - "$BUMP" <<'PY'
import json, sys
bump = sys.argv[1]
with open(".claude-plugin/plugin.json", "r", encoding="utf-8") as fh:
    d = json.load(fh)
a, b, c = (int(x) for x in d["version"].split("."))
a, b, c = {"major": (a + 1, 0, 0), "minor": (a, b + 1, 0), "patch": (a, b, c + 1)}[bump]
print(f"{a}.{b}.{c}")
PY
) || fail bump "cannot compute version from .claude-plugin/plugin.json"

if [ "$DRY" = 1 ]; then
  echo "release: dry-run ok for v${NEWVER}: - ${LINE}"
  exit 0
fi

# Step 5: write version + changelog, commit, tag, push
BACKUP=$(mktemp -d)
RESTORE=1
cp .claude-plugin/plugin.json "$BACKUP/plugin.json"
cp CHANGELOG.md "$BACKUP/CHANGELOG.md"

restore() {
  if [ "$RESTORE" = 1 ]; then
    cp "$BACKUP/plugin.json" .claude-plugin/plugin.json
    cp "$BACKUP/CHANGELOG.md" CHANGELOG.md
  fi
  rm -rf "$BACKUP"
}
trap restore EXIT

python3 - "$NEWVER" <<'PY' || fail bump "cannot bump .claude-plugin/plugin.json"
import json, sys
ver = sys.argv[1]
path = ".claude-plugin/plugin.json"
with open(path, "r", encoding="utf-8") as fh:
    d = json.load(fh)
d["version"] = ver
with open(path, "w", encoding="utf-8") as fh:
    json.dump(d, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
PY

python3 - "$NEWVER" "$LINE" <<'PY' || fail changelog "cannot edit CHANGELOG.md"
import sys
ver, line = sys.argv[1], sys.argv[2]
with open("CHANGELOG.md", "r", encoding="utf-8") as fh:
    t = fh.read()
head = "# Changelog\n\n"
if not t.startswith(head):
    sys.exit(1)
with open("CHANGELOG.md", "w", encoding="utf-8") as fh:
    fh.write(head + f"## {ver}\n- {line}\n\n" + t[len(head):])
PY

RESTORE=0
git add .claude-plugin/plugin.json CHANGELOG.md
git commit -m "Release v${NEWVER}"
git tag "v${NEWVER}"
git push origin main --tags
echo "released: v${NEWVER}"
