#!/usr/bin/env python3
"""Run Codex / OpenCode / Gemini CLI tasks in resumable sessions.

  task start [<worker>] <id> <brief-file> [--model M] [--row ROW] [--pinned] [--write] [--cwd DIR]
  task resolve <row>
  task off <name> | task on <name> | task workers
  task reply <id> <message>
  task status <id>
  task list

`start` and `reply` run the worker in the foreground until it exits, then print the
status block. Exit codes: 0 DONE, 3 NEED-INPUT, 1 FAIL, 4 QUOTA, 5 RUNNING, 2 usage error.
Registry: $TASK_STATE_DIR (default ~/.claude/state/tasks). Raw logs and the optional
Gemini serial-mode lock: $TASK_LOG_DIR (default ~/.claude/logs/task-runner).
"""
import argparse
import contextlib
import datetime
import fcntl
import io
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

DEFAULT_MODELS = {
    "codex": "gpt-6-luna",
    "opencode": "opencode-go/deepseek-v4.1-flash",
    "gemini": "gemini-3.8-flash-high",
    "perch": "deepseek-flash-starter",
    "relay": "relay-sonnet",
}
MODELS = {
    "luna": ("codex", "gpt-6-luna"), "sol": ("codex", "gpt-6.1-sol"),
    "chatanywhere": ("codex", "gpt-6-luna"),
    "deepseek-free": ("opencode", "opencode/deepseek-v4.1-flash-free"),
    "deepseek": ("opencode", "opencode-go/deepseek-v4.1-flash"),
    "perch-deepseek": ("perch", "deepseek-flash-starter"),
    "relay-sonnet": ("relay", "relay-sonnet"), "relay-haiku": ("relay", "relay-haiku"),
    "relay-opus": ("relay", "relay-opus"),
    "flash": ("gemini", "gemini-3.8-flash-high"),
    "hydrogen": ("gemini", "gemini-4-hydrogen-medium"),
    "argon": ("gemini", "gemini-4-argon-low"),
    "carbon": ("gemini", "gemini-4-carbon-low"),
    "sonnet": ("gemini", "claude-sonnet-5-5-medium"),
}
ROWS = {
    # Priority with everything on: Gemini, then Codex, then OpenCode, chatanywhere last.
    # Argon is last in review: ~280 s per model call, kept only so a pinned argon task resolves.
    # Flash (agy) only reads and writes docs; Sonnet (agy) is pinned-only, its pool is tiny.
    # deepseek-free, perch-deepseek, and relay are explicitly named only (never in a row);
    # deepseek-free retries once on paid deepseek.
    "search": ["flash", "luna", "deepseek", "chatanywhere"],
    "inventory": ["flash", "luna", "deepseek", "chatanywhere"],
    "mechanical": ["hydrogen", "luna", "deepseek", "chatanywhere"],
    "code": ["hydrogen", "luna", "deepseek", "sol", "chatanywhere"],
    "review": ["carbon", "sol", "luna", "deepseek", "argon"],
    "hard": ["carbon", "sol", "luna", "deepseek"],
}
# chatanywhere: Luna on the ChatAnywhere free key through the Codex CLI (provider via -c), tail of rows.
CHATANYWHERE = "chatanywhere"
CHATANYWHERE_URL = "https://api.chatanywhere.org/v1"
CHATANYWHERE_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._/-]*)?$")
CHATANYWHERE_QUOTA_FILE = "task-chatanywhere-quota.json"
TWINS = {"deepseek-free": "deepseek"}
OWNER_ONLY = ("deepseek-free", "sonnet", "perch-deepseek", "relay-sonnet", "relay-haiku", "relay-opus")
ROW_OF_ALIAS = {"flash": "search", "luna": "code", "sol": "hard", "deepseek": "mechanical",
                "hydrogen": "mechanical", "argon": "review", "carbon": "hard",
                "chatanywhere": "code",
                "deepseek-free": "mechanical", "perch-deepseek": "mechanical", "sonnet": "code",
                "relay-sonnet": "mechanical", "relay-haiku": "mechanical", "relay-opus": "mechanical"}
WORKERS_STATE = "task-workers.json"
SESSION_KEYS = {
    "codex": ("thread_id",),
    "opencode": ("sessionID",),
    "gemini": ("conversation_id",),
    "perch": ("threadId",),
    "relay": ("session_id",),
}
STATES = ("RUNNING", "DONE", "NEED-INPUT", "FAIL", "QUOTA")
EXIT_CODES = {"DONE": 0, "NEED-INPUT": 3, "FAIL": 1, "QUOTA": 4, "RUNNING": 5}
MAX_REPLIES = 2
STATUS_BODY_LINES = 10
DEFAULT_LOCK_WAIT = 600
TEST_ALIASES = ("luna", "sol", "chatanywhere", "flash", "hydrogen", "argon", "carbon", "sonnet", "codex", "opencode", "gemini", "perch",
                "perch-deepseek", "deepseek-free", "relay-sonnet", "relay-haiku", "relay-opus", "relay")
TEST_ALIAS_MAP = {"codex": "luna", "opencode": "deepseek", "gemini": "flash", "perch": "perch-deepseek",
                  "relay": "relay-sonnet"}
_UNSET = object()
_REDACTION_VALUES = set()
QUOTA_RE = re.compile(r"\b429\b|rate[ _-]?limit|usage[ _-]?limit|insufficient_quota|quota[ _-]?exceeded|FreeUsageLimitError",
                      re.IGNORECASE)
# Structured Perch failure text only; assistantText is never scanned.
PERCH_LIMIT_RE = re.compile(
    r"turn_rate_limited|turn[ _-]rate[ _-]limit|rate[ _-]limit(?:ed)?\b|usage[ _-]limit"
    r"|allowance(?:\s+is)?\s+(?:spent|used|exhausted|reached)|limit[ _-]reached"
    r"|quota[ _-]?(?:exceeded|reached)|FreeUsageLimitError",
    re.IGNORECASE)
PERCH_REJECTED_RE = re.compile(
    r"starter_model_blocked|model_restricted|unknown model|model not found|invalid model",
    re.IGNORECASE)
PERCH_AUTH_RE = re.compile(
    r"not signed in|sign[ _-]?in|log[ _-]?in|session expired|authentication|unauthorized",
    re.IGNORECASE)
PROVIDER_RE = re.compile(r"ProviderModelNotFoundError|ProviderInitError|ProviderAuthError|model not found",
                         re.IGNORECASE)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RELAY_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
RELAY_CLI_MODELS = {"relay-sonnet": "claude-sonnet-5-5", "relay-haiku": "claude-haiku-5-5",
                    "relay-opus": "claude-opus-5-5"}

CONTRACT_FOOTER = """---
Work order: preflight (list ALL conflicts and missing prerequisites) -> do -> self-verify.
A step you cannot do: write `skipped: <reason>` and continue with the next step.
A prod-changing step: stop with STATE: NEED-INPUT and the exact plan; wait for approval.
Find facts yourself from code and docs before asking.
State an estimated duration in the brief. Commands expected to run longer than 2 minutes must write output to a log passed with `--watch` and print progress at least every 60 seconds; do not run them silently.
End your answer with this block:
STATE: DONE | NEED-INPUT | FAIL
ANSWER: <direct answer / what changed>
EVIDENCE: <file:line, commands + exit codes>
MISSING: <all missing items, NEED-INPUT only>
"""


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- paths / registry

def state_dir():
    return os.environ.get("TASK_STATE_DIR") or os.path.expanduser("~/.claude/state/tasks")


def workers_state_dir():
    return os.path.dirname(os.path.abspath(state_dir()))


def worker_switches(readonly=False):
    directory = workers_state_dir()
    path = os.path.join(workers_state_dir(), WORKERS_STATE)
    if readonly:
        return _worker_switches_locked(path, readonly=True)
    os.makedirs(directory, exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _worker_switches_locked(path)


def _worker_switches_locked(path, readonly=False):
    legacy_path = os.path.join(state_dir(), WORKERS_STATE)
    if legacy_path != path and not os.path.exists(path) and os.path.exists(legacy_path):
        if readonly:
            path = legacy_path
        else:
            try:
                os.replace(legacy_path, path)
            except OSError as error:
                raise UsageError("cannot migrate worker switches: %s" % error)
    try:
        with open(path) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as error:
        raise UsageError("cannot read worker switches: %s" % error)
    if not isinstance(data, dict):
        raise UsageError("cannot read worker switches: expected a JSON object")
    state = {name: True for name in list(MODELS) + list(DEFAULT_MODELS)}
    for name in state:
        if name in data:
            if not isinstance(data[name], bool):
                raise UsageError("cannot read worker switches: %s must be true or false" % name)
            state[name] = data[name]
    for cli in DEFAULT_MODELS:
        if not state[cli]:
            for alias, (worker, _) in MODELS.items():
                if worker == cli and alias != CHATANYWHERE:
                    state[alias] = False
    return state


def switch_file_update(name, enabled):
    valid = set(MODELS) | set(DEFAULT_MODELS)
    if name not in valid:
        raise UsageError("unknown worker/model: " + name)
    directory = workers_state_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, WORKERS_STATE)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _worker_switches_locked(path)
        if name in DEFAULT_MODELS:
            state[name] = enabled
            for alias, (worker, _) in MODELS.items():
                if worker == name and alias != CHATANYWHERE:
                    state[alias] = enabled
        else:
            state[name] = enabled
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)


