import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "task.py"
FAKE = Path(__file__).resolve().parent / "fake"
WATCHDOG_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "watchdog"


def load_module():
    spec = importlib.util.spec_from_file_location("task_mod", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParseContractTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def test_full_block(self):
        text = "chatter\nSTATE: NEED-INPUT\nANSWER: need a key\nmore answer\nEVIDENCE: a.py:3\nMISSING: key, token\n"
        got = self.mod.parse_contract(text)
        self.assertEqual(got["state"], "NEED-INPUT")
        self.assertEqual(got["answer"], "need a key\nmore answer")
        self.assertEqual(got["evidence"], "a.py:3")
        self.assertEqual(got["missing"], "key, token")

    def test_last_block_wins(self):
        text = "STATE: FAIL\nANSWER: first\n\nlater\nSTATE: DONE\nANSWER: second\n"
        got = self.mod.parse_contract(text)
        self.assertEqual(got["state"], "DONE")
        self.assertEqual(got["answer"], "second")

    def test_echoed_template_is_not_a_block(self):
        text = "STATE: DONE | NEED-INPUT | FAIL\nANSWER: <direct answer>\n"
        self.assertEqual(self.mod.parse_contract(text),
                         {"state": "FAIL", "answer": "no result contract"})

    def test_none(self):
        self.assertEqual(self.mod.parse_contract("nothing here"),
                         {"state": "FAIL", "answer": "no result contract"})

    def test_footer_text(self):
        footer = self.mod.CONTRACT_FOOTER
        self.assertTrue(footer.startswith("---\nWork order: preflight"))
        self.assertIn("STATE: DONE | NEED-INPUT | FAIL", footer)
        self.assertIn("MISSING: <all missing items, NEED-INPUT only>", footer)

    def test_task_skill_requires_watched_log_and_progress_without_old_tee_rule(self):
        skill = (SCRIPTS.parent / "skills" / "task" / "SKILL.md").read_text()
        self.assertIn("write output to a log passed with repeatable `--watch` and print progress",
                      skill)
        self.assertNotIn("or tee output to the log", skill)

    def test_command_tracker_matches_sanitized_incident_fixtures(self):
        for worker, active_name, done_name in (
                ("gemini", "agy-active.jsonl", "agy-done.jsonl"),
                ("codex", "codex-started.jsonl", "codex-completed.jsonl"),
                ("opencode", "opencode-running.jsonl", "opencode-completed.jsonl")):
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "raw.log"
                error = Path(directory) / "raw.err"
                error.touch()
                tracker = self.mod.CommandTracker(worker, str(output), str(error))
                output.write_bytes((WATCHDOG_FIXTURES / active_name).read_bytes())
                tracker.poll()
                self.assertTrue(tracker.in_progress, worker)
                with output.open("ab") as stream:
                    stream.write((WATCHDOG_FIXTURES / done_name).read_bytes())
                tracker.poll()
                self.assertFalse(tracker.in_progress, worker)

    def test_agy_background_wait_stderr_fixture_is_in_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "raw.log"
            error = Path(directory) / "raw.err"
            output.touch()
            tracker = self.mod.CommandTracker("gemini", str(output), str(error))
            error.write_bytes((WATCHDOG_FIXTURES / "agy-background-wait.err").read_bytes())
            tracker.poll()
            self.assertTrue(tracker.in_progress)

    def test_agy_step_completion_clears_background_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "raw.log"
            error = Path(directory) / "raw.err"
            output.touch()
            error.write_bytes((WATCHDOG_FIXTURES / "agy-background-wait.err").read_bytes())
            tracker = self.mod.CommandTracker("gemini", str(output), str(error))
            tracker.poll()
            self.assertTrue(tracker.in_progress)
            with output.open("ab") as stream:
                stream.write((WATCHDOG_FIXTURES / "agy-done.jsonl").read_bytes())
            tracker.poll()
            self.assertFalse(tracker.in_progress)

    def test_command_tracker_caps_poll_bytes_and_oversized_partial_line(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "raw.log"
            error = Path(directory) / "raw.err"
            output.write_bytes(b"x" * (self.mod.CommandTracker.MAX_PENDING_BYTES + 1024))
            error.touch()
            tracker = self.mod.CommandTracker("gemini", str(output), str(error))
            before = tracker.out_offset
            tracker.poll()
            self.assertLessEqual(tracker.out_offset - before,
                                 self.mod.CommandTracker.MAX_BYTES_PER_POLL // 2)
            while tracker.out_offset < output.stat().st_size:
                before = tracker.out_offset
                tracker.poll()
                self.assertLessEqual(tracker.out_offset - before,
                                     self.mod.CommandTracker.MAX_BYTES_PER_POLL // 2)
            self.assertEqual(tracker.out_pending, self.mod.CommandTracker.PENDING_DROPPED)
            self.assertLessEqual(len(tracker.out_pending.encode()),
                                 len(self.mod.CommandTracker.PENDING_DROPPED.encode()))


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / "state"
        self.logs = root / "logs"
        self.fake_dir = root / "fakeout"
        self.fake_dir.mkdir()
        self.work = root / "work"
        self.work.mkdir()
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(FAKE) + os.pathsep + os.environ["PATH"],
            "TASK_STATE_DIR": str(self.state),
            "TASK_LOG_DIR": str(self.logs),
            "FAKE_DIR": str(self.fake_dir),
            "HOME": str(root / "home"),
        })
        self.env.pop("FAKE_MODE", None)

    def brief(self, text="do the thing\n"):
        path = Path(self.tmp.name) / "brief.md"
        path.write_text(text)
        return str(path)

    def task(self, *args, **extra_env):
        env = dict(self.env)
        env.update(extra_env)
        return subprocess.run([sys.executable, str(SCRIPT)] + list(args), env=env,
                              capture_output=True, text=True, timeout=60)

    def start(self, worker, tid, *args, **extra_env):
        return self.task("start", worker, tid, self.brief(), "--cwd", str(self.work), *args, **extra_env)

    def start_process(self, worker, tid, *args, **extra_env):
        env = dict(self.env)
        env.update(extra_env)
        return subprocess.Popen([sys.executable, str(SCRIPT), "start", worker, tid, self.brief(),
                                 "--cwd", str(self.work)] + list(args), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def reg(self, tid):
        return json.loads((self.state / (tid + ".json")).read_text())

    def write_reg(self, tid, **fields):
        self.state.mkdir(parents=True, exist_ok=True)
        base = {"id": tid, "worker": "codex", "model": "m", "cwd": str(self.work), "worktree": None,
                "session": "SID-9", "state": "DONE", "replies": 0, "raw_log": "", "report": {},
                "started": "t", "updated": "t"}
        base.update(fields)
        (self.state / (tid + ".json")).write_text(json.dumps(base))

    def argv(self, n=1):
        data = (self.fake_dir / ("argv.%d" % n)).read_bytes().decode()
        return data.split("\0")[:-1]

    def switches(self, *names):
        for name in names:
            result = self.task("off", name)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TestCommandTests(RunnerCase):
    def mocked_test(self, names, start_codes, reply_codes, reports, start_side_effect=None,
                    resume=False):
        task = load_module()
        start_calls = []
        reply_calls = []

        def start(args):
            start_calls.append(args)
            if start_side_effect:
                return start_side_effect(args, len(start_calls))
            return start_codes.pop(0)

        def reply(args):
            reply_calls.append(args)
            return reply_codes.pop(0)

        output = io.StringIO()
        with mock.patch.object(task, "worker_switches", return_value={}), \
                mock.patch.object(task, "cmd_start", side_effect=start), \
                mock.patch.object(task, "cmd_reply", side_effect=reply), \
                mock.patch.object(task, "load_reg", side_effect=reports):
            with contextlib.redirect_stdout(output):
                code = task.cmd_test(type("Args", (), {"workers": names, "verbose": False,
                                                        "resume": resume})())
        return task, code, output.getvalue(), start_calls, reply_calls

    @staticmethod
    def report(alias, state="DONE", answer="pelican"):
        worker, model = load_module().MODELS[alias]
        return {"id": "test", "worker": worker, "model": model, "alias": alias, "state": state,
                "report": {"state": state, "answer": answer, "evidence": "fake:1", "missing": ""}}

    def test_no_worker_is_usage_error_with_valid_names(self):
        result = self.task("test")
        self.assertEqual(result.returncode, 2)
        self.assertIn("luna", result.stderr)
        self.assertIn("relay-opus", result.stderr)

    def test_unknown_worker_is_usage_error(self):
        result = self.task("test", "unknown")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown worker", result.stderr)

    def test_off_worker_runs_and_uses_temporary_state_and_cwd(self):
        self.switches("luna")
        result = self.task("test", "luna", FAKE_ANSWER="pelican")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("WARNING", result.stdout)
        self.assertNotIn("cost", result.stdout.lower())
        self.assertIn("(switch: off)", result.stdout)
        self.assertIn("SUMMARY ok=1 fail=0 skip=0", result.stdout)
        self.assertEqual(len(list(self.fake_dir.glob("argv.*"))), 1)
        worker_cwd = Path((self.fake_dir / "cwd.1").read_text().strip())
        self.assertNotEqual(worker_cwd, self.work)
        self.assertEqual(worker_cwd.name, "work")
        self.assertTrue(worker_cwd.parent.name.startswith("task-test-"))
        self.assertFalse((self.state / "test-luna.json").exists())
        self.assertEqual(list(self.state.glob("*.json")), [])
        self.assertEqual(list(self.logs.glob("*")), [])

    def test_quota_exit_is_skip_and_does_not_fail_command(self):
        result = self.task("test", "luna", FAKE_MODE="quota")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SKIP", result.stdout)
        self.assertIn("SUMMARY ok=0 fail=0 skip=1", result.stdout)

    def test_start_only_is_one_turn_and_does_not_require_pelican(self):
        result = self.task("test", "luna", FAKE_ANSWER="ok")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("WARNING", result.stdout)
        self.assertNotIn("reply ", result.stdout)
        self.assertEqual(len(list(self.fake_dir.glob("argv.*"))), 1)

    def test_resume_adds_second_turn_and_requires_pelican(self):
        result = self.task("test", "luna", "--resume", FAKE_ANSWER="not remembered")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("reply", result.stdout)
        self.assertEqual(len(list(self.fake_dir.glob("argv.*"))), 2)
        self.assertTrue(self.argv(2)[-1].endswith(load_module().CONTRACT_FOOTER))

    def test_multiline_reply_answer_contains_pelican(self):
        result = self.task("test", "luna", "--resume", FAKE_ANSWER="The word was:\npelican")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SUMMARY ok=1 fail=0 skip=0", result.stdout)

    def test_second_invocation_resumes_the_start_session(self):
        result = self.task("test", "luna", "--resume", FAKE_ANSWER="pelican")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reply_argv = self.argv(2)
        self.assertIn("resume", reply_argv)
        self.assertIn("SESSION-codex-1", reply_argv)

    def test_verbose_prints_full_report_beyond_ten_lines(self):
        answer = "\n".join(["report line %d" % index for index in range(14)] + ["pelican"])
        result = self.task("test", "luna", "--resume", "--verbose", FAKE_ANSWER=answer)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("report line 13", result.stdout)

    def test_low_effort_is_used_for_luna_sol_and_flash(self):
        for worker, expected in (("luna", ["codex", "exec"]), ("sol", ["codex", "exec"]),
                                 ("flash", ["gemini"])):
            with self.subTest(worker=worker):
                for recorded in self.fake_dir.glob("argv.*"):
                    recorded.unlink()
                result = self.task("test", worker, FAKE_ANSWER="ok")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                argv = self.argv(1)
                if worker in ("luna", "sol"):
                    self.assertIn('model_reasoning_effort="low"', " ".join(argv))
                else:
                    self.assertIn("gemini-3.8-flash-low", argv)

    def test_owner_only_workers_are_pinned_and_relay_alias_resolves(self):
        reports = [self.report("relay-opus"), self.report("relay-opus"),
                   self.report("perch-deepseek"), self.report("perch-deepseek")]
        _, code, _, starts, replies = self.mocked_test(
            ["relay-opus", "perch-deepseek"], [0, 0], [0, 0], reports, resume=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(replies), 2)
        self.assertTrue(starts[0].pinned)
        self.assertTrue(starts[1].pinned)
        self.assertEqual(starts[0].positionals[0], "relay")
        self.assertEqual(starts[0].model, "relay-opus")
        self.assertEqual(starts[1].positionals[0], "perch")
        self.assertEqual(starts[1].model, "deepseek-flash-starter")

        reports = [self.report("relay-sonnet"), self.report("relay-sonnet")]
        _, code, _, starts, _ = self.mocked_test(["relay"], [0], [0], reports, resume=True)
        self.assertEqual(code, 0)
        self.assertTrue(starts[0].pinned)
        self.assertEqual(starts[0].positionals[0], "relay")
        self.assertEqual(starts[0].model, "relay-sonnet")

    def test_env_is_restored_between_workers_and_after_start_exception(self):
        old_state = os.environ.get("TASK_STATE_DIR")
        old_logs = os.environ.get("TASK_LOG_DIR")
        seen = []

        def start_side_effect(args, index):
            seen.append((os.environ["TASK_STATE_DIR"], os.environ["TASK_LOG_DIR"]))
            if index == 1:
                raise RuntimeError("simulated start error")
            return 1

        reports = [self.report("flash", "FAIL", "simulated failure")]
        _, code, _, _, _ = self.mocked_test(["opencode", "flash"], [1], [], reports,
                                             start_side_effect=start_side_effect)
        self.assertEqual(code, 1)
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])
        self.assertNotEqual(seen[0][0], old_state)
        self.assertNotEqual(seen[0][1], old_logs)
        self.assertEqual(os.environ.get("TASK_STATE_DIR"), old_state)
        self.assertEqual(os.environ.get("TASK_LOG_DIR"), old_logs)

    def test_mixed_results_exit_code_and_failed_start_suppresses_reply(self):
        reports = [self.report("luna"), self.report("luna"),
                   self.report("flash", "FAIL", "start failed"),
                   self.report("relay-opus", "QUOTA", "rate limited")]
        _, code, output, _, replies = self.mocked_test(
            ["luna", "flash", "relay-opus"], [0, 1, 4], [0], reports, resume=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(replies), 1)
        self.assertIn("SUMMARY ok=1 fail=1 skip=1", output)

    def test_multiline_fail_detail_stays_on_one_result_line(self):
        reports = [self.report("deepseek", "FAIL", "start failed\nadditional detail")]
        _, code, output, _, _ = self.mocked_test(["opencode"], [1], [], reports)
        lines = output.splitlines()
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 2)
        self.assertIn("start failed", lines[0])
        self.assertNotIn("additional detail", output)

    def test_multiline_skip_detail_stays_on_one_result_line(self):
        reports = [self.report("deepseek", "QUOTA", "quota reached\nadditional detail")]
        _, code, output, _, _ = self.mocked_test(["opencode"], [4], [], reports)
        lines = output.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 2)
        self.assertIn("quota reached", lines[0])
        self.assertNotIn("additional detail", output)

    def test_reply_quota_is_skip(self):
        reports = [self.report("luna"), self.report("luna", "QUOTA", "rate limited")]
        _, code, output, _, replies = self.mocked_test(["luna"], [0], [4], reports, resume=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(replies), 1)
        self.assertIn("SUMMARY ok=0 fail=0 skip=1", output)

    def test_worker_timeouts_are_passed_to_both_steps(self):
        reports = [self.report("sol"), self.report("sol"),
                   self.report("perch-deepseek"), self.report("perch-deepseek")]
        _, code, _, starts, replies = self.mocked_test(
            ["sol", "perch-deepseek"], [0, 0], [0, 0], reports, resume=True)
        self.assertEqual(code, 0)
        self.assertEqual([call.timeout for call in starts], [300, 900])
        self.assertEqual([call.timeout for call in replies], [300, 900])

    def test_unlimited_worker_has_no_cost_warning(self):
        reports = [self.report("deepseek")]
        _, code, output, _, _ = self.mocked_test(["opencode"], [0], [], reports)
        self.assertEqual(code, 0)
        self.assertNotIn("WARNING:", output)

    def test_no_worker_warning_for_limited_workers(self):
        result = self.task("test", "luna", FAKE_ANSWER="ok")
        self.assertNotIn("WARNING", result.stdout)
        self.assertNotIn("cost", result.stdout.lower())

    def test_mocked_default_is_one_turn_and_low_effort(self):
        reports = [self.report("luna")]
        _, code, output, starts, replies = self.mocked_test(["luna"], [0], [], reports)
        self.assertEqual(code, 0)
        self.assertEqual(len(starts), 1)
        self.assertEqual(replies, [])
        self.assertEqual(starts[0].effort, "low")
        self.assertNotIn("reply ", output)

    def test_resume_uses_pelican_brief_and_reply_duration(self):
        reports = [self.report("luna"), self.report("luna")]
        _, code, output, starts, replies = self.mocked_test(
            ["luna"], [0], [0], reports, resume=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(replies), 1)
        self.assertEqual(starts[0].effort, "low")
        self.assertIn("reply", output)

    def test_start_brief_has_one_line_contract_and_resume_word(self):
        for resume, expected in ((False, "Reply with STATE: DONE and ANSWER: ok.\n"),
                                 (True, "Remember the word pelican.\n")):
            reports = [self.report("luna")] + ([self.report("luna")] if resume else [])
            seen = []

            def capture_start(args, index):
                seen.append(Path(args.positionals[2]).read_text())
                return 0

            _, code, _, _, _ = self.mocked_test(
                ["luna"], [0], [0] if resume else [], reports,
                start_side_effect=capture_start, resume=resume)
            self.assertEqual(code, 0)
            self.assertEqual(seen[0], expected)

    def test_help_lists_test_command(self):
        result = self.task("test", "--help")
        self.assertIn("test", result.stdout)
        self.assertIn("--resume", result.stdout)


