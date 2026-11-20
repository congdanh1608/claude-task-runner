#!/usr/bin/env python3
"""Check and merge upstream updates for copied user-level skills."""
import argparse
import difflib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


HOME = Path.home()
SKILLS = Path(os.environ.get("SKILLS_DIR", str(HOME / ".claude/skills"))).expanduser()
MANIFEST = Path(os.environ.get("SKILL_UPDATES_MANIFEST", str(SKILLS / "upstream.json"))).expanduser()
CACHE = Path(os.environ.get("SKILL_UPDATES_CACHE", str(HOME / ".cache/skill-updates"))).expanduser()
STATE_DIR = Path(os.environ.get("SKILL_UPDATES_STATE_DIR", str(HOME / ".claude/state"))).expanduser()
IGNORE = {".DS_Store"}


@contextmanager
def update_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "skill-updates.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def locked(operation):
    @wraps(operation)
    def wrapper(*args, **kwargs):
        with update_lock():
            return operation(*args, **kwargs)
    return wrapper


def check_pending_update():
    recovered = []
    for marker in sorted(SKILLS.glob(".skill-update-state-*.json")):
        finish_update(marker)
        recovered.append(marker.name)
    pending = sorted(SKILLS.glob(".skill-update-old-*"))
    if pending:
        raise RuntimeError("unfinished skill update; inspect and recover aside directories before retrying: "
                           + ", ".join(str(path) for path in pending))
    if recovered:
        raise RuntimeError("recovered unfinished skill update; retry command: " + ", ".join(recovered))


def finish_update(marker):
    if SKILLS.is_symlink() or marker.is_symlink():
        raise RuntimeError("update recovery path contains a symlink")
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
        validate_skill_name(state["name"])
        for field, prefix in (("held", ".skill-update-old-"), ("staged", ".skill-update-new-")):
            value = state[field]
            if not value.startswith(prefix) or Path(value).name != value:
                raise ValueError("invalid recovery directory")
        if state["manifest"] != str(MANIFEST.absolute()):
            raise ValueError("recovery manifest path differs")
        old, new = state["old_manifest"].encode("utf-8"), state["new_manifest"].encode("utf-8")
        identity = state["manifest_identity"]
        if not isinstance(identity, list) or len(identity) != 2 or not all(isinstance(n, int) for n in identity):
            raise ValueError("invalid manifest identity")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError("invalid update recovery marker: " + marker.name) from exc
    folder, held, staged = (SKILLS / state[field] for field in ("name", "held", "staged"))
    if any(path.is_symlink() for path in (folder, held, staged, MANIFEST)):
        raise RuntimeError("update recovery path contains a symlink")
    current = MANIFEST.read_bytes()
    info = MANIFEST.stat()
    # The on-disk replacement commits the update, even if its caller was interrupted.
    # File identity distinguishes replacements whose manifest bytes are unchanged.
    committed = current == new and (old != new or identity != [info.st_dev, info.st_ino])
    if committed:
        if not folder.is_dir() or staged.exists():
            raise RuntimeError("committed update tree needs manual recovery: " + marker.name)
        if held.exists():
            shutil.rmtree(held)
    elif current == old:
        if held.exists():
            if folder.exists():
                os.replace(folder, staged)
            os.replace(held, folder)
        if not folder.is_dir():
            raise RuntimeError("original update tree needs manual recovery: " + marker.name)
        if staged.exists():
            shutil.rmtree(staged)
    else:
        raise RuntimeError("manifest differs from update recovery snapshots: " + marker.name)
    marker.unlink()
    return committed


def run(args, cwd=None, check=True):
    p = subprocess.run(args, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip() or "command failed: " + " ".join(args))
    return p.stdout.strip()


def read_manifest():
    check_pending_update()
    if not MANIFEST.exists():
        return {}
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("manifest must be a JSON object")
    return data


def write_manifest(data):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="upstream-", dir=str(MANIFEST.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, str(MANIFEST))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate_skill_name(name):
    if (not isinstance(name, str) or not name or name.startswith(".") or ".." in name
            or "/" in name or "\\" in name or "\x00" in name):
        raise RuntimeError("invalid skill name")


