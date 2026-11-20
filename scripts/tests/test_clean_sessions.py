import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("clean_sessions", str(SCRIPT / "clean-sessions.py"))
clean_sessions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(clean_sessions)

SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"
SESSION_C = "33333333-3333-4333-8333-333333333333"


class CleanSessionsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        self.root = root
        self.projects = root / "projects"
        self.file_history = root / "file-history"
        self.projects.mkdir()
        self.file_history.mkdir()
        self.db = root / "state.vscdb"
        for name, value in (("PROJECTS_DIR", str(self.projects)),
                            ("FILE_HISTORY_DIR", str(self.file_history)),
                            ("STATE_DB_CANDIDATES", [str(self.db)])):
            patcher = mock.patch.object(clean_sessions, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        host = mock.patch.object(clean_sessions.socket, "gethostname", return_value="test-host")
        host.start()
        self.addCleanup(host.stop)
        environment = mock.patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("CLAUDE_SESSION_ID", None)

    def write_state_db(self, hidden):
        con = sqlite3.connect(str(self.db))
        try:
            con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value TEXT)")
            con.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                        ("Anthropic.claude-code", json.dumps({"hiddenSessionIds": list(hidden)})))
            con.commit()
        finally:
            con.close()

    def make_session(self, sid, project="project-a", history=True):
        folder = self.projects / project
        folder.mkdir(parents=True, exist_ok=True)
        transcript = folder / (sid + ".jsonl")
        transcript.write_text('{"type": "summary"}\n')
        companion = folder / sid
        companion.mkdir(exist_ok=True)
        (companion / "meta.json").write_text("{}")
        history_dir = self.file_history / sid
        if history:
            history_dir.mkdir(exist_ok=True)
            (history_dir / "state.json").write_text("{}")
        return transcript, companion, history_dir

    def write_tombstone(self, name, lines):
        (self.projects / name).write_text("\n".join(lines) + "\n")

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = clean_sessions.main()
        return code, out.getvalue(), err.getvalue()

    def test_hidden_session_removes_transcript_companion_and_file_history(self):
        kept = self.make_session(SESSION_B)
        gone = self.make_session(SESSION_A)
        self.write_state_db([SESSION_A])
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertEqual("", err)
        self.assertFalse(gone[0].exists())
        self.assertFalse(gone[1].exists())
        self.assertFalse(gone[2].exists())
        for path in kept:
            self.assertTrue(path.exists())
        self.assertIn("Cleaned 1 archived transcript(s)", out)
        self.assertIn("file-history removed: 1, from tombstones: 0", out)
        self.assertNotIn("Nothing to clean", out)

    def test_summary_omits_second_line_when_nothing_extra_removed(self):
        paths = self.make_session(SESSION_A, history=False)
        self.write_state_db([SESSION_A])
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertFalse(paths[0].exists())
        self.assertIn("Cleaned 1 archived transcript(s)", out)
        self.assertNotIn("file-history removed:", out)

    def test_current_session_is_never_removed(self):
        paths = self.make_session(SESSION_A)
        self.write_state_db([SESSION_A])
        os.environ["CLAUDE_SESSION_ID"] = SESSION_A
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        for path in paths:
            self.assertTrue(path.exists())
        self.assertIn("Nothing to clean", out)
        self.assertFalse(list(self.projects.glob(".cleaned-ids.*")))

    def test_tombstoned_session_that_reappears_is_removed(self):
        paths = self.make_session(SESSION_A)
        self.write_state_db([])
        self.write_tombstone(".cleaned-ids.peer", [SESSION_A])
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        for path in paths:
            self.assertFalse(path.exists())
        self.assertIn("Cleaned 1 archived transcript(s)", out)
        self.assertIn("from tombstones: 1", out)

    def test_tombstone_written_for_this_host_sorted_deduped_and_valid(self):
        existing = SESSION_C
        with mock.patch.object(clean_sessions.socket, "gethostname", return_value="test host/1"):
            for sid in (SESSION_B, SESSION_A):
                self.make_session(sid)
            self.write_state_db([SESSION_B, SESSION_A])
            self.write_tombstone(".cleaned-ids.test_host_1",
                                 ["junk", existing, SESSION_A, SESSION_A, "not-a-uuid"])
            code, out, err = self.run_main()
            self.assertEqual(0, code)
            path = self.projects / ".cleaned-ids.test_host_1"
            expected = sorted({existing, SESSION_A, SESSION_B})
            self.assertEqual(expected, path.read_text().splitlines())
            for line in path.read_text().splitlines():
                self.assertTrue(clean_sessions.UUID_RE.fullmatch(line))
            self.assertFalse((self.projects / ".cleaned-ids.other").exists())
            before = path.stat().st_mtime_ns
            code, out, err = self.run_main()
            self.assertEqual(0, code)
            self.assertEqual(expected, path.read_text().splitlines())
            self.assertEqual(before, path.stat().st_mtime_ns)

    def test_tombstones_ignore_non_uuid_and_sync_conflict(self):
        removed = self.make_session(SESSION_A)
        kept = self.make_session(SESSION_B)
        self.write_state_db([])
        self.write_tombstone(".cleaned-ids.peer", ["JUNK", SESSION_A, "", "  "])
        self.write_tombstone(".cleaned-ids.sync-conflict-20260101-1", [SESSION_B])
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertFalse(removed[0].exists())
        self.assertTrue(kept[0].exists())
        self.assertTrue(kept[1].exists())
        self.assertTrue(kept[2].exists())

    def test_missing_state_db_warns_and_still_applies_tombstones(self):
        paths = self.make_session(SESSION_A)
        self.write_tombstone(".cleaned-ids.peer", [SESSION_A])
        self.assertFalse(self.db.exists())
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertEqual(1, len(err.splitlines()))
        self.assertIn("VSCode extension state not found", err)
        self.assertFalse(paths[0].exists())
        self.assertIn("from tombstones: 1", out)
        self.assertFalse(self.db.exists())

    def test_unreadable_state_db_warns_and_still_applies_tombstones(self):
        paths = self.make_session(SESSION_A)
        self.write_tombstone(".cleaned-ids.peer", [SESSION_A])
        self.db.write_text("not sqlite")
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertEqual(1, len(err.splitlines()))
        self.assertIn("cannot read the VSCode state", err)
        self.assertFalse(paths[0].exists())
        self.assertIn("from tombstones: 1", out)

    def test_state_db_is_opened_read_only(self):
        paths = self.make_session(SESSION_A)
        self.write_state_db([SESSION_A])
        before = self.db.read_bytes()
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertEqual(before, self.db.read_bytes())
        self.assertFalse(paths[0].exists())
        self.assertIn("Cleaned 1 archived transcript(s)", out)

    def test_tombstone_temp_and_tmp_files_are_ignored(self):
        removed = self.make_session(SESSION_A)
        kept = self.make_session(SESSION_B)
        self.write_state_db([])
        self.write_tombstone(".cleaned-ids.peer", [SESSION_A])
        self.write_tombstone(".cleaned-ids.leftover.tmp", [SESSION_B])
        self.write_tombstone(".cleaned-ids-tmp.leftover", [SESSION_B])
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertFalse(removed[0].exists())
        for path in kept:
            self.assertTrue(path.exists())

    def test_tombstone_write_failure_warns_and_exits_zero(self):
        paths = self.make_session(SESSION_A)
        self.write_state_db([SESSION_A])
        with mock.patch.object(clean_sessions.os, "replace", side_effect=OSError("disk full")):
            code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertFalse(paths[0].exists())
        self.assertIn("tombstone not written: disk full", err)
        self.assertIn("Cleaned 1 archived transcript(s)", out)
        self.assertFalse(list(self.projects.glob(".cleaned-ids-tmp.*")))

    def test_orphan_file_history_is_removed_without_transcript(self):
        history = self.file_history / SESSION_A
        history.mkdir()
        (history / "state.json").write_text("{}")
        self.write_state_db([SESSION_A])
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertFalse(history.exists())
        self.assertIn("Nothing to clean", out)
        self.assertIn("file-history removed: 1, from tombstones: 0", out)
        tombstone = self.projects / ".cleaned-ids.test-host"
        self.assertEqual([SESSION_A], tombstone.read_text().splitlines())

    def test_non_uuid_hidden_ids_never_touch_file_history(self):
        plain = self.file_history / "not-a-uuid"
        plain.mkdir()
        (plain / "state.json").write_text("{}")
        escape = self.root / "escape"
        escape.mkdir()
        (escape / "state.json").write_text("{}")
        self.write_state_db(["not-a-uuid", "../escape"])
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertTrue(plain.exists())
        self.assertTrue(escape.exists())
        self.assertFalse((self.projects / ".cleaned-ids.test-host").exists())

    def test_symlinked_file_history_is_not_followed_or_counted(self):
        paths = self.make_session(SESSION_A, history=False)
        target = self.root / "outside-history"
        target.mkdir()
        (target / "state.json").write_text("{}")
        link = self.file_history / SESSION_A
        link.symlink_to(target, target_is_directory=True)
        self.write_state_db([SESSION_A])
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue((target / "state.json").exists())
        self.assertIn("Cleaned 1 archived transcript(s)", out)
        self.assertNotIn("file-history removed:", out)

    def test_symlinked_project_dir_is_not_scanned(self):
        outside = self.root / "outside-project"
        outside.mkdir()
        transcript = outside / (SESSION_A + ".jsonl")
        transcript.write_text("{}\n")
        companion = outside / SESSION_A
        companion.mkdir()
        (self.projects / "linked").symlink_to(outside, target_is_directory=True)
        self.write_state_db([SESSION_A])
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertTrue(transcript.exists())
        self.assertTrue(companion.exists())
        self.assertIn("Nothing to clean", out)

    def test_current_session_is_kept_when_only_in_tombstone(self):
        paths = self.make_session(SESSION_A)
        self.write_state_db([])
        self.write_tombstone(".cleaned-ids.peer", [SESSION_A])
        os.environ["CLAUDE_SESSION_ID"] = SESSION_A
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        for path in paths:
            self.assertTrue(path.exists())
        self.assertIn("Nothing to clean", out)
        self.assertFalse((self.projects / ".cleaned-ids.test-host").exists())

    def test_same_id_in_two_project_dirs_is_deleted_everywhere(self):
        first = self.make_session(SESSION_A, project="project-a")
        second = self.make_session(SESSION_A, project="project-b")
        self.write_state_db([SESSION_A])
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        for path in first + second:
            self.assertFalse(path.exists())
        self.assertIn("Cleaned 2 archived transcript(s)", out)
        self.assertIn("file-history removed: 1, from tombstones: 0", out)

    def test_symlinked_orphan_file_history_does_not_write_tombstone(self):
        target = self.root / "outside-orphan-history"
        target.mkdir()
        (target / "state.json").write_text("{}")
        link = self.file_history / SESSION_A
        link.symlink_to(target, target_is_directory=True)
        self.write_state_db([SESSION_A])
        self.write_tombstone(".cleaned-ids.test-host", [SESSION_B])
        path = self.projects / ".cleaned-ids.test-host"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        for _ in range(2):
            code, out, err = self.run_main()
            self.assertEqual(0, code)
            self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
            self.assertTrue(link.is_symlink())
            self.assertTrue((target / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