class ResolveTests(RunnerCase):
    def test_resolve_rows_uses_default_cli_and_short_model(self):
        for row, expected in (("search", "gemini - flash"),
                              ("inventory", "gemini - flash"),
                              ("mechanical", "gemini - hydrogen"),
                              ("code", "gemini - hydrogen"),
                              ("review", "gemini - carbon"),
                              ("hard", "gemini - carbon")):
            with self.subTest(row=row):
                result = self.task("resolve", row)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout, expected + "\n")
                self.assertEqual(result.stderr, "")

    def test_resolve_row_matches_e5fca05_off_perch(self):
        mod = load_module()
        e5_aliases = ["luna", "sol", "deepseek-free", "deepseek", "flash", "sonnet"]
        names = list(mod.MODELS) + list(mod.DEFAULT_MODELS)

        def reference(row, preferred, state):
            alias = preferred or mod.ROWS[row][0]
            if not mod.model_on(alias, state):
                alternatives = [name for name in mod.ROWS[row] if mod.model_on(name, state)]
                if not alternatives:
                    raise mod.UsageError("all workers off")
                alias = alternatives[0]
            worker, model = mod.MODELS[alias]
            if alias == "flash":
                model = "gemini-3.8-flash-medium" if row == "search" else "gemini-3.8-flash-high"
            return alias, worker, model

        for bits in range(1 << len(names)):
            state = {name: bool(bits & (1 << index)) for index, name in enumerate(names)}
            for row in mod.ROWS:
                for preferred in [None] + e5_aliases:
                    try:
                        expected = reference(row, preferred, state)
                    except mod.UsageError:
                        expected = None
                    try:
                        got = mod.resolve_row(row, preferred, state=state)
                    except mod.UsageError:
                        got = None
                    self.assertEqual(got, expected, (row, preferred, state))

    def test_deepseek_free_off_on_search_row_matches_e5fca05(self):
        self.switches("deepseek-free")
        res = self.task("start", "opencode", "free-off-search", self.brief(), "--row", "search",
                        "--model", "opencode/deepseek-v4.1-flash-free", "--cwd", str(self.work))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("FALLBACK: deepseek-free off -> flash", res.stdout)
        self.assertEqual(self.reg("free-off-search")["alias"], "flash")
        self.assertEqual(self.reg("free-off-search")["model"], "gemini-3.8-flash-medium")

    def test_resolve_code_falls_back_gemini_then_codex_then_opencode(self):
        self.switches("gemini")
        self.assertEqual(self.task("resolve", "code").stdout, "codex - luna\n")
        self.switches("codex")
        result = self.task("resolve", "code")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "opencode - deepseek\n")

    def test_priority_with_everything_on_is_gemini_codex_opencode(self):
        mod = load_module()
        for row in ("mechanical", "code", "review", "hard"):
            names = mod.ROWS[row]
            clis = [mod.MODELS[name][0] for name in names]
            self.assertEqual("gemini", clis[0], row)
            self.assertNotIn("flash", names, row)
            self.assertLess(clis.index("codex"), clis.index("opencode"), row)
        self.assertEqual(["flash", "luna", "deepseek", "chatanywhere"], mod.ROWS["search"])
        self.assertEqual("claude-sonnet-5-5-medium", mod.MODELS["sonnet"][1])
        self.assertEqual({"hydrogen": "gemini-4-hydrogen-medium", "argon": "gemini-4-argon-low",
                          "carbon": "gemini-4-carbon-low"},
                         {name: mod.MODELS[name][1] for name in ("hydrogen", "argon", "carbon")})

    def test_resolve_all_off_is_one_line_failure(self):
        self.switches("gemini", "codex", "opencode", "chatanywhere")
        result = self.task("resolve", "code")
        self.assertNotEqual(result.returncode, 0)
        output = (result.stdout + result.stderr).strip()
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn("all workers for row code are off", output)

    def test_resolve_does_not_create_state_or_switch_files(self):
        result = self.task("resolve", "code")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.state.parent / "task-workers.json").exists())
        self.assertFalse((self.state.parent / "task-workers.json.lock").exists())


class WorkerSwitchTests(RunnerCase):
    def test_switches_persist_and_cli_off_turns_all_its_models_off(self):
        self.switches("codex", "flash")
        result = self.task("workers")
        self.assertIn("luna: off", result.stdout)
        self.assertIn("sol: off", result.stdout)
        self.assertIn("codex: off", result.stdout)
        self.assertIn("flash: off", result.stdout)
        self.assertEqual(self.task("on", "codex").returncode, 0)
        self.assertIn("luna: on", self.task("workers").stdout)

    def test_two_concurrent_switch_updates_both_persist(self):
        commands = []
        for name in ("sol", "flash"):
            commands.append(subprocess.Popen([sys.executable, str(SCRIPT), "off", name], env=self.env,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for command in commands:
            stdout, stderr = command.communicate(timeout=30)
            self.assertEqual(command.returncode, 0, stdout + stderr)
        result = self.task("workers")
        self.assertIn("sol: off", result.stdout)
        self.assertIn("flash: off", result.stdout)

    def test_corrupt_switch_file_is_an_error_but_missing_file_defaults_on(self):
        self.assertIn("flash: on", self.task("workers").stdout)
        switch_file = self.state.parent / "task-workers.json"
        switch_file.write_text("{")
        result = self.task("workers")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read worker switches", result.stderr)

    def test_legacy_switch_file_is_migrated_from_custom_registry(self):
        legacy_file = self.state / "task-workers.json"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.write_text('{"flash": false, "gemini": false}')
        result = self.task("workers")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("flash: off", result.stdout)
        self.assertIn("gemini: off", result.stdout)
        self.assertFalse(legacy_file.exists())
        self.assertTrue((self.state.parent / "task-workers.json").is_file())

    def test_new_switch_file_takes_precedence_over_legacy_file(self):
        legacy_file = self.state / "task-workers.json"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.write_text('{"flash": false}')
        (self.state.parent / "task-workers.json").write_text('{"flash": true}')
        result = self.task("workers")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("flash: on", result.stdout)
        self.assertTrue(legacy_file.exists())

    def test_switch_file_is_outside_registry_and_list_skips_it(self):
        self.assertEqual(self.task("off", "flash").returncode, 0)
        switch_file = self.state.parent / "task-workers.json"
        self.assertTrue(switch_file.is_file())
        self.assertFalse((self.state / "task-workers.json").exists())
        self.write_reg("listed")
        result = self.task("list")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("codex - luna - listed | DONE", result.stdout)
        self.assertNotIn("task-workers", result.stdout)

    def test_list_skips_valid_non_task_json(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "unrelated.json").write_text('["not", "a task"]')
        (self.state / "partial.json").write_text('{"anything": true}')
        (self.state / "malformed-task.json").write_text(
            '{"id": "malformed-task", "state": "DONE", "worker": []}')
        (self.state / "running-without-id.json").write_text(
            '{"state": "RUNNING", "worker": "codex", "model": "gpt-6-luna"}')
        result = self.task("list")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("unrelated", result.stdout)
        self.assertNotIn("partial", result.stdout)
        self.assertNotIn("malformed-task", result.stdout)
        self.assertNotIn("running-without-id", result.stdout)

    def test_fallback_rows_and_skip_two_off_entries(self):
        cases = [("search", "flash", "luna", "gpt-6-luna"),
                 ("code", "hydrogen", "luna", "gpt-6-luna"),
                 ("review", "carbon", "sol", "gpt-6.1-sol"),
                 ("hard", "carbon", "sol", "gpt-6.1-sol"),
                 ("mechanical", "hydrogen", "luna", "gpt-6-luna")]
        for index, (row, disabled, chosen, model) in enumerate(cases):
            with self.subTest(row=row):
                for name in ("sol", "luna", "deepseek-free", "deepseek", "flash", "hydrogen", "argon", "carbon",
                             "sonnet", "codex", "opencode", "gemini"):
                    self.task("on", name)
                self.switches(disabled)
                res = self.task("start", "row%d" % index, self.brief(), "--row", row,
                                "--cwd", str(self.work))
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertIn("FALLBACK: %s off -> %s" % (disabled, chosen), res.stdout)
                self.assertEqual(self.reg("row%d" % index)["model"], model)
                self.assertEqual(self.reg("row%d" % index)["fallback"], {"from": disabled, "to": chosen})

    def test_mechanical_fallback_skips_two_off_entries(self):
        self.switches("hydrogen", "luna")
        res = self.task("start", "skip", self.brief(), "--row", "mechanical", "--cwd", str(self.work))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("FALLBACK: hydrogen off -> deepseek", res.stdout)
        self.assertEqual(self.reg("skip")["alias"], "deepseek")

    def test_search_row_uses_flash_low(self):
        res = self.task("start", "search-low", self.brief(), "--row", "search", "--cwd", str(self.work))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.reg("search-low")["model"], "gemini-3.8-flash-medium")

    def test_inventory_row_uses_flash_high_then_luna(self):
        res = self.task("start", "inv", self.brief(), "--row", "inventory", "--cwd", str(self.work))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.reg("inv")["model"], "gemini-3.8-flash-high")
        self.switches("flash")
        res = self.task("start", "inv-off", self.brief(), "--row", "inventory", "--cwd", str(self.work))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.reg("inv-off")["model"], "gpt-6-luna")

    def test_explicit_gemini_cli_search_uses_low_and_code_fallback_uses_sonnet(self):
        search = self.task("start", "gemini", "gemini-search", self.brief(), "--row", "search",
                           "--cwd", str(self.work))
        self.assertEqual(search.returncode, 0, search.stdout + search.stderr)
        self.assertEqual(self.reg("gemini-search")["model"], "gemini-3.8-flash-medium")
        self.switches("luna", "hydrogen")
        code = self.task("start", "codex", "code-flash", self.brief(), "--row", "code", "--model",
                         "gpt-6-luna", "--cwd", str(self.work))
        self.assertEqual(code.returncode, 0, code.stdout + code.stderr)
        self.assertEqual(self.reg("code-flash")["model"], "opencode-go/deepseek-v4.1-flash")
        self.assertEqual(self.reg("code-flash")["alias"], "deepseek")

    def test_search_fallback_to_flash_uses_low(self):
        self.switches("luna")
        result = self.task("start", "codex", "search-fallback", self.brief(), "--row", "search",
                           "--model", "gpt-6-luna", "--cwd", str(self.work))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("FALLBACK: luna off -> flash", result.stdout)
        self.assertEqual(self.reg("search-fallback")["model"], "gemini-3.8-flash-medium")

    def test_mechanical_row_never_falls_back_to_flash(self):
        self.switches("hydrogen", "luna", "deepseek")
        self.assertEqual(self.task("resolve", "mechanical").stdout, "codex - chatanywhere\n")
        self.switches("chatanywhere")
        result = self.task("resolve", "mechanical")
        self.assertEqual(result.returncode, 2)
        self.assertIn("all workers for row mechanical are off", result.stdout + result.stderr)

    def test_pinned_flash_low_stays_exact_on_mechanical_row(self):
        result = self.task("start", "gemini", "pinned-low", self.brief(), "--row", "search",
                           "--model", "gemini-3.8-flash-low", "--pinned", "--cwd", str(self.work))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.reg("pinned-low")["model"], "gemini-3.8-flash-low")
        self.assertIn("gemini-3.8-flash-low", self.argv())

    def test_owner_pinned_sonnet_starts_on_code_row(self):
        result = self.start("gemini", "pinned-sonnet", "--model", "sonnet", "--pinned")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.reg("pinned-sonnet")["alias"], "sonnet")
        self.assertEqual(self.reg("pinned-sonnet")["model"], "claude-sonnet-5-5-medium")
        self.assertNotIn("sonnet", load_module().ROWS["code"])

    def test_model_only_start_accepts_both_flash_variants_without_rewriting(self):
        for index, model in enumerate(("gemini-3.8-flash-low", "gemini-3.8-flash-high")):
            task_id = "model-only-%d" % index
            result = self.task("start", task_id, self.brief(), "--model", model, "--cwd", str(self.work))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.reg(task_id)["model"], model)

    def test_explicit_flash_high_is_not_rewritten_by_search_row(self):
        result = self.task("start", "gemini", "explicit-high", self.brief(), "--row", "search",
                           "--model", "gemini-3.8-flash-high", "--cwd", str(self.work))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.reg("explicit-high")["model"], "gemini-3.8-flash-high")

    def test_all_off_and_pinned_off_block(self):
        self.switches("flash", "luna", "sol", "deepseek-free", "deepseek", "chatanywhere")
        all_off = self.task("start", "blocked", self.brief(), "--row", "search", "--cwd", str(self.work))
        self.assertEqual(all_off.returncode, 2)
        self.assertIn("BLOCKED: all workers for row search are off", all_off.stdout)
        pinned = self.task("start", "codex", "pinned", self.brief(), "--row", "hard",
                           "--model", "gpt-6.1-sol", "--pinned", "--cwd", str(self.work))
        self.assertEqual(pinned.returncode, 2)
        self.assertIn("BLOCKED: gpt-6.1-sol is off (explicitly named); ask the user", pinned.stdout)

    def test_whole_cli_off_applies_even_if_model_on(self):
        self.switches("gemini", "codex")
        res = self.task("start", "cli-off", self.brief(), "--row", "code", "--cwd", str(self.work))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("FALLBACK: hydrogen off -> deepseek", res.stdout)

    def test_quota_hint_and_reply_after_worker_off(self):
        started = self.task("start", "codex", "quota", self.brief(), "--row", "code", "--model", "gpt-6-luna",
                            "--cwd", str(self.work), FAKE_MODE="quota")
        self.assertEqual(started.returncode, 4)
        self.assertIn("HINT: task off luna", started.stdout)
        self.assertIn("codex: on", self.task("workers").stdout)
        self.assertEqual(self.task("start", "codex", "reply-off", self.brief(),
                                   "--cwd", str(self.work)).returncode, 0)
        self.assertEqual(self.task("off", "codex").returncode, 0)
        reply = self.task("reply", "reply-off", "continue", FAKE_ANSWER="reply after off")
        self.assertEqual(reply.returncode, 0, reply.stdout + reply.stderr)
        self.assertIn("ANSWER: reply after off", reply.stdout)
        self.assertTrue(self.argv(2)[-1].endswith(load_module().CONTRACT_FOOTER))

    def test_reply_does_not_double_append_footer(self):
        self.assertEqual(self.start("codex", "reply-footer").returncode, 0)
        footer = load_module().CONTRACT_FOOTER
        reply = self.task("reply", "reply-footer", "continue\n\n" + footer)
        self.assertEqual(reply.returncode, 0, reply.stdout + reply.stderr)
        self.assertEqual(self.argv(2)[-1].count(footer), 1)

    def test_legacy_quota_registry_derives_switch_hint(self):
        self.write_reg("legacy-quota", worker="gemini", model="gemini-3.8-flash-high", state="QUOTA")
        result = self.task("status", "legacy-quota")
        self.assertEqual(result.returncode, 4)
        self.assertIn("HINT: task off flash", result.stdout)


