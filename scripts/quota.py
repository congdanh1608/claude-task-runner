#!/usr/bin/env python3
"""Summarize worker quota signals from local, non-credential sources."""
import concurrent.futures
import datetime
import importlib.util
import json
import math
import os
import re
import subprocess
import tempfile
import time
import argparse
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
MAX_SECONDS = 30.0
CLAUDE_USAGE_CACHE_SECONDS = 60
CLAUDE_STATUS_MAX_AGE = 15 * 60
CODEX_MAX_FILES = 400
CODEX_SCAN_SPAN = 2 * 24 * 3600
CODEX_CACHE = os.path.join(HOME, ".claude/state/codex-quota.json")
FRESH_READING_SECONDS = 60


class Deadline:
    def __init__(self, seconds=MAX_SECONDS):
        self.end = time.monotonic() + seconds

    def remaining(self):
        left = self.end - time.monotonic()
        if left <= 0:
            raise TimeoutError("quota deadline exceeded")
        return left


def read_json(path):
    with open(path) as handle:
        return json.load(handle)


def format_age(seconds):
    """Compact age of a reading, e.g. 12m ago / 3h ago / 5d ago."""
    minutes = max(0, int(seconds)) // 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return "%dm ago" % minutes
    hours = minutes // 60
    if hours < 24:
        return "%dh ago" % hours
    return "%dd ago" % (hours // 24)


def write_json_atomic(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    file_descriptor, temporary_path = tempfile.mkstemp(prefix=".quota.", dir=directory)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def is_finite_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def percent_left(used_percent):
    """Quota table columns always show the percentage still available."""
    return max(0.0, min(100.0, 100.0 - float(used_percent)))


def _codex_limits(obj):
    """The last rate_limits object in a session line that has a real window."""
    limits = obj.get("rate_limits")
    if not isinstance(limits, dict):
        payload = obj.get("payload")
        limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    for key in ("primary", "secondary"):
        item = limits.get(key)
        if isinstance(item, dict) and item.get("used_percent") is not None:
            return limits
    return None


def codex_reading(root, deadline=None):
    """Newest usable rate_limits reading bounded by file count and 2 days of sessions."""
    entries = []
    for directory, _, names in os.walk(root):
        if deadline:
            deadline.remaining()
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(directory, name)
            try:
                entries.append((os.path.getmtime(path), path))
            except OSError:
                continue
    if not entries:
        raise ValueError("no codex sessions")
    entries.sort(reverse=True)
    newest = entries[0][0]
    for index, (mtime, path) in enumerate(entries):
        if index >= CODEX_MAX_FILES or newest - mtime > CODEX_SCAN_SPAN:
            break
        found, at = None, None
        try:
            with open(path, errors="replace") as handle:
                for line in handle:
                    if deadline:
                        deadline.remaining()
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    limits = _codex_limits(obj)
                    if limits is not None:
                        found = limits
                        timestamp = obj.get("timestamp")
                        try:
                            at = parse_time(timestamp) if timestamp else mtime
                        except (TypeError, ValueError):
                            at = mtime
        except OSError:
            continue
        if found is not None:
            return found, mtime if at is None else at
    raise ValueError("no rate_limits event")


def codex_row(root, deadline=None):
    return codex_reading(root, deadline)[0]


def codex_usage(root, deadline=None, cache_path=None):
    """Live reading, else the last good one from the state cache."""
    cache_path = cache_path or CODEX_CACHE
    try:
        limits, at = codex_reading(root, deadline)
        try:
            write_json_atomic(cache_path, {"at": at, "limits": limits})
        except OSError:
            pass
        return limits, at
    except Exception:
        cached = read_json(cache_path)
        limits = cached.get("limits") if isinstance(cached, dict) else None
        at = cached.get("at") if isinstance(cached, dict) else None
        if not isinstance(limits, dict) or not is_finite_number(at):
            raise ValueError("no codex reading")
        return limits, at


def parse_epoch(value):
    if value is None:
        return None
    try:
        return parse_time(value)
    except (TypeError, ValueError, AttributeError):
        return None


def claude_row(path, now=None):
    limits, at = _claude_state_reading(path)
    return _claude_state_rows(limits, at, "statusline",
                              time.time() if now is None else now, annotate_age=False)


def _claude_state_reading(path):
    data = read_json(path)
    limits = data.get("rate_limits")
    if not isinstance(limits, dict) or not limits:
        raise ValueError("no rate limit data")
    at = parse_epoch(data.get("timestamp"))
    return limits, os.path.getmtime(path) if at is None else at


def _claude_state_rows(limits, at, source, now=None, annotate_age=False):
    now_epoch = time.time() if now is None else now
    result = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        item = limits.get(key)
        if not isinstance(item, dict) or item.get("used_percentage") is None:
            continue
        reset = item.get("resets_at")
        result.append(("Claude", "Claude Code",
                       _quota_cell(item["used_percentage"], parse_epoch(reset),
                                   now_epoch - at, now_epoch, annotate_age),
                       label, local_time(reset), "on",
                       "%s %s" % (source, format_age(now_epoch - at))))
    if not result:
        raise ValueError("no supported windows")
    return result


def _quota_cell(used_percent, reset_epoch, age, now=None, annotate_age=True, stale_zero=False):
    """Percentage left, with age when stale and an explicit mark after a reset."""
    now_epoch = time.time() if now is None else now
    if reset_epoch is not None and reset_epoch <= now_epoch:
        return "100% (reset)"
    left = percent_left(used_percent)
    cell = "%.0f%%" % left
    if annotate_age and age >= FRESH_READING_SECONDS:
        # A stale 0% must not read as "no quota right now": mark it as an old
        # observation. Non-zero readings already communicate their value with age.
        if stale_zero and left <= 0:
            cell += " (stale)"
        cell += " · " + format_age(age)
    return cell


def parse_claude_usage(raw, now=None):
    """Parse Claude's session and weekly percentages and reset timestamps."""
    lines = {"session": None, "weekly": None}
    zone_name = None
    for line in raw.splitlines():
        if zone_name is None:
            zone_match = re.search(r"\(([^()]+)\)\s*$", line)
            if zone_match:
                zone_name = zone_match.group(1).strip()
        if line.lstrip().startswith("Current session:"):
            key = "session"
        elif line.lstrip().startswith("Current week (all models):"):
            key = "weekly"
        else:
            continue
        match = re.search(
            r"(\d+(?:\.\d+)?)%\s+used\s*·\s*resets\s+"
            r"(?:(?P<date>[A-Za-z]+\s+\d{1,2}(?:,?\s+\d{4})?)\s+at\s+)?"
            r"(?P<clock>\d{1,2}(?::\d{2})?\s*[ap]m)", line, re.IGNORECASE)
        if not match:
            continue
        zone = None
        if zone_name:
            try:
                zone = ZoneInfo(zone_name)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("unknown Claude usage timezone: " + zone_name)
        reference = now if now is not None else (
            datetime.datetime.now(zone) if zone else datetime.datetime.now().astimezone())
        if zone:
            reference = reference.astimezone(zone)
        clock_text = re.sub(r"\s+", "", match.group("clock")).lower()
        clock_format = "%I:%M%p" if ":" in clock_text else "%I%p"
        clock = datetime.datetime.strptime(clock_text, clock_format).time()
        date_text = match.group("date")
        if date_text:
            date_match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?", date_text.strip())
            if not date_match:
                continue
            month = datetime.datetime.strptime(date_match.group(1), "%b").month
            day = int(date_match.group(2))
            explicit_year = date_match.group(3)
            years = [int(explicit_year)] if explicit_year else range(reference.year, reference.year + 9)
            reset = None
            for year in years:
                try:
                    candidate = datetime.datetime(year, month, day, clock.hour, clock.minute,
                                                  tzinfo=zone or reference.tzinfo)
                except ValueError:
                    continue
                if explicit_year or candidate >= reference:
                    reset = candidate
                    break
            if reset is None:
                continue
        else:
            reset = datetime.datetime.combine(reference.date(), clock, tzinfo=zone or reference.tzinfo)
            if reset < reference:
                reset += datetime.timedelta(days=1)
        lines[key] = {"percent": float(match.group(1)), "resets_at": reset.timestamp()}
    if lines["session"] is None or lines["weekly"] is None:
        raise ValueError("Claude usage lines missing or malformed")
    return lines


def claude_usage_rows(state_path=None, cache_path=None, now=None):
    state_path = state_path or os.path.join(HOME, ".claude/state/claude-rate.json")
    cache_path = cache_path or os.path.join(HOME, ".claude/state/claude-usage-cache.json")
    now_epoch = time.time() if now is None else now
    cache = None
    try:
        cache = read_json(cache_path)
        cached_at = cache.get("at")
        data = cache.get("usage")
        if (isinstance(cache, dict) and is_finite_number(cached_at)
                and 0 <= now_epoch - cached_at < CLAUDE_USAGE_CACHE_SECONDS
                and _valid_claude_usage(data)):
            earliest_reset = min(data[key]["resets_at"] for key in ("session", "weekly"))
            if now_epoch < earliest_reset:
                return _claude_usage_rows(data, "claude -p /usage", cached_at, now_epoch)
    except Exception:
        pass
    try:
        result = subprocess.run(["claude", "-p", "/usage"], cwd=HOME,
                                capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            raise ValueError("Claude usage command failed")
        usage = parse_claude_usage(result.stdout)
        try:
            write_json_atomic(cache_path, {"at": now_epoch, "usage": usage})
        except OSError:
            pass
        return _claude_usage_rows(usage, "claude -p /usage", now_epoch, now_epoch)
    except Exception:
        pass
    try:
        limits, at = _claude_state_reading(state_path)
        if 0 <= now_epoch - at < CLAUDE_STATUS_MAX_AGE:
            return _claude_state_rows(limits, at, "statusline", now_epoch)
    except Exception:
        pass
    # Never fall back to n/a while an older reading exists: show it with its age.
    if isinstance(cache, dict) and _valid_claude_usage(cache.get("usage")) \
            and is_finite_number(cache.get("at")):
        return _claude_usage_rows(cache["usage"], "cached usage", cache["at"], now_epoch, True)
    try:
        limits, at = _claude_state_reading(state_path)
        return _claude_state_rows(limits, at, "statusline", now_epoch, annotate_age=True)
    except Exception:
        return [("Claude", "Claude Code", "n/a (usage unavailable)", "n/a", "n/a",
                 "on", "n/a (usage unavailable)")]


def _claude_usage_rows(usage, source, at=None, now=None, annotate_age=False):
    now_epoch = time.time() if now is None else now
    reading_at = now_epoch if at is None else at
    rows = []
    for key, window in (("session", "5h"), ("weekly", "weekly")):
        value = usage[key]
        reset = parse_epoch(value.get("resets_at"))
        rows.append(("Claude", "Claude Code",
                     _quota_cell(value["percent"], reset, now_epoch - reading_at, now_epoch,
                                 annotate_age),
                     window, local_time(value.get("resets_at")), "on",
                     "%s %s" % (source, format_age(now_epoch - reading_at))))
    return rows


def _valid_claude_usage(usage):
    if not isinstance(usage, dict):
        return False
    for key in ("session", "weekly"):
        value = usage.get(key)
        if not isinstance(value, dict):
            return False
        percent = value.get("percent")
        resets_at = value.get("resets_at")
        if (not isinstance(percent, (int, float)) or isinstance(percent, bool) or not math.isfinite(percent)
                or not isinstance(resets_at, (int, float)) or isinstance(resets_at, bool)
                or not math.isfinite(resets_at)):
            return False
        try:
            datetime.datetime.fromtimestamp(resets_at, datetime.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return False
    return True


def parse_time(value):
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def local_time(value):
    if value is None:
        return "n/a"
    return datetime.datetime.fromtimestamp(parse_time(value)).astimezone().strftime("%Y-%m-%d %H:%M")


def _agy_groups(value):
    """Groups from either a command_result event or the final result event."""
    candidates = []
    command = value.get("command")
    if isinstance(command, dict):
        candidates.append(command.get("data"))
    result = value.get("result")
    if isinstance(result, dict):
        result_command = result.get("command")
        if isinstance(result_command, dict):
            candidates.append(result_command.get("data"))
    candidates.append(value.get("data"))
    for data in candidates:
        if isinstance(data, dict) and isinstance(data.get("groups"), list):
            return data["groups"]
    return None


def _agy_rows(groups):
    rows = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = group.get("name", "")
        if name not in ("Gemini Models", "Claude and GPT models"):
            continue
        worker = "Gemini" if name == "Gemini Models" else "agy-3p"
        model = "agy" if name == "Gemini Models" else "Claude and GPT models"
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            window = bucket.get("window")
            remaining = bucket.get("remaining_fraction")
            if remaining is None and bucket.get("remaining_percent") is not None:
                remaining = bucket["remaining_percent"] / 100.0
            if window not in ("weekly", "5h") or not is_finite_number(remaining):
                continue
            left = max(0.0, min(1.0, float(remaining))) * 100
            rows.append((worker, model, "%.0f%%" % left, window,
                         local_time(bucket.get("reset_time")), "on", "agy /quota"))
    return rows


def parse_agy_usage(raw):
    events = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    # command_result carries the structured data; result repeats it plus a text table.
    ordered = ([event for event in events if event.get("event") == "command_result"]
               + [event for event in events if event.get("event") != "command_result"])
    for event in ordered:
        groups = _agy_groups(event)
        if groups is None:
            continue
        rows = _agy_rows(groups)
        if rows:
            return rows
    raise ValueError("no supported usage buckets")


AGY_CACHE = os.path.join(HOME, ".claude/state/agy-quota.json")
AGY_CACHE_MAX_AGE = 6 * 3600
PERCH_CACHE = os.path.join(HOME, ".claude/state/perch-quota.json")
PERCH_CACHE_MAX_AGE = 15 * 60
PERCH_TIMEOUT = 15


def parse_perch_usage(raw):
    """Parse Perch's /usage assistant text; drift-tolerant and never raises."""
    usage = {"month": None, "five_hour": None, "weekly": None}
    if not isinstance(raw, str):
        return usage
    for line in raw.splitlines():
        text = line.strip()
        if text.startswith("-"):
            text = text[1:].strip()
        if not text:
            continue
        label, _, rest = text.partition(" ")
        rest = rest.strip()
        key_label = label.lower().rstrip(":").strip().replace(" ", "")
        if key_label not in ("month", "5h", "weekly"):
            continue
        if key_label == "month":
            match = re.search(r"([\d,]+)\s*(?:PT\s*)?/\s*([\d,]+)(?:\s*PT)?", rest)
            if not match:
                continue
            used = int(match.group(1).replace(",", ""))
            limit = int(match.group(2).replace(",", ""))
            percent = re.search(r"(\d+(?:\.\d+)?)\s*%", rest)
            usage["month"] = {
                "used": used,
                "limit": limit,
                "percent": float(percent.group(1)) if percent else (
                    min(100.0, used * 100.0 / limit) if limit > 0 else None),
            }
        elif key_label in ("5h", "weekly"):
            percent = re.search(r"(\d+(?:\.\d+)?)\s*%", rest)
            if not percent:
                continue
            frees_up = re.search(r"frees up in\s+(.+?)\s*$", rest)
            usage["five_hour" if key_label == "5h" else "weekly"] = {
                "percent": float(percent.group(1)),
                "frees_up": "frees up in " + frees_up.group(1) if frees_up else None,
            }
    return usage


def _has_perch_reading(usage):
    return isinstance(usage, dict) and any(
        isinstance(usage.get(key), dict) and usage[key]
        for key in ("month", "five_hour", "weekly"))


def _perch_cache(cache_path):
    try:
        cached = read_json(cache_path)
    except Exception:
        return None
    usage = cached.get("usage") if isinstance(cached, dict) else None
    at = cached.get("at") if isinstance(cached, dict) else None
    if is_finite_number(at) and _has_perch_reading(usage):
        return usage, float(at)
    return None


def _json_document(text):
    """Perch may print progress before its JSON payload."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("json document missing")
    return json.loads(text[start:end + 1])


def perch_usage(deadline=None, cache_path=None):
    """Call perch /usage (zero PT) and cache it; fall back to the last good reading."""
    cache_path = cache_path or PERCH_CACHE
    cached = _perch_cache(cache_path)
    now = time.time()
    if cached and now - cached[1] < PERCH_CACHE_MAX_AGE:
        return cached[0]
    try:
        result = subprocess.run(["perch", "run", "/usage", "--json"],
                                cwd=tempfile.gettempdir(), capture_output=True,
                                text=True, timeout=PERCH_TIMEOUT)
        if result.returncode != 0:
            raise ValueError("perch usage command failed")
        data = _json_document(result.stdout)
        if not isinstance(data, dict) or not data.get("ok"):
            raise ValueError("perch usage command failed")
        usage = parse_perch_usage(data.get("assistantText") or "")
        if not _has_perch_reading(usage):
            raise ValueError("perch usage empty")
        try:
            write_json_atomic(cache_path, {"at": now, "usage": usage})
        except OSError:
            pass
        return usage
    except Exception:
        if cached:
            usage = dict(cached[0])
            usage["cached_at"] = cached[1]
            return usage
        raise


def perch_rows(usage, switches):
    rows = []
    enabled = switch_state("perch-deepseek", switches)
    month = usage.get("month") if isinstance(usage, dict) else None
    if month:
        used, limit = month.get("used"), month.get("limit")
        if is_finite_number(used) and is_finite_number(limit) and limit > 0:
            left = percent_left(min(100.0, used * 100.0 / limit))
            # A few points left must never round down to 0% and a red row.
            shown = 0 if used >= limit else max(1, int(math.ceil(left)))
            remaining = max(0, int(limit - used))
            cell = "{}% · {:,} PT".format(shown, remaining)
        elif month.get("percent") is not None:
            cell = "%.0f%% left" % percent_left(month["percent"])
        else:
            cell = "{:,} PT / n/a".format(int(used)) if is_finite_number(used) else "n/a"
        rows.append(("Perch", "deepseek-flash-starter", cell, "month", "n/a", enabled, "perch /usage"))
    for key, window in (("five_hour", "5h"), ("weekly", "weekly")):
        item = usage.get(key) if isinstance(usage, dict) else None
        if isinstance(item, dict) and is_finite_number(item.get("percent")):
            rows.append(("Perch", "deepseek-flash-starter",
                         "%.0f%%" % percent_left(item["percent"]), window,
                         item.get("frees_up") or "n/a", enabled, "perch /usage"))
    if not rows:
        rows.append(("Perch", "deepseek-flash-starter", "n/a", "n/a", "n/a", enabled,
                     "perch /usage"))
    age_at = usage.get("cached_at") if isinstance(usage, dict) else None
    if rows and is_finite_number(age_at):
        note = " · " + format_age(max(0.0, time.time() - age_at))
        target = next((row for row in rows if row[3] == "weekly"), rows[0])
        index = rows.index(target)
        rows[index] = target[:2] + (target[2] + note,) + target[3:]
    return rows


def agy_usage(deadline=None):
    """Live agy /quota; when it is slow or fails, the last good reading if under 6h old."""
    try:
        result = subprocess.run(["agy", "-p", "/quota", "--output-format", "stream-json"],
                                cwd="/tmp", capture_output=True, text=True, timeout=deadline.remaining() if deadline else MAX_SECONDS)
        if result.returncode:
            raise ValueError("agy quota command failed")
        rows = parse_agy_usage(result.stdout)
        try:
            write_json_atomic(AGY_CACHE, {"at": time.time(), "rows": rows})
        except OSError:
            pass
        return rows
    except Exception:
        cached = read_json(AGY_CACHE)
        if not cached or time.time() - cached.get("at", 0) > AGY_CACHE_MAX_AGE:
            raise
        return [tuple(row) for row in cached["rows"]]


def switch_state(model, switches):
    keys = {"luna+sol": ("luna", "sol"), "deepseek": ("deepseek",), "deepseek-free": ("deepseek-free",),
            "perch-deepseek": ("perch-deepseek",),
            "relay-sonnet": ("relay-sonnet",), "relay-haiku": ("relay-haiku",),
            "relay-opus": ("relay-opus",),
            "relay": ("relay",), "chatanywhere": ("chatanywhere",),
            "gemini": ("flash", "hydrogen", "argon", "carbon"), "agy-3p": ("sonnet",), "Claude Code": ("claude",)}.get(model, ())
    return "on" if any(switches.get(key, True) for key in keys) else "off"


def codex_window(minutes):
    if minutes == 10080:
        return "weekly"
    if minutes == 300:
        return "5h"
    return str(minutes) + "m"


def get_switches():
    spec = __import__("importlib.util").util.spec_from_file_location("task_module", os.path.join(SCRIPT_DIR, "task.py"))
    module = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.worker_switches()


def codex_rows(root, switches, deadline=None, cache_path=None, now=None):
    now_epoch = time.time() if now is None else now
    limits, at = codex_usage(root, deadline, cache_path)
    age = max(0.0, now_epoch - at)
    rows = []
    for key in ("primary", "secondary"):
        item = limits.get(key)
        if not isinstance(item, dict) or item.get("used_percent") is None:
            continue
        window = codex_window(item.get("window_minutes", "?"))
        reset = item.get("resets_at")
        cell = _quota_cell(item["used_percent"], parse_epoch(reset), age, now_epoch,
                           stale_zero=True)
        rows.append(("Codex", "luna+sol", cell, window, local_time(reset),
                     switch_state("luna+sol", switches), "session " + format_age(age)))
    if not rows:
        raise ValueError("no supported windows")
    return rows


def _future_result(future, deadline):
    # A ready future must not be discarded even if the deadline expired while
    # waiting on another source.
    if future.done():
        return future.result()
    return future.result(timeout=deadline.remaining())


def render(root=None, state_path=None, logs=None, gemini_log=None, switches=None, deadline=None,
           codex_cache=None, perch_cache=None):
    deadline = deadline or Deadline()
    root = root or os.path.join(HOME, ".codex/sessions")
    state_path = state_path or os.path.join(HOME, ".claude/state/claude-rate.json")
    switches = switches if switches is not None else get_switches()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    try:
        codex_future = executor.submit(codex_rows, root, switches, deadline, codex_cache)
        claude_future = executor.submit(claude_usage_rows, state_path)
        perch_future = executor.submit(perch_usage, deadline, perch_cache)
        agy_future = executor.submit(agy_usage, deadline)

        rows = []
        try:
            rows.extend(_future_result(codex_future, deadline))
        except Exception:
            rows.append(("Codex", "n/a", "n/a", "n/a", "n/a", "on", "n/a (source unavailable)"))
        try:
            claude_rows = _future_result(claude_future, deadline)
        except Exception:
            claude_rows = [("Claude", "Claude Code", "n/a (usage unavailable)", "n/a", "n/a",
                            "on", "n/a (usage unavailable)")]
        rows.extend(tuple(row[:5]) + (switch_state("Claude Code", switches), row[6]) for row in claude_rows)
        rows.append(("OpenCode", "opencode-go/deepseek-v4.1-flash", "not supported", "n/a", "n/a",
                     switch_state("deepseek", switches), "not supported"))
        rows.append(("OpenCode free", "opencode/deepseek-v4.1-flash-free", "not supported", "n/a", "n/a",
                     switch_state("deepseek-free", switches), "not supported"))
        try:
            rows.extend(perch_rows(_future_result(perch_future, deadline), switches))
        except Exception:
            rows.append(("Perch", "deepseek-flash-starter", "n/a", "n/a", "n/a",
                         switch_state("perch-deepseek", switches), "n/a (source unavailable)"))
        rows.append(("Relay", "relay-sonnet", "not supported", "n/a", "n/a",
                     switch_state("relay", switches), "not supported"))
        rows.append(("ChatAnywhere", "gpt-6-luna", "not supported", "n/a", "n/a",
                     switch_state("chatanywhere", switches), "not supported"))
        try:
            agy_rows = _future_result(agy_future, deadline)
            for worker, model, used, window, resets, enabled, source in agy_rows:
                model_name = "gemini" if worker == "Gemini" else "agy-3p"
                rows.append((worker, model_name, used, window, resets, switch_state(model_name, switches), source))
        except Exception:
            rows.append(("Gemini", "agy", "n/a", "n/a", "n/a", "on", "n/a (source unavailable)"))
        return rows, switches
    finally:
        executor.shutdown(wait=False)


def _duration_text(value):
    """Compact live durations such as `4h 52m`, `2 d 4 h`, `15 minutes`."""
    match = re.fullmatch(
        r"\s*(?:(\d+)\s*d(?:ays?)?)?\s*(?:(\d+)\s*h(?:ours?)?)?\s*"
        r"(?:(\d+)\s*(?:m|min|mins|minutes?))?\s*", str(value), re.IGNORECASE)
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(group) if group else 0 for group in match.groups())
    if days:
        return "%dd%dh" % (days, hours)
    if hours:
        return "%dh%02dm" % (hours, minutes)
    return "%dm" % minutes


def render_table(rows, switches):
    workers = (("Codex", "Codex", "luna+sol", "luna+sol"), ("Claude", "Claude", "Claude Code", "Claude Code"),
               ("Gemini", "Gemini", "gemini", "gemini"), ("agy-3p", "Gemini", "sonnet", "agy-3p"),
               ("OpenCode", "OpenCode", "opencode-go/deepseek-v4.1-flash", "deepseek"),
               ("OpenCode free", "OpenCode", "opencode/deepseek-v4.1-flash-free", "deepseek-free"),
               ("Perch", "Perch", "deepseek-flash-starter", "perch-deepseek"),
               ("Relay", "Relay", "relay-sonnet", "relay"),
               ("ChatAnywhere", "ChatAnywhere", "gpt-6-luna", "chatanywhere"))
    output = [("Worker", "Model", "Weekly left", "5h left", "Reset", "Status")]

    def short(value):
        if not isinstance(value, str):
            return "-"
        if value.startswith("frees up"):
            return _duration_text(re.sub(r"^frees up in\s*", "", value, flags=re.IGNORECASE)) or value
        try:
            delta = datetime.datetime.strptime(value, "%Y-%m-%d %H:%M") - datetime.datetime.now()
        except (TypeError, ValueError):
            return "-"
        minutes = max(0, int(delta.total_seconds() // 60))
        days, hours = minutes // 1440, minutes % 1440 // 60
        if days:
            return "%dd%dh" % (days, hours)
        return "%dh%02dm" % (hours, minutes % 60) if hours else "%dm" % minutes

    def note(cell):
        """Trailing text after the percentage, e.g. ` · 3h ago` or ` (reset)`."""
        match = re.search(r"\d+(?:\.\d+)?\s*%", cell)
        if not match:
            return ""
        tail = cell[match.end():]
        return tail if tail.strip() else ""

    for key, worker, model, switch_key in workers:
        matches = [row for row in rows if row[0] == key]
        if not matches:
            continue
        active = switch_state(switch_key, switches) != "off"
        windows = {}
        for row in matches:
            value = re.search(r"\d+(?:\.\d+)?\s*%", row[2])
            if not value or row[3] not in ("weekly", "7d", "5h", "month"):
                continue
            window = "5h" if row[3] == "5h" else ("month" if row[3] == "month" else "weekly")
            windows[window] = (float(value.group(0).replace("%", "").strip()), row)
        if windows:
            weekly, five, month = windows.get("weekly"), windows.get("5h"), windows.get("month")
            low = min(pct for pct, _ in windows.values())
            if month:
                # A monthly budget is the headline number; never stack it on the
                # 7-day percentage (two conflicting percentages in one cell).
                cell = month[1][2]
            elif weekly:
                cell = "%.0f%%%s" % (weekly[0], note(weekly[1][2]))
            else:
                cell = "-"
            if five:
                duration = short(five[1][4])
                five_cell = "%.0f%%" % five[0] + (" · " + duration if duration != "-" else "")
                five_cell += note(five[1][2])
            else:
                five_cell = "-"
            anchor = weekly or five or month
            reset = short(anchor[1][4])
            status = "⚫ off" if not active else ("🔴 limit" if low <= 0 else "🟡 ≤20%" if low <= 20 else "🟢")
        else:
            chosen = matches[0]
            if chosen[2] == "last error":
                cell = chosen[-1][:40]
            elif chosen[2] == "free":
                cell = chosen[-1]
            else:
                cell = chosen[2]
            five_cell, reset = "-", short(chosen[4])
            status = "⚫ off" if not active else ("🔴 error" if chosen[2] == "last error" else "🟢")
        output.append((worker, model, cell, five_cell, reset, status))
    widths = [max(display_width(row[index]) for row in output) for index in range(len(output[0]))]
    lines = [" | ".join(pad_display(cell, widths[index]) for index, cell in enumerate(row))
             for row in output]
    separator = "-+-".join("-" * width for width in widths)
    return "\n".join([lines[0], separator] + lines[1:])


def display_width(value):
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in str(value))


def pad_display(value, width):
    value = str(value)
    return value + " " * (width - display_width(value))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--md", action="store_true")
    args = parser.parse_args()
    rows, switches = render()
    print(render_table(rows, switches))


if __name__ == "__main__":
    main()
