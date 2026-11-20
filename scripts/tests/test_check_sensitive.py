"""Tests for scripts/check-sensitive.py using runtime-constructed fixtures."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts' / 'check-sensitive.py'


class CheckSensitiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp, True)
        self._git('init')
        self._git('config', 'user.name', 'Test User')
        self._git('config', 'user.email', '12345+testuser' + '@users.noreply.github.com')

    def _git(self, *args):
        return subprocess.run(
            ['git', *args],
            cwd=self.temp,
            check=True,
            capture_output=True,
            text=True,
        )

    def _run_scan(self, *args, denylist_path=None, name_dirs=None, name_allow=None, url_allow=None):
        env = dict(os.environ)
        env.pop('PLUGIN_DENYLIST', None)
        env.pop('PLUGIN_NAME_DIRS', None)
        env.pop('PLUGIN_NAME_ALLOW', None)
        env.pop('PLUGIN_URL_ALLOW', None)
        if denylist_path is not None:
            env['PLUGIN_DENYLIST'] = str(denylist_path)
        if name_dirs is not None:
            env['PLUGIN_NAME_DIRS'] = str(name_dirs)
        if name_allow is not None:
            env['PLUGIN_NAME_ALLOW'] = name_allow
        if url_allow is not None:
            env['PLUGIN_URL_ALLOW'] = url_allow
        return subprocess.run(
            ['python3', str(SCRIPT), *args],
            cwd=self.temp,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_clean_repo_passes(self):
        (self.temp / 'clean.txt').write_text('just normal public text\n', encoding='utf-8')
        self._git('add', 'clean.txt')
        res = self._run_scan()
        self.assertEqual(0, res.returncode, res.stderr)
        self.assertIn('0 hits', res.stdout + res.stderr)
        res_all = self._run_scan('--all')
        self.assertEqual(0, res_all.returncode, res_all.stderr)

    def test_detects_all_patterns_without_printing_matched_values(self):
        # Construct all offending values at runtime by concatenation/escapes
        # so this test file itself contains no key-shaped or sensitive literals.
        secrets = [
            ('machine path', '/' + 'Users/' + 'alice/proj'),
            ('machine path', '/' + 'Volumes/' + 'External/data'),
            ('machine path', '/' + 'home/' + 'bob/work'),
            ('email', 'alice' + '@' + 'example.org'),
            ('vietnamese', 'xin ch' + '\u00e0' + 'o'),
            ('private IP', '10.' + '0.0.1'),
            ('private IP', '192.168.' + '1.50'),
            ('private IP', '172.16.' + '5.4'),
            ('key', 'cr_' + 'a' * 20),
            ('key', 'sk-' + 'b' * 20),
            ('key', 'AIza' + 'c' * 30),
            ('key', 'ghp_' + 'd' * 20),
            ('key', 'github_pat_' + 'e' * 20),
            ('key', 'AKIA' + '1' * 16),
            ('key', 'xox' + 'b-' + '12345-abcde'),
            ('key', '-----BEGIN ' + 'RSA PRIVATE KEY-----'),
            ('key', 'Bearer ' + 'f' * 20),
        ]
        content = '\n'.join(val for _, val in secrets) + '\n'
        (self.temp / 'bad.txt').write_text(content, encoding='utf-8')
        (self.temp / '.env.local').write_text('FOO=bar\n', encoding='utf-8')
        (self.temp / 'service-account-dev.json').write_text('{}\n', encoding='utf-8')
        self._git('add', 'bad.txt', '.env.local', 'service-account-dev.json')

        # Default (staged) scan
        res = self._run_scan()
        self.assertEqual(1, res.returncode)
        output = res.stdout + res.stderr
        for label, val in secrets:
            self.assertIn(label, output)
            self.assertNotIn(val, output)
        self.assertIn('sensitive file name', output)
        self.assertIn('hits', output)

        # Commit and verify --all scan
        self._git('commit', '-m', 'add test fixtures')
        res_all = self._run_scan('--all')
        self.assertEqual(1, res_all.returncode)
        output_all = res_all.stdout + res_all.stderr
        for label, val in secrets:
            self.assertIn(label, output_all)
            self.assertNotIn(val, output_all)

    def test_denylist_prints_entry_line_number_never_text(self):
        deny_secret = 'MegaSecret' + 'Codename42'
        denylist = self.temp / 'denylist.txt'
        denylist.write_text('# comment line\n\n' + deny_secret + '\n', encoding='utf-8')

        (self.temp / 'notes.txt').write_text('Contains ' + deny_secret.lower() + ' here\n', encoding='utf-8')
        self._git('add', 'notes.txt')

        res = self._run_scan(denylist_path=denylist)
        self.assertEqual(1, res.returncode)
        output = res.stdout + res.stderr
        self.assertIn('notes.txt:1: denylist #3', output)
        self.assertNotIn(deny_secret, output)
        self.assertNotIn(deny_secret.lower(), output)

    def test_vietnamese_flags_y_acute_and_ignores_math_signs(self):
        math_text = '2 ' + '\u00d7' + ' 3 = 6 and 6 ' + '\u00f7' + ' 2 = 3\n'
        (self.temp / 'math.txt').write_text(math_text, encoding='utf-8')
        self._git('add', 'math.txt')
        res_clean = self._run_scan()
        self.assertEqual(0, res_clean.returncode, res_clean.stderr)
        res_clean_all = self._run_scan('--all')
        self.assertEqual(0, res_clean_all.returncode, res_clean_all.stderr)

        vn_text = 'word ' + '\u00fd' + ' end\n'
        (self.temp / 'vn.txt').write_text(vn_text, encoding='utf-8')
        self._git('add', 'vn.txt')
        res_vn = self._run_scan()
        self.assertEqual(1, res_vn.returncode)
        output = res_vn.stdout + res_vn.stderr
        self.assertIn('vn.txt:1: vietnamese', output)
        self.assertNotIn('\u00fd', output)

    def test_names_mode_depth1_only_globs_allow_stoplist_and_missing(self):
        name_root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, name_root, True)

        proj1 = 'Fake' + 'Alpha' + 'Proj'
        depth2_proj = 'Sub' + 'Beta' + 'Client'
        allowed_proj = 'Allowed' + 'Repo' + 'Name'
        glob_proj_a = 'Glob' + 'Gamma' + 'App'
        glob_proj_b = 'Glob' + 'Delta' + 'Svc'

        (name_root / proj1 / depth2_proj).mkdir(parents=True)
        (name_root / allowed_proj).mkdir()
        (name_root / 'abc').mkdir()
        (name_root / '.hidden_proj').mkdir()

        added_stoplist = [
            'output', 'data', 'skills', 'logs', 'source', 'demo', 'content',
            'chosen', 'secrets', 'plugins', 'docs', 'images', 'fonts', 'public',
            'private', 'temp', 'tmp', 'cache', 'dist', 'out', 'bin', 'vendor',
            'examples', 'sample', 'samples',
        ]
        for word in added_stoplist:
            (name_root / word).mkdir()

        glob_parent = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, glob_parent, True)
        (glob_parent / 'ws_one' / glob_proj_a).mkdir(parents=True)
        (glob_parent / 'ws_two' / glob_proj_b).mkdir(parents=True)

        missing_dir = name_root / 'does_not_exist'
        name_dirs = os.pathsep.join([str(missing_dir), str(name_root), str(glob_parent / 'ws_*')])

        # Depth-2 names, stoplist words, short names, dot names, partial substrings,
        # and PLUGIN_NAME_ALLOW entries are all ignored; missing dir warns without crash.
        clean_text = (
            ' '.join(added_stoplist)
            + ' ' + depth2_proj.lower()
            + ' ' + allowed_proj.lower()
            + ' abc hidden_proj x' + proj1.lower() + 'y\n'
        )
        (self.temp / 'safe.txt').write_text(clean_text, encoding='utf-8')
        self._git('add', 'safe.txt')
        res_clean = self._run_scan(
            name_dirs=name_dirs,
            name_allow='other_name, ' + allowed_proj.upper(),
        )
        self.assertEqual(0, res_clean.returncode, res_clean.stderr)
        self.assertIn('warning:', res_clean.stderr.lower())

        # Whole-word case-insensitive matches for depth-1 names (direct & glob-expanded) are flagged,
        # and the matched name never appears in output.
        for idx, target in enumerate((proj1, glob_proj_a, glob_proj_b)):
            hit_file = self.temp / f'hit_{idx}.txt'
            hit_file.write_text('Working on ' + target.upper() + ' today.\n', encoding='utf-8')
            self._git('add', hit_file.name)
            res_hit = self._run_scan(
                name_dirs=name_dirs,
                name_allow=allowed_proj,
            )
            self.assertEqual(1, res_hit.returncode)
            output = res_hit.stdout + res_hit.stderr
            self.assertIn(hit_file.name + ':1: project-name', output)
            self.assertNotIn(target.lower(), output)
            self.assertNotIn(target.upper(), output)
            self._git('rm', '-f', hit_file.name)

    def test_history_finds_deleted_secret_and_flags_non_noreply_author(self):
        old_secret = 'sk-' + 'z' * 24
        (self.temp / 'old.txt').write_text(old_secret + '\n', encoding='utf-8')
        self._git('add', 'old.txt')
        self._git('commit', '-m', 'add secret')

        self._git('rm', 'old.txt')
        (self.temp / 'clean.txt').write_text('all clean now\n', encoding='utf-8')
        self._git('add', 'clean.txt')
        self._git('commit', '-m', 'remove secret')

        # Tree is clean, but --history catches the deleted secret.
        res_tree = self._run_scan('--all')
        self.assertEqual(0, res_tree.returncode, res_tree.stderr)

        res_hist = self._run_scan('--history')
        self.assertEqual(1, res_hist.returncode)
        hist_out = res_hist.stdout + res_hist.stderr
        self.assertIn(':old.txt:1: key', hist_out)
        self.assertNotIn(old_secret, hist_out)
        self.assertIn('hits', hist_out)

        # Non-noreply author email is flagged by --history.
        bad_email = 'dev' + '@' + 'example.org'
        self._git('config', 'user.email', bad_email)
        (self.temp / 'clean2.txt').write_text('another clean file\n', encoding='utf-8')
        self._git('add', 'clean2.txt')
        self._git('commit', '-m', 'bad author')

        res_author = self._run_scan('--history')
        self.assertEqual(1, res_author.returncode)
        author_out = res_author.stdout + res_author.stderr
        self.assertIn(':author-email', author_out)
        self.assertNotIn(bad_email, author_out)

    def test_url_host_allowlist_and_env_extension(self):
        # Construct non-allowlisted URLs at runtime so this file has no literal unknown URLs.
        allowed_lines = [
            'https://' + 'github.com/owner/repo',
            'https://' + 'API.GITHUB.COM/users',
            'https://' + 'docs.anthropic.com/en/docs',
            'https://' + 'api.anthropic.com/v1/messages',
            'https://' + 'api.chatanywhere.org/v1',
            'https://' + 'api.chatanywhere.tech/v1',
            'https://' + 'sub.example.com/path',
            'http://' + 'example.org:8080/test',
            'https://' + 'relay.invalid/v1',
            'https://' + 'foo.invalid/bar',
            'http://' + 'localhost:8080/health',
            'https://' + 'x/single-label',
            'https://' + '<your-gateway>/v1',
        ]
        (self.temp / 'allowed.txt').write_text('\n'.join(allowed_lines) + '\n', encoding='utf-8')
        self._git('add', 'allowed.txt')
        res_clean = self._run_scan()
        self.assertEqual(0, res_clean.returncode, res_clean.stderr)

        bad_host = 'evil.' + 'example.' + 'net'
        custom_host = 'custom-' + 'gw.internal.' + 'test'
        bad_url = 'https://' + bad_host + '/secret'
        custom_url = 'http://' + 'sub.' + custom_host + ':8443/v1'
        (self.temp / 'urls.txt').write_text(bad_url + '\n' + custom_url + '\n', encoding='utf-8')
        self._git('add', 'urls.txt')

        # Staged scan flags both unknown hosts without printing them.
        res = self._run_scan()
        self.assertEqual(1, res.returncode)
        output = res.stdout + res.stderr
        self.assertIn('urls.txt:1: url-host', output)
        self.assertIn('urls.txt:2: url-host', output)
        self.assertNotIn(bad_host, output)
        self.assertNotIn(custom_host, output)

        # PLUGIN_URL_ALLOW permits custom_host (and subdomains, case-insensitively).
        res_ext = self._run_scan(url_allow='other.test, ' + custom_host.upper())
        self.assertEqual(1, res_ext.returncode)
        ext_output = res_ext.stdout + res_ext.stderr
        self.assertIn('urls.txt:1: url-host', ext_output)
        self.assertNotIn('urls.txt:2: url-host', ext_output)
        self.assertNotIn(bad_host, ext_output)

        # --all and --history both report url-host without printing the host.
        self._git('commit', '-m', 'add urls')
        res_all = self._run_scan('--all', url_allow=bad_host + ',' + custom_host)
        self.assertEqual(0, res_all.returncode, res_all.stderr)

        res_hist = self._run_scan('--history')
        self.assertEqual(1, res_hist.returncode)
        hist_out = res_hist.stdout + res_hist.stderr
        self.assertIn(':urls.txt:1: url-host', hist_out)
        self.assertNotIn(bad_host, hist_out)

    def test_usage_error_exits_with_2(self):
        res = self._run_scan('--not-a-real-flag')
        self.assertEqual(2, res.returncode)


if __name__ == '__main__':
    unittest.main()