class StartTests(RunnerCase):
    def test_codex_done_session_and_report(self):
        res = self.start("codex", "t1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        reg = self.reg("t1")
        self.assertEqual(reg["state"], "DONE")
        self.assertEqual(reg["session"], "SESSION-codex-1")
        self.assertEqual(reg["worker"], "codex")
        self.assertEqual(reg["model"], "gpt-6-luna")
        self.assertEqual(reg["report"]["state"], "DONE")
        self.assertEqual(reg["report"]["answer"], "all good 1")
        self.assertEqual(reg["replies"], 0)
        self.assertEqual(len(reg["runs"]), 1)
        self.assertIsNotNone(reg["runs"][0]["log_start"])
        self.assertIsNotNone(reg["runs"][0]["log_end"])
        self.assertIsNotNone(reg["runs"][0]["finished"])
        self.assertIsNotNone(reg["runs"][0]["first_output_s"])
        self.assertGreaterEqual(reg["runs"][0]["first_output_s"], 0)
        self.assertTrue(Path(reg["raw_log"]).is_file())
        self.assertIn("codex - luna - t1 | DONE", res.stdout)
        self.assertIn("ANSWER: all good 1", res.stdout)
        argv = self.argv()
        self.assertEqual(argv[:11], ["exec", "--json", "--skip-git-repo-check", "-m", "gpt-6-luna",
                                     "-c", "model_auto_compact_token_limit=250000", "-s",
                                     "danger-full-access", "-C", str(self.work)])
        self.assertEqual((self.fake_dir / "role.1").read_text(), "worker")

    def test_brief_byte_identical(self):
        brief = 'say "hi" $HOME `whoami` $(id)\nline two \\n \'q\'\n\n\n'
        res = self.task("start", "codex", "t2", self.brief(brief), "--cwd", str(self.work))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        prompt = self.argv()[-1]
        self.assertTrue(prompt.startswith(brief))
        mod = load_module()
        self.assertTrue(prompt.endswith(mod.CONTRACT_FOOTER))

    def test_missing_worker_binary_fails_clearly(self):
        res = self.start("codex", "t-missing", PATH=str(Path(self.tmp.name) / "empty-path"))
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        reg = self.reg("t-missing")
        self.assertEqual(reg["state"], "FAIL")
        self.assertEqual(reg["report"]["state"], "FAIL")
        self.assertIn("cannot run worker CLI", reg["report"]["answer"])
        self.assertIn("codex", reg["report"]["answer"])

    def test_no_contract_fails(self):
        res = self.start("codex", "t3", FAKE_MODE="nocontract")
        self.assertEqual(res.returncode, 1)
        reg = self.reg("t3")
        self.assertEqual(reg["state"], "FAIL")
        self.assertEqual(reg["report"]["answer"], "no result contract")

    def test_silent_run_records_null_first_output(self):
        self.start("codex", "silent-output", "--stall", "1", FAKE_MODE="silent", TASK_CPU_POLL="0.2")
        self.assertIsNone(self.reg("silent-output")["runs"][0]["first_output_s"])

    def test_no_session_start_ok_reply_refused(self):
        res = self.start("codex", "t4", FAKE_MODE="nosession")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIsNone(self.reg("t4")["session"])
        rep = self.task("reply", "t4", "more")
        self.assertNotEqual(rep.returncode, 0)
        self.assertIn("worker gave no session id; start a new task", rep.stdout + rep.stderr)

    def test_stall_kills_process_group(self):
        t0 = time.time()
        res = self.start("codex", "t5", "--stall", "1", FAKE_MODE="silent", TASK_CPU_POLL="0.2")
        self.assertLess(time.time() - t0, 4.2)
        self.assertEqual(res.returncode, 1)
        reg = self.reg("t5")
        self.assertEqual(reg["state"], "FAIL")
        self.assertIn("killed: stall", reg["report"]["answer"])
        self.assertEqual(reg["kill_reason"], "stall")
        self.assertIn("STALL-WARN 1s no activity", Path(reg["raw_log"] + ".err").read_text())
        pid = int((self.fake_dir / "child.pid").read_text())
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            self.fail("child survived the group kill")

    def test_silent_cpu_busy_child_is_not_killed(self):
        result = self.start("codex", "busy", "--stall", "1", FAKE_MODE="busy", TASK_CPU_POLL="0.2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.reg("busy")["state"], "DONE")

    def test_detached_descendant_cpu_counts_as_live(self):
        result = self.start("codex", "detached-busy", "--stall", "1", FAKE_MODE="detached_busy",
                            TASK_CPU_POLL="0.2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.reg("detached-busy")["state"], "DONE")

    def test_new_idle_descendant_does_not_count_as_cpu_activity(self):
        task = load_module()
        self.assertFalse(task._cpu_increased({100: 0.0}, {100: 0.0, 101: 0.0}))
        self.assertTrue(task._cpu_increased({100: 0.0, 101: 0.0}, {100: 0.0, 101: 0.01}))

    def test_kill_reaches_detached_idle_descendant(self):
        result = self.start("codex", "detached-idle", "--stall", "1", FAKE_MODE="detached_idle",
                            TASK_CPU_POLL="0.2")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        pid = int((self.fake_dir / "child.pid").read_text())
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
            if stat.returncode != 0 or stat.stdout.strip().startswith("Z"):
                return
            time.sleep(0.05)
        detail = subprocess.run(["ps", "-o", "pid=,ppid=,pgid=,stat=,command=", "-p", str(pid)],
                                capture_output=True, text=True).stdout.strip()
        self.fail("detached descendant survived worker-tree kill: " + detail + "; " + result.stdout)

    def test_silent_file_writes_are_live(self):
        result = self.start("codex", "writing", "--stall", "1", FAKE_MODE="writing", TASK_CPU_POLL="0.2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.work / "work-output.txt").exists())

    def test_stall_report_contains_partial_and_produced_files(self):
        (self.work / "untouched-fixture.txt").write_text("baseline")
        result = self.start("codex", "salvage", "--stall", "1", FAKE_MODE="silent_salvage",
                            TASK_CPU_POLL="0.2")
        self.assertEqual(result.returncode, 1)
        reg = self.reg("salvage")
        answer = reg["report"]["answer"]
        self.assertIn("killed: stall", answer)
        self.assertIn("TSV exported to result.tsv", reg["report"]["partial"])
        self.assertIn("produced.tsv", reg["report"]["files"])
        self.assertNotIn("untouched-fixture.txt", reg["report"]["files"])
        self.assertTrue(Path(reg["report"]["salvage_file"]).exists())
        status = self.task("status", "salvage")
        self.assertIn("produced.tsv", status.stdout)
        self.assertIn("TSV exported to result.tsv", status.stdout)

    def test_tool_event_json_is_never_used_as_partial_salvage(self):
        result = self.start("codex", "tool-output", "--stall", "1", FAKE_MODE="tool_silent",
                            TASK_CPU_POLL="0.2")
        self.assertEqual(result.returncode, 1)
        reg = self.reg("tool-output")
        self.assertEqual(reg["report"]["partial"], "(none)")
        report_text = Path(reg["report"]["salvage_file"]).read_text()
        self.assertNotIn("PRIVATE_COMMAND_OUTPUT_SENTINEL", report_text)
        self.assertNotIn("PRIVATE_COMMAND_OUTPUT_SENTINEL", reg["report"]["answer"])

    def test_non_git_salvage_truncation_is_reported(self):
        baseline = {}
        for index in range(205):
            (self.work / ("output-%03d.txt" % index)).write_text("x")
        files, truncated = load_module()._salvage_files(str(self.work), baseline)
        self.assertEqual(len(files), 200)
        self.assertTrue(truncated)

    def test_snapshot_cap_counts_directories_and_status_notes_omissions(self):
        nested = self.work / "one" / "two" / "three"
        nested.mkdir(parents=True)
        task = load_module()
        self.assertIsNone(task._worktree_snapshot(str(self.work), max_entries=2))
        reg = {"id": "render", "worker": "codex", "model": "m", "state": "FAIL", "raw_log": "raw.log",
               "report": {"answer": "killed: stall", "partial": "line one", "files": ["path-%02d" % n for n in range(15)],
                          "files_truncated": True, "salvage_file": "task.report"}}
        rendered, _ = task.render(reg)
        self.assertIn("path-03", rendered)
        self.assertIn("additional path(s) omitted", rendered)
        self.assertIn("file list capped at 200", rendered)
        self.assertIn("task.report", rendered)

    def test_slow_snapshot_does_not_delay_hard_timeout(self):
        task = load_module()
        started = time.time()
        env = dict(self.env, FAKE_MODE="silent", TASK_CPU_POLL="100")
        with mock.patch.dict(os.environ, env), mock.patch.object(
                task, "_worktree_snapshot", side_effect=lambda *args, **kwargs: (time.sleep(1.2) or {})):
            kind, _ = task.run_worker([str(FAKE / "codex")], str(self.work), str(self.logs / "slow.log"),
                                      str(self.logs / "slow.err"), 1.1, 100, {})
        self.assertEqual(kind, "timeout")
        self.assertLess(time.time() - started, 1.7)

    def _run_preloaded_worker(self, stdout=b"", stderr=b"", poll_side_effect=None, write_on_poll=False):
        task = load_module()
        output = self.logs / "preloaded.log"
        error = self.logs / "preloaded.err"
        observed = []

        def spawn(argv, **kwargs):
            if not write_on_poll:
                for name, data in (("stdout", stdout), ("stderr", stderr)):
                    stream = kwargs.get(name)
                    if stream is not None and data:
                        stream.write(data)
                        stream.flush()
            proc = mock.Mock(pid=987654, returncode=0)
            if write_on_poll:
                def write_then_exit():
                    kwargs["stdout"].write(stdout)
                    kwargs["stdout"].flush()
                    return 0
                proc.poll.side_effect = write_then_exit
            else:
                proc.poll.side_effect = poll_side_effect or [0]
            return proc

        with mock.patch.dict(os.environ, dict(self.env, TASK_CPU_POLL="100")), \
                mock.patch.object(task.subprocess, "Popen", side_effect=spawn), \
                mock.patch.object(task, "_worker_processes", return_value=set()), \
                mock.patch.object(task, "_process_table", return_value={}), \
                mock.patch.object(task, "kill_group"):
            result = task.run_worker(["fake-worker"], str(self.work), str(output), str(error),
                                     10, 10, {}, worker="codex",
                                     on_first_output=observed.append)
        self.assertEqual(result, ("exit", 0))
        return observed

    def test_first_output_seen_for_immediate_bytes(self):
        observed = self._run_preloaded_worker(stdout=b"ready\n", poll_side_effect=[None, 0])
        self.assertEqual(len(observed), 1)

    def test_first_output_sampled_when_worker_exits_before_loop(self):
        observed = self._run_preloaded_worker(stdout=b"ready\n", write_on_poll=True)
        self.assertEqual(len(observed), 1)

    def test_stderr_only_counts_as_first_output(self):
        observed = self._run_preloaded_worker(stderr=b"rate limit\n")
        self.assertEqual(len(observed), 1)

    def test_relay_output_sampled_after_final_pipe_drain(self):
        task = load_module()
        gate = threading.Event()
        observed = []

        class BufferedStream:
            def __init__(self, payload):
                self.payload = payload

            def read1(self, _size):
                gate.wait()
                payload, self.payload = self.payload, b""
                return payload

            def close(self):
                pass

        proc = mock.Mock(pid=987655, returncode=0,
                         stdout=BufferedStream(b""), stderr=BufferedStream(b"rate limit\n"))
        proc.poll.side_effect = lambda: (gate.set() or 0)
        output = self.logs / "relay-drain.log"
        error = str(output) + ".err"
        with mock.patch.dict(os.environ, dict(self.env, TASK_CPU_POLL="100")), \
                mock.patch.object(task.subprocess, "Popen", return_value=proc), \
                mock.patch.object(task, "_worker_processes", return_value=set()), \
                mock.patch.object(task, "_process_table", return_value={}), \
                mock.patch.object(task, "kill_group"):
            result = task.run_worker(["fake-relay"], str(self.work), str(output), error,
                                     10, 10, {}, worker="relay", child_env={},
                                     on_first_output=observed.append)
        self.assertEqual(result, ("exit", 0))
        self.assertEqual(len(observed), 1)
        self.assertIn("rate limit", Path(error).read_text())

    def test_stderr_before_stdout_is_first_output(self):
        task = load_module()
        for worker in ("codex", "relay"):
            with self.subTest(worker=worker):
                marker = self.work / (worker + "-stdout-ready")
                output = self.logs / (worker + "-stderr-first.log")
                error = str(output) + ".err"
                observed_marker_states = []
                script = ("import pathlib, sys, time\n"
                          "sys.stderr.write('early error\\n'); sys.stderr.flush()\n"
                          "time.sleep(0.4)\n"
                          "pathlib.Path(%r).touch()\n"
                          "print('later output', flush=True)\n" % str(marker))
                env = dict(self.env, TASK_CPU_POLL="100")
                with mock.patch.dict(os.environ, env):
                    result = task.run_worker([sys.executable, "-c", script], str(self.work), str(output),
                                             error, 10, 1, {}, worker=worker,
                                             on_first_output=lambda _: observed_marker_states.append(marker.exists()))
                self.assertEqual(result, ("exit", 0))
                self.assertEqual(observed_marker_states, [False])

    def test_hard_timeout_is_checked_again_after_bounded_log_parsing(self):
        task = load_module()
        started = time.monotonic()
        env = dict(self.env, FAKE_MODE="silent", TASK_CPU_POLL="100")
        with mock.patch.dict(os.environ, env), mock.patch.object(
                task.CommandTracker, "poll", side_effect=lambda: time.sleep(0.2)):
            kind, _ = task.run_worker([str(FAKE / "codex")], str(self.work), str(self.logs / "parse.log"),
                                      str(self.logs / "parse.err"), 0.1, 100, {})
        self.assertEqual(kind, "timeout")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_worktree_snapshots_are_throttled(self):
        task = load_module()
        calls = []
        env = dict(self.env, FAKE_MODE="silent", TASK_CPU_POLL="100")
        with mock.patch.dict(os.environ, env), mock.patch.object(
                task, "_worktree_snapshot", side_effect=lambda *args, **kwargs: calls.append(1) or {}):
            kind, _ = task.run_worker([str(FAKE / "codex")], str(self.work), str(self.logs / "throttle.log"),
                                      str(self.logs / "throttle.err"), 1.4, 100, {})
        self.assertEqual(kind, "timeout")
        self.assertEqual(len(calls), 1)

    def test_total_timeout(self):
        res = self.start("codex", "t5b", "--timeout", "2", "--stall", "100", FAKE_MODE="silent")
        self.assertEqual(res.returncode, 1)
        self.assertIn("timeout", self.reg("t5b")["report"]["answer"])

    def test_command_in_progress_uses_cmd_stall_cap_and_still_warns(self):
        started = time.time()
        result = self.start("codex", "command-cap", "--stall", "1", "--cmd-stall", "2",
                            FAKE_MODE="command_hang", TASK_CPU_POLL="0.2")
        elapsed = time.time() - started
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("killed: stall", self.reg("command-cap")["report"]["answer"])
        self.assertGreaterEqual(elapsed, 1.8)
        self.assertLess(elapsed, 2.8)
        self.assertIn("STALL-WARN 1s no activity",
                      Path(self.reg("command-cap")["raw_log"] + ".err").read_text())

    def test_command_in_progress_still_obeys_hard_timeout(self):
        result = self.start("codex", "command-timeout", "--timeout", "1", "--stall", "1",
                            "--cmd-stall", "100", FAKE_MODE="command_hang", TASK_CPU_POLL="0.2")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.reg("command-timeout")["kill_reason"], "timeout")

    def test_model_stall_defaults(self):
        cases = (("codex", "gpt-6-luna", 180), ("codex", "gpt-6.1-sol", 300),
                 ("gemini", "gemini-3.8-flash-high", 180),
                 ("opencode", "opencode-go/deepseek-v4.1-flash", 180))
        for index, (worker, model, expected) in enumerate(cases):
            task_id = "stall-default-%d" % index
            result = self.start(worker, task_id, "--model", model, FAKE_MODE="ok")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run = self.reg(task_id)["runs"][0]
            self.assertEqual(run["stall"], expected)
            self.assertEqual(run["cmd_stall"], 1800)

    def test_watch_path_persists_to_reply_and_reply_timeout_is_half_start_timeout(self):
        watched = Path(self.tmp.name) / "script.log"
        additional = Path(self.tmp.name) / "extra.log"
        result = self.start("codex", "watch-reply", "--timeout", "20", "--watch", str(watched),
                            "--watch", str(additional), FAKE_MODE="ok")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reg = self.reg("watch-reply")
        self.assertEqual(reg["watch_paths"], [str(watched), str(additional)])
        reply = self.task("reply", "watch-reply", "continue", "--stall", "1", "--cmd-stall", "2",
                          FAKE_MODE="watchwrite", WATCH_TARGET=str(watched), TASK_CPU_POLL="100")
        self.assertEqual(reply.returncode, 0, reply.stdout + reply.stderr)
        reg = self.reg("watch-reply")
        self.assertEqual(reg["runs"][-1]["timeout"], 10)
        self.assertEqual(reg["watch_paths"], [str(watched), str(additional)])

    def test_quota_on_stderr(self):
        res = self.start("codex", "t6", FAKE_MODE="quota")
        self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
        self.assertEqual(self.reg("t6")["state"], "QUOTA")

    def test_quota_words_in_assistant_text_are_not_quota(self):
        res = self.start("codex", "t7", FAKE_MODE="quotatext")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.reg("t7")["state"], "DONE")

    def test_duplicate_id_refused(self):
        self.assertEqual(self.start("codex", "t8").returncode, 0)
        res = self.start("codex", "t8")
        self.assertEqual(res.returncode, 2)
        self.assertEqual(len(list((self.fake_dir).glob("argv.*"))), 1)

    def test_write_creates_worktree_and_branch(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=none"]
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "f").write_text("x")
        subprocess.run(["git", "add", "f"], cwd=repo, check=True)
        subprocess.run(git + ["commit", "-qm", "init"], cwd=repo, check=True)
        res = self.task("start", "codex", "w1", self.brief(), "--cwd", str(repo), "--write")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        wt = repo / ".worktrees" / "w1"
        self.assertTrue(wt.is_dir())
        reg = self.reg("w1")
        self.assertEqual(Path(reg["worktree"]).resolve(), wt.resolve())
        branches = subprocess.run(["git", "branch", "--list", "task/w1"], cwd=repo,
                                  capture_output=True, text=True).stdout
        self.assertIn("task/w1", branches)
        self.assertEqual(Path(self.argv()[10]).resolve(), wt.resolve())
        self.assertEqual(Path((self.fake_dir / "cwd.1").read_text().strip()).resolve(), wt.resolve())

    def test_write_outside_git_repo_fails(self):
        res = self.start("codex", "w2", "--write")
        self.assertEqual(res.returncode, 2)
        self.assertFalse((self.state / "w2.json").exists())


class ReplyTests(RunnerCase):
    def test_reply_continues_with_session_id(self):
        self.assertEqual(self.start("codex", "r1").returncode, 0)
        res = self.task("reply", "r1", "second \"msg\"\n$HOME", FAKE_ANSWER="reply ans")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        argv = self.argv(2)
        expected_message = "second \"msg\"\n$HOME\n\n" + load_module().CONTRACT_FOOTER
        self.assertEqual(argv, ["exec", "resume", "--json", "--skip-git-repo-check", "-m", "gpt-6-luna",
                                "-c", "model_auto_compact_token_limit=250000",
                                "-c", 'sandbox_mode="danger-full-access"', "SESSION-codex-1",
                                expected_message])
        reg = self.reg("r1")
        self.assertEqual(reg["replies"], 1)
        self.assertEqual(len(reg["runs"]), 2)
        self.assertLessEqual(reg["runs"][0]["log_end"], reg["runs"][1]["log_start"])
        self.assertEqual(reg["report"]["answer"], "reply ans")
        self.assertEqual(reg["session"], "SESSION-codex-1")
        self.assertEqual(Path((self.fake_dir / "cwd.2").read_text().strip()).resolve(), self.work.resolve())

    def test_effort_reaches_codex_on_start_and_reply(self):
        self.assertEqual(self.start("codex", "e1", "--effort", "high").returncode, 0)
        self.assertIn('model_reasoning_effort="high"', self.argv(1))
        self.assertEqual(self.task("reply", "e1", "more").returncode, 0)
        self.assertIn('model_reasoning_effort="high"', self.argv(2))
        self.assertEqual(self.start("codex", "e2").returncode, 0)
        self.assertNotIn("model_reasoning_effort", " ".join(self.argv(3)))
        self.assertEqual(self.start("codex", "e3", "--effort", "xhigh").returncode, 0)
        self.assertIn('model_reasoning_effort="xhigh"', self.argv(4))

    def test_reply_refused_while_running(self):
        self.write_reg("r2", state="RUNNING")
        res = self.task("reply", "r2", "hi")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("still running", res.stdout + res.stderr)

    def test_reply_limit(self):
        self.write_reg("r3", replies=2)
        res = self.task("reply", "r3", "hi")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("reply limit reached; escalate or ask the user", res.stdout + res.stderr)

    def test_third_reply_refused_end_to_end(self):
        self.assertEqual(self.start("codex", "r4").returncode, 0)
        self.assertEqual(self.task("reply", "r4", "a").returncode, 0)
        self.assertEqual(self.task("reply", "r4", "b").returncode, 0)
        res = self.task("reply", "r4", "c")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("reply limit reached", res.stdout + res.stderr)

    def test_unknown_id(self):
        self.assertEqual(self.task("reply", "nope", "x").returncode, 2)
        self.assertEqual(self.task("status", "nope").returncode, 2)


class AdapterTests(RunnerCase):
    def test_codex_short_model_passes_full_id_to_worker(self):
        res = self.start("codex", "short-model", "--model", "luna", "--pinned")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        argv = self.argv()
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-luna")

    def test_opencode_start_and_reply(self):
        self.assertEqual(self.start("opencode", "o1").returncode, 0)
        argv = self.argv()
        self.assertEqual(argv[:4], ["run", "--format", "json", "-m"])
        self.assertEqual(argv[4], "opencode-go/deepseek-v4.1-flash")
        self.assertEqual(self.reg("o1")["session"], "SESSION-opencode-1")
        self.assertEqual(self.reg("o1")["state"], "DONE")
        self.assertEqual(self.task("reply", "o1", "more").returncode, 0)
        self.assertEqual(self.argv(2), ["run", "--format", "json", "-s", "SESSION-opencode-1",
                                        "more\n\n" + load_module().CONTRACT_FOOTER])

    def test_gemini_start_and_reply(self):
        res = self.start("gemini", "g1", "--model", "gemini-3.8-flash-low")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        argv = self.argv()
        self.assertEqual(argv[0], "-p")
        self.assertEqual(argv[2:], ["--model", "gemini-3.8-flash-low", "--output-format", "stream-json",
                                    "--dangerously-skip-permissions"])
        reg = self.reg("g1")
        self.assertEqual(reg["session"], "SESSION-gemini-1")
        self.assertEqual(reg["state"], "DONE")
        self.assertEqual(reg["report"]["answer"], "all good 1")
        self.assertEqual(self.task("reply", "g1", "more").returncode, 0)
        self.assertEqual(self.argv(2), ["--conversation", "SESSION-gemini-1", "-p",
                                        "more\n\n" + load_module().CONTRACT_FOOTER, "--model",
                                        "gemini-3.8-flash-low", "--output-format", "stream-json",
                                        "--dangerously-skip-permissions"])

    def test_search_row_defaults_to_flash_medium(self):
        self.assertEqual(self.start("gemini", "g2").returncode, 0)
        self.assertEqual(self.reg("g2")["model"], "gemini-3.8-flash-medium")

    def test_gemini_default_runs_parallel_without_lock(self):
        self.logs.mkdir(parents=True)
        handle = open(str(self.logs / "agy.lock"), "a+")
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        first = self.start_process("gemini", "g3a", FAKE_MODE="delay", FAKE_SLEEP="1.5",
                                   TASK_LOCK_WAIT="0.4")
        self.addCleanup(lambda: first.poll() is None and first.kill())
        deadline = time.time() + 3
        while not (self.fake_dir / "argv.1").exists() and time.time() < deadline:
            time.sleep(.02)
        self.assertTrue((self.fake_dir / "argv.1").exists(), "first fake agy did not start")
        second = self.start_process("gemini", "g3b", FAKE_MODE="delay", FAKE_SLEEP="1.5",
                                    TASK_LOCK_WAIT="0.4")
        self.addCleanup(lambda: second.poll() is None and second.kill())
        deadline = time.time() + 1
        while not (self.fake_dir / "argv.2").exists() and time.time() < deadline:
            time.sleep(.02)
        self.assertTrue((self.fake_dir / "argv.2").exists(), "second fake agy was blocked")
        self.assertIsNone(first.poll(), "second agy started only after the first completed")
        self.assertEqual(first.wait(timeout=10), 0)
        self.assertEqual(second.wait(timeout=10), 0)

    def test_gemini_serial_mode_waits_for_holder(self):
        first = self.start_process("gemini", "g3c", FAKE_MODE="delay", FAKE_SLEEP="1.2",
                                   TASK_AGY_SERIAL="1")
        self.addCleanup(lambda: first.poll() is None and first.kill())
        deadline = time.time() + 3
        while not (self.fake_dir / "argv.1").exists() and time.time() < deadline:
            time.sleep(.02)
        self.assertTrue((self.fake_dir / "argv.1").exists(), "holder fake agy did not start")
        second = self.start_process("gemini", "g3d", TASK_AGY_SERIAL="1", TASK_LOCK_WAIT="3")
        self.addCleanup(lambda: second.poll() is None and second.kill())
        time.sleep(.3)
        self.assertFalse((self.fake_dir / "argv.2").exists(), "serial agy started before lock holder finished")
        self.assertEqual(first.wait(timeout=10), 0)
        self.assertEqual(second.wait(timeout=10), 0)
        self.assertTrue((self.fake_dir / "argv.2").exists())

    def test_gemini_serial_mode_keeps_queued_too_long_failure(self):
        self.logs.mkdir(parents=True)
        handle = open(str(self.logs / "agy.lock"), "a+")
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        res = self.start("gemini", "g3e", TASK_AGY_SERIAL="1", TASK_LOCK_WAIT="0.4")
        self.assertEqual(res.returncode, 1)
        reg = self.reg("g3e")
        self.assertEqual(reg["state"], "FAIL")
        self.assertIn("queued too long: another agy run held the machine lock for 0s",
                      reg["report"]["answer"])
        self.assertEqual(list(self.fake_dir.glob("argv.*")), [])

    def test_codex_does_not_take_the_lock(self):
        self.assertEqual(self.start("codex", "g4", TASK_LOCK_WAIT="1").returncode, 0)
        self.assertFalse((self.logs / "agy.lock").exists())


class RealShapeTests(unittest.TestCase):
    """Event shapes captured from the real CLIs (codex-cli 0.162, opencode 2.0, agy 1.3)."""

    def setUp(self):
        self.mod = load_module()

    def events(self, *lines):
        return self.mod.parse_events("\n".join(json.dumps(line) for line in lines))

    def test_agy_deltas_without_result_are_joined(self):
        step = {"conversation_id": "c1", "step_type": "agent_response", "state": "ACTIVE"}
        events = self.events(
            {"event": "init", "init": {"conversation_id": "c1"}},
            {"event": "step_update", "step_update": dict(step, text_delta="STATE:")},
            {"event": "step_update", "step_update": dict(step, text_delta=" DONE\nANSWER: ok")})
        self.assertEqual(self.mod.find_session(events, "gemini"), "c1")
        self.assertEqual(self.mod.parse_contract(self.mod.final_text(events, ""))["answer"], "ok")

    def test_agy_failed_result_is_quota(self):
        events = self.events({"event": "result", "result": {"status": "ERROR", "response": "429 usage limit"}})
        self.assertIn("usage limit", self.mod.quota_line(events, ""))

    def test_opencode_go_quota_error_event(self):
        events = self.events({"type": "error", "sessionID": "ses_1", "error": {
            "type": "provider.quota", "message": "Go usage limit exceeded", "status": 429}})
        self.assertIn("usage limit", self.mod.quota_line(events, ""))
        self.assertEqual(self.mod.find_session(events, "opencode"), "ses_1")


class DeepseekTwinTests(RunnerCase):
    FREE = "opencode/deepseek-v4.1-flash-free"
    PAID = "opencode-go/deepseek-v4.1-flash"

    def test_model_alias_accepts_short_name_for_matching_worker(self):
        mod = load_module()
        self.assertEqual(mod.model_alias("codex", "luna"), "luna")
        self.assertEqual(mod.model_alias("codex", "sol"), "sol")
        self.assertEqual(mod.model_alias(None, "luna"), "luna")
        self.assertIsNone(mod.model_alias("opencode", "luna"))
        self.assertEqual(mod.model_alias("codex", "gpt-6-luna"), "luna")

    def test_alias_resolution_and_row_order(self):
        mod = load_module()
        self.assertEqual(mod.MODELS["deepseek-free"], ("opencode", self.FREE))
        self.assertEqual(mod.MODELS["deepseek"], ("opencode", self.PAID))
        self.assertEqual(mod.model_alias("opencode", self.FREE), "deepseek-free")
        for row, names in mod.ROWS.items():
            self.assertNotIn("deepseek-free", names, row)
        self.assertEqual(mod.ROWS["mechanical"], ["hydrogen", "luna", "deepseek", "chatanywhere"])

    def test_off_on_and_workers_list(self):
        self.assertIn("deepseek-free: on", self.task("workers").stdout)
        self.switches("deepseek-free")
        self.assertIn("deepseek-free: off", self.task("workers").stdout)
        self.assertIn("deepseek: on", self.task("workers").stdout)
        self.task("on", "deepseek-free")
        self.switches("opencode")
        out = self.task("workers").stdout
        self.assertIn("deepseek-free: off", out)
        self.assertIn("deepseek: off", out)

    def test_explicit_free_model_maps_to_mechanical(self):
        self.task("on", "deepseek-free")
        res = self.start("opencode", "free-pin", "--model", self.FREE, "--pinned")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.reg("free-pin")["row"], "mechanical")
        self.assertEqual(self.argv()[4], self.FREE)

    def hop(self, tid, mode, *args):
        self.task("on", "deepseek-free")
        return self.task("start", "opencode", tid, self.brief(), "--row", "mechanical", "--model", self.FREE,
                         "--cwd", str(self.work),
                         *args, FAKE_MODE=mode)

    def test_free_quota_hops_to_paid(self):
        res = self.hop("hop-fp", "quota_free")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("FALLBACK: deepseek-free quota -> deepseek", res.stdout)
        reg = self.reg("hop-fp")
        self.assertEqual((reg["alias"], reg["model"], reg["state"]), ("deepseek", self.PAID, "DONE"))
        self.assertEqual(reg["fallback"], {"from": "deepseek-free", "to": "deepseek", "reason": "quota"})
        self.assertEqual(self.argv(1)[4], self.FREE)
        self.assertEqual(self.argv(2)[4], self.PAID)
        self.assertEqual(len(reg["runs"]), 2)

    def test_paid_quota_never_hops_to_free(self):
        res = self.task("start", "opencode", "hop-pf", self.brief(), "--row", "mechanical", "--model", self.PAID,
                        "--cwd", str(self.work), FAKE_MODE="quota_paid")
        self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
        self.assertNotIn("FALLBACK", res.stdout)
        self.assertFalse((self.fake_dir / "argv.2").exists())

    def test_default_rows_never_pick_free(self):
        for row in ("search", "inventory", "code", "hard", "mechanical"):
            res = self.task("start", "row-" + row, self.brief(), "--row", row, "--cwd", str(self.work))
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertNotEqual(self.reg("row-" + row)["alias"], "deepseek-free", row)

    def test_pinned_does_not_hop(self):
        self.task("on", "deepseek-free")
        res = self.task("start", "opencode", "hop-pin", self.brief(), "--row", "mechanical", "--model", self.FREE,
                        "--pinned", "--cwd", str(self.work), FAKE_MODE="quota")
        self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
        self.assertNotIn("FALLBACK", res.stdout)
        self.assertEqual(self.reg("hop-pin")["alias"], "deepseek-free")
        self.assertFalse((self.fake_dir / "argv.2").exists())

    def test_single_hop_only(self):
        res = self.hop("hop-two", "quota")
        self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
        self.assertEqual(self.reg("hop-two")["alias"], "deepseek")
        self.assertEqual(self.reg("hop-two")["state"], "QUOTA")
        self.assertTrue((self.fake_dir / "argv.2").exists())
        self.assertFalse((self.fake_dir / "argv.3").exists())

    def test_no_hop_when_twin_is_off(self):
        self.switches("deepseek")
        res = self.hop("hop-off", "quota_free")
        self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
        self.assertFalse((self.fake_dir / "argv.2").exists())

    def test_free_usage_limit_error_is_quota(self):
        mod = load_module()
        events = [{"type": "error", "error": {"name": "FreeUsageLimitError", "message": "limit"}}]
        self.assertIsNotNone(mod.quota_line(events, ""))
        self.assertIsNotNone(mod.quota_line([], "FreeUsageLimitError: try later"))


class RelayWorkerTests(RunnerCase):
    TEST_MARKER = "synthetic-marker-7f"

    def relay_env(self, **extra):
        key_cmd = self.fake_dir / "relay-key"
        key_cmd.write_text("#!/bin/sh\nprintf '%s' '%s'\n" % ("%s", self.TEST_MARKER))
        key_cmd.chmod(0o755)
        env = {"RELAY_BASE_URL": "https://relay.invalid", "TASK_RELAY_KEY_CMD": str(key_cmd)}
        env.update(extra)
        return env

    def start_relay(self, tid, *args, **env):
        mode = env.pop("FAKE_MODE", None)
        if mode:
            return self.task("start", "relay", tid, self.brief("RELAY_FAKE_MODE:%s\n" % mode),
                             "--cwd", str(self.work), "--model", "relay-sonnet", "--pinned",
                             *args, **self.relay_env(**env))
        return self.start("relay", tid, "--model", "relay-sonnet", "--pinned", *args,
                          **self.relay_env(**env))

    def relay_fake_dir(self):
        return Path(self.env["HOME"]) / ".relay-fake"

    def relay_argv(self, number=1):
        data = (self.relay_fake_dir() / ("argv.%d" % number)).read_bytes().decode()
        return data.split("\0")[:-1]

    def relay_model(self, number=1):
        return (self.relay_fake_dir() / ("model.%d" % number)).read_text()

    def test_relay_is_owner_only_and_unpinned_refused(self):
        mod = load_module()
        self.assertIn("relay-sonnet", mod.OWNER_ONLY)
        self.assertIn("relay-haiku", mod.OWNER_ONLY)
        for row, names in mod.ROWS.items():
            self.assertNotIn("relay-sonnet", names, row)
            self.assertNotIn("relay-haiku", names, row)
        self.assertNotIn("relay-sonnet", mod.TWINS)
        self.assertNotIn("relay-sonnet", mod.TWINS.values())
        result = self.start("relay", "relay-unpinned", "--model", "relay-sonnet")
        self.assertEqual(2, result.returncode)
        self.assertIn("relay is explicitly named", result.stderr)
        self.assertEqual([], list(self.relay_fake_dir().glob("argv.*")))

    def test_relay_missing_url_or_key_fails_without_start(self):
        no_url = self.start_relay("relay-no-url", RELAY_BASE_URL="")
        self.assertEqual(1, no_url.returncode)
        self.assertIn("RELAY_BASE_URL is not set", no_url.stdout)
        unset_env = dict(self.env, **self.relay_env())
        unset_env.pop("RELAY_BASE_URL", None)
        unset_url = subprocess.run([sys.executable, str(SCRIPT), "start", "relay", "relay-unset-url",
                                    self.brief(), "--cwd", str(self.work), "--model", "relay-sonnet", "--pinned"],
                                   env=unset_env, capture_output=True, text=True, timeout=60)
        self.assertEqual(1, unset_url.returncode)
        self.assertIn("RELAY_BASE_URL is not set", unset_url.stdout)
        no_key = self.start_relay("relay-no-key", TASK_RELAY_KEY_CMD="/usr/bin/false")
        self.assertEqual(1, no_key.returncode)
        self.assertIn("authentication key unavailable", no_key.stdout)
        self.assertEqual([], list(self.relay_fake_dir().glob("argv.*")))

    def test_relay_key_is_child_env_only_and_config_is_isolated(self):
        parent_env = {}
        parent_env["ANTHROPIC_API_KEY"] = "parent-api-marker"
        parent_env["ANTHROPIC_AUTH_TOKEN"] = "parent-auth-marker"
        parent_env["ANTHROPIC_BASE_URL"] = "ambient-invalid"
        parent_env["ANTHROPIC_CUSTOM_HEADERS"] = "parent-header-marker"
        parent_env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        parent_env["AWS_SESSION_TOKEN"] = "parent-cloud-marker"
        parent_env["CLAUDE_CODE_OAUTH_TOKEN"] = "parent-oauth-marker"
        parent_env["CLAUDE_CONFIG_DIR"] = "ambient-config"
        result = self.start_relay("relay-key-private", **parent_env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        argv = self.relay_argv()
        self.assertNotIn(self.TEST_MARKER, argv)
        fake_dir = self.relay_fake_dir()
        self.assertEqual("yes", (fake_dir / "auth.1").read_text())
        self.assertEqual("no", (fake_dir / "api-key.1").read_text())
        self.assertEqual("yes", (fake_dir / "url.1").read_text())
        self.assertEqual("yes", (fake_dir / "baseurlmatch.1").read_text())
        env_names = set((fake_dir / "envnames.1").read_text().splitlines())
        self.assertIn("ANTHROPIC_AUTH_TOKEN", env_names)
        self.assertIn("ANTHROPIC_BASE_URL", env_names)
        self.assertIn("CLAUDE_CONFIG_DIR", env_names)
        self.assertNotIn("ANTHROPIC_API_KEY", env_names)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env_names)
        self.assertNotIn("TASK_STATE_DIR", env_names)
        allowed_env = {"PATH", "HOME", "LANG", "TMPDIR", "TERM", "PWD", "SHLVL", "_",
                       "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR"}
        self.assertTrue(all(name in allowed_env or name.startswith("LC_") for name in env_names))
        config = Path(self.env["HOME"]) / ".claude/state/relay-config"
        self.assertEqual(str(config), (fake_dir / "config.1").read_text())
        self.assertTrue(config.is_dir())
        self.assertEqual(0o700, config.stat().st_mode & 0o777)
        self.assertIn("--safe-mode", argv)
        self.assertNotIn("--bare", argv)
        self.assertIn("--restricted", argv)
        reg_text = (self.state / "relay-key-private.json").read_text()
        log_text = (self.logs / "relay-key-private.log").read_text()
        status = self.task("status", "relay-key-private").stdout
        for text in (result.stdout, reg_text, log_text, status):
            self.assertNotIn(self.TEST_MARKER, text)
            self.assertNotIn("parent-api-marker", text)
            self.assertNotIn("parent-auth-marker", text)

    def test_relay_redacts_child_credentials_from_logs_errors_registry_and_status(self):
        result = self.start_relay("relay-redact", FAKE_MODE="leak")
        self.assertEqual(1, result.returncode)
        registry = (self.state / "relay-redact.json").read_text()
        stdout_log = (self.logs / "relay-redact.log").read_text()
        stderr_log = (self.logs / "relay-redact.log.err").read_text()
        status = self.task("status", "relay-redact").stdout
        for text in (result.stdout, registry, stdout_log, stderr_log, status):
            self.assertNotIn(self.TEST_MARKER, text)
            self.assertIn("[REDACTED]", text)
        self.assertNotIn(self.TEST_MARKER, result.stderr)
        self.assertIn("relay turn failed: credential [REDACTED]", result.stdout)

    def test_relay_nonzero_exit_never_becomes_done(self):
        for mode, code in (("bad_exit", 7), ("clean_nonzero", 9)):
            with self.subTest(mode=mode):
                task_id = "relay-" + mode
                failed = self.start_relay(task_id, FAKE_MODE=mode)
                self.assertEqual(1, failed.returncode, failed.stdout + failed.stderr)
                self.assertIn("CLI exited with code %s" % code, failed.stdout)
                self.assertNotIn("| DONE", failed.stdout)
                self.assertEqual("FAIL", self.reg(task_id)["state"])
                status = self.task("status", task_id)
                self.assertEqual(1, status.returncode)
                self.assertIn("| FAIL", status.stdout)
                self.assertNotIn("FALLBACK", failed.stdout)
        self.assertEqual(2, len(list(self.relay_fake_dir().glob("argv.*"))))

    def test_relay_tools_readonly_and_write(self):
        readonly = self.start_relay("relay-readonly")
        self.assertEqual(0, readonly.returncode, readonly.stdout + readonly.stderr)
        read_argv = self.relay_argv()
        tools = read_argv[read_argv.index("--tools") + 1]
        self.assertEqual("Read,Grep,Glob", tools)
        allowed = read_argv[read_argv.index("--allowedTools") + 1]
        self.assertEqual("Read,Grep,Glob", allowed)
        self.assertNotIn("Bash", read_argv)
        read_denied = read_argv[read_argv.index("--disallowedTools") + 1:read_argv.index("--model")]
        self.assertIn("Read(**/*.pem)", read_denied)
        self.assertIn("Edit(**/*.pem)", read_denied)
        self.assertIn("Write(**/*.pem)", read_denied)

        repo = Path(self.tmp.name) / "write-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "tracked.txt").write_text("fixture\n")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                        "user.email=test", "commit", "-qm", "fixture"], check=True)
        write = self.task("start", "relay", "relay-write", self.brief(), "--cwd", str(repo),
                          "--model", "relay-sonnet", "--pinned", "--write", **self.relay_env())
        self.assertEqual(0, write.returncode, write.stdout + write.stderr)
        write_argv = self.relay_argv(2)
        tools = write_argv[write_argv.index("--tools") + 1]
        self.assertEqual("Read,Grep,Glob,Edit,Write", tools)
        allowed = write_argv[write_argv.index("--allowedTools") + 1]
        self.assertEqual("Read,Grep,Glob,Edit,Write", allowed)
        self.assertNotIn("Bash", write_argv)
        self.assertEqual(self.reg("relay-write")["worktree"],
                         (self.relay_fake_dir() / "cwd.2").read_text().strip())
        denied = write_argv[write_argv.index("--disallowedTools") + 1:write_argv.index("--model")]
        for rules in (read_denied, denied):
            for tool in ("Read", "Edit", "Write", "Grep", "Glob"):
                for pattern in (".env*", "**/.env*", "**/*.pem", "**/*.p12", "**/*.keystore", "**/*.jks",
                                "**/key.properties", "**/*service-account*.json", "**/secrets/**"):
                    self.assertIn("%s(%s)" % (tool, pattern), rules)

    def test_relay_auth_failure_stops_without_fallback(self):
        result = self.start_relay("relay-auth", FAKE_MODE="auth")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("authentication failed", result.stdout)
        self.assertEqual("FAIL", self.reg("relay-auth")["state"])
        self.assertNotIn("FALLBACK", result.stdout)
        self.assertEqual(1, len(list(self.relay_fake_dir().glob("argv.*"))))

    def test_relay_small_output_keeps_watchdog_alive(self):
        mod = load_module()
        out = str(self.logs / "relay-ticks.log")
        err = out + ".err"
        script = ("import os, sys, time\n"
                  "token = os.environ['ANTHROPIC_AUTH_TOKEN']\n"
                  "sys.stderr.write(token[:5]); sys.stderr.flush()\n"
                  "for _ in range(20):\n"
                  " print('tick', flush=True)\n"
                  " time.sleep(.1)\n"
                  "sys.stderr.write(token[5:]); sys.stderr.flush()\n")
        kind, detail = mod.run_worker([sys.executable, "-c", script], str(self.work),
                                      out, err, timeout=10, stall=.25, worker="relay",
                                      child_env={"ANTHROPIC_AUTH_TOKEN": self.TEST_MARKER})
        self.assertEqual(("exit", 0), (kind, detail))
        self.assertEqual(20, Path(out).read_text().count("tick"))
        self.assertEqual("[REDACTED]", Path(err).read_text())

    def test_relay_session_resume_models_effort_and_no_fallback(self):
        result = self.start_relay("relay-resume", "--effort", "high")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("claude-sonnet-5-5", self.relay_model())
        self.assertEqual("relay-sonnet", self.reg("relay-resume")["alias"])
        self.assertIn("relay - relay-sonnet - relay-resume", self.task("status", "relay-resume").stdout)
        self.assertEqual("session-1", self.reg("relay-resume")["session"])
        argv = self.relay_argv()
        self.assertIn("--effort", argv)
        self.assertEqual("high", argv[argv.index("--effort") + 1])
        reply = self.task("reply", "relay-resume", "continue", **self.relay_env())
        self.assertEqual(0, reply.returncode, reply.stdout + reply.stderr)
        resumed = self.relay_argv(2)
        self.assertEqual("claude-sonnet-5-5", self.relay_model(2))
        self.assertEqual("session-1", resumed[resumed.index("--resume") + 1])
        self.assertEqual(2, len(list(self.relay_fake_dir().glob("argv.*"))))

        failed = self.start_relay("relay-quota", FAKE_MODE="quota")
        self.assertEqual(4, failed.returncode)
        self.assertIn("429 rate limit exceeded", failed.stdout)
        self.assertEqual(3, len(list(self.relay_fake_dir().glob("argv.*"))))

    def test_relay_alias_and_full_model_ids(self):
        haiku = self.task("start", "relay", "relay-haiku", self.brief(), "--cwd", str(self.work),
                          "--model", "relay-haiku", "--pinned", **self.relay_env())
        self.assertEqual(0, haiku.returncode, haiku.stdout + haiku.stderr)
        self.assertEqual("claude-haiku-5-5", self.relay_model())
        self.assertEqual("relay-haiku", self.reg("relay-haiku")["alias"])

        full_id = "claude-opus-5-5"
        result = self.task("start", "relay", "relay-full-id", self.brief(), "--cwd", str(self.work),
                           "--model", full_id, "--pinned", **self.relay_env())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(full_id, self.relay_model(2))
        self.assertEqual("relay-sonnet", self.reg("relay-full-id")["alias"])

    def test_relay_opus_is_owner_only_and_unpinned_refused(self):
        mod = load_module()
        self.assertIn("relay-opus", mod.OWNER_ONLY)
        for row, names in mod.ROWS.items():
            self.assertNotIn("relay-opus", names, row)
        self.assertNotIn("relay-opus", mod.TWINS)
        self.assertNotIn("relay-opus", mod.TWINS.values())
        result = self.start("relay", "relay-opus-unpinned", "--model", "relay-opus")
        self.assertEqual(2, result.returncode)
        self.assertIn("relay is explicitly named", result.stderr)
        self.assertEqual([], list(self.relay_fake_dir().glob("argv.*")))

    def test_relay_opus_pinned_model_status_and_off(self):
        pinned = self.task("start", "relay", "relay-opus-pinned", self.brief(), "--cwd", str(self.work),
                           "--model", "relay-opus", "--pinned", **self.relay_env())
        self.assertEqual(0, pinned.returncode, pinned.stdout + pinned.stderr)
        self.assertEqual("claude-opus-5-5", self.relay_model())
        self.assertEqual("relay-opus", self.reg("relay-opus-pinned")["alias"])
        self.assertIn("relay - relay-opus - relay-opus-pinned",
                      self.task("status", "relay-opus-pinned").stdout)
        self.assertIn("relay-opus: on", self.task("workers").stdout)

        self.switches("relay-opus")
        self.assertIn("relay-opus: off", self.task("workers").stdout)
        blocked = self.task("start", "relay", "relay-opus-off", self.brief(), "--cwd", str(self.work),
                            "--model", "relay-opus", "--pinned", **self.relay_env())
        self.assertEqual(2, blocked.returncode)
        self.assertIn("BLOCKED: relay-opus is off (explicitly named); ask the user", blocked.stdout)
        self.assertEqual(1, len(list(self.relay_fake_dir().glob("argv.*"))))

    def test_relay_rejects_invalid_model_id(self):
        result = self.task("start", "relay", "relay-invalid-model", self.brief(), "--cwd", str(self.work),
                           "--model", "bad/model", "--pinned", **self.relay_env())
        self.assertEqual(2, result.returncode)
        self.assertIn("invalid relay model id", result.stderr)
        self.assertEqual([], list(self.relay_fake_dir().glob("argv.*")))


class PerchWorkerTests(RunnerCase):
    MODEL = "deepseek-flash-starter"
    PAID = "opencode-go/deepseek-v4.1-flash"

    def start_perch(self, tid, *args, **extra_env):
        return self.start("perch", tid, "--model", "perch-deepseek", "--pinned",
                          *args, **extra_env)

    def path_without_perch(self):
        directory = Path(self.tmp.name) / "no-perch-bin"
        directory.mkdir()
        for name in ("opencode", "_common.sh", "_emit.py"):
            target = directory / name
            target.write_bytes((FAKE / name).read_bytes())
            target.chmod(0o755)
        return str(directory) + os.pathsep + "/usr/bin" + os.pathsep + "/bin"

    def write_perch_cache(self, usage, at=None):
        path = Path(self.env["HOME"]) / ".claude" / "state" / "perch-quota.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time() if at is None else at, "usage": usage}))
        return path

    def argv_count(self):
        return len(list(self.fake_dir.glob("argv.*")))

    def test_alias_and_model_mapping(self):
        mod = load_module()
        self.assertEqual(mod.MODELS["perch-deepseek"], ("perch", self.MODEL))
        self.assertEqual(mod.DEFAULT_MODELS["perch"], self.MODEL)
        self.assertEqual(mod.model_alias("perch", self.MODEL), "perch-deepseek")
        self.assertEqual(mod.model_alias("perch", "perch-deepseek"), "perch-deepseek")
        self.assertEqual(mod.model_alias(None, self.MODEL), "perch-deepseek")
        self.assertEqual(mod.ROW_OF_ALIAS["perch-deepseek"], "mechanical")

    def test_not_in_any_row_and_not_a_twin(self):
        mod = load_module()
        for row, names in mod.ROWS.items():
            self.assertNotIn("perch-deepseek", names, row)
        self.assertEqual(mod.ROWS["mechanical"], ["hydrogen", "luna", "deepseek", "chatanywhere"])
        self.assertNotIn("perch-deepseek", mod.TWINS)
        self.assertNotIn("perch-deepseek", mod.TWINS.values())
        self.assertIn("perch-deepseek", mod.OWNER_ONLY)

    def test_no_cross_match_with_deepseek_workers(self):
        mod = load_module()
        free, paid = mod.MODELS["deepseek-free"][1], mod.MODELS["deepseek"][1]
        self.assertIsNone(mod.model_alias("opencode", self.MODEL))
        self.assertIsNone(mod.model_alias("opencode", "perch-deepseek"))
        self.assertIsNone(mod.model_alias("perch", free))
        self.assertIsNone(mod.model_alias("perch", paid))
        self.assertIsNone(mod.model_alias("perch", "deepseek-free"))
        self.assertIsNone(mod.model_alias("perch", "deepseek"))
        self.assertEqual(mod.model_alias(None, "deepseek-free"), "deepseek-free")
        self.assertEqual(mod.model_alias(None, "deepseek"), "deepseek")
        self.assertEqual(mod.model_alias(None, "perch-deepseek"), "perch-deepseek")
        for target in ("deepseek-free", "deepseek"):
            self.assertNotEqual(mod.MODELS[target][0], "perch")
            self.assertNotIn("perch", mod.MODELS[target][1])

    def test_pinned_required_and_unpinned_refused(self):
        unpinned = self.start("perch", "p-unpinned", "--model", "perch-deepseek")
        self.assertEqual(unpinned.returncode, 2, unpinned.stdout + unpinned.stderr)
        self.assertIn("perch-deepseek is explicitly named", unpinned.stderr)
        self.assertIn("--pinned", unpinned.stderr)
        self.assertFalse((self.state / "p-unpinned.json").exists())
        full = self.start("perch", "p-unpinned-full", "--model", self.MODEL)
        self.assertEqual(full.returncode, 2, full.stdout + full.stderr)
        self.assertIn("perch-deepseek is explicitly named", full.stderr)
        model_only = self.task("start", "p-model-only", self.brief(), "--model", self.MODEL,
                               "--pinned", "--cwd", str(self.work))
        self.assertEqual(model_only.returncode, 2, model_only.stdout + model_only.stderr)
        self.assertIn("--pinned requires an explicit worker and --model", model_only.stderr)
        self.assertEqual(self.argv_count(), 0)

    def test_pinned_start_parses_json_and_uses_plan_permission(self):
        res = self.start_perch("p1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        argv = self.argv()
        self.assertEqual(argv[:3], ["run", argv[1], "--json"])
        self.assertEqual(argv[3:7], ["--model", self.MODEL, "--permission", "plan"])
        self.assertEqual(argv[7:9], ["--cwd", str(self.work)])
        reg = self.reg("p1")
        self.assertEqual((reg["worker"], reg["model"], reg["alias"]),
                         ("perch", self.MODEL, "perch-deepseek"))
        self.assertEqual(reg["state"], "DONE")
        self.assertEqual(reg["session"], "THREAD-perch-1")
        self.assertEqual(reg["report"]["answer"], "all good 1")
        self.assertIn("perch - perch-deepseek - p1 | DONE", res.stdout)
        self.assertNotIn("PERCH_EVENTS_SENTINEL", res.stdout)
        self.assertNotIn("PERCH_EVENTS_SENTINEL", json.dumps(reg["report"]))

    def test_full_model_id_also_resolves(self):
        res = self.start("perch", "p-full", "--model", self.MODEL, "--pinned")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.reg("p-full")["alias"], "perch-deepseek")

    def test_reply_reuses_thread(self):
        self.assertEqual(self.start_perch("p-reply").returncode, 0)
        res = self.task("reply", "p-reply", "more", FAKE_ANSWER="reply ans")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        argv = self.argv(2)
        self.assertEqual(argv[argv.index("--thread") + 1], "THREAD-perch-1")
        self.assertEqual(argv[argv.index("--permission") + 1], "plan")
        self.assertIn("ANSWER: reply ans", res.stdout)
        self.assertNotIn("PERCH_EVENTS_SENTINEL", res.stdout)

    def test_write_task_uses_take_the_wheel(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=none"]
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "f").write_text("x")
        subprocess.run(["git", "add", "f"], cwd=repo, check=True)
        subprocess.run(git + ["commit", "-qm", "init"], cwd=repo, check=True)
        res = self.task("start", "perch", "p-write", self.brief(), "--cwd", str(repo), "--write",
                        "--model", "perch-deepseek", "--pinned")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        argv = self.argv()
        self.assertEqual(argv[argv.index("--permission") + 1], "take_the_wheel")
        self.assertEqual(Path(argv[argv.index("--cwd") + 1]).resolve(),
                         (repo / ".worktrees" / "p-write").resolve())

    def test_off_pinned_start_is_blocked(self):
        self.switches("perch-deepseek")
        blocked = self.start_perch("p-off")
        self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
        self.assertIn("BLOCKED: deepseek-flash-starter is off (explicitly named); ask the user",
                      blocked.stdout)
        self.task("on", "perch-deepseek")
        self.switches("perch")
        cli_off = self.start_perch("p-cli-off")
        self.assertEqual(cli_off.returncode, 2, cli_off.stdout + cli_off.stderr)
        self.assertIn("BLOCKED", cli_off.stdout)
        self.assertEqual(self.argv_count(), 0)

    def test_quota_signals_are_quota_without_any_hop(self):
        for index, mode in enumerate(("quota", "allowance_fail", "stderr_limit",
                                      "errorcode_limit", "segment_limit")):
            task_id = "p-quota-%d" % index
            res = self.start_perch(task_id, FAKE_MODE=mode)
            self.assertEqual(res.returncode, 4, (mode, res.stdout + res.stderr))
            reg = self.reg(task_id)
            self.assertEqual(reg["state"], "QUOTA", mode)
            self.assertEqual(reg["alias"], "perch-deepseek", mode)
            self.assertEqual(len(reg["runs"]), 1, mode)
            self.assertNotIn("FALLBACK", res.stdout, mode)
            self.assertEqual(self.argv_count(), index + 1, mode)

    def test_ok_true_limit_text_is_done(self):
        res = self.start_perch("p-limit-ok", FAKE_MODE="ok_limit")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        reg = self.reg("p-limit-ok")
        self.assertEqual(reg["state"], "DONE")
        self.assertIn("turn_rate_limited", reg["report"]["answer"])
        self.assertEqual(len(reg["runs"]), 1)
        self.assertNotIn("FALLBACK", res.stdout)

    def test_ordinary_answer_mentioning_rate_limit_stays_done(self):
        answer = "documented the rate limit and allowance handling"
        res = self.start_perch("p-answer-limit", FAKE_ANSWER=answer)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        reg = self.reg("p-answer-limit")
        self.assertEqual(reg["state"], "DONE")
        self.assertEqual(reg["report"]["answer"], answer)
        self.assertEqual(len(reg["runs"]), 1)
        self.assertNotIn("FALLBACK", res.stdout)

    def test_preamble_before_json_is_parsed(self):
        res = self.start_perch("p-preamble", FAKE_MODE="preamble")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.reg("p-preamble")["state"], "DONE")
        self.assertEqual(self.reg("p-preamble")["report"]["answer"], "all good 1")

    def test_failed_run_ends_fail_without_hop(self):
        res = self.start_perch("p-work-fail", FAKE_MODE="work_fail")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        reg = self.reg("p-work-fail")
        self.assertEqual(reg["state"], "FAIL")
        self.assertEqual(reg["alias"], "perch-deepseek")
        self.assertIn("perch turn failed: model failed after edits", reg["report"]["answer"])
        self.assertNotIn("FALLBACK", res.stdout)
        self.assertEqual(self.argv_count(), 1)
        nocontract = self.start_perch("p-nocontract", FAKE_MODE="nocontract")
        self.assertEqual(nocontract.returncode, 1, nocontract.stdout + nocontract.stderr)
        self.assertEqual(self.reg("p-nocontract")["state"], "FAIL")
        self.assertNotIn("FALLBACK", nocontract.stdout)
        self.assertEqual(self.argv_count(), 2)

    def test_rejected_model_fails_with_valid_names(self):
        res = self.start_perch("p-rejected", FAKE_MODE="rejected")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        answer = self.reg("p-rejected")["report"]["answer"]
        self.assertIn("perch rejected the model (no fallback)", answer)
        self.assertIn("Unknown model", answer)
        self.assertIn("Valid models: deepseek-flash-starter", answer)
        self.assertNotIn("FALLBACK", res.stdout)
        self.assertEqual(self.argv_count(), 1)
        blocked = self.start_perch("p-blocked", FAKE_MODE="model_blocked")
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("starter_model_blocked", self.reg("p-blocked")["report"]["answer"])
        self.assertNotIn("FALLBACK", blocked.stdout)
        self.assertEqual(self.argv_count(), 2)

    def test_auth_failure_ends_fail_without_hop(self):
        res = self.start_perch("p-auth", FAKE_MODE="auth")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        reg = self.reg("p-auth")
        self.assertEqual(reg["state"], "FAIL")
        self.assertEqual(reg["alias"], "perch-deepseek")
        self.assertIn("perch auth failure (no fallback)", reg["report"]["answer"])
        self.assertIn("Not signed in", reg["report"]["answer"])
        self.assertNotIn("FALLBACK", res.stdout)
        self.assertEqual(self.argv_count(), 1)

    def test_missing_perch_binary_ends_fail_without_hop(self):
        res = self.start_perch("p-nobin", PATH=self.path_without_perch())
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        reg = self.reg("p-nobin")
        self.assertEqual(reg["state"], "FAIL")
        self.assertEqual(reg["alias"], "perch-deepseek")
        self.assertIn("cannot run worker CLI: perch", reg["report"]["answer"])
        self.assertNotIn("FALLBACK", res.stdout)
        self.assertEqual(self.argv_count(), 0)

    def test_mechanical_row_is_paid_deepseek_and_ignores_perch_cache(self):
        spent = {"month": {"used": 20000, "limit": 20000, "percent": 100},
                 "five_hour": {"percent": 4, "frees_up": None},
                 "weekly": {"percent": 4, "frees_up": None}}
        self.write_perch_cache(spent)
        self.switches("hydrogen", "luna")
        self.assertEqual(self.task("resolve", "mechanical").stdout, "opencode - deepseek\n")
        res = self.task("start", "row", self.brief(), "--row", "mechanical", "--cwd", str(self.work))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("FALLBACK: hydrogen off -> deepseek", res.stdout)
        self.assertEqual(self.reg("row")["alias"], "deepseek")
        self.assertEqual(self.argv()[4], self.PAID)
        self.switches("perch-deepseek")
        self.assertEqual(self.task("resolve", "mechanical").stdout, "opencode - deepseek\n")
        self.task("on", "perch-deepseek")
        self.switches("perch")
        self.assertEqual(self.task("resolve", "mechanical").stdout, "opencode - deepseek\n")

    def test_pinned_start_runs_despite_spent_cache(self):
        self.write_perch_cache({"month": {"used": 20000, "limit": 20000, "percent": 100},
                                "five_hour": {"percent": 100, "frees_up": None},
                                "weekly": {"percent": 100, "frees_up": None}})
        res = self.start_perch("p-spent")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.reg("p-spent")["alias"], "perch-deepseek")
        self.assertEqual(self.reg("p-spent")["state"], "DONE")
        self.assertEqual(self.argv_count(), 1)

    def test_no_row_can_pick_perch(self):
        self.switches("hydrogen", "deepseek", "luna", "chatanywhere")
        result = self.task("resolve", "mechanical")
        self.assertEqual(result.returncode, 2)
        output = (result.stdout + result.stderr).strip()
        self.assertIn("all workers for row mechanical are off", output)
        self.assertNotIn("perch", output.lower())

    def test_switches_are_independent_both_directions(self):
        self.switches("opencode")
        out = self.task("workers").stdout
        self.assertIn("deepseek-free: off", out)
        self.assertIn("deepseek: off", out)
        self.assertIn("perch-deepseek: on", out)
        self.assertIn("perch: on", out)
        self.task("on", "opencode")
        self.switches("perch-deepseek")
        out = self.task("workers").stdout
        self.assertIn("perch-deepseek: off", out)
        self.assertIn("deepseek-free: on", out)
        self.assertIn("deepseek: on", out)
        self.assertIn("perch: on", out)
        self.task("on", "perch-deepseek")
        self.switches("perch")
        out = self.task("workers").stdout
        self.assertIn("perch-deepseek: off", out)
        self.assertIn("perch: off", out)
        self.assertIn("deepseek-free: on", out)
        self.assertIn("deepseek: on", out)
        self.task("on", "perch")
        self.switches("deepseek-free")
        out = self.task("workers").stdout
        self.assertIn("deepseek-free: off", out)
        self.assertIn("perch-deepseek: on", out)
        self.assertIn("deepseek: on", out)
        self.task("on", "deepseek-free")
        self.switches("deepseek")
        out = self.task("workers").stdout
        self.assertIn("deepseek: off", out)
        self.assertIn("deepseek-free: on", out)
        self.assertIn("perch-deepseek: on", out)
        self.assertIn("perch: on", out)

    def test_structured_error_json_without_text_is_parsed(self):
        quota = self.start_perch("p-structured-quota", FAKE_MODE="structured_quota")
        self.assertEqual(quota.returncode, 4, quota.stdout + quota.stderr)
        self.assertEqual(self.reg("p-structured-quota")["state"], "QUOTA")
        self.assertIn("turn_rate_limited", self.reg("p-structured-quota")["report"]["answer"])
        self.assertNotIn("FALLBACK", quota.stdout)
        rejected = self.start_perch("p-structured-rejected", FAKE_MODE="structured_rejected")
        self.assertEqual(rejected.returncode, 1, rejected.stdout + rejected.stderr)
        answer = self.reg("p-structured-rejected")["report"]["answer"]
        self.assertIn("starter_model_blocked", answer)
        self.assertIn("valid models: deepseek-flash-starter", answer)
        self.assertNotIn("FALLBACK", rejected.stdout)
        noname = self.start_perch("p-structured-noname", FAKE_MODE="structured_rejected_noname")
        self.assertEqual(noname.returncode, 1, noname.stdout + noname.stderr)
        self.assertIn("valid model names unavailable",
                      self.reg("p-structured-noname")["report"]["answer"])
        auth = self.start_perch("p-structured-auth", FAKE_MODE="structured_auth")
        self.assertEqual(auth.returncode, 1, auth.stdout + auth.stderr)
        self.assertIn("perch auth failure (no fallback)",
                      self.reg("p-structured-auth")["report"]["answer"])
        self.assertIn("Not signed in", self.reg("p-structured-auth")["report"]["answer"])
        self.assertEqual(self.argv_count(), 4)

    def test_stderr_does_not_reclassify_successful_turns(self):
        ok = self.start_perch("p-stderr-auth-ok", FAKE_MODE="stderr_auth_ok")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual(self.reg("p-stderr-auth-ok")["state"], "DONE")
        self.assertEqual(self.reg("p-stderr-auth-ok")["report"]["answer"], "successful turn")
        nocontract = self.start_perch("p-stderr-usage", FAKE_MODE="stderr_usage_ok_nocontract")
        self.assertEqual(nocontract.returncode, 1, nocontract.stdout + nocontract.stderr)
        self.assertEqual(self.reg("p-stderr-usage")["state"], "FAIL")
        self.assertIn("no result contract", self.reg("p-stderr-usage")["report"]["answer"])
        self.assertNotIn("quota", self.reg("p-stderr-usage")["report"]["answer"].lower())
        self.assertNotIn("FALLBACK", ok.stdout + nocontract.stdout)
        self.assertEqual(self.argv_count(), 2)

    def test_failure_with_stderr_limit_is_quota(self):
        res = self.start_perch("p-failure-stderr", FAKE_MODE="failure_stderr_limit")
        self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
        self.assertEqual(self.reg("p-failure-stderr")["state"], "QUOTA")
        self.assertIn("turn_rate_limited", self.reg("p-failure-stderr")["report"]["answer"])
        self.assertNotIn("FALLBACK", res.stdout)
        self.assertEqual(self.argv_count(), 1)

    def test_perch_default_stall_is_900(self):
        mod = load_module()
        self.assertEqual(mod.default_stall("perch-deepseek"), 900)
        self.assertEqual(mod.default_stall("deepseek"), 180)
        self.assertEqual(mod.default_stall("sol"), 300)
        res = self.start_perch("p-stall")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.reg("p-stall")["runs"][0]["stall"], 900)

    def test_deepseek_free_off_falls_to_paid_not_perch(self):
        self.switches("deepseek-free", "hydrogen", "luna")
        res = self.task("start", "opencode", "free-off", self.brief(), "--row", "mechanical",
                        "--model", "opencode/deepseek-v4.1-flash-free", "--cwd", str(self.work))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("FALLBACK: deepseek-free off -> deepseek", res.stdout)
        reg = self.reg("free-off")
        self.assertEqual((reg["alias"], reg["worker"], reg["model"]),
                         ("deepseek", "opencode", self.PAID))
        self.assertEqual(self.argv()[4], self.PAID)

    def test_perch_limit_only_uses_structured_failure_signals(self):
        mod = load_module()
        self.assertIsNotNone(mod.perch_limit(
            {"ok": False, "status": "failed", "error": "turn_rate_limited"}, ""))
        self.assertIsNotNone(mod.perch_limit({"ok": False, "errorCode": "turn_rate_limited"}, ""))
        self.assertIsNotNone(mod.perch_limit(
            {"ok": False, "segments": [{"kind": "error", "limit": {"code": "turn_rate_limited"}}]}, ""))
        self.assertIsNotNone(mod.perch_limit(None, "turn_rate_limited: wait"))
        self.assertIsNone(mod.perch_limit(
            {"ok": True, "assistantText": "STATE: DONE\nANSWER: the rate limit doc"}, ""))
        self.assertIsNone(mod.perch_limit(
            {"ok": True, "assistantText": "STATE: DONE\nANSWER: allowance handling"}, "rate limit"))
        self.assertIsNone(mod.perch_limit({"ok": False, "error": "model failed after edits"}, ""))
        self.assertIsNone(mod.perch_limit(None, "Unknown model: nope. Valid models: deepseek-flash-starter"))


