"""Tests for .githooks/pre-commit in an isolated temporary git repository."""
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]


class PreCommitHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp, True)

        hooks_dir = self.temp / '.githooks'
        scripts_dir = self.temp / 'scripts'
        hooks_dir.mkdir()
        scripts_dir.mkdir()

        hook_dst = hooks_dir / 'pre-commit'
        shutil.copy2(REPO / '.githooks' / 'pre-commit', hook_dst)
        hook_dst.chmod(hook_dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        shutil.copy2(REPO / 'scripts' / 'check-sensitive.py', scripts_dir / 'check-sensitive.py')

        self._git('init')
        self._git('config', 'user.name', 'Test User')
        self._git('config', 'user.email', '12345+testuser' + '@users.noreply.github.com')
        self._git('config', 'core.hooksPath', '.githooks')

    def _git(self, *args, check=True):
        env = dict(os.environ)
        env.pop('PLUGIN_DENYLIST', None)
        env.pop('PLUGIN_NAME_DIRS', None)
        return subprocess.run(
            ['git', *args],
            cwd=self.temp,
            env=env,
            check=check,
            capture_output=True,
            text=True,
        )

    def test_precommit_accepts_clean_and_blocks_key_shaped_string(self):
        (self.temp / 'clean.txt').write_text('hello public world\n', encoding='utf-8')
        self._git('add', 'clean.txt')
        ok = self._git('commit', '-m', 'clean commit', check=False)
        self.assertEqual(0, ok.returncode, ok.stderr)

        secret = 'sk-' + 'k' * 24
        (self.temp / 'leak.txt').write_text('token=' + secret + '\n', encoding='utf-8')
        self._git('add', 'leak.txt')
        blocked = self._git('commit', '-m', 'should fail', check=False)
        self.assertNotEqual(0, blocked.returncode)
        output = blocked.stdout + blocked.stderr
        self.assertIn('leak.txt:1: key', output)
        self.assertNotIn(secret, output)


if __name__ == '__main__':
    unittest.main()