def model_on(alias, state):
    worker, _ = MODELS[alias]
    if alias == CHATANYWHERE:
        return state[alias]
    return state[alias] and state[worker]


def _utc_today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def quota_blocked(alias):
    """chatanywhere is skipped for the rest of the UTC day after a QUOTA."""
    if alias != CHATANYWHERE:
        return False
    try:
        with open(os.path.join(workers_state_dir(), CHATANYWHERE_QUOTA_FILE)) as handle:
            return json.load(handle).get("date") == _utc_today()
    except (OSError, ValueError, AttributeError):
        return False


def mark_quota_blocked(alias):
    if alias != CHATANYWHERE:
        return
    try:
        os.makedirs(workers_state_dir(), exist_ok=True)
        with open(os.path.join(workers_state_dir(), CHATANYWHERE_QUOTA_FILE), "w") as handle:
            json.dump({"date": _utc_today()}, handle)
    except OSError:
        pass


def resolve_row(row, preferred=None, state=None):
    """Choose the first enabled row candidate, preserving start's fallback order."""
    state = worker_switches() if state is None else state
    alias = preferred or ROWS[row][0]
    if not model_on(alias, state) or quota_blocked(alias):
        alternatives = [name for name in ROWS[row] if model_on(name, state) and not quota_blocked(name)]
        if not alternatives:
            raise UsageError("all workers for row %s are off; Claude mode (task skill) or ask the user" % row)
        alias = alternatives[0]
    worker, model = MODELS[alias]
    if alias == "flash":
        model = "gemini-3.8-flash-medium" if row == "search" else "gemini-3.8-flash-high"
    return alias, worker, model


def cmd_resolve(args):
    alias, worker, _ = resolve_row(args.row, state=worker_switches(readonly=True))
    print("%s - %s" % (worker, alias))
    return 0


def display_name(reg):
    alias = reg.get("alias") or model_alias(reg.get("worker"), reg.get("model"))
    if not alias:
        alias = {"codex": "luna", "opencode": "deepseek", "gemini": "flash",
                 "perch": "perch-deepseek"}.get(reg.get("worker"), "luna")
    return "%s - %s - %s" % (reg.get("worker", "worker"), alias, reg.get("id", "unknown"))


def model_alias(worker, model):
    if model in MODELS and (worker is None or MODELS[model][0] == worker):
        return model
    if model in ("gemini-3.8-flash-medium", "gemini-3.8-flash-low") and worker in (None, "gemini"):
        return "flash"
    if worker == "relay" and isinstance(model, str) and model:
        if model in ("relay-sonnet", "relay-haiku", "relay-opus"):
            return model
        return "relay-sonnet" if RELAY_MODEL_RE.fullmatch(model) else None
    return next((name for name, (cli, model_name) in MODELS.items()
                 if (worker is None or cli == worker) and model_name == model), None)


def log_dir():
    return os.environ.get("TASK_LOG_DIR") or os.path.expanduser("~/.claude/logs/task-runner")


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def precise_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def reg_path(task_id):
    return os.path.join(state_dir(), task_id + ".json")


def load_reg(task_id):
    try:
        with open(reg_path(task_id)) as handle:
            reg = json.load(handle)
    except (OSError, ValueError):
        raise UsageError("unknown task id: " + task_id)
    if not isinstance(reg, dict):
        raise UsageError("invalid task record: " + task_id)
    return reg


def save_reg(reg):
    reg = redact_object(reg)
    reg["updated"] = now()
    os.makedirs(state_dir(), exist_ok=True)
    path = reg_path(reg["id"])
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as handle:
        json.dump(reg, handle, indent=2)
    os.replace(tmp, path)


def _secret_values(env=None):
    values = set(_REDACTION_VALUES)
    for source in (os.environ, env or {}):
        for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            value = source.get(name)
            if isinstance(value, str) and value:
                values.add(value)
    return sorted(values, key=len, reverse=True)


def redact_text(value, secrets=None):
    text = str(value)
    for secret in secrets if secrets is not None else _secret_values():
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def redact_object(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_object(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_object(item) for item in value)
    if isinstance(value, dict):
        return {redact_text(key) if isinstance(key, str) else key: redact_object(item)
                for key, item in value.items()}
    return value


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def refresh(reg):
    """A RUNNING record whose runner process is gone is a failure, not a live task."""
    pid = reg.get("pid")
    if reg.get("state") == "RUNNING" and isinstance(pid, int) and not pid_alive(pid):
        reg["state"] = "FAIL"
        reg["report"] = {"state": "FAIL", "answer": "runner process died before the worker finished"}
        save_reg(reg)
    return reg


# ---------------------------------------------------------------- contract / events

_KEY_RE = re.compile(r"^\s*[*_`>#-]*\s*(STATE|ANSWER|EVIDENCE|MISSING)\s*[*_`]*\s*:\s*[*_`]*\s*(.*)$")
_STATE_VALUE_RE = re.compile(r"^(DONE|NEED-INPUT|FAIL)[*_`.\s]*$")


def parse_contract(text):
    """Return the fields of the LAST `STATE:` block; no block means FAIL."""
    lines = (text or "").splitlines()
    start = None
    for index, line in enumerate(lines):
        match = _KEY_RE.match(line)
        if match and match.group(1) == "STATE" and _STATE_VALUE_RE.match(match.group(2).strip()):
            start = index
    if start is None:
        return {"state": "FAIL", "answer": "no result contract"}
    state = _STATE_VALUE_RE.match(_KEY_RE.match(lines[start]).group(2).strip()).group(1)
    fields = {"answer": [], "evidence": [], "missing": []}
    current = None
    for line in lines[start + 1:]:
        match = _KEY_RE.match(line)
        if match and match.group(1) != "STATE":
            current = match.group(1).lower()
            fields[current].append(match.group(2))
        elif match:
            break
        elif current:
            fields[current].append(line)
    result = {"state": state}
    for key in ("answer", "evidence", "missing"):
        result[key] = "\n".join(fields[key]).strip()
    return result


def parse_events(raw):
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts) or None
    return None


def _agy_step(event):
    step = event.get("step_update") if event.get("event") == "step_update" else None
    return step if isinstance(step, dict) else None


def agy_delta(event):
    """Streamed assistant text of one agy step_update event, or None."""
    step = _agy_step(event)
    if step and step.get("step_type") == "agent_response" and isinstance(step.get("text_delta"), str):
        return step["text_delta"]
    return None


def agy_result(event):
    """Final response of an agy `result` event that succeeded, or None."""
    if event.get("event") != "result" or not isinstance(event.get("result"), dict):
        return None
    result = event["result"]
    if str(result.get("status", "")).upper() == "SUCCESS" and isinstance(result.get("response"), str):
        return result["response"]
    return None


def event_text(event):
    """Assistant text carried by one event, or None."""
    if event.get("event") in ("step_update", "result"):
        return agy_delta(event) if event["event"] == "step_update" else agy_result(event)
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") in ("agent_message", "assistant_message", "message"):
        if item.get("role") in (None, "assistant"):
            return _content_text(item.get("text", item.get("content")))
        return None
    part = event.get("part")
    if isinstance(part, dict) and part.get("type") == "text":
        return part.get("text") if isinstance(part.get("text"), str) else None
    message = event.get("message")
    role = event.get("role")
    if role is None and isinstance(message, dict):
        role = message.get("role")
    if role == "assistant":
        if isinstance(message, dict):
            return _content_text(message.get("content"))
        return _content_text(event.get("content", event.get("text")))
    if event.get("type") in ("assistant", "assistant_message") and role is None:
        return _content_text(event.get("content", event.get("text")))
    return None


def final_text(events, raw):
    return assistant_text(events) or raw


def assistant_text(events):
    """Return assistant event text only; tool payloads and raw JSON are excluded."""
    for event in events:
        response = agy_result(event)
        if response:
            return response
    chunks = []
    for event in events:
        text = event_text(event)
        if text:
            if chunks and not event.get("delta") and agy_delta(event) is None:
                chunks.append("\n")
            chunks.append(text)
    return "".join(chunks)


def _perch_result(data):
    return isinstance(data, dict) and any(
        key in data for key in ("assistantText", "threadId", "ok", "status", "error",
                                "errorCode", "segments", "validModels"))