class StatusTests(RunnerCase):
    def test_status_timing_line_when_data_present(self):
        self.write_reg("timed", runs=[{"first_output_s": 1.2, "started": "2026-11-17T00:00:00+00:00",
                                       "finished": "2026-11-17T00:00:04+00:00"}])
        out = self.task("status", "timed").stdout.splitlines()
        self.assertEqual(out.count("timing: first output 1.2s, total 4.0s"), 1)

    def test_status_timing_line_omitted_when_missing(self):
        self.write_reg("untimed")
        self.assertNotIn("timing:", self.task("status", "untimed").stdout)

    def test_status_exit_codes(self):
        for state, code in (("DONE", 0), ("NEED-INPUT", 3), ("FAIL", 1), ("QUOTA", 4)):
            self.write_reg("s-" + state.lower(), state=state,
                           report={"state": state, "answer": "a", "evidence": "e", "missing": "m"})
            res = self.task("status", "s-" + state.lower())
            self.assertEqual(res.returncode, code, state)
            self.assertTrue(res.stdout.splitlines()[0].startswith(
                "codex - luna - s-" + state.lower() + " | "), res.stdout)
            self.assertIn("| " + state, res.stdout.splitlines()[0])

    def test_status_truncates_to_ten_lines(self):
        answer = "\n".join("line%d" % i for i in range(30))
        self.write_reg("s1", report={"state": "DONE", "answer": answer, "evidence": "e", "missing": ""},
                       raw_log="/x/raw.log")
        out = self.task("status", "s1").stdout.splitlines()
        self.assertEqual(out[0], "codex - luna - s1 | DONE")
        self.assertEqual(out[-1], "RAW: /x/raw.log")
        self.assertLessEqual(len(out), 12)

    def test_status_dead_runner_is_fail(self):
        self.write_reg("s2", state="RUNNING", pid=2 ** 22 + 12345)
        res = self.task("status", "s2")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(self.reg("s2")["state"], "FAIL")

    def test_list(self):
        self.write_reg("a1")
        self.write_reg("b1", state="FAIL")
        out = self.task("list").stdout
        self.assertIn("codex - luna - a1 | DONE", out)
        self.assertIn("codex - luna - b1 | FAIL", out)


