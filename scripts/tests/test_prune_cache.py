import os
import pathlib
import subprocess
import tempfile
import unittest


REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "prune-cache.sh"


class PruneCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.home = self.root / "home"
        self.cache = self.home / ".claude" / "plugins" / "cache"
        self.links = self.home / ".claude" / "scripts"
        self.plugin = self.cache / "market" / "task-runner"
        self.plugin.mkdir(parents=True)
        self.links.mkdir(parents=True)
        for name in ("1.0.0", "1.0.1", "1.0.2", "2.0.0"):
            (self.plugin / name).mkdir()
            (self.plugin / name / "payload").write_text(name)
        self.env = dict(os.environ, HOME=str(self.home))

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, *args):
        return subprocess.run(
            ["bash", str(SCRIPT), "--cache-root", str(self.cache), *args],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def test_dry_run_lists_old_versions_without_deleting(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(str(self.plugin / "1.0.0"), result.stdout)
        self.assertIn(str(self.plugin / "1.0.1"), result.stdout)
        self.assertIn("dry-run: nothing deleted", result.stdout)
        self.assertTrue((self.plugin / "1.0.0").is_dir())

    def test_yes_removes_only_older_than_newest_two(self):
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.plugin / "1.0.0").exists())
        self.assertFalse((self.plugin / "1.0.1").exists())
        self.assertTrue((self.plugin / "1.0.2").is_dir())
        self.assertTrue((self.plugin / "2.0.0").is_dir())

    def test_linked_version_survives(self):
        target = self.plugin / "1.0.0" / "scripts" / "task"
        target.parent.mkdir()
        target.touch()
        (self.links / "task").symlink_to(target)
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.plugin / "1.0.0").is_dir())

    def test_non_numeric_and_symlinked_dirs_are_untouched(self):
        odd = self.plugin / "backup"
        odd.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.plugin / "0.9.0"
        linked.symlink_to(outside, target_is_directory=True)
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(odd.is_dir())
        self.assertTrue(linked.is_symlink())
        self.assertTrue(outside.is_dir())

    def test_oversized_keep_is_rejected_without_listing_candidates(self):
        result = self.run_script("--keep", "18446744073709551616")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(str(self.plugin / "1.0.0"), result.stdout)
        self.assertTrue((self.plugin / "1.0.0").is_dir())

    def test_link_resolution_failure_aborts_before_listing(self):
        target = self.plugin / "1.0.0" / "scripts" / "task"
        target.parent.mkdir()
        target.touch()
        (self.links / "task").symlink_to(target)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        readlink = bin_dir / "readlink"
        readlink.write_text("#!/bin/sh\nexit 1\n")
        readlink.chmod(0o755)
        env = dict(self.env, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
        result = subprocess.run(
            ["bash", str(SCRIPT), "--cache-root", str(self.cache)],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(str(self.plugin / "1.0.0"), result.stdout)

    def test_hidden_relative_script_link_protects_version(self):
        target = self.plugin / "1.0.0" / "scripts" / "task"
        target.parent.mkdir()
        target.touch()
        hidden_link = self.links / ".hidden-task"
        hidden_link.symlink_to(os.path.relpath(target, self.links))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(str(self.plugin / "1.0.0"), result.stdout)

    def test_symlinked_version_does_not_consume_retention_slot(self):
        plugin = self.cache / "market" / "other-plugin"
        plugin.mkdir()
        for name in ("1.0", "2.0", "3.0"):
            (plugin / name).mkdir()
        (plugin / "9.0").symlink_to(plugin / "1.0", target_is_directory=True)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(str(plugin / "1.0"), result.stdout)
        self.assertNotIn(str(plugin / "2.0"), result.stdout)


if __name__ == "__main__":
    unittest.main()