def parse_perch(raw):
    """Return Perch's single JSON result object, tolerating a preamble; events are never used."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if _perch_result(data):
            return data
    except ValueError:
        pass
    lines = text.splitlines(True)
    starts = []
    offset = 0
    for line in lines:
        if line.lstrip().startswith("{"):
            starts.append(offset + (len(line) - len(line.lstrip())))
        offset += len(line)
    decoder = json.JSONDecoder()
    best = None
    for start in reversed(starts):
        try:
            data, end = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if _perch_result(data):
            consumed = start + end
            if best is None or consumed > best[0]:
                best = (consumed, data)
    return best[1] if best else None


def perch_text(data):
    text = data.get("assistantText") if isinstance(data, dict) else None
    return text if isinstance(text, str) else ""


def perch_failure(data):
    """True when a parsed Perch result reports a failed turn."""
    if not isinstance(data, dict):
        return False
    return (data.get("ok") is False
            or str(data.get("status") or "").lower() in ("failed", "error")
            or bool(data.get("error"))
            or bool(data.get("errorCode")))


def _first_perch_line(text, pattern):
    for line in (text or "").splitlines():
        if pattern.search(line):
            return line.strip()[:200]
    return None


def perch_limit(data, stderr_text):
    """Structured Perch limit signal from a failed turn or its stderr, or None.

    assistantText is never inspected, so an ok:true answer that mentions a
    rate limit or an allowance stays a normal answer.
    """
    if (isinstance(data, dict) and data.get("ok") is True and perch_text(data).strip()
            and not data.get("error") and not data.get("errorCode")):
        return None
    if isinstance(data, dict):
        code = data.get("errorCode")
        if isinstance(code, str) and PERCH_LIMIT_RE.search(code):
            return code.strip()[:200]
        segments = data.get("segments")
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                kind = str(segment.get("kind") or segment.get("type") or "").lower()
                limit = segment.get("limit")
                if kind == "error" and isinstance(limit, dict):
                    detail = limit.get("message") or limit.get("code") or "limit"
                    return str(detail)[:200]
        if perch_failure(data):
            error = data.get("error")
            if isinstance(error, str):
                line = _first_perch_line(error, PERCH_LIMIT_RE)
                if line:
                    return line
    return _first_perch_line(stderr_text, PERCH_LIMIT_RE)


def perch_valid_models(data, stderr_text):
    """Valid model names listed by the CLI in JSON or stderr, or None."""
    candidates = []
    if isinstance(data, dict):
        for key in ("validModels", "valid_models", "models"):
            value = data.get(key)
            if isinstance(value, str):
                candidates.append(value)
            elif isinstance(value, list):
                names = [str(item).strip() for item in value
                         if isinstance(item, str) and item.strip()]
                if names:
                    return ", ".join(names)[:200]
        for key in ("error", "errorCode"):
            value = data.get(key)
            if isinstance(value, str):
                candidates.append(value)
    candidates.append(stderr_text or "")
    for text in candidates:
        match = re.search(r"valid models?\s*[:=]\s*([^\n;)]+)", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()[:200]
    return None


def perch_rejected(data, stderr_text):
    """Clear 'rejected model' failure message, or None."""
    line = _first_perch_line(stderr_text, PERCH_REJECTED_RE)
    if line:
        if "valid models" not in line.lower():
            names = perch_valid_models(data, stderr_text)
            line += ("; valid models: %s" % names if names
                     else "; valid model names unavailable")
        return "perch rejected the model (no fallback): " + line
    if isinstance(data, dict):
        for key in ("errorCode", "error"):
            value = data.get(key)
            if isinstance(value, str) and PERCH_REJECTED_RE.search(value):
                detail = value.strip()[:200]
                if "valid models" not in detail.lower():
                    names = perch_valid_models(data, stderr_text)
                    detail += (" (valid models: %s)" % names if names
                               else " (valid model names unavailable)")
                return "perch rejected the model (no fallback): " + detail
    return None


def perch_auth(data, stderr_text):
    """Clear auth failure message, or None."""
    line = _first_perch_line(stderr_text, PERCH_AUTH_RE)
    if line:
        return "perch auth failure (no fallback): " + line
    if isinstance(data, dict):
        for key in ("errorCode", "error"):
            value = data.get(key)
            if isinstance(value, str) and PERCH_AUTH_RE.search(value):
                return "perch auth failure (no fallback): " + value.strip()[:200]
    return None


def perch_error(data):
    """Human-readable failure text from a failed Perch result."""
    if isinstance(data, dict):
        for key in ("error", "errorCode", "status"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    return "turn failed"


class CommandTracker:
    """Track active shell/tool calls from appended worker event streams."""
    MAX_BYTES_PER_POLL = 256 * 1024
    MAX_PENDING_BYTES = 1024 * 1024
    PENDING_DROPPED = "[task-runner: dropped oversized partial line]\n"

    def __init__(self, worker, out_path, err_path, out_offset=0, err_offset=0):
        self.worker = worker
        self.out_path = out_path
        self.err_path = err_path
        self.out_offset = out_offset
        self.err_offset = err_offset
        self.out_pending = ""
        self.err_pending = ""
        self.active = set()
        self.background_wait = False

    @property
    def in_progress(self):
        return bool(self.active) or self.background_wait

    def poll(self):
        limit = self.MAX_BYTES_PER_POLL // 2
        self._read(self.err_path, "err", limit)
        self._read(self.out_path, "out", limit)

    def _read(self, path, stream, limit):
        attr_offset = stream + "_offset"
        attr_pending = stream + "_pending"
        try:
            with open(path, "rb") as handle:
                handle.seek(getattr(self, attr_offset))
                raw = handle.read(limit)
        except OSError:
            return 0
        if not raw:
            return 0
        setattr(self, attr_offset, getattr(self, attr_offset) + len(raw))
        text = getattr(self, attr_pending) + raw.decode("utf-8", "replace")
        lines = text.splitlines(True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            pending = lines.pop()
            if len(pending.encode("utf-8", "replace")) > self.MAX_PENDING_BYTES:
                pending = self.PENDING_DROPPED
            setattr(self, attr_pending, pending)
        else:
            setattr(self, attr_pending, "")
        if stream == "err":
            for line in lines:
                if re.search(r"root agent idle; waiting up to .+ for \d+ background task\(s\)", line, re.I):
                    self.background_wait = True
            return len(raw)
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            if self.worker == "gemini":
                self._agy(event)
            elif self.worker == "codex":
                self._codex(event)
            elif self.worker == "opencode":
                self._opencode(event)
        return len(raw)

    def _agy(self, event):
        if event.get("event") in ("step_update", "result"):
            self.background_wait = False
        step = event.get("step_update")
        if event.get("event") != "step_update" or not isinstance(step, dict):
            return
        index = step.get("step_index")
        state = str(step.get("state", "")).upper()
        if step.get("tool_name") == "run_command" and state == "ACTIVE":
            self.active.add(("agy", index))
        elif state == "DONE":
            self.active.discard(("agy", index))

    def _codex(self, event):
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            return
        item_id = item.get("id", "__codex_command__")
        if event.get("type") == "item.started":
            self.active.add(("codex", item_id))
        elif event.get("type") == "item.completed":
            self.active.discard(("codex", item_id))

    def _opencode(self, event):
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != "tool":
            return
        state = part.get("state")
        status = part.get("status")
        if not isinstance(status, str) and isinstance(state, dict):
            status = state.get("status")
        status = str(status or "").lower()
        call_id = part.get("callID") or part.get("id") or "__opencode_tool__"
        key = ("opencode", call_id)
        if status == "running":
            self.active.add(key)
        elif status in ("completed", "done", "error"):
            self.active.discard(key)


def find_key(value, keys):
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
        for child in value.values():
            found = find_key(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_key(child, keys)
            if found:
                return found
    return None


def find_session(events, worker):
    for event in events:
        found = find_key(event, SESSION_KEYS[worker])
        if found:
            return found
    return None


def quota_line(events, stderr_text):
    """First CLI error line that looks like a quota/rate-limit; assistant text never counts."""
    for line in stderr_text.splitlines():
        if QUOTA_RE.search(line):
            return line.strip()[:200]
    for event in events:
        etype = str(event.get("type", "") or event.get("event", ""))
        if event_text(event) is not None:
            continue
        failed_result = event.get("event") == "result" and agy_result(event) is None
        if event.get("error") or "error" in etype or "fail" in etype or failed_result:
            blob = json.dumps(event.get("error") or event.get("message") or event)
            if QUOTA_RE.search(blob):
                return blob[:200]
    return None


# ---------------------------------------------------------------- adapters

def chatanywhere_flags():
    """Provider overrides for the codex CLI; the key comes from the child env only."""
    base = os.environ.get("CHATANYWHERE_BASE_URL", "").strip()
    if not CHATANYWHERE_URL_RE.match(base):
        base = CHATANYWHERE_URL
    flags = ['model_provider="chatanywhere"', 'model_providers.chatanywhere.name="ChatAnywhere"',
             'model_providers.chatanywhere.base_url="%s"' % base,
             'model_providers.chatanywhere.env_key="CHATANYWHERE_API_KEY"',
             'model_providers.chatanywhere.wire_api="responses"', "model_context_window=272000"]
    return [part for flag in flags for part in ("-c", flag)]


def build_argv(worker, model, dirpath, session, message, start, effort=None, write=False, alias=None):
    if worker == "codex":
        # Workers compact at 250k; the interactive Codex config is left alone.
        extra = ["-c", "model_auto_compact_token_limit=250000"]
        if alias == CHATANYWHERE:
            extra += chatanywhere_flags()
        extra += ["-c", 'model_reasoning_effort="%s"' % effort] if effort else []
        if start:
            return ["codex", "exec", "--json", "--skip-git-repo-check", "-m", model, *extra,
                    "-s", "danger-full-access", "-C", dirpath, message]
        # `resume` has no -s/-C: the sandbox goes through -c, the directory is the process cwd.
        return ["codex", "exec", "resume", "--json", "--skip-git-repo-check", "-m", model, *extra,
                "-c", 'sandbox_mode="danger-full-access"', session, message]
    if worker == "opencode":
        if start:
            return ["opencode", "run", "--format", "json", "-m", model, message]
        return ["opencode", "run", "--format", "json", "-s", session, message]
    if worker == "perch":
        argv = ["run", message, "--json", "--model", model,
                "--permission", "take_the_wheel" if write else "plan"]
        if not start and session:
            argv += ["--thread", session]
        return ["perch"] + argv + ["--cwd", dirpath]
    if worker == "relay":
        model = RELAY_CLI_MODELS.get(model, model)
        argv = ["claude", "-p", "--output-format", "json", "--safe-mode", "--restricted",
                "--permission-mode", "acceptEdits" if write else "plan",
                "--tools", "Read,Grep,Glob" + (",Edit,Write" if write else ""),
                "--allowedTools", "Read,Grep,Glob" + (",Edit,Write" if write else ""),
                "--strict-mcp-config", "--disable-slash-commands",
                "--model", model]
        patterns = (".env*", "**/.env*", "**/*.pem", "**/*.p12", "**/*.keystore", "**/*.jks",
                    "**/key.properties", "**/*service-account*.json", "**/secrets/**")
        denied = ["%s(%s)" % (tool, pattern)
                  for tool in ("Read", "Edit", "Write", "Grep", "Glob") for pattern in patterns]
        argv[argv.index("--model"):argv.index("--model")] = ["--disallowedTools"] + denied
        if effort:
            argv += ["--effort", effort]
        if not start and session:
            argv += ["--resume", session]
        argv += [message]
        return argv
    tail = ["--model", model, "--output-format", "stream-json", "--dangerously-skip-permissions"]
    if start:
        return ["agy", "-p", message] + tail
    return ["agy", "--conversation", session, "-p", message] + tail


# ---------------------------------------------------------------- process control

def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


class Interrupted(Exception):
    pass


def _on_signal(signum, frame):
    raise Interrupted("signal %d" % signum)


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _cpu_seconds(value):
    parts = value.strip().split(":")
    try:
        total = 0.0
        for part in parts:
            total = total * 60 + float(part)
        return total
    except ValueError:
        return 0


def _process_table():
    """Return (pid, ppid, pgid, CPU seconds) rows from ps."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,time="], capture_output=True,
                                text=True, timeout=0.25)
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) != 4:
            continue
        try:
            pid, ppid, group = map(int, fields[:3])
        except ValueError:
            continue
        rows.append((pid, ppid, group, _cpu_seconds(fields[3])))
    return rows