class ChatAnywhereWorkerTests(RunnerCase):
    KEY = "ca-test-marker"

    def ca_env(self, **extra):
        env = {"TASK_CHATANYWHERE_KEY_CMD": "printf " + self.KEY}
        env.update(extra)
        return env

    def start_ca(self, tid, *args, **extra):
        return self.start("codex", tid, "--model", "chatanywhere", *args, **self.ca_env(**extra))

    def test_alias_is_in_row_tails_only_and_not_owner_only(self):
        mod = load_module()
        self.assertEqual(("codex", "gpt-6-luna"), mod.MODELS["chatanywhere"])
        self.assertNotIn("chatanywhere", mod.OWNER_ONLY)
        self.assertEqual("code", mod.ROW_OF_ALIAS["chatanywhere"])
        for row in ("code", "mechanical", "search", "inventory"):
            self.assertEqual("chatanywhere", mod.ROWS[row][-1], row)
        self.assertNotIn("chatanywhere", mod.ROWS["hard"])
        self.assertEqual("luna", mod.model_alias("codex", "gpt-6-luna"))

    def test_start_uses_provider_flags_and_key_only_in_child_env(self):
        result = self.start_ca("ca-1")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        argv = self.argv(1)
        joined = " ".join(argv)
        self.assertNotIn(self.KEY, joined)
        self.assertIn("model_auto_compact_token_limit=250000", argv)
        self.assertIn("model_context_window=272000", argv)
        self.assertIn('model_provider="chatanywhere"', argv)
        self.assertIn('model_providers.chatanywhere.base_url="https://api.chatanywhere.org/v1"', argv)
        self.assertIn('model_providers.chatanywhere.env_key="CHATANYWHERE_API_KEY"', argv)
        self.assertIn('model_providers.chatanywhere.wire_api="responses"', argv)
        self.assertEqual("gpt-6-luna", argv[argv.index("-m") + 1])
        self.assertEqual(self.KEY, (self.fake_dir / "key.1").read_text())
        self.assertEqual("chatanywhere", self.reg("ca-1")["alias"])
        status = self.task("status", "ca-1").stdout
        log_text = (self.logs / "ca-1.log").read_text()
        for text in (result.stdout, (self.state / "ca-1.json").read_text(), log_text, status):
            self.assertNotIn(self.KEY, text)

    def test_plain_luna_has_no_provider_flags_and_no_key(self):
        result = self.start("codex", "luna-plain", "--model", "luna", **self.ca_env())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('model_provider="chatanywhere"', self.argv(1))
        self.assertEqual("", (self.fake_dir / "key.1").read_text())

    def test_missing_key_fails_without_starting_any_worker(self):
        result = self.start("codex", "ca-nokey", "--model", "chatanywhere",
                            TASK_CHATANYWHERE_KEY_CMD="/usr/bin/false")
        self.assertEqual(1, result.returncode)
        self.assertIn("chatanywhere key unavailable", result.stdout)
        self.assertEqual([], list(self.fake_dir.glob("argv.*")))

    def test_base_url_override_is_validated(self):
        ok = self.start_ca("ca-url", CHATANYWHERE_BASE_URL="https://api.chatanywhere.tech/v1")
        self.assertEqual(0, ok.returncode, ok.stdout + ok.stderr)
        self.assertIn('model_providers.chatanywhere.base_url="https://api.chatanywhere.tech/v1"', self.argv(1))
        bad = self.start_ca("ca-badurl", CHATANYWHERE_BASE_URL='http://x"; evil')
        self.assertEqual(0, bad.returncode, bad.stdout + bad.stderr)
        self.assertIn('model_providers.chatanywhere.base_url="https://api.chatanywhere.org/v1"', self.argv(2))

    def test_reply_keeps_provider_flags_and_key(self):
        self.assertEqual(0, self.start_ca("ca-reply").returncode)
        reply = self.task("reply", "ca-reply", "more", **self.ca_env())
        self.assertEqual(0, reply.returncode, reply.stdout + reply.stderr)
        argv = self.argv(2)
        self.assertEqual("resume", argv[1])
        self.assertIn('model_provider="chatanywhere"', argv)
        self.assertEqual(self.KEY, (self.fake_dir / "key.2").read_text())

    def test_codex_switch_does_not_control_chatanywhere(self):
        self.switches("codex", "opencode", "gemini")
        for row in ("code", "mechanical", "search"):
            result = self.task("resolve", row)
            self.assertEqual("codex - chatanywhere\n", result.stdout, row)
        self.assertEqual(0, self.task("on", "codex").returncode)
        self.assertEqual(0, self.task("off", "chatanywhere").returncode)
        blocked = self.task("resolve", "code")
        self.assertEqual("codex - luna\n", blocked.stdout)
        self.switches("luna", "sol", "deepseek")
        self.assertIn("all workers for row code are off", self.task("resolve", "code").stdout + self.task("resolve", "code").stderr)

    def test_quota_skips_alias_for_the_rest_of_the_utc_day(self):
        self.switches("hydrogen", "luna", "sol", "deepseek")
        self.assertEqual("codex - chatanywhere\n", self.task("resolve", "code").stdout)
        result = self.start_ca("ca-quota", FAKE_MODE="quota")
        self.assertEqual(4, result.returncode, result.stdout + result.stderr)
        blocked = self.task("resolve", "code")
        self.assertNotEqual(0, blocked.returncode)
        mod = load_module()
        old = os.environ.get("TASK_STATE_DIR")
        os.environ["TASK_STATE_DIR"] = str(self.state)
        try:
            self.assertTrue(mod.quota_blocked("chatanywhere"))
            marker = self.state.parent / mod.CHATANYWHERE_QUOTA_FILE
            marker.write_text(json.dumps({"date": "2000-01-01"}))
            self.assertFalse(mod.quota_blocked("chatanywhere"))
        finally:
            if old is None:
                os.environ.pop("TASK_STATE_DIR", None)
            else:
                os.environ["TASK_STATE_DIR"] = old
        self.assertEqual("codex - chatanywhere\n", self.task("resolve", "code").stdout)

    def test_task_test_command_runs_alias_with_low_effort(self):
        result = self.task("test", "chatanywhere", FAKE_ANSWER="ok", **self.ca_env())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        argv = self.argv(1)
        self.assertIn('model_reasoning_effort="low"', " ".join(argv))
        self.assertIn('model_provider="chatanywhere"', argv)
        self.assertEqual(self.KEY, (self.fake_dir / "key.1").read_text())

    def test_workers_list_groups_models_under_their_cli(self):
        lines = self.task("workers").stdout.splitlines()
        self.assertEqual("codex: on", lines[0])
        self.assertEqual(["  luna: on", "  sol: on"], lines[1:3])
        self.assertTrue(lines[3].startswith("  chatanywhere: on"))
        self.assertIn("opencode: on", lines)
        self.assertIn("  deepseek-free: on", lines)
        self.assertIn("relay: on", lines)

    def test_task_test_label_ignores_codex_switch(self):
        self.switches("codex")
        result = self.task("test", "chatanywhere", FAKE_ANSWER="ok", **self.ca_env())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("(switch: off)", result.stdout)


