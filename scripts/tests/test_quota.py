import importlib.util
import datetime
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

SCRIPT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("quota", str(SCRIPT / "quota.py"))
quota = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quota)


def format_in_zone(timestamp, zone):
    return datetime.datetime.fromtimestamp(timestamp, ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")


class QuotaTest(unittest.TestCase):
    def setUp(self):
        # Provider CLI calls must be explicitly mocked by each test.
        offline = mock.patch.object(quota, "subprocess", mock.Mock(wraps=subprocess))
        offline.start().run.side_effect = AssertionError("external CLI must not run")
        self.addCleanup(offline.stop)
        # Never read or write the real ~/.claude/state caches from tests.
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        for name, filename in (("CODEX_CACHE", "codex-quota.json"),
                               ("AGY_CACHE", "agy-quota.json"),
                               ("HOME", "home")):
            patcher = mock.patch.object(quota, name, os.path.join(state.name, filename))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_plain_text_columns_align_and_have_separator(self):
        rows = [
            ("Codex", "luna+sol", "10%", "5h", "soon", "on", "x"),
            ("Claude", "Claude Code", "100%", "weekly", "tomorrow", "off", "long source"),
        ]
        output = quota.render_table(rows, {"Claude Code": False})
        self.assertIn("Worker", output)
        lines = output.splitlines()
        self.assertEqual([quota.display_width(line) for line in lines],
                         [quota.display_width(lines[0])] * len(lines))
        self.assertIn("+", lines[1])

    def test_codex_nested_event(self):
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "session.jsonl"
            path.write_text(json.dumps({"payload": {"rate_limits": {
                "primary": {"used_percent": 42, "window_minutes": 300, "resets_at": 1800000000},
                "secondary": None}}}) + "\n")
            self.assertEqual(42, quota.codex_row(root)["primary"]["used_percent"])

    def test_claude_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "claude-rate.json"
            path.write_text(json.dumps({"timestamp": "2026-10-15T00:00:00+00:00", "rate_limits": {
                "five_hour": {"used_percentage": 25, "resets_at": 1800000000},
                "seven_day": {"used_percentage": 70, "resets_at": 1800000000}}}))
            rows = quota.claude_row(str(path))
            self.assertEqual(2, len(rows))
            self.assertEqual(["75%", "30%"], [row[2] for row in rows])

    def test_log_parser_and_empty_sources(self):
        with tempfile.TemporaryDirectory() as root:
            log = pathlib.Path(root) / "runner.log"
            log.write_text(json.dumps({"event": "assistant_message", "text": "added quota parser tests"}) + "\n")
            self.assertEqual([], quota.log_hits([str(log)], ("quota",), "opencode"))
            with mock.patch.object(quota, "agy_usage", side_effect=ValueError("offline")), \
                    mock.patch.object(quota, "claude_usage_rows", return_value=[
                        ("Claude", "Claude Code", "n/a (usage unavailable)", "n/a", "n/a", "on", "n/a")]), \
                    mock.patch.object(quota, "perch_usage", side_effect=ValueError("offline")), \
                    mock.patch.object(quota.subprocess, "run", side_effect=AssertionError("unexpected CLI")):
                rows, _ = quota.render(root=str(pathlib.Path(root) / "missing"),
                                       state_path=str(pathlib.Path(root) / "missing-state"),
                                       logs=[], gemini_log=str(pathlib.Path(root) / "missing-gemini"), switches={})
            self.assertEqual(8, len(rows))
            self.assertIn("no quota error seen", rows[-1][-1])
            free = [row for row in rows if row[0] == "OpenCode free"]
            self.assertEqual("free, no published cap", free[0][-1])

    def test_agy_quota_fixture(self):
        fixture = SCRIPT / "tests/fixtures/agy-quota.jsonl"
        rows = quota.parse_agy_usage(fixture.read_text())
        self.assertEqual(4, len(rows))
        self.assertEqual(["75%", "50%", "100%", "90%"], [row[2] for row in rows])
        self.assertEqual("agy-3p", rows[2][0])

    def test_agy_current_stream_json_fixture(self):
        fixture = SCRIPT / "tests/fixtures/agy-quota-live.jsonl"
        rows = quota.parse_agy_usage(fixture.read_text())
        self.assertEqual(["Gemini", "Gemini", "agy-3p", "agy-3p"], [row[0] for row in rows])
        self.assertEqual(["54%", "54%", "1%", "100%"], [row[2] for row in rows])
        self.assertEqual(["weekly", "5h", "weekly", "5h"], [row[3] for row in rows])

    def test_model_labels_switches_and_windows(self):
        self.assertEqual("off", quota.switch_state("deepseek", {"deepseek": False}))
        self.assertEqual("on", quota.switch_state("luna+sol", {"luna": False, "sol": True}))
        self.assertEqual("weekly", quota.codex_window(10080))
        self.assertEqual("5h", quota.codex_window(300))

    def test_relay_row_is_unmetered_and_switchable(self):
        switches = {"relay": False}
        with mock.patch.object(quota, "codex_rows", return_value=[]), \
                mock.patch.object(quota, "agy_usage", return_value=[]), \
                mock.patch.object(quota, "claude_usage_rows", return_value=[]), \
                mock.patch.object(quota, "perch_usage", return_value={}), \
                mock.patch.object(quota.subprocess, "run",
                                  side_effect=AssertionError("external CLI must not run")) as run:
            rows, _ = quota.render(root="/nonexistent", state_path="/nonexistent", logs=[],
                                   gemini_log="/nonexistent", switches=switches)
            run.assert_called_once_with(["opencode", "stats"], capture_output=True, text=True, timeout=20)
        relay = next(row for row in rows if row[0] == "Relay")
        self.assertEqual(("Relay", "relay-sonnet", "n/a", "n/a", "n/a", "off",
                          "n/a (no quota API)"), relay)
        table = quota.render_table([relay], switches)
        self.assertIn("Relay", table)
        self.assertIn("off", table)

    def test_missing_statusline_has_actionable_fallback_failure(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(quota, "agy_usage", side_effect=ValueError("offline")), \
                    mock.patch.object(quota, "claude_usage_rows", return_value=[
                        ("Claude", "Claude Code", "n/a (usage unavailable)", "n/a", "n/a", "on", "n/a")]), \
                    mock.patch.object(quota, "perch_usage", side_effect=ValueError("offline")):
                rows, _ = quota.render(root=str(pathlib.Path(root) / "sessions"),
                                       state_path=str(pathlib.Path(root) / "no-snapshot"), logs=[],
                                       gemini_log=str(pathlib.Path(root) / "no-gemini-log"), switches={})
            claude = next(row for row in rows if row[0] == "Claude")
            self.assertEqual("n/a (usage unavailable)", claude[2])

    def test_claude_usage_parser_sample_lines(self):
        now = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=ZoneInfo("UTC"))
        raw = ("Current session: 32% used · resets Nov 4 at 1:10pm (UTC)\n"
               "Current week (all models): 55% used · resets Nov 5 at 5am (UTC)")
        parsed = quota.parse_claude_usage(raw, now=now)
        self.assertEqual(32, parsed["session"]["percent"])
        self.assertEqual(55, parsed["weekly"]["percent"])
        self.assertEqual("2026-11-04 13:10", format_in_zone(parsed["session"]["resets_at"], "UTC"))
        self.assertEqual("2026-11-05 05:00", format_in_zone(parsed["weekly"]["resets_at"], "UTC"))

    def test_claude_usage_parser_am_without_minutes_and_year(self):
        now = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=ZoneInfo("UTC"))
        raw = ("Current session: 8% used · resets Nov 4, 2026 at 1am (UTC)\n"
               "Current week (all models): 12% used · resets Nov 5, 2026 at 5am (UTC)")
        parsed = quota.parse_claude_usage(raw, now=now)
        self.assertEqual("2026-11-04 01:00", format_in_zone(parsed["session"]["resets_at"], "UTC"))

    def test_claude_usage_parser_requires_both_lines(self):
        with self.assertRaises(ValueError):
            quota.parse_claude_usage("Current session: 32% used · resets 1:10pm")

    def test_claude_usage_parser_bare_times(self):
        now = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=ZoneInfo("UTC"))
        raw = ("Current session: 20% used · resets 1:10pm (UTC)\n"
               "Current week (all models): 40% used · resets 5am (UTC)")
        parsed = quota.parse_claude_usage(raw, now=now)
        self.assertEqual("2026-11-03 13:10", format_in_zone(parsed["session"]["resets_at"], "UTC"))
        self.assertEqual("2026-11-04 05:00", format_in_zone(parsed["weekly"]["resets_at"], "UTC"))

    def test_claude_usage_rejects_unknown_timezone(self):
        raw = ("Current session: 32% used · resets Nov 4 at 1:10pm (Eastern Standard Time)\n"
               "Current week (all models): 55% used · resets Nov 5 at 5am (Eastern Standard Time)")
        with self.assertRaises(ValueError):
            quota.parse_claude_usage(raw, now=datetime.datetime(2026, 11, 3, tzinfo=ZoneInfo("UTC")))

    def test_claude_usage_handles_feb_29_next_occurrence(self):
        now = datetime.datetime(2028, 2, 28, 12, 0, tzinfo=ZoneInfo("UTC"))
        raw = ("Current session: 32% used · resets Feb 29 at 1am (UTC)\n"
               "Current week (all models): 55% used · resets Mar 1 at 5am (UTC)")
        parsed = quota.parse_claude_usage(raw, now=now)
        self.assertEqual("2028-02-29 01:00", format_in_zone(parsed["session"]["resets_at"], "UTC"))

    def test_claude_usage_command_fallbacks_and_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            state_path = pathlib.Path(root) / "claude-rate.json"
            cache_path = pathlib.Path(root) / "claude-usage-cache.json"
            state_path.write_text(json.dumps({
                "timestamp": datetime.datetime.now(datetime.timezone.utc)
                             .isoformat().replace("+00:00", "Z"),
                "rate_limits": {
                    "five_hour": {"used_percentage": 21, "resets_at": 1800000000},
                    "seven_day": {"used_percentage": 48, "resets_at": 1800000000}}}))
            with mock.patch.object(quota.subprocess, "run",
                                   side_effect=subprocess.TimeoutExpired("claude", 20)) as run:
                rows = quota.claude_usage_rows(str(state_path), str(cache_path))
            run.assert_called_once_with(["claude", "-p", "/usage"], cwd=quota.HOME,
                                        capture_output=True, text=True, timeout=20)
            self.assertEqual(["79%", "52%"], [row[2] for row in rows])
            with mock.patch.object(quota.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
                rows = quota.claude_usage_rows(str(state_path), str(cache_path))
            self.assertEqual(["79%", "52%"], [row[2] for row in rows])
            state_path.unlink()
            with mock.patch.object(quota.subprocess, "run", side_effect=OSError("unavailable")):
                rows = quota.claude_usage_rows(str(state_path), str(cache_path))
            self.assertEqual("n/a (usage unavailable)", rows[0][2])

    def test_claude_usage_uses_stale_statusline_with_age(self):
        with tempfile.TemporaryDirectory() as root:
            state_path = pathlib.Path(root) / "claude-rate.json"
            state_path.write_text(json.dumps({"rate_limits": {
                "five_hour": {"used_percentage": 21}, "seven_day": {"used_percentage": 48}}}))
            old = time.time() - 16 * 60
            os.utime(str(state_path), (old, old))
            with mock.patch.object(quota.subprocess, "run", side_effect=OSError("unavailable")):
                rows = quota.claude_usage_rows(str(state_path), str(pathlib.Path(root) / "cache.json"))
            self.assertTrue(rows[0][2].startswith("79%"))
            self.assertIn("16m ago", rows[0][2])

    def test_claude_usage_never_na_when_only_an_old_cache_exists(self):
        with tempfile.TemporaryDirectory() as root:
            cache_path = pathlib.Path(root) / "cache.json"
            cache_path.write_text(json.dumps({"at": 1000, "usage": {
                "session": {"percent": 30, "resets_at": 1800000000},
                "weekly": {"percent": 55, "resets_at": 1800000000}}}))
            with mock.patch.object(quota.subprocess, "run", side_effect=OSError("unavailable")):
                rows = quota.claude_usage_rows(str(pathlib.Path(root) / "missing.json"),
                                               str(cache_path), now=1000 + 3 * 3600)
            self.assertEqual(["70% · 3h ago", "45% · 3h ago"], [row[2] for row in rows])

    def test_claude_usage_marks_a_window_reset_after_its_reset_time(self):
        with tempfile.TemporaryDirectory() as root:
            state_path = pathlib.Path(root) / "claude-rate.json"
            state_path.write_text(json.dumps({"rate_limits": {
                "five_hour": {"used_percentage": 21, "resets_at": 1000},
                "seven_day": {"used_percentage": 48, "resets_at": 1800000000}}}))
            rows = quota.claude_row(str(state_path), now=2000)
            self.assertEqual(["100% (reset)", "52%"], [row[2] for row in rows])

    def test_claude_usage_cache_hit_skips_subprocess(self):
        with tempfile.TemporaryDirectory() as root:
            cache_path = pathlib.Path(root) / "claude-usage-cache.json"
            cache_path.write_text(json.dumps({"at": 1000, "usage": {
                "session": {"percent": 30, "resets_at": 1800000000},
                "weekly": {"percent": 55, "resets_at": 1800100000}}}))
            with mock.patch.object(quota.subprocess, "run", side_effect=AssertionError("must use cache")):
                rows = quota.claude_usage_rows(cache_path=str(cache_path), now=1001)
            self.assertEqual(["70%", "45%"], [row[2] for row in rows])

    def test_claude_usage_cache_is_stale_after_either_reset(self):
        with tempfile.TemporaryDirectory() as root:
            cache_path = pathlib.Path(root) / "claude-usage-cache.json"
            cache_path.write_text(json.dumps({"at": 1000, "usage": {
                "session": {"percent": 30, "resets_at": 1005},
                "weekly": {"percent": 55, "resets_at": 5000}}}))
            output = ("Current session: 31% used · resets Nov 5, 2099 at 1pm (UTC)\n"
                      "Current week (all models): 56% used · resets Nov 6, 2099 at 5am (UTC)")
            result = mock.Mock(returncode=0, stdout=output)
            with mock.patch.object(quota.subprocess, "run", return_value=result) as run:
                rows = quota.claude_usage_rows(cache_path=str(cache_path), now=1010)
            run.assert_called_once()
            self.assertEqual(["69%", "44%"], [row[2] for row in rows])
            self.assertFalse(list(pathlib.Path(root).glob(".claude-usage-cache.*")))

    def test_malformed_claude_usage_cache_is_ignored(self):
        invalid_caches = (
            "not-json",
            json.dumps({"at": 1000}),
            json.dumps({"at": 1000, "usage": {
                "session": {"percent": 30, "resets_at": 2000},
                "weekly": {"percent": 55}}}),
            json.dumps({"at": 1000, "usage": {
                "session": {"percent": 30},
                "weekly": {"percent": 55, "resets_at": 2000}}}),
        )
        output = ("Current session: 31% used · resets Nov 5, 2099 at 1pm (UTC)\n"
                  "Current week (all models): 56% used · resets Nov 6, 2099 at 5am (UTC)")
        for invalid_cache in invalid_caches:
            with self.subTest(cache=invalid_cache), tempfile.TemporaryDirectory() as root:
                cache_path = pathlib.Path(root) / "claude-usage-cache.json"
                cache_path.write_text(invalid_cache)
                with mock.patch.object(quota.subprocess, "run",
                                       return_value=mock.Mock(returncode=0, stdout=output)) as run:
                    rows = quota.claude_usage_rows(cache_path=str(cache_path), now=1001)
                run.assert_called_once()
                self.assertEqual(["69%", "44%"], [row[2] for row in rows])

    def test_deadline_and_error_event_attribution(self):
        deadline = quota.Deadline(0.001)
        import time
        time.sleep(0.003)
        with self.assertRaises(TimeoutError):
            deadline.remaining()
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "task.log"
            path.write_text("\n".join((
                json.dumps({"event": "assistant_message", "text": "quota"}),
                json.dumps({"event": "error", "worker": "gemini", "error": "429 quota"}),
                json.dumps({"event": "error", "worker": "opencode", "error": "429 quota"}))) + "\n")
            self.assertEqual(1, len(quota.log_hits([str(path)], ("429", "quota"), "opencode")))
            self.assertEqual(1, len(quota.log_hits([str(path)], ("429", "quota"), "gemini")))

    def test_claude_switch_independent_of_codex(self):
        self.assertEqual("on", quota.switch_state("Claude Code", {"luna": False, "sol": False}))
        self.assertEqual("off", quota.switch_state("Claude Code", {"claude": False, "luna": True}))

    def test_aligned_table_plain_percent_without_bar(self):
        rows = [("Codex", "luna+sol", "10%", "5h", "2026-10-15 00:00", "on", "session"),
                ("Codex", "luna+sol", "80%", "weekly", "2026-10-16 00:00", "on", "session")]
        output = quota.render_table(rows, {"luna": True, "sol": True})
        self.assertNotIn("█", output)
        self.assertNotIn("░", output)
        self.assertIn("80%", output)
        self.assertIn("10%", output)
        self.assertIn("Worker", output)
        self.assertIn("Weekly left", output)
        self.assertIn("🟡 ≤20%", output)
        lines = output.splitlines()
        self.assertTrue(all(quota.display_width(line) == quota.display_width(lines[0]) for line in lines))

    def test_codex_reading_skips_credit_only_events_and_keeps_age(self):
        with tempfile.TemporaryDirectory() as root:
            newer = pathlib.Path(root) / "newer.jsonl"
            credit_only = {"timestamp": "2026-11-10T00:00:00Z", "payload": {"rate_limits": {
                "primary": None, "secondary": None,
                "rate_limit_reached_type": "workspace_member_credits_depleted"}}}
            newer.write_text(json.dumps(credit_only) + "\n")
            older = pathlib.Path(root) / "older.jsonl"
            old = time.time() - 3 * 3600
            usable = {
                "timestamp": datetime.datetime.fromtimestamp(old, datetime.timezone.utc)
                             .isoformat().replace("+00:00", "Z"),
                "payload": {"rate_limits": {
                    "primary": {"used_percent": 58, "window_minutes": 10080,
                                "resets_at": int(time.time()) + 3600}, "secondary": None}}}
            older.write_text(json.dumps(usable) + "\n")
            os.utime(str(older), (old, old))
            limits, at = quota.codex_reading(root)
            self.assertEqual(58, limits["primary"]["used_percent"])
            self.assertAlmostEqual(old, at, delta=5)
            rows = quota.codex_rows(root, {"luna": True, "sol": True})
            self.assertEqual("42% · 3h ago", rows[0][2])
            self.assertEqual("weekly", rows[0][3])

    def test_codex_usage_falls_back_to_cached_reading(self):
        with tempfile.TemporaryDirectory() as root:
            cache = pathlib.Path(root) / "codex-quota.json"
            at = time.time() - 3 * 3600
            cached = {"at": at, "limits": {"primary": {
                "used_percent": 58, "window_minutes": 10080,
                "resets_at": int(time.time()) + 3600}, "secondary": None}}
            cache.write_text(json.dumps(cached))
            limits, reading_at = quota.codex_usage(str(pathlib.Path(root) / "sessions"),
                                                   cache_path=str(cache))
            self.assertEqual(58, limits["primary"]["used_percent"])
            self.assertAlmostEqual(at, reading_at, delta=1)
            rows = quota.codex_rows(str(pathlib.Path(root) / "sessions"),
                                    {"luna": True, "sol": True}, cache_path=str(cache))
            self.assertEqual("42% · 3h ago", rows[0][2])

    def test_codex_reset_window_is_marked_after_its_reset_time(self):
        with tempfile.TemporaryDirectory() as root:
            cache = pathlib.Path(root) / "codex-quota.json"
            cached = {"at": time.time() - 5 * 86400, "limits": {"primary": {
                "used_percent": 100, "window_minutes": 10080,
                "resets_at": 1000}, "secondary": None}}
            cache.write_text(json.dumps(cached))
            rows = quota.codex_rows(str(pathlib.Path(root) / "missing"),
                                    {"luna": True}, cache_path=str(cache))
            self.assertEqual("100% (reset)", rows[0][2])

    def test_codex_real_cache_keeps_age_before_reset(self):
        data = json.loads((SCRIPT / "tests/fixtures/codex-quota-cache.json").read_text())
        with tempfile.TemporaryDirectory() as root:
            cache = pathlib.Path(root) / "codex-quota.json"
            cache.write_text(json.dumps(data))
            before_reset = data["limits"]["primary"]["resets_at"] - 19 * 3600
            rows = quota.codex_rows(str(pathlib.Path(root) / "missing"),
                                    {"luna": True}, cache_path=str(cache),
                                    now=before_reset)
            self.assertEqual("0% (stale) · 4d ago", rows[0][2])

    def test_codex_real_cache_shows_reset_after_reset_time(self):
        data = json.loads((SCRIPT / "tests/fixtures/codex-quota-cache.json").read_text())
        with tempfile.TemporaryDirectory() as root:
            cache = pathlib.Path(root) / "codex-quota.json"
            cache.write_text(json.dumps(data))
            after_reset = data["limits"]["primary"]["resets_at"] + 1
            rows = quota.codex_rows(str(pathlib.Path(root) / "missing"),
                                    {"luna": True}, cache_path=str(cache),
                                    now=after_reset)
            self.assertEqual("100% (reset)", rows[0][2])

    def test_opencode_token_total_is_compact(self):
        self.assertEqual("5.4B tok", quota.parse_opencode_tokens("5.4b tokens"))
        self.assertEqual("5.4B tok", quota.parse_opencode_tokens(
            "1.8k prompts · 64.5k steps · 5.4b tokens"))
        self.assertEqual("1.2M tok", quota.parse_opencode_tokens("Total tokens: 1,234,567"))
        with self.assertRaises(ValueError):
            quota.parse_opencode_tokens("no totals here")

    def test_md_is_alias_for_default_layout(self):
        rows = [("Codex", "luna+sol", "10%", "5h", "2026-10-15 00:00", "on", "session")]
        expected = quota.render_table(rows, {"luna": True, "sol": True})
        with mock.patch.object(quota, "render", return_value=(rows, {"luna": True, "sol": True})), \
                mock.patch("builtins.print") as output, mock.patch("sys.argv", ["quota.py"]):
            quota.main()
            plain = output.call_args.args[0]
        with mock.patch.object(quota, "render", return_value=(rows, {"luna": True, "sol": True})), \
                mock.patch("builtins.print") as output, mock.patch("sys.argv", ["quota.py", "--md"]):
            quota.main()
            md = output.call_args.args[0]
        self.assertEqual(expected, plain)
        self.assertEqual(plain, md)

    def test_log_hits_ignores_non_object_json_lines(self):
        with tempfile.TemporaryDirectory() as root:
            log = pathlib.Path(root) / "runner.log"
            log.write_text('"a bare string"\n' + json.dumps(
                {"event": "error", "worker": "opencode", "error": "429 quota"}) + "\n")
            self.assertEqual(1, len(quota.log_hits([str(log)], ("429",), "opencode")))

    def test_render_opencode_shows_token_total(self):
        stats = mock.Mock(returncode=0, stdout="1.8k prompts · 64.6k steps · 5.5b tokens")
        claude = [("Claude", "Claude Code", "16%", "weekly", "n/a", "on", "x")]
        with mock.patch.object(quota, "agy_usage", side_effect=ValueError("offline")), \
                mock.patch.object(quota, "claude_usage_rows", return_value=claude), \
                mock.patch.object(quota, "perch_usage", side_effect=ValueError("offline")), \
                mock.patch.object(quota.subprocess, "run", return_value=stats):
            rows, _ = quota.render(root="/nonexistent", state_path="/nonexistent", logs=[],
                                   gemini_log="/nonexistent", switches={})
        opencode = next(row for row in rows if row[0] == "OpenCode")
        self.assertEqual("5.5B tok", opencode[2])

    def test_credential_path_is_not_interpreted_as_log(self):
        with tempfile.TemporaryDirectory() as root:
            credential = pathlib.Path(root) / "credential.json"
            credential.write_text(json.dumps({"event": "error", "worker": "opencode", "error": "429 quota"}))
            self.assertEqual([], quota.log_hits([str(credential)], ("429", "quota"), "opencode"))

    def test_perch_usage_parser_rows_and_table(self):
        raw = ("Perch Tokens\n"
               "- Plan: Starter · 20,000 PT / month (up to $20 API usage)\n"
               "- Month  ████░ 9 PT / 20,000 PT · 0%\n"
               "- 5h  12% · used · next usage frees up in 3 h 20 min\n"
               "- Weekly  34% · used · next usage frees up in 2 d 4 h\n"
               "- Activity  3 chats this month · 9 PT in 7 days · 1,200 model tokens\n")
        usage = quota.parse_perch_usage(raw)
        self.assertEqual(9, usage["month"]["used"])
        self.assertEqual(20000, usage["month"]["limit"])
        self.assertEqual(12, usage["five_hour"]["percent"])
        self.assertEqual("frees up in 3 h 20 min", usage["five_hour"]["frees_up"])
        self.assertEqual(34, usage["weekly"]["percent"])
        rows = quota.perch_rows(usage, {"perch-deepseek": True})
        self.assertEqual(["month", "5h", "weekly"], [row[3] for row in rows])
        self.assertEqual("100% · 19,991 PT", rows[0][2])
        self.assertEqual(["88%", "66%"], [row[2] for row in rows[1:]])
        output = quota.render_table(rows, {"perch-deepseek": True})
        self.assertIn("Perch", output)
        self.assertIn("deepseek-flash-starter", output)
        self.assertIn("100% · 19,991 PT", output)
        self.assertIn("88% · 3h20m", output)
        self.assertIn("🟢", output)

    def test_perch_usage_partial_and_drift_never_raises(self):
        self.assertIsNone(quota.parse_perch_usage("")["month"])
        self.assertIsNone(quota.parse_perch_usage("Perch Tokens\n- Plan: Starter")["month"])
        no_percent = quota.parse_perch_usage("- Month 20,000 / 20,000 PT")
        self.assertEqual(20000, no_percent["month"]["used"])
        self.assertEqual(20000, no_percent["month"]["limit"])
        self.assertEqual(100, no_percent["month"]["percent"])
        partial = quota.parse_perch_usage("- Month 9 PT / 20,000 PT\n- 5h 12% · used")
        self.assertIsNotNone(partial["month"])
        self.assertIsNotNone(partial["five_hour"])
        self.assertIsNone(partial["weekly"])
        rows = quota.perch_rows(quota.parse_perch_usage("garbage"), {"perch-deepseek": True})
        self.assertEqual("n/a", rows[0][2])
        self.assertIn("Perch", quota.render_table(rows, {"perch-deepseek": True}))

    def test_perch_partial_usage_renders_without_crash(self):
        usage = {"month": None, "five_hour": {"percent": 12, "frees_up": None}, "weekly": None}
        rows = quota.perch_rows(usage, {"perch-deepseek": True})
        output = quota.render_table(rows, {"perch-deepseek": True})
        self.assertIn("88%", output)
        self.assertIn("Perch", output)

    def test_perch_spent_month_is_red_and_shows_budget(self):
        usage = {"month": {"used": 20000, "limit": 20000, "percent": 100},
                 "five_hour": {"percent": 4, "frees_up": "frees up in 2h"},
                 "weekly": {"percent": 4, "frees_up": "frees up in 6d"}}
        rows = quota.perch_rows(usage, {"perch-deepseek": True})
        output = quota.render_table(rows, {"perch-deepseek": True})
        self.assertIn("0% · 0 PT", output)
        self.assertIn("🔴 limit", output)
        self.assertNotIn("🟢", output)

    def test_perch_month_is_not_red_while_points_remain(self):
        usage = {"month": {"used": 19999, "limit": 20000}, "five_hour": None, "weekly": None}
        rows = quota.perch_rows(usage, {"perch-deepseek": True})
        output = quota.render_table(rows, {"perch-deepseek": True})
        self.assertIn("1% · 1 PT", output)
        self.assertNotIn("🔴 limit", output)

    def test_perch_parser_accepts_month_colon(self):
        usage = quota.parse_perch_usage("- Month: 9 PT / 20,000 PT · 0%")
        self.assertIsNotNone(usage["month"])
        self.assertEqual(9, usage["month"]["used"])
        self.assertEqual(20000, usage["month"]["limit"])
        rows = quota.perch_rows(usage, {"perch-deepseek": True})
        self.assertEqual("100% · 19,991 PT", rows[0][2])

    def test_perch_parser_accepts_lines_without_bullet(self):
        raw = ("Month 9 PT / 20,000 PT · 0%\n"
               "5h 12% · used · next usage frees up in 3 h 20 min\n"
               "Weekly 34% · used · next usage frees up in 2 d 4 h\n")
        usage = quota.parse_perch_usage(raw)
        self.assertEqual(9, usage["month"]["used"])
        self.assertEqual(20000, usage["month"]["limit"])
        self.assertEqual(12, usage["five_hour"]["percent"])
        self.assertEqual(34, usage["weekly"]["percent"])
        rows = quota.perch_rows(usage, {"perch-deepseek": True})
        self.assertEqual(["month", "5h", "weekly"], [row[3] for row in rows])

    def test_perch_parser_never_raises_on_bad_input(self):
        for raw in (None, 42, [], {}, "garbage", "- Month", "- Month:", "- Plan: Starter"):
            usage = quota.parse_perch_usage(raw)
            self.assertIsNone(usage["month"], raw)
            self.assertIsNone(usage["five_hour"], raw)
            self.assertIsNone(usage["weekly"], raw)
        rows = quota.perch_rows(quota.parse_perch_usage(None), {"perch-deepseek": True})
        self.assertEqual("n/a", rows[0][2])
        self.assertIn("Perch", quota.render_table(rows, {"perch-deepseek": True}))

    def test_perch_switch_is_independent(self):
        self.assertEqual("off", quota.switch_state("perch-deepseek", {"perch-deepseek": False}))
        self.assertEqual("on", quota.switch_state("perch-deepseek", {"perch-deepseek": True}))
        self.assertEqual("on", quota.switch_state("deepseek",
                                                  {"perch-deepseek": False, "deepseek": True}))
        self.assertEqual("on", quota.switch_state("deepseek-free",
                                                  {"perch-deepseek": False, "deepseek-free": True}))

    def test_perch_usage_cache_skips_repeated_calls(self):
        with tempfile.TemporaryDirectory() as root:
            cache = pathlib.Path(root) / "perch-quota.json"
            payload = json.dumps({"ok": True, "assistantText": "- Month  9 PT / 20,000 PT · 0%"})
            output = mock.Mock(returncode=0, stdout=payload)
            with mock.patch.object(quota.subprocess, "run", return_value=output) as run:
                first = quota.perch_usage(cache_path=str(cache))
            run.assert_called_once_with(["perch", "run", "/usage", "--json"],
                                        cwd=quota.tempfile.gettempdir(), capture_output=True,
                                        text=True, timeout=quota.PERCH_TIMEOUT)
            with mock.patch.object(quota.subprocess, "run", side_effect=AssertionError("must use cache")):
                second = quota.perch_usage(cache_path=str(cache))
            self.assertEqual(first, second)

    def test_perch_usage_falls_back_to_last_good_cache_with_age(self):
        with tempfile.TemporaryDirectory() as root:
            cache = pathlib.Path(root) / "perch-quota.json"
            at = time.time() - 3 * 3600
            cache.write_text(json.dumps({"at": at, "usage": {
                "month": None, "five_hour": {"percent": 12, "frees_up": None},
                "weekly": {"percent": 34, "frees_up": None}}}))
            with mock.patch.object(quota.subprocess, "run", side_effect=OSError("offline")):
                usage = quota.perch_usage(cache_path=str(cache))
            self.assertAlmostEqual(at, usage["cached_at"], delta=1)
            rows = quota.perch_rows(usage, {"perch-deepseek": True})
            self.assertEqual("66% · 3h ago", rows[-1][2])
            self.assertIn("3h ago", quota.render_table(rows, {"perch-deepseek": True}))

    def test_perch_usage_tolerates_progress_before_json(self):
        with tempfile.TemporaryDirectory() as root:
            cache = pathlib.Path(root) / "perch-quota.json"
            payload = ('warming up\n{"ok": true, "assistantText": '
                       '"- Month 9 PT / 20,000 PT · 0%"}')
            with mock.patch.object(quota.subprocess, "run",
                                   return_value=mock.Mock(returncode=0, stdout=payload)):
                usage = quota.perch_usage(cache_path=str(cache))
            self.assertEqual(9, usage["month"]["used"])

    def test_perch_usage_raises_when_live_and_cache_are_empty(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(quota.subprocess, "run", side_effect=OSError("offline")):
                with self.assertRaises(OSError):
                    quota.perch_usage(cache_path=str(pathlib.Path(root) / "missing.json"))

    def test_render_includes_three_perch_rows(self):
        usage = {"month": {"used": 9, "limit": 20000, "percent": 0},
                 "five_hour": {"percent": 12, "frees_up": "frees up in 3 h 20 min"},
                 "weekly": {"percent": 34, "frees_up": "frees up in 2 d 4 h"}}
        claude = [("Claude", "Claude Code", "5h 1%", "5h", "n/a", "on", "x")]
        with mock.patch.object(quota, "agy_usage", side_effect=ValueError("offline")), \
                mock.patch.object(quota, "claude_usage_rows", return_value=claude), \
                mock.patch.object(quota, "perch_usage", return_value=usage), \
                mock.patch.object(quota.subprocess, "run",
                                  return_value=mock.Mock(returncode=0, stdout="")):
            rows, switches = quota.render(root="/nonexistent", state_path="/nonexistent", logs=[],
                                          gemini_log="/nonexistent", switches={"perch-deepseek": True})
        perch = [row for row in rows if row[0] == "Perch"]
        self.assertEqual(3, len(perch))
        self.assertIn("Perch", quota.render_table(rows, switches))


if __name__ == "__main__":
    unittest.main()