def _descendants(root_pid, rows):
    by_parent = {}
    for pid, ppid, _, cpu in rows:
        by_parent.setdefault(ppid, []).append((pid, cpu))
    found = {root_pid: 0.0}
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for pid, cpu in by_parent.get(parent, []):
            if pid not in found:
                found[pid] = cpu
                pending.append(pid)
    return found


def _worker_processes(root_pid, rows, tracked=()):
    """Track the descendant tree plus every process still in the CLI group."""
    found = _descendants(root_pid, rows)
    for pid, _, pgid, cpu in rows:
        if pgid == root_pid or pid in tracked:
            found[pid] = cpu
    return found


def _cpu_increased(previous, current):
    return any(seconds > previous.get(pid, 0.0) for pid, seconds in current.items())


def _kill_worker(root_pid, tracked=()):
    """Kill descendants before their parents, including detached descendants."""
    for _ in range(3):
        rows = _process_table()
        children = _worker_processes(root_pid, rows, tracked)
        for pid in reversed(list(children)):
            if pid != root_pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        kill_group(root_pid)
        try:
            os.kill(root_pid, signal.SIGKILL)
        except OSError:
            pass


def _worktree_snapshot(root, max_entries=20000, max_seconds=0.25):
    """Map bounded worktree file mtimes; return None on either cap."""
    skip = {".git", "node_modules", "build", "dist", "target", ".next", ".dart_tool"}
    snapshot = {}
    started = time.monotonic()
    entries = 0
    try:
        for current, dirs, files in os.walk(root):
            entries += len(dirs) + len(files)
            if entries > max_entries or time.monotonic() - started > max_seconds:
                return None
            dirs[:] = [name for name in dirs if name not in skip]
            for name in files:
                if time.monotonic() - started > max_seconds:
                    return None
                path = os.path.join(current, name)
                try:
                    snapshot[os.path.relpath(path, root)] = os.stat(path).st_mtime_ns
                except OSError:
                    pass
    except OSError:
        return None
    return snapshot


class _SnapshotPoller:
    """Run at most one bounded snapshot in the background."""
    def __init__(self, root, initial):
        self.root = root
        self.lock = threading.Lock()
        self.result = None
        self.ready = False
        self.running = False
        self.last_start = time.monotonic()
        self.baseline = initial

    def poll(self, now):
        with self.lock:
            if self.ready:
                current = self.result
                self.ready = False
                changed = current is not None and self.baseline is not None and any(
                    path not in self.baseline or mtime != self.baseline[path]
                    for path, mtime in current.items())
                if current is not None:
                    self.baseline = current
            else:
                changed = False
            if not self.running and now - self.last_start >= 1.0:
                self.running = True
                self.last_start = now
                thread = threading.Thread(target=self._scan, daemon=True)
                thread.start()
            return changed

    def _scan(self):
        result = _worktree_snapshot(self.root)
        with self.lock:
            self.result = result
            self.ready = True
            self.running = False


def _salvage_files(root, baseline):
    try:
        result = subprocess.run(["git", "-C", root, "status", "--porcelain"], capture_output=True,
                                text=True, timeout=5)
        if result.returncode == 0:
            rows = result.stdout.splitlines()
            return rows[:200], len(rows) > 200
    except (OSError, subprocess.TimeoutExpired):
        pass
    rows = []
    entries = 0
    if baseline is None:
        return ["(baseline unavailable: worktree scan exceeded its bound)"], False
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in {".git", "node_modules", "build", "dist", "target", ".next", ".dart_tool"}]
        entries += len(dirs) + len(files)
        if entries > 20000:
            return rows[:200], True
        for name in files:
            path = os.path.join(current, name)
            try:
                relative = os.path.relpath(path, root)
                mtime = os.stat(path).st_mtime_ns
                if relative not in baseline or mtime != baseline[relative]:
                    rows.append(relative)
            except OSError:
                pass
    return rows[:200], len(rows) > 200