class OptionalScriptTests(RunnerCase):
    def _copy_task_only(self):
        copy_dir = Path(self.tmp.name) / "isolated-scripts"
        copy_dir.mkdir(exist_ok=True)
        task_copy = copy_dir / "task.py"
        task_copy.write_bytes(SCRIPT.read_bytes())
        return copy_dir, task_copy

    def test_usage_without_daily_usage_script_fails_cleanly(self):
        _, task_copy = self._copy_task_only()
        result = subprocess.run([sys.executable, str(task_copy), "usage"],
                                env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(2, result.returncode)
        self.assertEqual("task usage: usage reports are not part of this plugin\n", result.stderr)
        self.assertEqual("", result.stdout)

    def test_usage_finds_sibling_daily_usage_script(self):
        copy_dir, task_copy = self._copy_task_only()
        (copy_dir / "daily-usage.py").write_text("print('from-sibling')\n")
        home_scripts = Path(self.env["HOME"]) / ".claude" / "scripts"
        home_scripts.mkdir(parents=True)
        (home_scripts / "daily-usage.py").write_text("print('from-home')\n")
        result = subprocess.run([sys.executable, str(task_copy), "usage"],
                                env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("from-sibling\n", result.stdout)

    def test_usage_falls_back_to_home_claude_scripts(self):
        _, task_copy = self._copy_task_only()
        home_scripts = Path(self.env["HOME"]) / ".claude" / "scripts"
        home_scripts.mkdir(parents=True)
        (home_scripts / "daily-usage.py").write_text("print('from-home')\n")
        result = subprocess.run([sys.executable, str(task_copy), "usage"],
                                env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("from-home\n", result.stdout)

    def test_clean_sessions_reaches_clean_sessions_script(self):
        result = self.task("clean", "sessions")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Nothing to clean", result.stdout)


if __name__ == "__main__":
    unittest.main()
