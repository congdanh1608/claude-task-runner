#!/usr/bin/env python3
"""Delete transcripts of sessions archived ("deleted") in the VSCode Claude Code extension.

The extension only appends the session id to hiddenSessionIds in its state.vscdb;
this removes the matching ~/.claude/projects/*/<id>.jsonl, its companion dir and
~/.claude/file-history/<id>/. A synced tombstone per host keeps cleaned ids deleted
when another machine syncs the transcript back. Anything not archived is never touched.
"""
import glob
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile

STATE_DB_CANDIDATES = [
    "~/Library/Application Support/Code/User/globalStorage/state.vscdb",
    "~/.config/Code/User/globalStorage/state.vscdb",
    "~/AppData/Roaming/Code/User/globalStorage/state.vscdb",
]
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
FILE_HISTORY_DIR = os.path.expanduser("~/.claude/file-history")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TOMBSTONE_PREFIX = ".cleaned-ids."
TOMBSTONE_TEMP_PREFIX = ".cleaned-ids-tmp."


def hidden_ids():
    for candidate in STATE_DB_CANDIDATES:
        path = os.path.expanduser(candidate)
        if not os.path.isfile(path):
            continue
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
        try:
            row = con.execute("SELECT value FROM ItemTable WHERE key='Anthropic.claude-code'").fetchone()
        finally:
            con.close()
        if not row:
            return set()
        return set(json.loads(row[0]).get("hiddenSessionIds", []))
    return None


def host_name():
    return re.sub(r"[^A-Za-z0-9_-]", "_", socket.gethostname())


def tombstone_path():
    return os.path.join(PROJECTS_DIR, TOMBSTONE_PREFIX + host_name())


def is_session_id(value):
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def read_tombstone(path):
    ids = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                value = line.rstrip("\r\n")
                if is_session_id(value):
                    ids.add(value)
    except OSError:
        pass
    return ids


def tombstone_ids():
    ids = set()
    for path in glob.glob(os.path.join(PROJECTS_DIR, TOMBSTONE_PREFIX + "*")):
        name = os.path.basename(path)
        if "sync-conflict" in name or name.endswith(".tmp") or not os.path.isfile(path):
            continue
        ids |= read_tombstone(path)
    return ids


def write_tombstones(ids):
    ids = {sid for sid in ids if is_session_id(sid)}
    if not ids:
        return
    path = tombstone_path()
    merged = sorted(read_tombstone(path) | ids)
    fd, temp = tempfile.mkstemp(dir=PROJECTS_DIR, prefix=TOMBSTONE_TEMP_PREFIX, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(merged) + "\n")
        os.replace(temp, path)
    except Exception:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def tree_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def remove_tree(path):
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        return False


def transcript_paths():
    root = os.path.realpath(PROJECTS_DIR)
    paths = []
    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        folder = os.path.dirname(path)
        if os.path.islink(folder):
            continue
        real = os.path.realpath(folder)
        if real != root and not real.startswith(root + os.sep):
            continue
        paths.append(path)
    return paths


def main():
    try:
        hidden = hidden_ids()
    except Exception as error:
        print("clean sessions: cannot read the VSCode state: %s" % error, file=sys.stderr)
        hidden = set()
    if hidden is None:
        print("clean sessions: VSCode extension state not found", file=sys.stderr)
        hidden = set()
    tombstones = tombstone_ids()
    current = os.environ.get("CLAUDE_SESSION_ID")
    archived = hidden | tombstones
    if current:
        archived.discard(current)
    by_id = {}
    for path in transcript_paths():
        by_id.setdefault(os.path.splitext(os.path.basename(path))[0], []).append(path)
    targets = archived & set(by_id)
    freed = deleted = history_removed = 0
    cleaned = set(targets)
    for sid in sorted(targets):
        for path in by_id[sid]:
            freed += os.path.getsize(path)
            os.remove(path)
            deleted += 1
            companion = os.path.join(os.path.dirname(path), sid)
            if os.path.isdir(companion):
                size = tree_size(companion)
                if remove_tree(companion):
                    freed += size
        if is_session_id(sid):
            history = os.path.join(FILE_HISTORY_DIR, sid)
            if os.path.isdir(history):
                size = tree_size(history)
                if remove_tree(history):
                    freed += size
                    history_removed += 1
    orphans = {sid for sid in archived - set(by_id) if is_session_id(sid)}
    for sid in sorted(orphans):
        history = os.path.join(FILE_HISTORY_DIR, sid)
        if not os.path.isdir(history):
            continue
        size = tree_size(history)
        if remove_tree(history):
            cleaned.add(sid)
            freed += size
            history_removed += 1
    try:
        write_tombstones(cleaned)
    except OSError as error:
        print("clean sessions: tombstone not written: %s" % error, file=sys.stderr)
    from_tombstones = len(targets & tombstones)
    if not deleted:
        print("Nothing to clean (%d archived, none left on disk)" % len(archived))
    else:
        print("Cleaned %d archived transcript(s), freed %.1f MB" % (deleted, freed / 1024 / 1024))
    if history_removed or from_tombstones:
        print("file-history removed: %d, from tombstones: %d" % (history_removed, from_tombstones))
    return 0


if __name__ == "__main__":
    sys.exit(main())