def run_worker(argv, cwd, out_path, err_path, timeout, stall, initial_snapshot=_UNSET,
               cmd_stall=1800, out_offset=0, err_offset=0, watch_paths=(), worker=None, child_env=None,
               on_first_output=None):
    """Run argv in its own process group; return exit, timeout, stall or spawn."""
    if worker == "relay":
        env = {}
        for name, value in os.environ.items():
            if name in ("PATH", "HOME", "LANG", "TMPDIR", "TERM") or name.startswith("LC_"):
                env[name] = value
        env.setdefault("PATH", os.defpath)
        env.setdefault("HOME", os.path.expanduser("~"))
        env.update(child_env or {})
        redactions = _secret_values(env)
        out_buffer, err_buffer = bytearray(), bytearray()
        buffer_lock = threading.Lock()
    else:
        env = dict(os.environ)
        env["TASK_RUNNER_ROLE"] = "worker"
        env.update(child_env or {})
        redactions = []
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if initial_snapshot is _UNSET:
        initial_snapshot = _worktree_snapshot(cwd)
    with open(out_path, "ab") as out, open(err_path, "ab") as err:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE if worker == "relay" else out,
                                    stderr=subprocess.PIPE if worker == "relay" else err,
                                    start_new_session=True)
        except OSError as error:
            return "spawn", "%s: %s" % (argv[0], error.strerror or error.__class__.__name__)
        reader_threads = []
        captured_size = [0]
        if worker == "relay":
            def drain(stream, buffer):
                try:
                    while True:
                        chunk = stream.read1(65536)
                        if not chunk:
                            return
                        with buffer_lock:
                            buffer.extend(chunk)
                            captured_size[0] += len(chunk)
                finally:
                    stream.close()
            for stream, buffer in ((proc.stdout, out_buffer), (proc.stderr, err_buffer)):
                thread = threading.Thread(target=drain, args=(stream, buffer), daemon=True)
                thread.start()
                reader_threads.append(thread)

        def finish_relay_capture():
            if worker != "relay":
                return
            for thread in reader_threads:
                thread.join()
            out_text = redact_text(out_buffer.decode("utf-8", "replace"), redactions)
            err_text = redact_text(err_buffer.decode("utf-8", "replace"), redactions)
            with open(out_path, "ab") as out:
                out.write(out_text.encode("utf-8"))
            with open(err_path, "ab") as err:
                err.write(err_text.encode("utf-8"))
        started = time.monotonic()
        last_live = started
        last_size = file_size(out_path) + file_size(err_path)
        last_cpu = _worker_processes(proc.pid, _process_table())
        tracked_pids = set(last_cpu)
        last_cpu_poll = started
        snapshots = _SnapshotPoller(cwd, initial_snapshot)
        tracker = CommandTracker(worker or os.path.basename(argv[0]), out_path, err_path, out_offset, err_offset)
        watch_sizes = {path: file_size(path) for path in watch_paths}
        last_command_signal = started
        command_was_active = False
        interval = min(0.5, max(0.05, min(timeout, stall) / 10.0))
        warned = False
        first_output_seen = False
        watcher_warning_bytes = 0

        def sample_first_output():
            nonlocal first_output_seen
            if first_output_seen:
                return
            if worker == "relay":
                with buffer_lock:
                    output_bytes = captured_size[0]
            else:
                output_bytes = max(0, file_size(out_path) - out_offset)
                output_bytes += max(0, file_size(err_path) - err_offset - watcher_warning_bytes)
            if output_bytes > 0:
                first_output_seen = True
                if on_first_output:
                    on_first_output(time.monotonic())

        sample_first_output()
        try:
            while proc.poll() is None:
                try:
                    proc.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    pass
                current = time.monotonic()
                sample_first_output()
                size = file_size(out_path) + file_size(err_path)
                if worker == "relay":
                    with buffer_lock:
                        size += captured_size[0]
                live = False
                if size > last_size:
                    last_size = size
                    live = True
                if current - started >= timeout:
                    _kill_worker(proc.pid, tracked_pids)
                    proc.wait()
                    finish_relay_capture()
                    sample_first_output()
                    return "timeout", "%ds" % timeout
                tracker.poll()
                current = time.monotonic()
                if current - started >= timeout:
                    _kill_worker(proc.pid, tracked_pids)
                    proc.wait()
                    finish_relay_capture()
                    sample_first_output()
                    return "timeout", "%ds" % timeout
                command_active = tracker.in_progress
                if command_active and not command_was_active:
                    last_command_signal = current
                command_was_active = command_active
                signal_live = False
                if current - last_cpu_poll >= float(os.environ.get("TASK_CPU_POLL", "5")):
                    cpu = _worker_processes(proc.pid, _process_table(), tracked_pids)
                    tracked_pids.update(cpu)
                    if _cpu_increased(last_cpu, cpu):
                        live = True
                        signal_live = True
                    last_cpu = cpu
                    last_cpu_poll = current
                if snapshots.poll(current):
                    live = True
                    signal_live = True
                for path in watch_sizes:
                    size = file_size(path)
                    if size > watch_sizes[path]:
                        live = True
                        signal_live = True
                    watch_sizes[path] = size
                if live:
                    last_live = current
                if signal_live:
                    last_command_signal = current
                idle = current - last_live
                if idle >= stall and not warned:
                    warning = "STALL-WARN %ds no activity\n" % int(idle)
                    with open(err_path, "a") as warn:
                        warn.write(warning)
                    watcher_warning_bytes += len(warning.encode("utf-8"))
                    last_size = file_size(out_path) + file_size(err_path)
                    warned = True
                if command_active and current - last_command_signal >= cmd_stall:
                    _kill_worker(proc.pid, tracked_pids)
                    proc.wait()
                    finish_relay_capture()
                    sample_first_output()
                    return "stall", "%ds command in progress without CPU/file/watch activity" % int(
                        current - last_command_signal)
                if not command_active and idle >= 3 * stall:
                    _kill_worker(proc.pid, tracked_pids)
                    proc.wait()
                    finish_relay_capture()
                    sample_first_output()
                    return "stall", "%ds no activity" % int(idle)
        except BaseException:
            _kill_worker(proc.pid, tracked_pids)
            proc.wait()
            finish_relay_capture()
            sample_first_output()
            raise
        kill_group(proc.pid)  # stray children of a finished worker
        finish_relay_capture()
        sample_first_output()
        return "exit", proc.returncode


def acquire_lock(path, wait):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+")
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(0.2)


# ---------------------------------------------------------------- run + finalize

def read_from(path, offset):
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def work_dir(reg):
    return reg.get("worktree") or reg["cwd"]


