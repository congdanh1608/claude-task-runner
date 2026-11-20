#!/usr/bin/env python3
"""Scan staged, tracked, or historical files for secrets and sensitive info.

Default scans staged files; --all scans every tracked file (git ls-files);
--history scans every commit reachable from any ref plus author/committer emails.
Prints `file:line: <pattern name>` or `commit:file:line: <pattern name>` only,
never the matched text.
Optional env vars:
  PLUGIN_DENYLIST: readable file of case-insensitive literal terms (one per line).
  PLUGIN_NAME_DIRS: os.pathsep-separated directories whose depth-1/2 subdirs
                    are matched as whole words (`project-name`).
  PLUGIN_URL_ALLOW: comma-separated extra allowed URL hosts (exact or subdomain).
"""
import argparse
import os
import re
import subprocess
import sys

SENSITIVE_NAME = re.compile(
    r'(^|/)(\.env[^/]*|[^/]*\.(pem|p12|p8|keystore)|service-account[^/]*)$',
    re.I,
)

RULES = [
    ('machine path', re.compile(r'/(?:Users|Volumes|home)/[A-Za-z0-9._-]+')),
    ('vietnamese', re.compile(
        r'[\u0103\u00e2\u0111\u00ea\u00f4\u01a1\u01b0'
        r'\u00e0\u00e1\u1ea3\u00e3\u1ea1'
        r'\u1eb1\u1eaf\u1eb3\u1eb5\u1eb7'
        r'\u1ea7\u1ea5\u1ea9\u1eab\u1ead'
        r'\u00e8\u00e9\u1ebb\u1ebd\u1eb9'
        r'\u1ec1\u1ebf\u1ec3\u1ec5\u1ec7'
        r'\u00ec\u00ed\u1ec9\u0129\u1ecb'
        r'\u00f2\u00f3\u1ecf\u00f5\u1ecd'
        r'\u1ed3\u1ed1\u1ed5\u1ed7\u1ed9'
        r'\u1edd\u1edb\u1edf\u1ee1\u1ee3'
        r'\u00f9\u00fa\u1ee7\u0169\u1ee5'
        r'\u1eeb\u1ee9\u1eed\u1eef\u1ef1'
        r'\u1ef3\u00fd\u1ef7\u1ef9\u1ef5]',
        re.I,
    )),
    ('private IP', re.compile(r'\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b')),
    ('key', re.compile(
        r'(?:\bcr_[A-Za-z0-9]{20,}'
        r'|\bsk-[A-Za-z0-9_-]{20,}'
        r'|\bAIza[A-Za-z0-9_-]{30,}'
        r'|\bghp_[A-Za-z0-9]{20,}'
        r'|\bgithub_pat_[A-Za-z0-9_]{20,}'
        r'|\bAKIA[0-9A-Z]{16}\b'
        r'|\bxox[bp]-[A-Za-z0-9-]+'
        r'|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----'
        r'|Bearer\s+[A-Za-z0-9._-]{20,})'
    )),
]

EMAIL = re.compile(r'[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}')
ALLOW_EMAIL = {'git@github.com', 'noreply@anthropic.com'}
NOREPLY_SUFFIX = '@users.noreply.github.com'

URL_HOST = re.compile(r'https?://([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)', re.I)
ALLOW_URL_HOSTS = frozenset({
    'github.com',
    'docs.anthropic.com',
    'anthropic.com',
    'api.chatanywhere.org',
    'api.chatanywhere.tech',
    'example.com',
    'example.org',
    'relay.invalid',
    'invalid',
})

NAME_STOPLIST = frozenset({
    'app', 'apps', 'ios', 'android', 'web', 'docs', 'doc', 'src', 'lib',
    'test', 'tests', 'scripts', 'servers', 'research', 'reports', 'resources',
    'build', 'assets', 'backend', 'frontend', 'mobile', 'api', 'common',
    'shared', 'tools', 'node_modules', 'readme.md', 'release', 'debug',
    'main', 'master', 'develop',
})

HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')


def git(*args):
    return subprocess.run(['git', *args], capture_output=True, check=True).stdout


def load_denylist():
    path = os.environ.get('PLUGIN_DENYLIST', '')
    if not path:
        return []
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
            entries = []
            for lineno, raw in enumerate(fh, 1):
                stripped = raw.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                entries.append((lineno, stripped.lower()))
            return entries
    except OSError:
        print('check-sensitive: warning: cannot read PLUGIN_DENYLIST', file=sys.stderr)
        return []


def _add_project_name(names, raw_name):
    lowered = raw_name.lower()
    if len(lowered) <= 3 or lowered.startswith('.') or lowered in NAME_STOPLIST:
        return
    names.add(lowered)


def load_project_names():
    raw_dirs = os.environ.get('PLUGIN_NAME_DIRS', '')
    if not raw_dirs:
        return None
    names = set()
    for root_dir in raw_dirs.split(os.pathsep):
        if not root_dir:
            continue
        try:
            with os.scandir(root_dir) as d1_entries:
                d1_list = list(d1_entries)
        except OSError:
            print('check-sensitive: warning: cannot read name directory', file=sys.stderr)
            continue
        for e1 in d1_list:
            try:
                if not e1.is_dir() or e1.name.startswith('.'):
                    continue
            except OSError:
                continue
            _add_project_name(names, e1.name)
            try:
                with os.scandir(e1.path) as d2_entries:
                    d2_list = list(d2_entries)
            except OSError:
                print('check-sensitive: warning: cannot read name subdirectory', file=sys.stderr)
                continue
            for e2 in d2_list:
                try:
                    if not e2.is_dir() or e2.name.startswith('.'):
                        continue
                except OSError:
                    continue
                _add_project_name(names, e2.name)
    if not names:
        return None
    pattern = r'\b(?:' + '|'.join(re.escape(n) for n in sorted(names)) + r')\b'
    return re.compile(pattern, re.I)


