"""Integration tests for skill-updates using isolated local repositories."""
import json
import importlib.util
import os
import stat
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "skill-updates.py"


def git(args, cwd):
    return subprocess.run(["git"] + args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True).stdout.strip()


class SkillUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.skills = self.root / "skills"
        self.skills.mkdir()
        self.repo = self.root / "upstream"
        self.repo.mkdir()
        git(["init", "-b", "main"], self.repo)
        git(["config", "user.email", "test-user"], self.repo)
        git(["config", "user.name", "Test"], self.repo)
        self.skill = self.skills / "demo"
        self.skill.mkdir()
        self.manifest = self.skills / "upstream.json"
        self.cache = self.root / "cache"
        self.write_repo("hello.txt", "one\ntwo\nthree\n")
        self.base = git(["rev-parse", "HEAD"], self.repo)
        (self.skill / "hello.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        self.record()

    def tearDown(self):
        self.tmp.cleanup()

    def write_repo(self, name, content):
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        git(["add", name], self.repo)
        git(["commit", "-m", "change " + name], self.repo)

    def record(self, data=None):
        obj = data if data is not None else {"demo": {"repo": str(self.repo), "path": "", "base": self.base, "branch": "main"}}
        self.manifest.write_text(json.dumps(obj), encoding="utf-8")

    def environment(self):
        env = os.environ.copy()
        env.update({"HOME": str(self.root), "SKILLS_DIR": str(self.skills),
                    "SKILL_UPDATES_MANIFEST": str(self.manifest),
                    "SKILL_UPDATES_CACHE": str(self.cache),
                    "SKILL_UPDATES_STATE_DIR": str(self.root / "state")})
        return env

    def invoke(self, *args):
        return subprocess.run(["python3", str(SCRIPT)] + list(args), env=self.environment(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def load_module(self):
        spec = importlib.util.spec_from_file_location("skill_updates_test_module", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.SKILLS, module.MANIFEST, module.CACHE = self.skills, self.manifest, self.cache
        module.STATE_DIR = self.root / "state"
        return module

    def test_check_during_apply_does_not_recover_live_staging(self):
        self.write_repo("hello.txt", "updated\n")
        paused, resume = self.root / "paused", self.root / "resume"
        driver = self.root / "paused_apply.py"
        driver.write_text('''import importlib.util
import os
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("skill_updates", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
replace = os.replace
paused_path, resume_path = sys.argv[2:]
sys.argv = [sys.argv[0], "apply", "demo"]
def wait_before_replace(source, target):
    if Path(target).name.startswith(".skill-update-old-"):
        Path(paused_path).touch()
        deadline = time.monotonic() + 15
        while not Path(resume_path).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("test resume timed out")
            time.sleep(0.01)
    return replace(source, target)
module.os.replace = wait_before_replace
sys.exit(module.main())
''', encoding="utf-8")
        with subprocess.Popen(["python3", str(driver), str(SCRIPT), str(paused), str(resume)],
                              env=self.environment(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as applying:
            try:
                deadline = time.monotonic() + 10
                while not paused.exists() and applying.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(paused.exists(), "apply did not reach the first rename")
                with subprocess.Popen(["python3", str(SCRIPT), "check"], env=self.environment(),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as checking:
                    try:
                        checking.communicate(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
                    finally:
                        resume.touch()
                    apply_out, apply_err = applying.communicate(timeout=10)
                    check_out, check_err = checking.communicate(timeout=10)
                self.assertEqual(applying.returncode, 0, apply_out + apply_err)
                self.assertEqual(checking.returncode, 0, check_out + check_err)
            finally:
                resume.touch()
                if applying.poll() is None:
                    applying.kill()
                    applying.communicate()
        self.assertEqual((self.skill / "hello.txt").read_text(), "updated\n")
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"],
                         git(["rev-parse", "HEAD"], self.repo))
        for prefix in (".skill-update-state-*", ".skill-update-old-*", ".skill-update-new-*"):
            self.assertFalse(list(self.skills.glob(prefix)))

    def test_apply_fetches_shallow_base_and_preserves_local_deletion(self):
        (self.skill / "hello.txt").unlink()
        self.write_repo("added.txt", "upstream addition\n")
        shallow = self.root / "shallow"
        git(["clone", "--depth=1", self.repo.as_uri(), str(shallow)], self.root)
        missing = subprocess.run(["git", "cat-file", "-e", self.base + "^{commit}"],
                                 cwd=str(shallow), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(missing.returncode, 0)
        module = self.load_module()
        with mock.patch.object(module, "ensure_repo", return_value=shallow):
            module.apply_update("demo")
        self.assertFalse((self.skill / "hello.txt").exists(), "owner-deleted file was restored")
        self.assertEqual((self.skill / "added.txt").read_text(), "upstream addition\n")
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"],
                         git(["rev-parse", "HEAD"], self.repo))

    def test_apply_unavailable_base_fails_without_installing(self):
        self.base = "0" * 40
        self.record()
        before = self.manifest.read_bytes()
        self.write_repo("added.txt", "upstream addition\n")
        result = self.invoke("apply", "demo")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("base commit unavailable", result.stderr)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual((self.skill / "hello.txt").read_text(), "one\ntwo\nthree\n")
        self.assertFalse((self.skill / "added.txt").exists())
        self.assertFalse((self.skills / ".backup").exists())
        self.assertFalse(list(self.skills.glob(".skill-update-*")))

    def test_bootstrap_uses_fetched_remote_branch_in_existing_cache(self):
        result = self.invoke("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.write_repo("hello.txt", "new upstream\n")
        head = git(["rev-parse", "HEAD"], self.repo)
        (self.skill / "hello.txt").write_text("new upstream\n", encoding="utf-8")
        self.record({})
        result = self.invoke("bootstrap", "demo", "--repo", str(self.repo), "--branch", "main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"], head)
        self.assertIn("exact match", result.stdout)

    def interrupt_swap(self, point, fail_rollback=False):
        self.write_repo("hello.txt", "updated\n")
        head = git(["rev-parse", "HEAD"], self.repo)
        git(["update-ref", "refs/remotes/origin/main", head], self.repo)
        module = self.load_module()
        replace = os.replace

        def interrupted_replace(source, target):
            source, target = Path(source), Path(target)
            if source == self.skill and point == "before old rename":
                raise KeyboardInterrupt("simulated interrupt")
            if source == self.skill and point == "after old rename":
                replace(source, target)
                raise KeyboardInterrupt("simulated interrupt")
            if source.name.startswith(".skill-update-new-"):
                if point == "after new rename":
                    replace(source, target)
                raise KeyboardInterrupt("simulated interrupt")
            if fail_rollback and source.name.startswith(".skill-update-old-"):
                raise OSError("simulated rollback failure")
            return replace(source, target)

        expected = OSError if fail_rollback else KeyboardInterrupt
        with mock.patch.object(module, "ensure_repo", return_value=self.repo), \
                mock.patch.object(module.os, "replace", side_effect=interrupted_replace):
            with self.assertRaises(expected):
                module.apply_update("demo")
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"], self.base)
        if not fail_rollback:
            self.assertEqual((self.skill / "hello.txt").read_text(), "one\ntwo\nthree\n")
            self.assertFalse(list(self.skills.glob(".skill-update-old-*")))
            self.assertFalse(list(self.skills.glob(".skill-update-new-*")))
        backup = list((self.skills / ".backup").glob("demo-*"))
        self.assertEqual(len(backup), 1)
        self.assertEqual((backup[0] / "hello.txt").read_text(), "one\ntwo\nthree\n")

    def test_interrupt_before_old_rename_keeps_installed_tree(self):
        self.interrupt_swap("before old rename")

    def test_interrupt_after_old_rename_restores_installed_tree(self):
        self.interrupt_swap("after old rename")

    def test_interrupt_between_renames_restores_installed_tree(self):
        self.interrupt_swap("before new rename")

    def test_interrupt_after_new_rename_restores_installed_tree(self):
        self.interrupt_swap("after new rename")

    def test_failed_rollback_keeps_staging_and_next_run_recovers(self):
        self.interrupt_swap("before new rename", fail_rollback=True)
        self.assertFalse(self.skill.exists())
        staged = list(self.skills.glob(".skill-update-new-*"))
        held = list(self.skills.glob(".skill-update-old-*"))
        self.assertEqual(len(staged), 1)
        self.assertEqual(len(held), 1)
        self.assertEqual((staged[0] / "hello.txt").read_text(), "updated\n")
        self.assertEqual((held[0] / "hello.txt").read_text(), "one\ntwo\nthree\n")
        result = self.invoke("check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("recovered unfinished skill update", result.stderr)
        self.assertEqual((self.skill / "hello.txt").read_text(), "one\ntwo\nthree\n")
        self.assertFalse(staged[0].exists())
        self.assertFalse(held[0].exists())
        self.assertFalse(list(self.skills.glob(".skill-update-state-*")))
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"], self.base)
        self.assertFalse(self.cache.exists())

    def test_failed_rollback_after_new_rename_keeps_both_trees(self):
        self.interrupt_swap("after new rename", fail_rollback=True)
        self.assertFalse(self.skill.exists())
        staged = list(self.skills.glob(".skill-update-new-*"))
        held = list(self.skills.glob(".skill-update-old-*"))
        self.assertEqual(len(staged), 1)
        self.assertEqual(len(held), 1)
        self.assertEqual((staged[0] / "hello.txt").read_text(), "updated\n")
        self.assertEqual((held[0] / "hello.txt").read_text(), "one\ntwo\nthree\n")

    def interrupt_manifest_commit(self, committed, restart=False, same_base=False):
        if same_base:
            self.manifest.write_text(json.dumps(json.loads(self.manifest.read_text()),
                                               indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            self.write_repo("hello.txt", "updated\n")
        original_folder = self.skill.stat().st_ino
        head = git(["rev-parse", "HEAD"], self.repo)
        git(["update-ref", "refs/remotes/origin/main", head], self.repo)
        module = self.load_module()
        replace = os.replace

        def interrupted_replace(source, target):
            if Path(target) == self.manifest:
                if committed:
                    replace(source, target)
                raise KeyboardInterrupt("interrupted manifest replacement")
            return replace(source, target)

        finish = module.finish_update
        with mock.patch.object(module, "ensure_repo", return_value=self.repo), \
                mock.patch.object(module.os, "replace", side_effect=interrupted_replace), \
                mock.patch.object(module, "finish_update", wraps=finish) as recovery:
            if restart:
                recovery.side_effect = KeyboardInterrupt("interrupted handler")
            with self.assertRaises(KeyboardInterrupt):
                module.apply_update("demo")
        if restart:
            self.assertTrue(list(self.skills.glob(".skill-update-state-*")))
            self.assertTrue(list(self.skills.glob(".skill-update-old-*")))
            result = self.invoke("check")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("recovered unfinished skill update", result.stderr)
            self.assertFalse(self.cache.exists())
        expected_base = head if committed else self.base
        expected_tree = "updated\n" if committed and not same_base else "one\ntwo\nthree\n"
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"], expected_base)
        self.assertEqual((self.skill / "hello.txt").read_text(), expected_tree)
        self.assertEqual(self.skill.stat().st_ino == original_folder, not committed)
        for prefix in (".skill-update-state-*", ".skill-update-old-*", ".skill-update-new-*"):
            self.assertFalse(list(self.skills.glob(prefix)))
        if committed and not same_base:
            self.write_repo("hello.txt", "one\ntwo\nthree\n")
        result = self.invoke("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| demo | " + ("up to date" if same_base else "update available"), result.stdout)

    def test_interrupt_before_manifest_replace_restores_old_tree_and_base(self):
        self.interrupt_manifest_commit(committed=False)

    def test_interrupt_after_manifest_replace_keeps_new_tree_and_base(self):
        self.interrupt_manifest_commit(committed=True)

    def test_restart_before_manifest_commit_recovers_old_tree_and_base(self):
        self.interrupt_manifest_commit(committed=False, restart=True)

    def test_restart_after_manifest_commit_keeps_new_tree_and_base(self):
        self.interrupt_manifest_commit(committed=True, restart=True)

    def test_identical_manifest_before_commit_restores_old_tree(self):
        self.interrupt_manifest_commit(committed=False, restart=True, same_base=True)

    def test_identical_manifest_after_commit_keeps_new_tree(self):
        self.interrupt_manifest_commit(committed=True, restart=True, same_base=True)

    def test_existing_skill_and_leftover_aside_are_reported_without_writes(self):
        held = self.skills / ".skill-update-old-leftover"
        shutil.copytree(self.skill, held)
        result = self.invoke("apply", "demo")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unfinished skill update", result.stderr)
        self.assertIn(held.name, result.stderr)
        self.assertEqual((self.skill / "hello.txt").read_text(), "one\ntwo\nthree\n")
        self.assertEqual((held / "hello.txt").read_text(), "one\ntwo\nthree\n")
        self.assertFalse(self.cache.exists())

    def test_apply_replaces_old_directory_with_upstream_file(self):
        self.write_repo("nested/old.txt", "old\n")
        self.base = git(["rev-parse", "HEAD"], self.repo)
        self.record()
        (self.skill / "nested").mkdir()
        (self.skill / "nested/old.txt").write_text("old\n", encoding="utf-8")
        (self.skill / "nested/.DS_Store").write_bytes(b"ignored")
        (self.skill / ".DS_Store").write_bytes(b"root metadata")
        git(["rm", "nested/old.txt"], self.repo)
        if (self.repo / "nested").exists():
            (self.repo / "nested").rmdir()
        self.write_repo("nested", "replacement\n")
        result = self.invoke("apply", "demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.skill / "nested").read_text(), "replacement\n")
        self.assertEqual((self.skill / ".DS_Store").read_bytes(), b"root metadata")
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"],
                         git(["rev-parse", "HEAD"], self.repo))
        backup = list((self.skills / ".backup").glob("demo-*"))
        self.assertEqual((backup[0] / "nested/old.txt").read_text(), "old\n")

    def test_up_to_date(self):
        result = self.invoke("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up to date", result.stdout)

    def test_update_available(self):
        self.write_repo("hello.txt", "one\ntwo\nthree\nfour\n")
        result = self.invoke("check")
        self.assertIn("update available (1 commits)", result.stdout)
        self.assertIn("change hello.txt", result.stdout)

    def test_local_edits_detected(self):
        (self.skill / "hello.txt").write_text("one\nowner\nthree\n", encoding="utf-8")
        result = self.invoke("check")
        self.assertIn("local edits", result.stdout)

    def test_apply_merges_non_overlapping_edits(self):
        (self.skill / "hello.txt").write_text("one\nowner\nthree\n", encoding="utf-8")
        self.write_repo("hello.txt", "one\ntwo\nthree\nupstream\n")
        result = self.invoke("apply", "demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.skill / "hello.txt").read_text(), "one\nowner\nthree\nupstream\n")
        self.assertTrue(list((self.skills / ".backup").glob("demo-*")))
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"], git(["rev-parse", "HEAD"], self.repo))

    def test_apply_conflict_keeps_original_and_backup(self):
        original = "one\nowner\nthree\n"
        (self.skill / "hello.txt").write_text(original, encoding="utf-8")
        self.write_repo("hello.txt", "one\nupstream\nthree\n")
        result = self.invoke("apply", "demo")
        self.assertEqual(result.returncode, 1)
        self.assertIn("hello.txt", result.stdout)
        self.assertEqual((self.skill / "hello.txt").read_text(), original)
        self.assertTrue(list((self.skills / ".backup").glob("demo-*")))

    def test_apply_from_installs_verified_staged_merge_and_advances_base(self):
        (self.skill / "hello.txt").write_text("one\nlocal\nthree\n", encoding="utf-8")
        self.write_repo("hello.txt", "one\nupstream\nthree\n")
        self.write_repo("added.txt", "upstream addition\n")
        staged = self.root / "staged"
        staged.mkdir()
        (staged / "hello.txt").write_text("one\nlocal + upstream\nthree\n", encoding="utf-8")
        (staged / "added.txt").write_text("upstream addition\n", encoding="utf-8")
        result = self.invoke("apply", "demo", "--from", str(staged))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.skill / "hello.txt").read_text(), "one\nlocal + upstream\nthree\n")
        self.assertEqual((self.skill / "added.txt").read_text(), "upstream addition\n")
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"], git(["rev-parse", "HEAD"], self.repo))
        self.assertTrue(list((self.skills / ".backup").glob("demo-*")))

    def test_apply_from_rejects_unmerged_clean_paths_without_writing(self):
        self.write_repo("hello.txt", "one\ntwo\nthree\nfour\n")
        staged = self.root / "staged"
        staged.mkdir()
        (staged / "hello.txt").write_text("wrong staged content\n", encoding="utf-8")
        result = self.invoke("apply", "demo", "--from", str(staged))
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed merge verification", result.stderr)
        self.assertEqual((self.skill / "hello.txt").read_text(), "one\ntwo\nthree\n")
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"], self.base)

    def test_apply_rejects_traversal_skill_name(self):
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("safe\n", encoding="utf-8")
        result = self.invoke("apply", "../outside")
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid skill name", result.stderr)
        self.assertEqual(marker.read_text(), "safe\n")

    def test_apply_refuses_symlinked_skill_directory(self):
        outside = self.root / "outside-skill"
        self.skill.rename(outside)
        self.skill.symlink_to(outside, target_is_directory=True)
        result = self.invoke("apply", "demo")
        self.assertEqual(result.returncode, 1)
        self.assertIn("symlink", result.stderr)
        self.assertEqual((outside / "hello.txt").read_text(), "one\ntwo\nthree\n")

    def test_apply_refuses_nested_symlink_before_read_or_write(self):
        outside = self.root / "outside.txt"
        outside.write_text("private\n", encoding="utf-8")
        (self.skill / "linked.txt").symlink_to(outside)
        result = self.invoke("apply", "demo")
        self.assertEqual(result.returncode, 1)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(outside.read_text(), "private\n")

    def test_apply_restores_old_tree_if_manifest_write_fails(self):
        git(["update-ref", "refs/remotes/origin/main", self.base], self.repo)
        spec = importlib.util.spec_from_file_location("skill_updates_test_module", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(module, "SKILLS", self.skills), \
                mock.patch.object(module, "MANIFEST", self.manifest), \
                mock.patch.object(module, "CACHE", self.cache), \
                mock.patch.object(module, "STATE_DIR", self.root / "state"), \
                mock.patch.object(module, "ensure_repo", return_value=self.repo), \
                mock.patch.object(module, "write_manifest", side_effect=OSError("simulated manifest failure")):
            with self.assertRaisesRegex(OSError, "simulated manifest failure"):
                module.apply_update("demo")
        self.assertEqual((self.skill / "hello.txt").read_text(), "one\ntwo\nthree\n")
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"], self.base)
        self.assertFalse(list(self.skills.glob(".skill-update-old-*")))

    def test_apply_preserves_executable_mode(self):
        (self.skill / "hello.txt").chmod(0o755)
        self.write_repo("hello.txt", "one\ntwo\nthree\nupstream\n")
        result = self.invoke("apply", "demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE((self.skill / "hello.txt").stat().st_mode), 0o755)

    def test_check_detects_mode_only_local_edit(self):
        (self.skill / "hello.txt").chmod(0o755)
        result = self.invoke("check")
        self.assertIn("local edits", result.stdout)

    def test_bootstrap_scores_changed_line_lengths_not_offsets(self):
        self.record({})
        (self.skill / "hello.txt").unlink()
        lines = ["line-{:03d}".format(i) for i in range(100)]
        base_tree = "\n".join(lines) + "\n"
        older = lines[:]
        older[70] = "older-difference"
        self.write_repo("tree/skill.txt", "\n".join(older) + "\n")
        older_commit = git(["rev-parse", "HEAD"], self.repo)
        newer = lines[:]
        newer[0], newer[1] = "new-difference-a", "new-difference-b"
        self.write_repo("tree/skill.txt", "\n".join(newer) + "\n")
        (self.skill / "skill.txt").write_text(base_tree, encoding="utf-8")
        result = self.invoke("bootstrap", "demo", "--repo", str(self.repo), "--path", "tree", "--branch", "main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("closest match", result.stdout)
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"], older_commit)

    def test_bootstrap_finds_exact_base_commit(self):
        (self.skill / "hello.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        self.record({})
        result = self.invoke("bootstrap", "demo", "--repo", str(self.repo), "--branch", "main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exact match", result.stdout)
        self.assertEqual(json.loads(self.manifest.read_text())["demo"]["base"], self.base)

    def test_new_folder_reported(self):
        (self.skills / "stray").mkdir()
        result = self.invoke("check")
        self.assertIn("new: not in manifest", result.stdout)

    def test_local_only_skill(self):
        self.record({"demo": {"local": True}})
        result = self.invoke("check")
        self.assertIn("local only", result.stdout)


if __name__ == "__main__":
    unittest.main()