def execute(reg, message, start, timeout, stall, cmd_stall=1800):
    """Run one CLI turn for reg, update and save the registry."""
    worker = reg["worker"]
    run_started = time.monotonic()
    run = {"started": precise_now(), "finished": None, "log_start": None, "log_end": None,
           "first_output_s": None,
           "timeout": timeout, "stall": stall, "cmd_stall": cmd_stall}
    reg.setdefault("runs", []).append(run)
    reg["state"] = "RUNNING"
    reg["pid"] = os.getpid()
    save_reg(reg)
    lock = None
    old_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        old_handlers[sig] = signal.signal(sig, _on_signal)
    try:
        if worker == "gemini" and os.environ.get("TASK_AGY_SERIAL") == "1":
            wait = float(os.environ.get("TASK_LOCK_WAIT") or DEFAULT_LOCK_WAIT)
            lock = acquire_lock(os.path.join(log_dir(), "agy.lock"), wait)
            if lock is None:
                return finish(reg, "FAIL", {"state": "FAIL", "answer": "queued too long: another agy run held "
                                            "the machine lock for %ds" % wait})
        argv = build_argv(worker, reg["model"], work_dir(reg), reg.get("session"), message, start,
                          reg.get("effort"), bool(reg.get("worktree")), reg.get("alias"))
        child_env = None
        if reg.get("alias") == CHATANYWHERE:
            key_cmd = os.environ.get("TASK_CHATANYWHERE_KEY_CMD")
            command = shlex.split(key_cmd) if key_cmd else ["security", "find-generic-password", "-s",
                                                           "task-chatanywhere-key", "-w"]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                result = None
            key = result.stdout.strip() if result and result.returncode == 0 else ""
            if not key:
                return finish(reg, "FAIL", {"state": "FAIL", "answer": "chatanywhere key unavailable in Keychain"})
            child_env = {"CHATANYWHERE_API_KEY": key}
            _REDACTION_VALUES.add(key)
        if worker == "relay":
            base_url = os.environ.get("RELAY_BASE_URL", "").strip()
            if not base_url:
                return finish(reg, "FAIL", {"state": "FAIL", "answer": "relay configuration missing: RELAY_BASE_URL is not set"})
            key_cmd = os.environ.get("TASK_RELAY_KEY_CMD")
            command = shlex.split(key_cmd) if key_cmd else ["security", "find-generic-password", "-s", "task-relay-key", "-w"]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                result = None
            key = result.stdout.strip() if result and result.returncode == 0 else ""
            if not key:
                return finish(reg, "FAIL", {"state": "FAIL", "answer": "relay authentication key unavailable in Keychain"})
            child_env = {"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_AUTH_TOKEN": key,
                         "CLAUDE_CONFIG_DIR": os.path.join(os.environ.get("HOME", os.path.expanduser("~")), ".claude/state/relay-config")}
            _REDACTION_VALUES.add(key)
            os.makedirs(child_env["CLAUDE_CONFIG_DIR"], mode=0o700, exist_ok=True)
            os.chmod(child_env["CLAUDE_CONFIG_DIR"], 0o700)
        out_path = reg["raw_log"]
        err_path = out_path + ".err"
        out_off, err_off = file_size(out_path), file_size(err_path)
        baseline = _worktree_snapshot(work_dir(reg))
        run["log_start"] = out_off
        save_reg(reg)
        try:
            kind, detail = run_worker(argv, work_dir(reg), out_path, err_path, timeout, stall, baseline,
                                      cmd_stall, out_off, err_off, reg.get("watch_paths", []), worker, child_env,
                                      on_first_output=lambda seen: record_first_output(reg, run,
                                                                                      seen - run_started))
        except Interrupted as error:
            return finish(reg, "FAIL", {"state": "FAIL", "answer": "interrupted (%s); worker killed" % error})
        raw = read_from(out_path, out_off)
        stderr_text = read_from(err_path, err_off)
        if worker == "perch":
            data = parse_perch(raw)
            events = []
            worker_text = perch_text(data)
        else:
            data = None
            events = parse_events(raw)
            if worker == "relay" and len(events) == 1 and isinstance(events[0].get("result"), str):
                events[0] = dict(events[0], type="assistant", text=events[0]["result"])
            worker_text = final_text(events, raw)
        if not reg.get("session"):
            if worker == "perch":
                found = data.get("threadId") if data else None
                reg["session"] = found if isinstance(found, str) and found else None
            else:
                reg["session"] = find_session(events, worker)
        if kind == "spawn":
            return finish(reg, "FAIL", {"state": "FAIL", "answer": "cannot run worker CLI: " + detail})
        if kind in ("timeout", "stall"):
            reason = kind
            reg["kill_reason"] = reason
            partial_source = worker_text if worker == "perch" else assistant_text(events)
            partial = redact_text("\n".join(partial_source.splitlines()[-5:])) or "(none)"
            files, truncated = _salvage_files(work_dir(reg), baseline)
            salvage_path = out_path + ".report"
            with open(salvage_path, "w") as salvage:
                salvage.write("partial:\n%s\nfiles:\n%s\n" % (partial, "\n".join(files) or "(none)"))
                if truncated:
                    salvage.write("files truncated; see registry for retained paths\n")
            report = {"state": "FAIL", "answer": "killed: %s %s; worker process tree killed" % (reason, detail),
                      "partial": partial, "files": files, "files_truncated": truncated,
                      "salvage_file": salvage_path}
            return finish(reg, "FAIL", report)
        if worker == "perch":
            # Only a structured failure is QUOTA; an ok:true answer is never
            # reclassified from stderr or its own text.
            failed = data is None or perch_failure(data)
            quota = perch_limit(data, stderr_text if failed else "")
            if quota:
                return finish(reg, "QUOTA", {"state": "QUOTA",
                                             "answer": "quota or rate limit: " + quota})
            if failed:
                rejected = perch_rejected(data, stderr_text)
                if rejected:
                    return finish(reg, "FAIL", {"state": "FAIL", "answer": rejected})
                auth = perch_auth(data, stderr_text)
                if auth:
                    return finish(reg, "FAIL", {"state": "FAIL", "answer": auth})
            if data is None:
                stderr_line = next((line.strip()[:200] for line in stderr_text.splitlines()
                                    if line.strip()), "")
                answer = "perch returned no JSON (exit %s)" % detail
                if stderr_line:
                    answer += ": " + stderr_line
                return finish(reg, "FAIL", {"state": "FAIL", "answer": answer})
            if perch_failure(data):
                return finish(reg, "FAIL", {"state": "FAIL",
                                            "answer": "perch turn failed: " + perch_error(data)})
            if not perch_text(data).strip():
                return finish(reg, "FAIL", {"state": "FAIL", "answer": "perch returned no assistant text"})
        elif worker == "relay":
            result = events[0] if events else None
            if result is None:
                reason = next((line.strip()[:200] for line in stderr_text.splitlines() if line.strip()),
                              "CLI exited with code %s" % detail if detail else "no JSON result")
                return finish(reg, "FAIL", {"state": "FAIL", "answer": "relay failed: " + reason})
            if result.get("is_error") or result.get("error"):
                error = str(result.get("error") or result.get("result") or "turn failed")[:200]
                if QUOTA_RE.search(error):
                    return finish(reg, "QUOTA", {"state": "QUOTA", "answer": "quota or rate limit: " + error})
                return finish(reg, "FAIL", {"state": "FAIL", "answer": "relay turn failed: " + error})
            if detail != 0:
                reason = next((line.strip()[:160] for line in stderr_text.splitlines() if line.strip()),
                              "CLI exited with code %s" % detail)
                if QUOTA_RE.search(reason):
                    return finish(reg, "QUOTA", {"state": "QUOTA", "answer": "quota or rate limit: " + reason})
                return finish(reg, "FAIL", {"state": "FAIL", "answer": "relay failed: " + reason})
            if not worker_text.strip():
                return finish(reg, "FAIL", {"state": "FAIL", "answer": "relay returned no assistant text"})
        report = parse_contract(worker_text)
        if report["answer"] == "no result contract" and len(report) == 2:
            if worker != "perch":
                quota = quota_line(events, stderr_text)
                if quota:
                    return finish(reg, "QUOTA", {"state": "QUOTA", "answer": "quota or rate limit: " + quota})
                provider = next((line.strip()[:200] for line in stderr_text.splitlines()
                                 if PROVIDER_RE.search(line)), None)
                if provider:
                    return finish(reg, "FAIL", {"state": "FAIL", "answer": "provider error: " + provider})
            report["evidence"] = "worker exit code %s" % detail
        return finish(reg, report["state"], report)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        if lock is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()


def finish(reg, state, report):
    runs = reg.get("runs") or []
    if runs and runs[-1].get("finished") is None:
        run = runs[-1]
        run["finished"] = precise_now()
        if run.get("log_start") is not None:
            run["log_end"] = file_size(reg.get("raw_log", ""))
    reg["state"] = state
    reg["report"] = report
    reg.pop("pid", None)
    if state == "QUOTA":
        mark_quota_blocked(reg.get("alias"))
    sanitized = redact_object(reg)
    reg.clear()
    reg.update(sanitized)
    save_reg(reg)


def record_first_output(reg, run, elapsed):
    if run.get("first_output_s") is None:
        run["first_output_s"] = round(max(0.0, elapsed), 1)
        save_reg(reg)


# ---------------------------------------------------------------- rendering

def render(reg):
    reg = redact_object(reg)
    state = reg["state"]
    report = reg.get("report") or {}
    lines = ["%s | %s" % (display_name(reg), state)]
    if state == "QUOTA":
        alias = reg.get("alias") or model_alias(reg.get("worker"), reg.get("model"))
        if alias:
            lines.append("HINT: task off " + alias)
    salvage = report.get("files")
    if isinstance(salvage, list):
        lines.append("ANSWER: " + (report.get("answer") or "").splitlines()[0])
        lines.append("PARTIAL:")
        lines.extend((report.get("partial") or "(none)").splitlines()[:5])
        lines.append("FILES:")
        shown = salvage[:12]
        lines.extend(shown or ["(none)"])
        omitted = max(0, len(salvage) - len(shown))
        if omitted or report.get("files_truncated"):
            note = "... %d additional path(s) omitted" % omitted
            if report.get("files_truncated"):
                note += "; file list capped at 200"
            note += "; full salvage: " + (report.get("salvage_file") or "registry report")
            lines.append(note)
        lines.append("RAW: " + (reg.get("raw_log") or ""))
        return "\n".join(lines), EXIT_CODES.get(state, 1)
    body = []
    for key in ("answer", "evidence", "missing"):
        value = (report.get(key) or "").strip()
        if value:
            body.extend((key.upper() + ": " + value).splitlines())
    if len(body) > STATUS_BODY_LINES:
        body = body[:STATUS_BODY_LINES - 1] + ["... (truncated; see raw log)"]
    lines.extend(body)
    lines.append("RAW: " + (reg.get("raw_log") or ""))
    return "\n".join(lines), EXIT_CODES.get(state, 1)


def emit(reg):
    text, code = render(reg)
    print(text)
    return code


# ---------------------------------------------------------------- commands

def make_worktree(cwd, task_id):
    probe = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise UsageError("--write needs a git repository at " + cwd)
    repo = probe.stdout.strip()
    path = os.path.join(repo, ".worktrees", task_id)
    added = subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "task/" + task_id, path, "HEAD"],
                           capture_output=True, text=True)
    if added.returncode != 0:
        raise UsageError("git worktree add failed: " + (added.stderr or added.stdout).strip())
    return path


def default_stall(alias):
    # Gemini 4 models think in long silent steps (Argon, Carbon about an hour per bench run): Sol-like stall.
    slow = ("sol", "chatanywhere", "hydrogen", "argon", "carbon", "relay-sonnet", "relay-haiku", "relay-opus")
    return 900 if alias == "perch-deepseek" else 300 if alias in slow else 180


def resolve_watch_paths(paths, base, existing=()):
    result = list(existing)
    for path in paths or []:
        path = os.path.expanduser(path)
        resolved = os.path.abspath(path if os.path.isabs(path) else os.path.join(base, path))
        if resolved not in result:
            result.append(resolved)
    return result