def load_url_allow():
    extra = os.environ.get('PLUGIN_URL_ALLOW', '')
    if not extra:
        return ALLOW_URL_HOSTS
    hosts = set(ALLOW_URL_HOSTS)
    for item in extra.split(','):
        cleaned = item.strip().lower()
        if cleaned:
            hosts.add(cleaned)
    return frozenset(hosts)


def _is_allowed_url_host(host, allowlist):
    lowered = host.lower()
    return any(lowered == allowed or lowered.endswith('.' + allowed) for allowed in allowlist)


def scan_line(line, denylist, project_name_rx, url_allow):
    if 'sensitive-ok' in line:
        return []
    found = [name for name, rx in RULES if rx.search(line)]
    if any(e not in ALLOW_EMAIL for e in EMAIL.findall(line)):
        found.append('email')
    if any(not _is_allowed_url_host(h, url_allow) for h in URL_HOST.findall(line)):
        found.append('url-host')
    if denylist:
        lower_line = line.lower()
        for entry_line, literal in denylist:
            if literal in lower_line:
                found.append(f'denylist #{entry_line}')
    if project_name_rx and project_name_rx.search(line):
        found.append('project-name')
    return found


def read_blob(path, all_files):
    if all_files and os.path.isfile(path):
        with open(path, 'rb') as fh:
            return fh.read()
    return git('show', f':{path}')


def scan_tree(all_files, denylist, project_name_rx, url_allow):
    names = (
        git('ls-files', '-z')
        if all_files
        else git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z')
    )
    files = [n for n in names.decode('utf-8', errors='replace').split('\0') if n]
    hits = []
    for f in files:
        if SENSITIVE_NAME.search(f):
            hits.append(f'{f}:1: sensitive file name')
            continue
        blob = read_blob(f, all_files)
        if b'\0' in blob[:8000]:
            continue
        for lineno, line in enumerate(blob.decode('utf-8', errors='ignore').splitlines(), 1):
            for name in scan_line(line, denylist, project_name_rx, url_allow):
                hits.append(f'{f}:{lineno}: {name}')
    return hits


def scan_history(denylist, project_name_rx, url_allow):
    if not git('rev-list', '--all').strip():
        return []
    hits = []
    authors_raw = git('log', '--all', '--format=%h%x00%ae%x00%ce').decode('utf-8', errors='replace')
    for row in authors_raw.splitlines():
        if not row:
            continue
        parts = row.split('\0')
        if len(parts) != 3:
            continue
        commit, ae, ce = parts
        if not ae.lower().endswith(NOREPLY_SUFFIX) or not ce.lower().endswith(NOREPLY_SUFFIX):
            hits.append(f'{commit}:author-email')

    diff_raw = git(
        'log', '--all', '-p', '--no-renames', '--no-color', '--format=__COMMIT__%h'
    ).decode('utf-8', errors='replace')
    commit = ''
    current_file = None
    new_lineno = 0
    for line in diff_raw.splitlines():
        if line.startswith('__COMMIT__'):
            commit = line[len('__COMMIT__'):].strip()
            current_file = None
            new_lineno = 0
            continue
        if line.startswith('diff --git '):
            current_file = None
            new_lineno = 0
            continue
        if line.startswith('+++ '):
            path_part = line[4:]
            if path_part == '/dev/null':
                current_file = None
            elif path_part.startswith('b/'):
                current_file = path_part[2:]
                if SENSITIVE_NAME.search(current_file):
                    hits.append(f'{commit}:{current_file}:1: sensitive file name')
            continue
        if line.startswith('@@ '):
            m = HUNK_RE.match(line)
            if m:
                new_lineno = int(m.group(1)) - 1
            continue
        if current_file is None:
            continue
        if line.startswith('+') and not line.startswith('+++'):
            new_lineno += 1
            for name in scan_line(line[1:], denylist, project_name_rx, url_allow):
                hits.append(f'{commit}:{current_file}:{new_lineno}: {name}')
        elif line.startswith(' '):
            new_lineno += 1
    return hits


def parse_args(argv):
    parser = argparse.ArgumentParser(description='Scan repo for secrets and sensitive info.')
    parser.add_argument('--all', action='store_true', help='Scan all tracked files instead of staged.')
    parser.add_argument('--history', action='store_true', help='Scan all git history and author emails.')
    return parser.parse_args(argv)


def main():
    args = parse_args(sys.argv[1:])
    denylist = load_denylist()
    project_name_rx = load_project_names()
    url_allow = load_url_allow()
    try:
        hits = []
        if args.all or not args.history:
            hits.extend(scan_tree(args.all, denylist, project_name_rx, url_allow))
        if args.history:
            hits.extend(scan_history(denylist, project_name_rx, url_allow))
    except subprocess.CalledProcessError as exc:
        print(f'check-sensitive: git error (exit {exc.returncode})', file=sys.stderr)
        return 2

    if hits:
        print(f'check-sensitive: {len(hits)} finding(s):', file=sys.stderr)
        for h in hits[:50]:
            print(h, file=sys.stderr)
        print(f'{len(hits)} hits', file=sys.stderr)
        return 1
    print('0 hits', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
