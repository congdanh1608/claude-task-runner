# Shared by the fake codex/opencode/agy CLIs. Behaviour comes from env vars:
#   FAKE_DIR     where argv.N / cwd.N / role files are recorded (required)
#   FAKE_MODE    ok (default) | delay | nocontract | nosession | silent | busy | writing | watchwrite | command_hang | detached_busy | detached_idle | tool_silent | silent_salvage | quota | quotatext | quota_free | quota_paid
#   FAKE_ANSWER  answer text for the contract block
fake_main() {
  kind="$1"; shift
  here="$(cd "$(dirname "$0")" && pwd)"
  n=$(ls "$FAKE_DIR"/argv.* 2>/dev/null | wc -l | tr -d ' ')
  n=$((n + 1))
  printf '%s\0' "$@" > "$FAKE_DIR/argv.$n"
  pwd > "$FAKE_DIR/cwd.$n"
  printf '%s' "${TASK_RUNNER_ROLE:-}" > "$FAKE_DIR/role.$n"
  mode="${FAKE_MODE:-ok}"
  case "$mode" in
    quota_free) case "$*" in *-free*) mode=quota ;; *) mode=ok ;; esac ;;
    quota_paid) case "$*" in *-free*) mode=ok ;; *) mode=quota ;; esac ;;
  esac
  answer="${FAKE_ANSWER:-all good $n}"
  session="SESSION-$kind-1"
  [ "$mode" = nosession ] && session=""
  if [ "$mode" = delay ]; then sleep "${FAKE_SLEEP:-1}"; mode=ok; fi
  case "$mode" in
    silent)
      sleep 300 &
      echo $! > "$FAKE_DIR/child.pid"
      sleep 300
      exit 0 ;;
    busy)
      python3 -c 'import time; end=time.time()+7
while time.time()<end: pass' &
      echo $! > "$FAKE_DIR/child.pid"
      wait
      text=$(printf 'STATE: DONE\nANSWER: busy work complete\nEVIDENCE: fake:1\nMISSING:') ;;
    writing)
      python3 -c 'import os,time
p=os.path.join(os.getcwd(),"work-output.txt")
end=time.time()+7
while time.time()<end:
 open(p,"a").write("working\\n")
 time.sleep(.2)' &
      wait
      text=$(printf 'STATE: DONE\nANSWER: file work complete\nEVIDENCE: fake:1\nMISSING:') ;;
    watchwrite)
      printf '%s\n' '{"type":"item.started","item":{"id":"watch-1","type":"command_execution"}}'
      python3 -c 'import os,time
p=os.environ["WATCH_TARGET"]
end=time.time()+3.5
while time.time()<end:
 open(p,"a").write("progress\\n")
 time.sleep(.1)'
      printf '%s\n' '{"type":"item.completed","item":{"id":"watch-1","type":"command_execution"}}'
      text=$(printf 'STATE: DONE\nANSWER: watched work complete\nEVIDENCE: fake:1\nMISSING:') ;;
    command_hang)
      case "$kind" in
        codex) printf '%s\n' '{"type":"item.started","item":{"id":"cmd-1","type":"command_execution"}}' ;;
        gemini) printf '%s\n' '{"event":"step_update","step_update":{"step_index":8,"step_type":"tool","tool_name":"run_command","state":"ACTIVE"}}' ;;
        opencode) printf '%s\n' '{"type":"tool","part":{"type":"tool","id":"cmd-1","status":"running"}}' ;;
      esac
      sleep 300 &
      sleep 300
      exit 0 ;;
    detached_busy)
      python3 -c 'import os,subprocess,sys,time
p=subprocess.Popen([sys.executable,"-c","import time; end=time.time()+3.5\nwhile time.time()<end: pass"],start_new_session=True)
open(os.path.join(os.environ["FAKE_DIR"],"child.pid"),"w").write(str(p.pid))
p.wait()'
      text=$(printf 'STATE: DONE\nANSWER: detached CPU work complete\nEVIDENCE: fake:1\nMISSING:') ;;
    detached_idle)
      python3 -c 'import os,subprocess,sys,time
p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(300)"],start_new_session=True)
open(os.path.join(os.environ["FAKE_DIR"],"child.pid"),"w").write(str(p.pid))
time.sleep(300)'
      exit 0 ;;
    tool_silent)
      python3 - <<'PY'
import json
print(json.dumps({"type":"tool_output","output":"PRIVATE_COMMAND_OUTPUT_SENTINEL"}), flush=True)
PY
      sleep 300 &
      sleep 300
      exit 0 ;;
    silent_salvage)
      python3 - "$kind" "$session" <<'PY'
import json, sys
kind, session = sys.argv[1:]
if kind == "codex":
 print(json.dumps({"type":"item.completed","item":{"id":"partial","type":"agent_message","text":"Sweep complete; TSV exported to result.tsv"}}), flush=True)
PY
      echo 'sweep data' > "$(pwd)/produced.tsv"
      sleep 300 &
      echo $! > "$FAKE_DIR/child.pid"
      sleep 300
      exit 0 ;;
    quota)
      echo "Error: 429 rate limit exceeded" >&2
      exit 1 ;;
    nocontract)
      text="I did things but forgot the block." ;;
    quotatext)
      text=$(printf 'We discussed the quota exceeded and rate limit 429 handling.\nSTATE: DONE\nANSWER: %s\nEVIDENCE: none' "$answer") ;;
    *)
      text=$(printf 'Preflight ok.\nSTATE: DONE\nANSWER: %s\nEVIDENCE: fake:1, exit 0\nMISSING:' "$answer") ;;
  esac
  python3 "$here/_emit.py" "$kind" "$session" "$text"
}