def cmd_start(args):
    if len(args.positionals) == 3:
        worker, task_id, brief_path = args.positionals
        if worker not in DEFAULT_MODELS:
            raise UsageError("unknown worker: " + worker)
    elif len(args.positionals) == 2:
        worker, task_id, brief_path = None, args.positionals[0], args.positionals[1]
    else:
        raise UsageError("usage: task start [worker] id brief-file (--row ROW or worker/model)")
    explicit_model = args.model
    if worker == "relay" and explicit_model and not RELAY_MODEL_RE.fullmatch(explicit_model):
        raise UsageError("invalid relay model id: use letters, digits, dot, underscore, colon or hyphen")
    alias = model_alias(worker, explicit_model) if explicit_model else None
    if explicit_model and not alias:
        raise UsageError("unsupported model: " + explicit_model)
    if explicit_model in MODELS and alias == explicit_model:
        explicit_model = MODELS[alias][1]
    if args.row:
        row = args.row
    elif alias:
        row = ROW_OF_ALIAS[alias]
    elif worker:
        default_alias = next((name for name, (_, model) in MODELS.items()
                              if MODELS[name][0] == worker and DEFAULT_MODELS[worker] == model), None)
        if not default_alias:
            raise UsageError("--row is required when deriving a model from a worker")
        alias = default_alias
        row = ROW_OF_ALIAS[alias]
    else:
        raise UsageError("--row or worker/model is required")
    if alias is None and worker:
        alias = next((name for name in ROWS[row] if MODELS[name][0] == worker), None)
        if explicit_model:
            raise UsageError("worker/model must name a supported model")
    if alias is None:
        alias = ROWS[row][0]
    if alias not in ROWS[row] and alias not in OWNER_ONLY:
        raise UsageError("worker/model does not belong to row " + row)
    if worker and worker != MODELS[alias][0]:
        raise UsageError("worker/model mismatch")
    if args.pinned and not (worker and explicit_model):
        raise UsageError("--pinned requires an explicit worker and --model")
    if alias == "perch-deepseek" and not args.pinned:
        raise UsageError("perch-deepseek is explicitly named: add --pinned "
                         "(task start perch <id> <brief> --model perch-deepseek --pinned)")
    if alias in ("relay-sonnet", "relay-haiku", "relay-opus") and not args.pinned:
        raise UsageError("relay is explicitly named: add --pinned "
                         "(task start relay <id> <brief> --model relay-sonnet --pinned)")
    worker, default_model = MODELS[alias]
    model = explicit_model or default_model
    if explicit_model is None and alias == "flash":
        model = "gemini-3.8-flash-medium" if row == "search" else "gemini-3.8-flash-high"
    state = worker_switches()
    if args.pinned and not model_on(alias, state):
        print("BLOCKED: %s is off (explicitly named); ask the user" % model)
        return 2
    try:
        chosen, worker, resolved_model = resolve_row(row, preferred=alias, state=state)
    except UsageError as error:
        print("BLOCKED: " + str(error))
        return 2
    fallback = None
    if chosen != alias:
        fallback = {"from": alias, "to": chosen}
        print("FALLBACK: %s off -> %s" % (alias, chosen))
    model = explicit_model if chosen == alias and explicit_model else resolved_model
    if not ID_RE.match(task_id):
        raise UsageError("bad task id (use letters, digits, . _ -): " + task_id)
    if os.path.exists(reg_path(task_id)):
        raise UsageError("task id already exists: %s (pick a new id)" % task_id)
    try:
        with open(brief_path, "rb") as handle:
            brief = handle.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise UsageError("cannot read brief file: %s" % error)
    cwd = os.path.abspath(args.cwd or os.getcwd())
    if not os.path.isdir(cwd):
        raise UsageError("cwd is not a directory: " + cwd)
    worktree = make_worktree(cwd, task_id) if args.write else None
    watch_paths = resolve_watch_paths(args.watch, worktree or cwd)
    prompt = brief + ("" if brief.endswith("\n") else "\n") + "\n" + CONTRACT_FOOTER
    os.makedirs(log_dir(), exist_ok=True)
    reg = {
        "id": task_id, "worker": worker, "model": model, "alias": chosen, "row": row, "effort": args.effort,
        "fallback": fallback,
        "cwd": cwd, "worktree": worktree, "session": None, "state": "RUNNING", "replies": 0,
        "raw_log": os.path.join(log_dir(), task_id + ".log"), "report": {},
        "timeout": args.timeout, "watch_paths": watch_paths, "cmd_stall": args.cmd_stall,
        "started": now(), "updated": now(),
    }
    stall = args.stall if args.stall is not None else default_stall(chosen)
    execute(reg, prompt, True, args.timeout, stall, args.cmd_stall)
    if twin_hop(reg, args.pinned, prompt, args):
        print("FALLBACK: %s %s -> %s" % (reg["fallback"]["from"], reg["fallback"]["reason"],
                                         reg["alias"]))
    return emit(reg)


def twin_hop(reg, pinned, prompt, args):
    """Re-run once on the twin after QUOTA or a provider error; never when pinned."""
    twin = TWINS.get(reg.get("alias"))
    report = reg.get("report") or {}
    answer = str(report.get("answer", ""))
    failed_start = reg["state"] == "FAIL" and answer.startswith("provider error")
    if pinned or not twin or not (reg["state"] == "QUOTA" or failed_start):
        return False
    if not model_on(twin, worker_switches()):
        return False
    prior = reg.get("fallback")
    reason = "quota" if reg["state"] == "QUOTA" else "provider error"
    reg["fallback"] = {"from": reg["alias"], "to": twin, "reason": reason}
    if prior:
        reg["fallback"]["prior"] = prior
    reg["worker"], reg["model"] = MODELS[twin]
    reg["alias"] = twin
    reg["session"] = None
    reg["report"] = {}
    execute(reg, prompt, True, args.timeout, args.stall if args.stall is not None else default_stall(twin),
            args.cmd_stall)
    return True


def cmd_reply(args):
    reg = refresh(load_reg(args.id))
    if reg["state"] == "RUNNING":
        raise UsageError("task %s is still running" % args.id)
    if reg.get("replies", 0) >= MAX_REPLIES:
        raise UsageError("reply limit reached; escalate or ask the user")
    if not reg.get("session"):
        raise UsageError("worker gave no session id; start a new task")
    timeout = args.timeout if args.timeout is not None else max(1, int(reg.get("timeout", 1800) / 2))
    stall = args.stall if args.stall is not None else default_stall(reg.get("alias") or model_alias(reg["worker"], reg["model"]))
    cmd_stall = args.cmd_stall if args.cmd_stall is not None else reg.get("cmd_stall", 1800)
    reg["watch_paths"] = resolve_watch_paths(args.watch, work_dir(reg), reg.get("watch_paths", []))
    reg["replies"] = reg.get("replies", 0) + 1
    message = args.message
    if CONTRACT_FOOTER not in message:
        message += ("" if message.endswith("\n") else "\n") + "\n" + CONTRACT_FOOTER
    execute(reg, message, False, timeout, stall, cmd_stall)
    return emit(reg)


def cmd_test(args):
    names = args.workers
    if not names:
        raise UsageError("usage: task test <worker> [<worker> ...] [--resume] [--verbose]; "
                         "valid workers: "
                         + ", ".join(TEST_ALIASES))
    for name in names:
        if name not in TEST_ALIASES:
            raise UsageError("unknown worker: %s; valid workers: %s" % (name, ", ".join(TEST_ALIASES)))

    switches = worker_switches(readonly=True)
    counts = {"ok": 0, "fail": 0, "skip": 0}
    for name in names:
        alias = TEST_ALIAS_MAP.get(name, name)
        worker, model = MODELS[alias]
        effort = "low" if alias in ("luna", "sol", "chatanywhere") else None
        if alias == "flash":
            model = "gemini-3.8-flash-low"
        if alias == CHATANYWHERE:
            model = alias
        switch_on = (switches.get(alias, True) if alias == CHATANYWHERE
                     else switches.get(alias, True) and switches.get(worker, True))
        start_seconds = 0.0
        reply_seconds = 0.0
        outputs = []
        detail = ""
        status = "FAIL"
        failure_phase = "start"
        failure_code = 1
        try:
            with tempfile.TemporaryDirectory(prefix="task-test-") as temp:
                isolated_state = os.path.join(temp, "state")
                isolated_logs = os.path.join(temp, "logs")
                cwd = os.path.join(temp, "work")
                os.makedirs(cwd)
                brief = os.path.join(temp, "brief.txt")
                with open(brief, "w") as handle:
                    if args.resume:
                        handle.write("Remember the word pelican.\n")
                    else:
                        handle.write("Reply with STATE: DONE and ANSWER: ok.\n")
                old_state = os.environ.get("TASK_STATE_DIR")
                old_logs = os.environ.get("TASK_LOG_DIR")
                os.environ["TASK_STATE_DIR"] = isolated_state
                os.environ["TASK_LOG_DIR"] = isolated_logs
                try:
                    task_id = "test-%s-%d" % (alias, int(time.time() * 1000))
                    start_args = argparse.Namespace(
                        positionals=[worker, task_id, brief], model=model, effort=effort,
                        row=ROW_OF_ALIAS.get(alias), pinned=alias in OWNER_ONLY,
                        write=False, cwd=cwd, timeout=default_timeout(alias), stall=None,
                        cmd_stall=1800, watch=[])
                    output = io.StringIO()
                    begin = time.monotonic()
                    with contextlib.redirect_stdout(output):
                        start_code = cmd_start(start_args)
                    start_seconds = time.monotonic() - begin
                    start_reg = load_reg(task_id)
                    outputs.append(_test_full_report(start_reg))
                    if start_code == 4:
                        status = "SKIP"
                        detail = _test_answer(start_reg)
                    elif start_code != 0:
                        failure_code = start_code
                        detail = _test_error(start_reg)
                    elif args.resume:
                        reply_args = argparse.Namespace(id=task_id, message="What word?",
                                                        timeout=default_timeout(alias), stall=None,
                                                        cmd_stall=None, watch=[])
                        output = io.StringIO()
                        begin = time.monotonic()
                        with contextlib.redirect_stdout(output):
                            reply_code = cmd_reply(reply_args)
                        reply_seconds = time.monotonic() - begin
                        reply_reg = load_reg(task_id)
                        outputs.append(_test_full_report(reply_reg))
                        answer = _test_answer(reply_reg)
                        if reply_code == 4:
                            status, detail = "SKIP", answer
                        elif reply_code != 0:
                            failure_phase = "reply"
                            failure_code = reply_code
                            detail = _test_error(reply_reg)
                        elif "pelican" not in answer.lower():
                            failure_phase = "reply"
                            failure_code = reply_code
                            detail = "reply did not contain pelican"
                        else:
                            status = "ok"
                    else:
                        status = "ok"
                finally:
                    if old_state is None:
                        os.environ.pop("TASK_STATE_DIR", None)
                    else:
                        os.environ["TASK_STATE_DIR"] = old_state
                    if old_logs is None:
                        os.environ.pop("TASK_LOG_DIR", None)
                    else:
                        os.environ["TASK_LOG_DIR"] = old_logs
        except Exception as error:
            detail = str(error).splitlines()[0] if str(error) else error.__class__.__name__
        counts[status.lower()] += 1
        duration = " start %.1fs" % start_seconds
        if args.resume:
            duration += "  reply %.1fs" % reply_seconds
        if status == "ok":
            print("ok    %-16s%s   (switch: %s)" %
                  (name, duration, "on" if switch_on else "off"))
        elif status == "SKIP":
            print("SKIP  %-16s%s quota: %s (switch: %s)" %
                  (name, duration, _test_first_line(detail or "quota"),
                   "on" if switch_on else "off"))
        else:
            print("FAIL  %-16s %s exit %s: %s%s (switch: %s)" %
                  (name, failure_phase, failure_code, _test_first_line(detail or "worker test failed"),
                   duration, "on" if switch_on else "off"))
        if args.verbose:
            for report in outputs:
                print(report, end="" if report.endswith("\n") else "\n")
            if not outputs and detail:
                print(detail)
    print("SUMMARY ok=%d fail=%d skip=%d" % (counts["ok"], counts["fail"], counts["skip"]))
    return 1 if counts["fail"] else 0