def skill_folder(name):
    validate_skill_name(name)
    check_pending_update()
    if SKILLS.is_symlink() or not SKILLS.is_dir():
        raise RuntimeError("skills root must be a real directory")
    root = SKILLS.resolve(strict=True)
    folder = SKILLS / name
    if folder.is_symlink():
        raise RuntimeError("skill path contains a symlink")
    try:
        resolved = folder.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise RuntimeError("skill path is outside the skills root")
    if resolved == root:
        raise RuntimeError("invalid skill path")
    for path in folder.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("skill tree contains a symlink: " + path.name)
    return resolved


def repo_dir(url):
    slug = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in slug) or "repo"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return CACHE / (slug + "-" + digest)


def ensure_repo(url, branch=None):
    branch = branch.removeprefix("origin/") if branch else None
    dest = repo_dir(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        args = ["git", "clone", "--filter=blob:none", "--no-checkout", "--depth", "300"]
        if branch:
            args += ["--branch", branch]
        args += [url, str(dest)]
        run(args)
    else:
        args = ["git", "fetch", "--depth", "300", "origin"]
        if branch:
            args.append("+refs/heads/{}:refs/remotes/origin/{}".format(branch, branch))
        run(args, cwd=dest)
    return dest


def upstream_ref(branch):
    if not branch:
        return "origin/HEAD"
    return branch if branch.startswith("origin/") else "origin/" + branch


def ensure_base_commit(repo, base):
    def available():
        result = subprocess.run(["git", "cat-file", "-e", base + "^{commit}"], cwd=str(repo),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.returncode == 0

    if available():
        return
    try:
        run(["git", "fetch", "--depth=1", "origin", base], cwd=repo)
    except RuntimeError:
        pass
    if not available():
        raise RuntimeError("base commit unavailable after fetch: " + base)


def file_map(root, reject_symlinks=False):
    result = {}
    if not root.exists():
        return result
    for p in root.rglob("*"):
        if reject_symlinks and p.is_symlink():
            raise RuntimeError("skill tree contains a symlink: " + p.name)
        if p.is_file() and p.name not in IGNORE:
            result[p.relative_to(root).as_posix()] = p.read_bytes()
    return result


def file_modes(root):
    result = {}
    if not root.exists():
        return result
    for p in root.rglob("*"):
        if p.is_file() and p.name not in IGNORE:
            result[p.relative_to(root).as_posix()] = stat.S_IMODE(p.stat().st_mode)
    return result


def git_tree(repo, rev, path):
    path = path.strip("/")
    spec = rev + ":" + path if path else rev
    p = subprocess.run(["git", "archive", "--format=tar", spec], cwd=str(repo),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError("cannot read Git tree {}: {}".format(spec, p.stderr.decode(errors="replace").strip()))
    import io
    import tarfile
    contents = {}
    with tarfile.open(fileobj=io.BytesIO(p.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isfile() and Path(member.name).name not in IGNORE:
                stream = archive.extractfile(member)
                if stream:
                    contents[member.name] = stream.read()
    tree_spec = rev + ":" + path if path else rev
    listed = subprocess.run(["git", "ls-tree", "-r", "-z", tree_spec], cwd=str(repo),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    modes = {}
    if listed.returncode == 0:
        for record in listed.stdout.split(b"\0"):
            if not record or b"\t" not in record:
                continue
            metadata, raw_name = record.split(b"\t", 1)
            mode = metadata.split(b" ", 1)[0]
            name = os.fsdecode(raw_name)
            if name in contents and mode in (b"100644", b"100755"):
                modes[name] = stat.S_IMODE(int(mode, 8))
    return contents, modes


def copied_skill_dirs():
    if not SKILLS.exists():
        return []
    return sorted(p for p in SKILLS.iterdir()
                  if p.is_dir() and not p.is_symlink() and not p.name.startswith(".") and not (p / ".git").exists())


def git_map(repo, rev, path):
    return git_tree(repo, rev, path)[0]


def changed_lines(a, b):
    total = 0
    for key in set(a) | set(b):
        if a.get(key) != b.get(key):
            try:
                aa = a.get(key, b"").decode("utf-8").splitlines()
                bb = b.get(key, b"").decode("utf-8").splitlines()
                total += sum((i2 - i1) + (j2 - j1)
                             for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, aa, bb).get_opcodes()
                             if tag != "equal")
            except UnicodeDecodeError:
                total += 1
    return total


def commits_for_path(repo, branch, path):
    args = ["git", "log", "--format=%H", "-300", branch]
    if path:
        args += ["--", path]
    return run(args, cwd=repo).splitlines()


def local_changes(local, base):
    return local != base


def check_skills(manifest):
    rows = []
    names = [p.name for p in copied_skill_dirs()]
    for name in names:
        entry = manifest.get(name)
        if entry is None:
            rows.append((name, "new: not in manifest", "run bootstrap with --repo URL or --local"))
            continue
        if entry.get("local"):
            rows.append((name, "local only", ""))
            continue
        try:
            repo = ensure_repo(entry["repo"], entry.get("branch"))
            branch = upstream_ref(entry.get("branch"))
            head = run(["git", "rev-parse", branch], cwd=repo)
            local = file_map(SKILLS / name, reject_symlinks=True)
            local_modes = file_modes(SKILLS / name)
            ensure_base_commit(repo, entry["base"])
            base, base_modes = git_tree(repo, entry["base"], entry.get("path", ""))
            dirty = local_changes(local, base) or local_modes != base_modes
            path = entry.get("path", "")
            commits = commits_for_path(repo, branch, path)
            newer = []
            if entry["base"] in commits:
                newer = commits[:commits.index(entry["base"])]
            elif head != entry["base"]:
                newer = commits[:3]
            if newer:
                status = "update + local edits" if dirty else "update available ({} commits)".format(len(newer))
                subjects = []
                for commit in newer[:3]:
                    subject = run(["git", "show", "-s", "--format=%s", commit], cwd=repo)
                    subjects.append(subject.replace("|", "/").replace("\n", " ")[:100])
                detail = "; ".join(subjects)
            else:
                status, detail = ("local edits", "") if dirty else ("up to date", "")
            rows.append((name, status, detail))
        except Exception:
            rows.append((name, "upstream unreachable", ""))
    return rows


def marketplace_rows():
    root = HOME / ".claude/plugins"
    config = root / "known_marketplaces.json"
    if not config.exists():
        return []
    data = json.loads(config.read_text(encoding="utf-8"))
    rows = []
    for name, info in sorted(data.items()):
        checkout = root / "marketplaces" / name
        source = info.get("source", {}) if isinstance(info, dict) else {}
        url = source if isinstance(source, str) else source.get("url") or source.get("repo")
        if isinstance(url, str) and "/" in url and not url.startswith(("https://", "http://", "ssh://", "git@", "file://")):
            url = "https://github.com/" + url + ".git"
        if not url or not (checkout / ".git").exists():
            continue
        try:
            local = run(["git", "rev-parse", "HEAD"], cwd=checkout)
            remote = run(["git", "ls-remote", url, "HEAD"])
            remote = remote.split()[0] if remote else ""
            status = "up to date" if local == remote else "update available"
            commands = "claude plugin marketplace update {} + claude plugin update <plugin>@{}".format(name, name)
            rows.append((name, status, commands if status != "up to date" else ""))
        except Exception:
            rows.append((name, "upstream unreachable", ""))
    return rows


def print_table(title, headers, rows):
    print("## " + title)
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        print("| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in row) + " |")


@locked
def check():
    print_table("Skills", ("Skill", "Status", "Upstream commits / hint"), check_skills(read_manifest()))
    print()
    print_table("Marketplace plugins", ("Marketplace", "Status", "Command"), marketplace_rows())


@locked
def bootstrap(name, repo_url=None, path="", branch=None, local=False):
    folder = skill_folder(name)
    manifest = read_manifest()
    if local:
        manifest[name] = {"local": True}
        write_manifest(manifest)
        print(name + ": recorded local-only")
        return
    if not repo_url:
        raise RuntimeError("--repo URL is required")
    repo = ensure_repo(repo_url, branch)
    branch_ref = upstream_ref(branch)
    commits = commits_for_path(repo, branch_ref, path)
    if not commits:
        raise RuntimeError("no history found for path")
    local, local_modes = file_map(folder, reject_symlinks=True), file_modes(folder)
    best, best_diff, best_exact = None, None, False
    for commit in commits:
        tree, tree_modes = git_tree(repo, commit, path)
        if tree == local and tree_modes == local_modes:
            best, best_diff = commit, 0
            best_exact = True
            break
        diff = changed_lines(local, tree) + sum(local_modes.get(p) != tree_modes.get(p)
                                                for p in set(local_modes) | set(tree_modes))
        if best_diff is None or diff < best_diff:
            best, best_diff = commit, diff
            best_exact = False
    manifest[name] = {"repo": repo_url, "path": path, "base": best, "branch": branch or ""}
    write_manifest(manifest)
    print("{}: base {} ({}; {} difference score)".format(name, best, "exact match" if best_exact else "closest match", best_diff))


def merge_trees(base, local, upstream):
    merged, conflicts = {}, []
    with tempfile.TemporaryDirectory(prefix="skill-update-") as tmp:
        tmp = Path(tmp)
        for rel in sorted(set(base) | set(local) | set(upstream)):
            b, l, u = base.get(rel), local.get(rel), upstream.get(rel)
            if l == b:
                if u is not None:
                    merged[rel] = u
            elif u == b or l == u:
                if l is not None:
                    merged[rel] = l
            elif b is None or l is None or u is None:
                conflicts.append(rel)
            else:
                try:
                    b.decode("utf-8"); l.decode("utf-8"); u.decode("utf-8")
                except UnicodeDecodeError:
                    conflicts.append(rel)
                    continue
                paths = [tmp / (str(i) + ".txt") for i in range(3)]
                for p, content in zip(paths, (l, b, u)):
                    p.write_bytes(content)
                result = subprocess.run(["git", "merge-file", "-p", str(paths[0]), str(paths[1]), str(paths[2])],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if result.returncode:
                    conflicts.append(rel)
                else:
                    merged[rel] = result.stdout
    return merged, conflicts


def merge_modes(base, local, upstream):
    merged, conflicts = {}, []
    for rel in set(base) | set(local) | set(upstream):
        b, l, u = base.get(rel), local.get(rel), upstream.get(rel)
        if l == b:
            if u is not None:
                merged[rel] = u
        elif u == b or l == u:
            if l is not None:
                merged[rel] = l
        else:
            conflicts.append(rel)
    return merged, conflicts


def make_backup(folder, name):
    root = SKILLS / ".backup"
    if root.is_symlink():
        raise RuntimeError("backup path contains a symlink")
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.resolve(strict=True).relative_to(SKILLS.resolve(strict=True))
    except (OSError, ValueError):
        raise RuntimeError("backup path is outside the skills root")
    backup = root / (name + "-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copytree(folder, backup, copy_function=shutil.copy2)
    return backup


def install_tree(folder, name, files, modes, manifest, entry, head):
    backup = make_backup(folder, name)
    staged = Path(tempfile.mkdtemp(prefix=".skill-update-new-", dir=str(SKILLS)))
    held = None
    marker = None
    cleanup_staged = True
    try:
        for rel, content in files.items():
            target = staged / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            os.chmod(target, modes.get(rel, 0o644))
        for p in folder.rglob("*"):
            if p.is_file() and p.name in IGNORE:
                target = staged / p.relative_to(folder)
                if target.parent.is_dir():
                    shutil.copy2(p, target)
        held = Path(tempfile.mkdtemp(prefix=".skill-update-old-", dir=str(SKILLS)))
        held.rmdir()
        old_manifest = MANIFEST.read_bytes().decode("utf-8")
        info = MANIFEST.stat()
        updated = dict(manifest, **{name: dict(entry, base=head)})
        state = {"name": name, "held": held.name, "staged": staged.name,
                 "manifest": str(MANIFEST.absolute()), "old_manifest": old_manifest,
                 "new_manifest": json.dumps(updated, indent=2, sort_keys=True) + "\n",
                 "manifest_identity": [info.st_dev, info.st_ino]}
        fd, path = tempfile.mkstemp(prefix=".skill-update-state-", suffix=".json", dir=str(SKILLS))
        marker = Path(path)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
        try:
            cleanup_staged = False
            os.replace(folder, held)
            os.replace(staged, folder)
            entry["base"] = head
            write_manifest(manifest)
        except BaseException:
            committed = finish_update(marker)
            if not committed:
                entry.clear()
                entry.update(json.loads(old_manifest)[name])
            cleanup_staged = True
            raise
        cleanup_staged = True
        finish_update(marker)
        return backup
    finally:
        if cleanup_staged and staged.exists():
            shutil.rmtree(staged)


@locked
def apply_update(name, staged_dir=None):
    folder = skill_folder(name)
    manifest = read_manifest()
    entry = manifest.get(name)
    if not entry or entry.get("local"):
        raise RuntimeError("skill has no upstream: " + name)
    repo = ensure_repo(entry["repo"], entry.get("branch"))
    branch = upstream_ref(entry.get("branch"))
    head = run(["git", "rev-parse", branch], cwd=repo)
    ensure_base_commit(repo, entry["base"])
    base, base_modes = git_tree(repo, entry["base"], entry.get("path", ""))
    local, local_modes = file_map(folder, reject_symlinks=True), file_modes(folder)
    upstream, upstream_modes = git_tree(repo, head, entry.get("path", ""))
    merged, conflicts = merge_trees(base, local, upstream)
    modes, mode_conflicts = merge_modes(base_modes, local_modes, upstream_modes)
    conflicts = sorted(set(conflicts) | set(mode_conflicts))
    if staged_dir:
        staged_dir = Path(staged_dir).expanduser()
        if not staged_dir.is_dir() or staged_dir.is_symlink():
            raise RuntimeError("staged directory not found")
        for path in staged_dir.rglob("*"):
            if path.is_symlink():
                raise RuntimeError("staged tree contains a symlink: " + path.name)
        staged = file_map(staged_dir)
        staged_modes = file_modes(staged_dir)
        allowed = set(merged) | set(conflicts)
        unexpected = set(staged) - allowed
        mismatched = [rel for rel in set(merged) - set(conflicts)
                      if staged.get(rel) != merged[rel] or staged_modes.get(rel) != modes.get(rel)]
        if unexpected or mismatched:
            paths = sorted(unexpected | set(mismatched))
            raise RuntimeError("staged tree failed merge verification: " + ", ".join(paths[:10]))
        merged = staged
        modes = staged_modes
    if conflicts:
        if not staged_dir:
            backup = make_backup(folder, name)
            print("Conflicts; no files written. Backup: {}".format(backup))
            for rel in conflicts:
                print("- " + rel)
            raise SystemExit(1)
    backup = install_tree(folder, name, merged, modes, manifest, entry, head)
    print("Updated {} to {}. Backup: {}".format(name, head, backup))


def scan(hints):
    with update_lock():
        manifest = read_manifest()
    readme = SKILLS / "README.md"
    sources = {}
    if readme.exists():
        # Accept explicit lines like: skill-name: https://... [path=...]
        import re
        for line in readme.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([\w.-]+)\s*:\s*(https?://\S+)(?:\s+path=(\S+))?", line)
            if m:
                sources[m.group(1)] = (m.group(2), m.group(3) or "")
    for hint in hints:
        if "=" not in hint:
            raise RuntimeError("hint must be name=URL[:path]")
        name, value = hint.split("=", 1)
        path = ""
        if value.startswith(("https://", "http://", "ssh://")):
            slash = value.find("/", value.find("://") + 3)
            colon = value.find(":", slash + 1) if slash >= 0 else -1
            if colon >= 0:
                value, path = value[:colon], value[colon + 1:]
        elif value.startswith("git@") and value.count(":") > 1:
            value, path = value.rsplit(":", 1)
        sources[name] = (value, path)
    names = [p.name for p in copied_skill_dirs()]
    for name in names:
        if name in manifest:
            continue
        if name in sources:
            url, path = sources[name]
            try:
                bootstrap(name, url, path)
            except Exception as exc:
                print(name + ": needs URL (source could not be recorded: {})".format(exc))
        else:
            print(name + ": needs URL or --local")


def main():
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    subs.add_parser("check")
    b = subs.add_parser("bootstrap")
    b.add_argument("skill", nargs="?")
    b.add_argument("--repo")
    b.add_argument("--path", default="")
    b.add_argument("--branch")
    b.add_argument("--local", action="store_true")
    b.add_argument("--scan", action="store_true")
    b.add_argument("--hint", action="append", default=[])
    a = subs.add_parser("apply")
    a.add_argument("skill")
    a.add_argument("--from", dest="staged_dir")
    args = parser.parse_args()
    try:
        if args.command == "bootstrap":
            if args.scan:
                scan(args.hint)
            elif not args.skill:
                parser.error("bootstrap needs a skill or --scan")
            else:
                bootstrap(args.skill, args.repo, args.path, args.branch, args.local)
        elif args.command == "apply":
            apply_update(args.skill, args.staged_dir)
        else:
            check()
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
