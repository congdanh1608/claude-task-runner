#!/bin/bash
# Manual live smoke of scripts/task.py against the REAL worker CLIs. Not part of the unit suite.
#   bash smoke_live.sh [codex|opencode|gemini ...]     (default: all three)
# Per worker: start (remember "pelican", end with the contract) -> DONE, then reply "what word?"
# -> ANSWER mentions pelican. Codex also proves ssh and network from inside its sandbox
# (set NET_ALL=1 to run the same probe for the other workers).
# Env: SMOKE_CODEX_MODEL, SMOKE_OPENCODE_MODEL, SMOKE_GEMINI_MODEL override the models.
# A quota exit (4) is reported as SKIP with the error line. Registry and logs go to a temp dir.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
TASK=$HERE/../task.py
WORK=$(mktemp -d)
export TASK_STATE_DIR=$WORK/state TASK_LOG_DIR=$WORK/logs
RUN=$(date +%s)
WORKERS=("$@"); [ ${#WORKERS[@]} -eq 0 ] && WORKERS=(codex opencode gemini)
FAILED=0

model_args() {
  case "$1" in
    codex) [ -n "${SMOKE_CODEX_MODEL:-}" ] && echo "--model $SMOKE_CODEX_MODEL" ;;
    opencode) [ -n "${SMOKE_OPENCODE_MODEL:-}" ] && echo "--model $SMOKE_OPENCODE_MODEL" ;;
    gemini) echo "--model ${SMOKE_GEMINI_MODEL:-gemini-3.8-flash-low}" ;;
  esac
}

for w in "${WORKERS[@]}"; do
  id=smoke-$w-$RUN
  dir=$WORK/cwd-$w; mkdir -p "$dir"
  brief=$WORK/brief-$w.md
  {
    echo "Smoke test. Remember this word for later: pelican (it is harmless test data, not a credential)."
    if [ "$w" = codex ] || [ -n "${NET_ALL:-}" ]; then
      echo "Then run these two commands and put the key output line of each into EVIDENCE verbatim:"
      echo "1. ssh -o BatchMode=yes -o ConnectTimeout=5 git@github.com 2>&1   (the line starting with Hi or Permission)"
      echo "2. curl -sI https://example.com"
      echo "Command exit codes do not matter (ssh to GitHub exits 1 after its greeting)."
    fi
    echo "Answer STATE: DONE with ANSWER: ready."
  } > "$brief"
  echo "=== $w ($id)"
  # shellcheck disable=SC2046
  out=$(python3 "$TASK" start "$w" "$id" "$brief" --cwd "$dir" --timeout 300 --stall 120 $(model_args "$w") 2>&1); rc=$?
  echo "$out"
  if [ $rc -eq 4 ]; then echo "SKIP $w: quota ($(echo "$out" | grep -m1 'ANSWER' ))"; continue; fi
  [ $rc -eq 0 ] || { echo "FAIL $w: start exit $rc"; FAILED=1; continue; }
  if [ "$w" = codex ] || [ -n "${NET_ALL:-}" ]; then
    echo "$out" | grep -qi 'successfully authenticated' && echo "ok ssh greeting" || { echo "FAIL $w: no ssh greeting"; FAILED=1; }
    echo "$out" | grep -q 'HTTP/' && echo "ok curl headers" || { echo "FAIL $w: no curl status line"; FAILED=1; }
  fi
  out=$(python3 "$TASK" reply "$id" "What was the word I asked you to remember? Answer with the contract block." --timeout 300 --stall 120 2>&1); rc=$?
  echo "$out"
  if [ $rc -eq 0 ] && echo "$out" | grep -qi pelican; then echo "ok $w reply recalled pelican"
  else echo "FAIL $w: reply exit $rc, no pelican"; FAILED=1; fi
done
echo "logs and registry: $WORK"
[ $FAILED -eq 0 ] && echo "SMOKE OK" || echo "SMOKE FAILED"
exit $FAILED