def default_timeout(alias):
    if alias == "perch-deepseek":
        return 900
    if alias in ("sol", "hydrogen", "argon", "carbon", "relay-sonnet", "relay-haiku", "relay-opus"):
        return 300
    return 180


def _test_answer(reg):
    return (reg.get("report") or {}).get("answer", "")


def _test_error(reg):
    report = reg.get("report") or {}
    return _test_first_line(report.get("answer") or report.get("state") or "worker failed")


def _test_first_line(value):
    return str(value).splitlines()[0] if value else ""


def _test_full_report(reg):
    report = reg.get("report") or {}
    return "%s | %s\nREPORT:\n%s\n" % (
        display_name(reg), reg.get("state", "FAIL"), json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_status(args):
    reg = refresh(load_reg(args.id))
    text, code = render(reg)
    runs = reg.get("runs") or []
    run = runs[-1] if runs else {}
    first_output = run.get("first_output_s")
    started, finished = run.get("started"), run.get("finished")
    if isinstance(first_output, (int, float)) and started and finished:
        try:
            start_time = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
            finish_time = datetime.datetime.fromisoformat(finished.replace("Z", "+00:00"))
            total = max(0.0, (finish_time - start_time).total_seconds())
        except (TypeError, ValueError):
            total = None
        if total is not None:
            lines = text.splitlines()
            lines.insert(-1, "timing: first output %.1fs, total %.1fs" % (first_output, total))
            text = "\n".join(lines)
    print(text)
    return code


def cmd_list(args):
    try:
        names = sorted(name for name in os.listdir(state_dir()) if name.endswith(".json"))
    except OSError:
        names = []
    for name in names:
        try:
            task_id = name[:-5]
            reg = load_reg(task_id)
        except UsageError:
            continue
        if (reg.get("id") != task_id or reg.get("state") not in STATES
                or not isinstance(reg.get("worker"), str)
                or reg.get("worker") not in DEFAULT_MODELS
                or not isinstance(reg.get("model"), str)):
            continue
        reg = refresh(reg)
        print("%s | %s | %s" % (display_name(reg), reg["state"], reg.get("updated", "")))
    return 0


def cmd_switch(args):
    switch_file_update(args.name, args.command == "on")
    return 0


def cmd_workers(args):
    state = worker_switches()
    for cli in DEFAULT_MODELS:
        print("%s: %s" % (cli, "on" if state[cli] else "off"))
        for alias, (worker, _) in MODELS.items():
            if worker == cli:
                note = "  (own switch, not tied to %s)" % cli if alias == CHATANYWHERE else ""
                print("  %s: %s%s" % (alias, "on" if state[alias] else "off", note))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="task", description="Run worker CLI tasks in resumable sessions.")
    sub = parser.add_subparsers(dest="command", required=True)
    test = sub.add_parser("test", help="check named workers once; --resume adds a second turn")
    test.add_argument("workers", nargs="*")
    test.add_argument("--resume", action="store_true",
                      help="add a second turn to check that the worker resumes the session")
    test.add_argument("--verbose", action="store_true", help="print each worker's full reports")
    start = sub.add_parser("start", help="start a task and wait for the worker to exit")
    start.add_argument("positionals", nargs="+")
    start.add_argument("--model")
    start.add_argument("--effort", choices=("low", "medium", "high", "xhigh"),
                       help="Codex reasoning effort for this task; default is the Codex config")
    start.add_argument("--row", choices=sorted(ROWS))
    start.add_argument("--pinned", action="store_true")
    start.add_argument("--write", action="store_true", help="work in a new worktree on branch task/<id>")
    start.add_argument("--cwd")
    start.add_argument("--timeout", type=int, default=1800)
    start.add_argument("--stall", type=int)
    start.add_argument("--cmd-stall", type=int, default=1800)
    start.add_argument("--watch", action="append", default=[])
    resolve = sub.add_parser("resolve", help="resolve the enabled CLI and model for a row")
    resolve.add_argument("row", choices=sorted(ROWS))
    reply = sub.add_parser("reply", help="send a follow-up message to the worker session")
    reply.add_argument("id")
    reply.add_argument("message")
    reply.add_argument("--timeout", type=int)
    reply.add_argument("--stall", type=int)
    reply.add_argument("--cmd-stall", type=int)
    reply.add_argument("--watch", action="append", default=[])
    status = sub.add_parser("status", help="print a task's state and report")
    status.add_argument("id")
    sub.add_parser("list", help="list all tasks")
    for name in ("on", "off"):
        switch = sub.add_parser(name, help="turn a worker/model on or off")
        switch.add_argument("name")
    sub.add_parser("workers", help="show worker/model switches")
    quota = sub.add_parser("quota", help="show quota signals for all workers")
    quota.add_argument("--md", action="store_true")
    usage = sub.add_parser("usage", help="show daily Claude and worker usage since the mark (requires optional daily-usage.py)")
    usage.add_argument("--mark", action="store_true", help="set the usage mark to now")
    usage.add_argument("--plain", action="store_true", help="print plain text instead of a Markdown table")
    clean = sub.add_parser("clean", help="clean up local state (also removes file-history, keeps synced tombstones)")
    clean.add_argument("what", choices=("sessions",), help="sessions: delete archived Claude chat transcripts")
    return parser


def find_optional_script(name):
    """Return the first existing regular file for an optional script, or None."""
    candidates = (
        os.path.join(os.path.dirname(os.path.realpath(__file__)), name),
        os.path.expanduser(os.path.join("~", ".claude", "scripts", name)),
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        for name in ("timeout", "stall", "cmd_stall"):
            value = getattr(args, name, None)
            if value is not None and value < 1:
                raise UsageError("--%s must be positive" % name.replace("_", "-"))
        if args.command == "quota":
            script = os.path.join(os.path.dirname(os.path.realpath(__file__)), "quota.py")
            return subprocess.call([sys.executable, script] + (["--md"] if args.md else []))
        if args.command == "usage":
            script = find_optional_script("daily-usage.py")
            if script is None:
                print("task usage: usage reports are not part of this plugin", file=sys.stderr)
                return 2
            return subprocess.call([sys.executable, script] + (["--mark"] if args.mark else [])
                                   + (["--plain"] if args.plain else []))
        if args.command == "clean":
            script = os.path.join(os.path.dirname(os.path.realpath(__file__)), "clean-sessions.py")
            return subprocess.call([sys.executable, script])
        return {"test": cmd_test, "start": cmd_start, "resolve": cmd_resolve, "reply": cmd_reply, "status": cmd_status, "list": cmd_list,
                "on": cmd_switch, "off": cmd_switch, "workers": cmd_workers}[args.command](args)
    except UsageError as error:
        print("task: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
