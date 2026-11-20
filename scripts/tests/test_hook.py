"""Tests for the SessionStart hook in hooks/hooks.json."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
HOOKS_JSON = REPO / 'hooks' / 'hooks.json'


def _load_session_start_command():
    with open(HOOKS_JSON, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    entries = data['hooks']['SessionStart']
    assert len(entries) == 1, f'Expected 1 SessionStart entry, got {len(entries)}'
    hooks = entries[0]['hooks']
    assert len(hooks) == 1, f'Expected 1 hook command, got {len(hooks)}'
    return hooks[0]['command']


class HookTests(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.cache_plugin = self.home / '.claude' / 'plugins' / 'cache' / 'x' / 'task-runner' / '1.0.0'
        self.cache_plugin.parent.mkdir(parents=True)
        shutil.copytree(REPO, self.cache_plugin, ignore=shutil.ignore_patterns('.git'))
        self.command = _load_session_start_command()

    def _run_hook(self):
        env = dict(os.environ, HOME=str(self.home), CLAUDE_PLUGIN_ROOT=str(self.cache_plugin))
        return subprocess.run(
            ['bash', '-c', self.command],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_session_start_hook_installs_executable_task_launcher(self):
        result = self._run_hook()
        self.assertEqual(0, result.returncode, result.stderr)

        env = dict(os.environ, HOME=str(self.home))
        for name in ('task', 'task.py', 'quota.py', 'skill-updates.py', 'clean-sessions.py'):
            launcher = self.home / '.claude' / 'scripts' / name
            self.assertTrue(launcher.is_file(), f'missing launcher: {name}')
            self.assertTrue(os.access(launcher, os.X_OK), f'not executable: {name}')
            help_res = subprocess.run(
                [str(launcher), '--help'],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, help_res.returncode, f'{name}: {help_res.stderr}')

    def test_unwritable_claude_dir_still_exits_zero(self):
        claude_dir = self.home / '.claude'
        os.chmod(claude_dir, 0o555)
        self.addCleanup(os.chmod, claude_dir, 0o755)

        probe = claude_dir / '.probe'
        try:
            probe.write_text('x')
        except PermissionError:
            pass
        else:
            probe.unlink()
            self.skipTest('chmod 0555 does not prevent writes for this user')

        result = self._run_hook()
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == '__main__':
    unittest.main()
