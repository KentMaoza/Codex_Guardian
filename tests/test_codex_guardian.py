import argparse
import importlib.util
import json
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "skills" / "codex-guardian" / "scripts" / "codex_guardian.py"


def run_cli(*args, cwd=None, env=None):
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def load_guardian_module():
    spec = importlib.util.spec_from_file_location("codex_guardian_under_test", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_sqlite_log(codex_home: Path, messages: list[str]) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(codex_home / "logs_2.sqlite")
    con.execute("create table logs (ts integer, level text, target text, feedback_log_body text)")
    now = int(time.time())
    for offset, message in enumerate(messages):
        con.execute(
            "insert into logs values (?, ?, ?, ?)",
            (now - offset, "ERROR", "codex-test", message),
        )
    con.commit()
    con.close()


def write_sqlite_log_rows(codex_home: Path, columns: str, rows: list[tuple]) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(codex_home / "logs_2.sqlite")
    con.execute(f"create table logs ({columns})")
    placeholders = ", ".join("?" for _ in rows[0])
    for row in rows:
        con.execute(f"insert into logs values ({placeholders})", row)
    con.commit()
    con.close()


class CodexGuardianTests(unittest.TestCase):
    def test_fixture_corpus_covers_classifier_families(self):
        fixture_path = ROOT / "skills" / "codex-guardian" / "fixtures" / "redacted-real-log-corpus.json"
        corpus = json.loads(fixture_path.read_text(encoding="utf-8"))
        covered = {
            code
            for case in corpus["cases"]
            for code in case.get("expect_matches", [])
        }
        required = {
            "stream_disconnect",
            "websocket_send_failed",
            "websocket_idle_timeout",
            "websocket_closed",
            "broken_pipe",
            "remote_compaction_failed",
            "compact_endpoint_failed",
            "unknown_conversation",
            "mcp_request_timeout",
            "turn_start_timeout",
            "no_progress_loop",
            "responses_websocket_failure",
            "dns_resolution_failed",
            "tls_handshake_failed",
            "connection_reset",
            "request_timeout",
            "auth_session_failed",
        }

        self.assertFalse(required - covered)

    def test_fixture_corpus_self_test_has_coverage_gates(self):
        guardian = load_guardian_module()
        checks = guardian.fixture_corpus_checks()
        by_name = {check["name"]: check for check in checks}

        for name in [
            "fixture corpus covers classifier families",
            "fixture corpus keeps real-log-shaped targets",
            "fixture corpus protects quoted payload false positives",
        ]:
            self.assertIn(name, by_name)
            self.assertTrue(by_name[name]["passed"], by_name[name])

    def test_diagnose_detects_required_failure_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            write_sqlite_log(codex_home, [
                "stream disconnected before completion",
                "responses_websocket error: closed before response.completed",
                "Received turn/completed for unknown conversation",
                "Error running remote compact task",
                "No progress loop: reread the same files repeatedly without edits",
            ])

            result = run_cli(
                "diagnose",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            counts = report["summary"]["pattern_counts"]
            self.assertGreaterEqual(counts.get("stream_disconnect", 0), 1)
            self.assertGreaterEqual(counts.get("responses_websocket_failure", 0), 1)
            self.assertGreaterEqual(counts.get("unknown_conversation", 0), 1)
            self.assertGreaterEqual(counts.get("remote_compaction_failed", 0), 1)
            self.assertGreaterEqual(counts.get("no_progress_loop", 0), 1)

    def test_diagnose_detects_real_responses_retry_disconnect_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(
                    now,
                    "WARN",
                    "codex_core::responses_retry",
                    "run_turn:run_sampling_request{turn_id=[REDACTED_ID] model=gpt-5.5}: "
                    "stream disconnected - retrying sampling request (1/5 in 195ms)...",
                )],
            )

            result = run_cli(
                "health",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["pattern_counts"].get("stream_disconnect"), 1)
            self.assertEqual(report["health"]["issue_type"], "transport")
            self.assertIn("stream_disconnect", report["health"]["transport_patterns"])
            self.assertTrue(any("stream as unreliable" in item for item in report["summary"]["recommendations"]))

    def test_diagnose_detects_startup_prewarm_badrecordmac_as_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(
                    now,
                    "WARN",
                    "codex_core::session_startup_prewarm",
                    "startup websocket prewarm setup failed: stream disconnected before completion: "
                    "received fatal alert: BadRecordMac",
                )],
            )

            result = run_cli(
                "health",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            counts = report["summary"]["pattern_counts"]
            self.assertEqual(counts.get("stream_disconnect"), 1)
            self.assertEqual(counts.get("tls_handshake_failed"), 1)
            self.assertEqual(report["health"]["issue_type"], "transport")

    def test_diagnose_ignores_streamed_text_containing_failure_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(
                    now,
                    "TRACE",
                    "codex_api::endpoint::responses_websocket",
                    'websocket event: {"type":"response.output_text.delta","delta":"that affects confidence in the failure count"}',
                )],
            )

            result = run_cli(
                "diagnose",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["event_count"], 0)

    def test_diagnose_ignores_failure_words_far_from_websocket_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(
                    now,
                    "INFO",
                    "feedback_tags",
                    "stream_request:model_client.stream_responses_websocket{model=gpt-5.5 transport=\"responses_websocket\" "
                    "api.path=\"responses\" websocket.warmup=false}: endpoint=\"/responses\" "
                    "tool_name=\"exec_command\" output=\"Ran tests that previously FAILED before the fix\"",
                )],
            )

            result = run_cli(
                "diagnose",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["event_count"], 0)

    def test_diagnose_ignores_transcript_payloads_quoting_failure_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            quoted_history = (
                "The following is the Codex agent history whose request action you are assessing. "
                ">>> TRANSCRIPT START\n"
                "[1] tool output: Received turn/completed for unknown conversation conversationId=abc\n"
                "[2] assistant: failed to send websocket request appeared in earlier logs\n"
                ">>> TRANSCRIPT END"
            )
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [
                    (now, "DEBUG", "codex_core::session::handlers", quoted_history),
                    (now - 1, "INFO", "codex_core::stream_events_utils", quoted_history),
                ],
            )

            result = run_cli(
                "diagnose",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["event_count"], 0)

    def test_diagnose_ignores_code_fixture_payloads_quoting_failure_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            quoted_test_code = (
                "def test_doctor_creates_bundle(self):\n"
                "    write_sqlite_log(codex_home, [\n"
                "        \"Received turn/started for unknown conversation\",\n"
                "        \"failed to send websocket request\",\n"
                "    ])\n"
                "    result = run_cli(\"doctor\", \"--format\", \"json\")\n"
            )
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(now, "INFO", "codex_core::stream_events_utils", quoted_test_code)],
            )

            result = run_cli(
                "diagnose",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["event_count"], 0)

    def test_diagnose_ignores_stream_event_token_usage_envelopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            telemetry_body = (
                "app_server.request{otel.name=\"thread/goal/set\"}:"
                "turn{otel.name=\"session_task.turn\" codex.turn.token_usage.input_tokens=123}:"
                "command text quoted \"unknown conversation\" and \"mcp_request_timeout\""
            )
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(now, "INFO", "codex_core::stream_events_utils", telemetry_body)],
            )

            result = run_cli(
                "diagnose",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["event_count"], 0)

    def test_diagnose_ignores_app_server_goal_payloads_quoting_recovery_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            goal_payload = (
                "app_server.request{otel.kind=\"server\" otel.name=\"thread/goal/set\" "
                "rpc.system=\"jsonrpc\" rpc.method=\"thread/goal/set\" "
                "app_server.client_name=\"Codex Desktop\"}:"
                "turn{otel.name=\"session_task.turn\" thread.id=[REDACTED_ID] "
                "turn.id=[REDACTED_ID] codex.turn.token_usage.input_tokens=123}:"
                "objective=\"automatic preflight checkpoint before long tasks, "
                "no-progress loops, stream disconnected before completion, "
                "failed to send websocket request, and unknown conversation\""
            )
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(now, "INFO", "codex_otel.log_only", goal_payload)],
            )

            result = run_cli(
                "diagnose",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["event_count"], 0)

    def test_diagnose_ignores_outbound_response_create_payloads_quoting_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            request_payload = (
                "responses_websocket.stream_request{transport=\"responses_websocket\"}: "
                "websocket request: {\"type\":\"response.create\",\"instructions\":"
                "\"Prior output said stream disconnected before completion, "
                "failed to send websocket request, and unknown conversation.\"}"
            )
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(now, "TRACE", "codex_api::endpoint::responses_websocket", request_payload)],
            )

            result = run_cli(
                "diagnose",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["event_count"], 0)

    def test_preflight_records_phase_git_state_and_slice_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
            (project / "target.txt").write_text("dirty\n", encoding="utf-8")

            result = run_cli(
                "preflight",
                "--project",
                str(project),
                "--task",
                "Edit target.txt",
                "--next-action",
                "Edit target.txt",
                "--touched",
                "target.txt",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            checkpoint = json.loads((project / ".codex-guardian" / "current.json").read_text())
            self.assertEqual(checkpoint["phase"], "preflight_done")
            self.assertEqual(checkpoint["next_action"], "Edit target.txt")
            self.assertIn("target.txt", checkpoint["touched"])
            self.assertIn("git status: dirty", checkpoint["verified"])
            self.assertEqual(checkpoint["slice_minutes"], 15)
            self.assertIn("checkpoint_due_at", checkpoint)

    def test_auto_preflight_writes_checkpoint_for_long_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("ready\n", encoding="utf-8")

            result = run_cli(
                "auto-preflight",
                "--project",
                str(project),
                "--task",
                "Long recovery task",
                "--next-action",
                "Edit target.txt",
                "--estimated-minutes",
                "20",
                "--threshold-minutes",
                "10",
                "--slice-minutes",
                "12",
                "--touched",
                "target.txt",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["created_preflight_checkpoint"])
            self.assertEqual(report["reason"], "estimated_minutes_meets_threshold")
            checkpoint_path = Path(report["checkpoint_path"])
            self.assertTrue(checkpoint_path.exists())
            checkpoint = json.loads((project / ".codex-guardian" / "current.json").read_text())
            self.assertEqual(checkpoint["task"], "Long recovery task")
            self.assertEqual(checkpoint["phase"], "preflight_done")
            self.assertEqual(checkpoint["slice_minutes"], 12)
            self.assertIn("touched file exists: target.txt", checkpoint["verified"])

    def test_auto_preflight_skips_short_task_without_writing_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()

            result = run_cli(
                "auto-preflight",
                "--project",
                str(project),
                "--task",
                "Small task",
                "--next-action",
                "Answer directly",
                "--estimated-minutes",
                "4",
                "--threshold-minutes",
                "10",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertFalse(report["created_preflight_checkpoint"])
            self.assertEqual(report["reason"], "estimated_minutes_below_threshold")
            self.assertIsNone(report["checkpoint_path"])
            self.assertFalse((project / ".codex-guardian" / "current.json").exists())

    def test_resume_prompt_limits_context_to_touched_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            checkpoint_result = run_cli(
                "checkpoint",
                "--project",
                str(project),
                "--task",
                "Patch recovery docs",
                "--phase",
                "write_done",
                "--next-action",
                "Run tests",
                "--touched",
                "README.md",
                "--touched",
                "skills/codex-guardian/SKILL.md",
                "--verified",
                "Docs edited",
            )
            self.assertEqual(checkpoint_result.returncode, 0, checkpoint_result.stderr)

            result = run_cli("resume-prompt", "--project", str(project))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Continue from phase write_done.", result.stdout)
            self.assertIn(
                "Read only these files first: README.md, skills/codex-guardian/SKILL.md.",
                result.stdout,
            )
            self.assertIn("First verify whether the recorded edits already happened.", result.stdout)

    def test_watch_once_returns_failure_when_stream_errors_are_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            write_sqlite_log(codex_home, ["failed to send websocket request"])

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertGreaterEqual(report["summary"]["pattern_counts"].get("websocket_send_failed", 0), 1)

    def test_desktop_log_scan_respects_hours_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            codex_home = Path(tmp) / "codex-home"
            today = time.strftime("%Y/%m/%d")
            log_dir = fake_home / "Library" / "Logs" / "com.openai.codex" / today
            log_dir.mkdir(parents=True)
            (log_dir / "codex-desktop-test.log").write_text(
                "2000-01-01T00:00:00.000Z error failed to send websocket request\n",
                encoding="utf-8",
            )
            env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["event_count"], 0)

    def test_install_check_installs_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"

            install = run_cli("install-check", "--codex-home", str(codex_home), "--install")
            self.assertEqual(install.returncode, 0, install.stderr)
            target = codex_home / "skills" / "codex-guardian"
            self.assertTrue((target / "SKILL.md").exists())
            self.assertTrue((target / "scripts" / "codex_guardian.py").exists())
            self.assertTrue((target / "scripts" / "diagnose_codex_streams.py").exists())
            self.assertTrue((target / "fixtures" / "redacted-real-log-corpus.json").exists())

            (target / "LOCAL.txt").write_text("keep me\n", encoding="utf-8")
            second = run_cli("install-check", "--codex-home", str(codex_home), "--install")
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("already exists", second.stderr)
            self.assertTrue((target / "LOCAL.txt").exists())

    def test_install_check_reports_incomplete_existing_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            target = codex_home / "skills" / "codex-guardian"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("---\nname: codex-guardian\n---\n", encoding="utf-8")

            result = run_cli("install-check", "--codex-home", str(codex_home))

            self.assertEqual(result.returncode, 1)
            self.assertIn("Installed Codex Guardian is incomplete", result.stderr)
            self.assertIn("scripts/codex_guardian.py", result.stderr)
            self.assertIn("fixtures/redacted-real-log-corpus.json", result.stderr)

    def test_watch_recovery_report_writes_bundle_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            write_sqlite_log(codex_home, [
                "responses_websocket failed for conversationId=019ebaf2-d57c-71d3-8f20-5e5b3f48d1ba user kent@example.com",
            ])
            checkpoint = run_cli(
                "checkpoint",
                "--project",
                str(project),
                "--task",
                "Recover stream",
                "--phase",
                "write_started",
                "--next-action",
                "Verify edits",
                "--touched",
                "README.md",
                "--verified",
                "Started write slice",
            )
            self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                "--recovery-report",
            )

            self.assertEqual(result.returncode, 1)
            recovery_root = project / ".codex-guardian" / "recovery"
            bundles = sorted(path for path in recovery_root.iterdir() if path.is_dir())
            self.assertEqual(len(bundles), 1)
            bundle = bundles[0]
            self.assertTrue((bundle / "diagnosis.json").exists())
            self.assertTrue((bundle / "diagnosis.md").exists())
            self.assertTrue((bundle / "checkpoint.json").exists())
            self.assertTrue((bundle / "resume-prompt.txt").exists())
            self.assertTrue((bundle / "events.json").exists())
            self.assertTrue((bundle / "manifest.json").exists())
            self.assertTrue((bundle / "README.md").exists())
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], "codex-guardian.recovery-bundle.v1")
            self.assertIn("resume-prompt.txt", manifest["files"])
            self.assertIn("README.md", manifest["open_first"])
            self.assertIn("resume-prompt.txt", (bundle / "README.md").read_text(encoding="utf-8"))
            combined = "\n".join(path.read_text(encoding="utf-8") for path in bundle.iterdir() if path.is_file())
            self.assertIn("Continue from phase write_started.", combined)
            self.assertNotIn("kent@example.com", combined)
            self.assertNotIn("019ebaf2-d57c-71d3-8f20-5e5b3f48d1ba", combined)

    def test_watch_reachability_failure_writes_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                "--recovery-report",
                "--check-reachability",
                "--reachability-endpoint",
                "http://127.0.0.1:9/health",
                "--reachability-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertTrue(report["reachability"]["reachability"]["local_network_issue"])
            bundle = Path(report["recovery_report"])
            self.assertTrue((bundle / "reachability.json").exists())
            self.assertTrue((bundle / "reachability.md").exists())
            bundled_reachability = json.loads((bundle / "reachability.json").read_text(encoding="utf-8"))
            self.assertEqual(bundled_reachability, report["reachability"])
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("reachability.md", manifest["open_first"])
            self.assertIn("reachability.json", manifest["files"])

    def test_watch_service_status_degraded_writes_bundle(self):
        guardian = load_guardian_module()
        service_report = guardian.build_service_status_report(
            "https://status.example.test/status.json",
            timeout=1.0,
            status_probe=lambda _endpoint, _timeout: {
                "status": {
                    "indicator": "major",
                    "description": "Major Outage",
                }
            },
        )
        original_status_builder = guardian.build_service_status_report
        original_desktop_log_dirs = guardian.desktop_log_dirs
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            output = Path(tmp) / "watch.json"
            args = argparse.Namespace(
                codex_home=str(codex_home),
                project=str(project),
                hours=1,
                limit=80,
                interval=30.0,
                once=True,
                format="json",
                output=str(output),
                recovery_report=True,
                doctor=False,
                mark_restart=False,
                task=None,
                touched=None,
                slice_minutes=15,
                check_reachability=False,
                reachability_endpoint=guardian.DEFAULT_REACHABILITY_ENDPOINT,
                reachability_timeout=1.0,
                reachability_dns_only=False,
                check_service_status=True,
                service_status_endpoint="https://status.example.test/status.json",
                service_status_timeout=1.0,
            )
            try:
                guardian.build_service_status_report = lambda _endpoint, _timeout: service_report
                guardian.desktop_log_dirs = lambda _hours: []
                exit_code = guardian.cmd_watch(args)
            finally:
                guardian.build_service_status_report = original_status_builder
                guardian.desktop_log_dirs = original_desktop_log_dirs

            self.assertEqual(exit_code, 1)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["health"]["issue_type"], "healthy")
            service_status = report["service_status"]["service_status"]
            self.assertEqual(service_status["status"], "degraded")
            self.assertTrue(service_status["upstream_issue"])
            bundle = Path(report["recovery_report"])
            self.assertTrue((bundle / "service-status.json").exists())
            self.assertTrue((bundle / "service-status.md").exists())
            self.assertTrue((bundle / "connection-triage.json").exists())
            self.assertTrue((bundle / "connection-triage.md").exists())
            bundled_status = json.loads((bundle / "service-status.json").read_text(encoding="utf-8"))
            self.assertEqual(bundled_status["service_status"], service_status)
            triage = json.loads((bundle / "connection-triage.json").read_text(encoding="utf-8"))
            self.assertEqual(triage["connection_triage"]["recovery_attention"], "upstream_degraded")
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("service-status.md", manifest["open_first"])
            self.assertIn("connection-triage.md", manifest["open_first"])

    def test_watch_service_status_failed_check_stays_clean_when_logs_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                "--check-service-status",
                "--service-status-endpoint",
                "http://127.0.0.1:9/status.json",
                "--service-status-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            service_status = report["service_status"]["service_status"]
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertEqual(service_status["status"], "unknown")
            self.assertTrue(service_status["check_failed"])
            self.assertFalse(service_status["upstream_issue"])
            self.assertNotIn("recovery_report", report)

    def test_watch_doctor_writes_full_recovery_bundle_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("ready\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                "--doctor",
                "--task",
                "Guard active work",
                "--touched",
                "target.txt",
                "--reachability-endpoint",
                "http://127.0.0.1:9/health",
                "--reachability-timeout",
                "0.2",
                "--service-status-endpoint",
                "http://127.0.0.1:9/status",
                "--service-status-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads(result.stdout)
            self.assertIn("recovery_report", report)
            self.assertIn("doctor", report)
            self.assertIn("status", report)
            self.assertIn("reachability", report)
            self.assertIn("service_status", report)
            self.assertTrue(report["doctor"]["created_recovery_bundle"])
            self.assertTrue(report["doctor"]["created_preflight_checkpoint"])
            bundle = Path(report["recovery_report"])
            for filename in (
                "doctor.json",
                "doctor.md",
                "status.json",
                "status.md",
                "reachability.json",
                "reachability.md",
                "service-status.json",
                "service-status.md",
                "environment.json",
                "environment.md",
                "connection-triage.json",
                "connection-triage.md",
            ):
                self.assertTrue((bundle / filename).exists(), filename)
            doctor_bundle = json.loads((bundle / "doctor.json").read_text(encoding="utf-8"))
            self.assertEqual(doctor_bundle["doctor"]["actions"], report["doctor"]["actions"])
            self.assertEqual(doctor_bundle["recovery_report"], report["recovery_report"])
            service_status = json.loads((bundle / "service-status.json").read_text(encoding="utf-8"))
            self.assertEqual(service_status["schema"], "codex-guardian.service-status.v1")
            self.assertEqual(service_status["service_status"]["status"], "unknown")
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            for filename in ("doctor.md", "status.md", "reachability.md", "service-status.md", "environment.md", "connection-triage.md"):
                self.assertIn(filename, manifest["open_first"])

    def test_watch_markdown_renders_reachability_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--once",
                "--check-reachability",
                "--reachability-endpoint",
                "http://127.0.0.1:9/health",
                "--reachability-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("## Reachability", result.stdout)
            self.assertIn("Reachability status:", result.stdout)
            self.assertIn("Local network issue: `true`", result.stdout)

    def test_watch_recovery_report_triggers_on_repeated_app_state_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                "--recovery-report",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["pattern_counts"].get("unknown_conversation"), 2)
            self.assertEqual(report["health"]["issue_type"], "app_state")
            self.assertTrue(report["health"]["restart_codex_now"])
            self.assertIn("recovery_report", report)
            bundle = Path(report["recovery_report"])
            self.assertTrue((bundle / "manifest.json").exists())
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["summary"]["pattern_counts"].get("unknown_conversation"), 2)
            self.assertEqual(manifest["health"]["issue_type"], "app_state")
            self.assertTrue(manifest["health"]["restart_codex_now"])
            diagnosis_markdown = (bundle / "diagnosis.md").read_text(encoding="utf-8")
            self.assertIn("## Health", diagnosis_markdown)
            self.assertIn("Issue type: `app_state`", diagnosis_markdown)
            self.assertIn("Restart Codex now: `true`", diagnosis_markdown)

    def test_watch_can_mark_restart_for_repeated_app_state_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                "--mark-restart",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertTrue(report["health"]["restart_codex_now"])
            marker_path = project / ".codex-guardian" / "restart-marker.json"
            self.assertEqual(Path(report["restart_marker"]["path"]).resolve(), marker_path.resolve())
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["schema"], "codex-guardian.restart-marker.v1")
            self.assertIn("watch", marker["reason"])
            self.assertEqual(marker["source"], "watch")
            self.assertEqual(marker["issue_type"], "app_state")
            self.assertEqual(marker["restart_timing"], "now_after_checkpoint")
            self.assertIn("Repeated app-state", marker["restart_reason"])

    def test_watch_mark_restart_renders_marker_in_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--once",
                "--mark-restart",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("## Restart Marker", result.stdout)
            self.assertIn("watch recommended restart for app_state", result.stdout)
            self.assertIn("Restart decision: `restart_now_after_checkpoint`", result.stdout)
            self.assertIn("Restart first action: `checkpoint`", result.stdout)
            self.assertIn("post-restart --project .", result.stdout)

    def test_watch_marker_records_mixed_restart_timing_after_state_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "failed to send websocket request",
                "Received turn/started for unknown conversation",
            ])

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                "--mark-restart",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "mixed")
            self.assertTrue(report["health"]["restart_recommended"])
            self.assertEqual(report["restart_marker"]["issue_type"], "mixed")
            self.assertEqual(report["restart_marker"]["restart_timing"], "after_state_preserved")
            self.assertTrue(report["restart_marker"]["restart_recommended"])
            self.assertFalse(report["restart_marker"]["restart_codex_now"])
            marker_path = project / ".codex-guardian" / "restart-marker.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["source"], "watch")
            self.assertEqual(marker["issue_type"], "mixed")
            self.assertEqual(marker["restart_timing"], "after_state_preserved")
            self.assertTrue(marker["restart_recommended"])
            self.assertFalse(marker["restart_codex_now"])

    def test_watch_can_write_preflight_checkpoint_before_reporting_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("ready\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--task",
                "Guard active work",
                "--touched",
                "target.txt",
                "--slice-minutes",
                "9",
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertIn("preflight_checkpoint", report)
            checkpoint = json.loads((project / ".codex-guardian" / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["phase"], "preflight_done")
            self.assertEqual(checkpoint["task"], "Guard active work")
            self.assertEqual(checkpoint["slice_minutes"], 9)
            self.assertIn("touched file exists: target.txt", checkpoint["verified"])

    def test_watch_preflight_checkpoint_is_in_recovery_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("ready\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--task",
                "Guard active work",
                "--touched",
                "target.txt",
                "--slice-minutes",
                "9",
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                "--recovery-report",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertIn("preflight_checkpoint", report)
            bundle = Path(report["recovery_report"])
            bundled_checkpoint = json.loads((bundle / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(bundled_checkpoint["phase"], "preflight_done")
            self.assertEqual(bundled_checkpoint["task"], "Guard active work")
            self.assertEqual(bundled_checkpoint["slice_minutes"], 9)
            self.assertIn("touched file exists: target.txt", bundled_checkpoint["verified"])

    def test_watch_recovery_report_preserves_unreadable_checkpoint_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            checkpoint_path = project / ".codex-guardian" / "current.json"
            checkpoint_path.parent.mkdir(parents=True)
            checkpoint_path.write_text("{broken checkpoint", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            write_sqlite_log(codex_home, ["stream disconnected before completion"])

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                "--recovery-report",
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertIn("checkpoint_attention", report)
            self.assertIn("checkpoint_read_error", report["checkpoint_attention"])
            self.assertEqual(Path(report["checkpoint_attention"]["checkpoint_read_error_path"]).resolve(), checkpoint_path.resolve())
            bundle = Path(report["recovery_report"])
            diagnosis = json.loads((bundle / "diagnosis.json").read_text(encoding="utf-8"))
            self.assertIn("checkpoint_read_error", diagnosis["checkpoint_attention"])
            resume_prompt = (bundle / "resume-prompt.txt").read_text(encoding="utf-8")
            self.assertIn("current checkpoint could not be read", resume_prompt)
            bundle_readme = (bundle / "README.md").read_text(encoding="utf-8")
            self.assertIn("checkpoint could not be read", bundle_readme)
            self.assertFalse((bundle / "checkpoint.json").exists())

    def test_watch_treats_unreadable_checkpoint_as_actionable_without_log_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            checkpoint_path = project / ".codex-guardian" / "current.json"
            checkpoint_path.parent.mkdir(parents=True)
            checkpoint_path.write_text("{broken checkpoint", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, ["normal diagnostic row without a failure"])

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                "--recovery-report",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertIn("checkpoint_read_error", report["checkpoint_attention"])
            bundle = Path(report["recovery_report"])
            diagnosis = json.loads((bundle / "diagnosis.json").read_text(encoding="utf-8"))
            self.assertIn("checkpoint_read_error", diagnosis["checkpoint_attention"])
            self.assertFalse((bundle / "checkpoint.json").exists())

    def test_watch_treats_post_restart_transport_attention_as_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            marker_dir = project / ".codex-guardian"
            marker_dir.mkdir()
            now = int(time.time())
            marker_created_at = now - 7200
            (marker_dir / "restart-marker.json").write_text(
                json.dumps({
                    "schema": "codex-guardian.restart-marker.v1",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(marker_created_at)),
                    "reason": "fixture restart",
                }) + "\n",
                encoding="utf-8",
            )
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(marker_created_at + 60, "ERROR", "codex-test", "failed to send websocket request")],
            )

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--once",
                "--format",
                "json",
                "--recovery-report",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertEqual(report["status"]["post_restart"]["status"], "transport_unreliable")
            self.assertTrue(report["status"]["fresh_recovery_bundle_recommended"])
            bundle = Path(report["recovery_report"])
            self.assertTrue((bundle / "status.json").exists())
            self.assertTrue((bundle / "status.md").exists())
            status = json.loads((bundle / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"]["post_restart"]["status"], "transport_unreliable")

    def test_watch_preflight_checkpoint_renders_in_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("ready\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "watch",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--task",
                "Guard active work",
                "--touched",
                "target.txt",
                "--hours",
                "1",
                "--once",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("## Preflight Checkpoint", result.stdout)
            self.assertIn("current state was checkpointed before watch reported", result.stdout)

    def test_bundle_command_writes_recovery_bundle_on_demand(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            write_sqlite_log(codex_home, ["normal diagnostic row without a failure"])
            checkpoint = run_cli(
                "checkpoint",
                "--project",
                str(project),
                "--task",
                "Manual recovery",
                "--phase",
                "preflight_done",
                "--next-action",
                "Continue safely",
                "--touched",
                "README.md",
                "--verified",
                "Manual checkpoint ready",
            )
            self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)

            result = run_cli(
                "bundle",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            bundle = Path(report["recovery_report"])
            self.assertTrue((bundle / "diagnosis.json").exists())
            self.assertTrue((bundle / "diagnosis.md").exists())
            self.assertTrue((bundle / "checkpoint.json").exists())
            self.assertTrue((bundle / "resume-prompt.txt").exists())
            self.assertTrue((bundle / "events.json").exists())
            self.assertTrue((bundle / "manifest.json").exists())
            self.assertTrue((bundle / "README.md").exists())
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], "codex-guardian.recovery-bundle.v1")
            self.assertTrue(manifest["checkpoint_present"])
            self.assertIn("README.md", manifest["open_first"])
            self.assertIn("resume-prompt.txt", manifest["files"])

    def test_bundle_can_write_full_recovery_plan_on_demand(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("ready\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "bundle",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--task",
                "Recover active task",
                "--touched",
                "target.txt",
                "--slice-minutes",
                "11",
                "--hours",
                "1",
                "--format",
                "json",
                "--doctor",
                "--mark-restart",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertIn("preflight_checkpoint", report)
            self.assertTrue(report["doctor"]["created_recovery_bundle"])
            self.assertTrue(report["doctor"]["created_preflight_checkpoint"])
            self.assertTrue(report["doctor"]["created_restart_marker"])
            self.assertIn("bundle", report["restart_marker"]["reason"])
            bundle = Path(report["recovery_report"])
            self.assertTrue((bundle / "doctor.json").exists())
            self.assertTrue((bundle / "doctor.md").exists())
            self.assertTrue((bundle / "connection-triage.json").exists())
            self.assertTrue((bundle / "connection-triage.md").exists())
            self.assertTrue((bundle / "status.json").exists())
            self.assertTrue((bundle / "status.md").exists())
            self.assertTrue((bundle / "reachability.json").exists())
            self.assertTrue((bundle / "reachability.md").exists())
            self.assertTrue((bundle / "service-status.json").exists())
            self.assertTrue((bundle / "service-status.md").exists())
            self.assertTrue((bundle / "environment.json").exists())
            self.assertTrue((bundle / "environment.md").exists())
            self.assertTrue((bundle / "checkpoint.json").exists())
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("doctor.md", manifest["open_first"])
            self.assertIn("status.md", manifest["open_first"])
            self.assertIn("reachability.md", manifest["open_first"])
            self.assertIn("service-status.md", manifest["open_first"])
            self.assertIn("environment.md", manifest["open_first"])
            self.assertIn("connection-triage.md", manifest["open_first"])
            self.assertIn("doctor.json", manifest["files"])
            self.assertIn("status.json", manifest["files"])
            self.assertIn("reachability.json", manifest["files"])
            self.assertIn("service-status.json", manifest["files"])
            self.assertIn("environment.json", manifest["files"])
            self.assertIn("connection-triage.json", manifest["files"])
            reachability_report = json.loads((bundle / "reachability.json").read_text(encoding="utf-8"))
            self.assertIn("reachability", reachability_report)
            self.assertIn("reachability", report)
            self.assertEqual(report["reachability"], reachability_report)
            self.assertIn(reachability_report["reachability"]["status"], {"reachable", "dns_reachable", "dns_resolution_failed", "tls_handshake_failed", "connection_reset", "request_timeout", "network_unreachable", "connection_refused"})
            reachability_markdown = (bundle / "reachability.md").read_text(encoding="utf-8")
            self.assertIn("# Codex Guardian Reachability", reachability_markdown)
            service_status_report = json.loads((bundle / "service-status.json").read_text(encoding="utf-8"))
            self.assertIn("service_status", service_status_report)
            self.assertIn("service_status", report)
            self.assertEqual(report["service_status"], service_status_report)
            self.assertIn(service_status_report["service_status"]["status"], {"operational", "degraded", "unknown"})
            service_status_markdown = (bundle / "service-status.md").read_text(encoding="utf-8")
            self.assertIn("# Codex Guardian Service Status", service_status_markdown)
            environment_report = json.loads((bundle / "environment.json").read_text(encoding="utf-8"))
            self.assertEqual(environment_report["schema"], "codex-guardian.environment.v1")
            self.assertEqual(environment_report["codex_home"], str(codex_home))
            self.assertEqual(environment_report["reachability_endpoint"], reachability_report["endpoint"])
            self.assertIn("python", environment_report)
            self.assertIn("codex_cli", environment_report)
            self.assertIn("log_sources", environment_report)
            self.assertTrue(environment_report["log_sources"]["sqlite_log"]["exists"])
            self.assertEqual(environment_report["log_sources"]["sqlite_log"]["path"], str(codex_home / "logs_2.sqlite"))
            self.assertIn("desktop_log_base", environment_report["log_sources"])
            environment_markdown = (bundle / "environment.md").read_text(encoding="utf-8")
            self.assertIn("# Codex Guardian Environment", environment_markdown)
            self.assertIn("Codex CLI", environment_markdown)
            self.assertIn("## Log Sources", environment_markdown)
            status_report = json.loads((bundle / "status.json").read_text(encoding="utf-8"))
            self.assertIn("status", report)
            self.assertEqual(Path(report["status"]["latest_recovery_bundle"]).resolve(), bundle.resolve())
            self.assertEqual(report["status"], status_report["status"])
            self.assertEqual(Path(status_report["status"]["latest_recovery_bundle"]).resolve(), bundle.resolve())
            self.assertTrue(status_report["status"]["restart_marker_present"])
            status_markdown = (bundle / "status.md").read_text(encoding="utf-8")
            self.assertIn("## Next Actions", status_markdown)
            self.assertIn("Post-restart status", status_markdown)
            resume_prompt = (bundle / "resume-prompt.txt").read_text(encoding="utf-8")
            self.assertIn("Open `status.md` first", resume_prompt)
            self.assertIn("Then use `doctor.md`", resume_prompt)
            bundled_checkpoint = json.loads((bundle / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(bundled_checkpoint["task"], "Recover active task")
            self.assertEqual(bundled_checkpoint["slice_minutes"], 11)
            self.assertIn("touched file exists: target.txt", bundled_checkpoint["verified"])
            doctor_markdown = (bundle / "doctor.md").read_text(encoding="utf-8")
            self.assertIn("Restart marker written", doctor_markdown)
            self.assertIn("post-restart --project .", doctor_markdown)
            connection_triage = json.loads((bundle / "connection-triage.json").read_text(encoding="utf-8"))
            self.assertFalse(connection_triage["connection_triage"]["direct_fix_available"])
            triage_markdown = (bundle / "connection-triage.md").read_text(encoding="utf-8")
            self.assertIn("Cannot patch Codex app internals", triage_markdown)

    def test_recover_now_writes_full_recovery_plan_without_doctor_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("ready\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "recover-now",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--task",
                "Recover active task",
                "--touched",
                "target.txt",
                "--slice-minutes",
                "11",
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["doctor"]["created_recovery_bundle"])
            self.assertTrue(report["doctor"]["created_preflight_checkpoint"])
            self.assertTrue(report["doctor"]["created_restart_marker"])
            self.assertEqual(report["restart_marker"]["source"], "recover-now")
            self.assertIn("recover-now", report["restart_marker"]["reason"])
            bundle = Path(report["recovery_report"])
            expected_files = {
                "doctor.json",
                "doctor.md",
                "connection-triage.json",
                "connection-triage.md",
                "status.json",
                "status.md",
                "reachability.json",
                "reachability.md",
                "service-status.json",
                "service-status.md",
                "environment.json",
                "environment.md",
                "checkpoint.json",
                "manifest.json",
                "README.md",
                "resume-prompt.txt",
            }
            self.assertTrue(expected_files.issubset({path.name for path in bundle.iterdir()}))
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            for name in ["status.md", "doctor.md", "reachability.md", "service-status.md", "environment.md", "connection-triage.md"]:
                self.assertIn(name, manifest["open_first"])

    def test_bundle_doctor_reachability_failure_adds_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "bundle",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                "--doctor",
                "--reachability-endpoint",
                "http://127.0.0.1:9/health",
                "--reachability-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["reachability"]["reachability"]["local_network_issue"])
            self.assertTrue(any("Reachability check failed" in action for action in report["doctor"]["actions"]))
            bundle = Path(report["recovery_report"])
            bundled_doctor = json.loads((bundle / "doctor.json").read_text(encoding="utf-8"))
            self.assertEqual(bundled_doctor["reachability"], report["reachability"])

    def test_bundle_doctor_preserves_unreadable_checkpoint_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            checkpoint_path = project / ".codex-guardian" / "current.json"
            checkpoint_path.parent.mkdir(parents=True)
            checkpoint_path.write_text("{broken checkpoint", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            write_sqlite_log(codex_home, ["normal diagnostic row without a failure"])

            result = run_cli(
                "bundle",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                "--doctor",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertIn("checkpoint_attention", report)
            self.assertIn("checkpoint_read_error", report["checkpoint_attention"])
            self.assertEqual(Path(report["checkpoint_attention"]["checkpoint_read_error_path"]).resolve(), checkpoint_path.resolve())
            self.assertTrue(any("current checkpoint could not be read" in action for action in report["doctor"]["actions"]))
            bundle = Path(report["recovery_report"])
            diagnosis = json.loads((bundle / "diagnosis.json").read_text(encoding="utf-8"))
            self.assertIn("checkpoint_read_error", diagnosis["checkpoint_attention"])
            doctor = json.loads((bundle / "doctor.json").read_text(encoding="utf-8"))
            self.assertIn("checkpoint_read_error", doctor["checkpoint_attention"])
            triage = json.loads((bundle / "connection-triage.json").read_text(encoding="utf-8"))
            self.assertTrue(any("current checkpoint could not be read" in action for action in triage["connection_triage"]["local_actions"]))
            resume_prompt = (bundle / "resume-prompt.txt").read_text(encoding="utf-8")
            self.assertIn("current checkpoint could not be read", resume_prompt)
            self.assertIn("current.json", resume_prompt)
            bundle_readme = (bundle / "README.md").read_text(encoding="utf-8")
            self.assertIn("Checkpoint attention", bundle_readme)
            self.assertIn("checkpoint could not be read", bundle_readme)
            self.assertFalse((bundle / "checkpoint.json").exists())

    def test_bundle_doctor_actions_use_post_restart_transport_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            marker_dir = project / ".codex-guardian"
            marker_dir.mkdir()
            now = int(time.time())
            marker_created_at = now - 7200
            (marker_dir / "restart-marker.json").write_text(
                json.dumps({
                    "schema": "codex-guardian.restart-marker.v1",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(marker_created_at)),
                    "reason": "fixture restart",
                }) + "\n",
                encoding="utf-8",
            )
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(marker_created_at + 60, "ERROR", "codex-test", "failed to send websocket request")],
            )

            result = run_cli(
                "bundle",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                "--doctor",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"]["post_restart"]["status"], "transport_unreliable")
            self.assertTrue(any("recover-now --project . --hours 1 --no-mark-restart" in action for action in report["doctor"]["actions"]))
            bundle = Path(report["recovery_report"])
            doctor = json.loads((bundle / "doctor.json").read_text(encoding="utf-8"))
            self.assertTrue(any("recover-now --project . --hours 1 --no-mark-restart" in action for action in doctor["doctor"]["actions"]))

    def test_bundle_full_recovery_plan_renders_artifacts_in_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("ready\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "bundle",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--task",
                "Recover active task",
                "--touched",
                "target.txt",
                "--hours",
                "1",
                "--doctor",
                "--mark-restart",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("## Artifacts", result.stdout)
            self.assertIn("Recovery bundle:", result.stdout)
            self.assertIn("Preflight checkpoint:", result.stdout)
            self.assertIn("Restart marker:", result.stdout)
            self.assertIn("Doctor files: `doctor.md`, `doctor.json`", result.stdout)
            self.assertIn("Reachability files: `reachability.md`, `reachability.json`", result.stdout)
            self.assertIn("Service status files: `service-status.md`, `service-status.json`", result.stdout)
            self.assertIn("Connection triage files: `connection-triage.md`, `connection-triage.json`", result.stdout)

    def test_status_reports_current_recovery_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("ready\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])
            bundle_result = run_cli(
                "bundle",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--task",
                "Recover active task",
                "--touched",
                "target.txt",
                "--hours",
                "1",
                "--doctor",
                "--mark-restart",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )
            self.assertEqual(bundle_result.returncode, 0, bundle_result.stderr)
            bundle_report = json.loads(bundle_result.stdout)

            result = run_cli(
                "status",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "app_state")
            self.assertTrue(report["status"]["checkpoint_present"])
            self.assertEqual(report["status"]["checkpoint"]["task"], "Recover active task")
            self.assertEqual(Path(report["status"]["latest_recovery_bundle"]).resolve(), Path(bundle_report["recovery_report"]).resolve())
            self.assertTrue(report["status"]["restart_marker_present"])
            self.assertIn("bundle recommended restart", report["status"]["restart_marker"]["reason"])
            self.assertEqual(report["status"]["restart_marker"]["source"], "bundle")
            self.assertEqual(report["status"]["restart_marker"]["issue_type"], "app_state")
            self.assertEqual(report["status"]["restart_marker"]["restart_timing"], "now_after_checkpoint")
            self.assertIn("post_restart", report["status"])
            self.assertIn(report["status"]["post_restart"]["status"], {"no_activity", "still_unstable", "clean"})

    def test_status_recommends_fresh_bundle_when_post_restart_is_still_unstable(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            marker_dir = project / ".codex-guardian"
            marker_dir.mkdir()
            marker_created_at = int(time.time()) - 10
            (marker_dir / "restart-marker.json").write_text(
                json.dumps({
                    "schema": "codex-guardian.restart-marker.v1",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(marker_created_at)),
                    "reason": "fixture restart",
                }) + "\n",
                encoding="utf-8",
            )
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "status",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"]["post_restart"]["status"], "still_unstable")
            self.assertTrue(report["status"]["fresh_recovery_bundle_recommended"])
            self.assertTrue(any("recover-now --project . --hours 1" in action for action in report["status"]["next_actions"]))
            self.assertTrue(any("post-restart --project . --hours 1" in action for action in report["status"]["next_actions"]))

    def test_bundle_mark_restart_refreshes_marker_for_post_restart_instability(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            marker_dir = project / ".codex-guardian"
            marker_dir.mkdir()
            now = int(time.time())
            old_marker_time = now - 7200
            old_marker = marker_dir / "restart-marker.json"
            old_marker.write_text(
                json.dumps({
                    "schema": "codex-guardian.restart-marker.v1",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(old_marker_time)),
                    "reason": "fixture stale restart",
                    "source": "bundle",
                    "issue_type": "mixed",
                    "restart_timing": "after_state_preserved",
                    "restart_recommended": True,
                    "restart_codex_now": False,
                }) + "\n",
                encoding="utf-8",
            )
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(old_marker_time + 60, "ERROR", "codex-test", "Received turn/completed for unknown conversation")],
            )

            result = run_cli(
                "bundle",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--doctor",
                "--mark-restart",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertTrue(report["doctor"]["created_restart_marker"])
            marker = json.loads(old_marker.read_text(encoding="utf-8"))
            self.assertEqual(marker["source"], "bundle")
            self.assertEqual(marker["issue_type"], "post_restart_still_unstable")
            self.assertEqual(marker["restart_timing"], "after_state_preserved")
            self.assertTrue(marker["restart_recommended"])
            self.assertFalse(marker["restart_codex_now"])
            self.assertIn("post_restart_still_unstable", marker["reason"])

    def test_status_reports_overdue_in_progress_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            guardian = project / ".codex-guardian"
            guardian.mkdir()
            checkpoint = {
                "schema": "codex-guardian.checkpoint.v1",
                "created_at": "2026-06-12T00:00:00+00:00",
                "task": "Long recovery task",
                "phase": "preflight_done",
                "status": "in_progress",
                "next_action": "Continue one bounded slice",
                "touched": ["target.txt"],
                "verified": [],
                "notes": [],
                "cwd": str(project),
                "slice_minutes": 15,
                "checkpoint_due_at": "2026-06-12T00:01:00+00:00",
            }
            (guardian / "current.json").write_text(json.dumps(checkpoint) + "\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "status",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertTrue(report["status"]["checkpoint_overdue"])
            self.assertEqual(report["status"]["checkpoint_due_at"], "2026-06-12T00:01:00+00:00")
            self.assertTrue(report["status"]["fresh_recovery_bundle_recommended"])
            self.assertTrue(any("checkpoint" in action.lower() for action in report["status"]["next_actions"]))

    def test_status_reports_unreadable_current_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            guardian = project / ".codex-guardian"
            guardian.mkdir()
            checkpoint_path = guardian / "current.json"
            checkpoint_path.write_text("{not valid json\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "status",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertFalse(report["status"]["checkpoint_present"])
            self.assertEqual(Path(report["status"]["checkpoint_read_error_path"]).resolve(), checkpoint_path.resolve())
            self.assertIn("checkpoint_read_error", report["status"])
            self.assertTrue(report["status"]["fresh_recovery_bundle_recommended"])
            self.assertTrue(any("current.json" in action for action in report["status"]["next_actions"]))

    def test_status_renders_recovery_state_in_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])
            marker_result = run_cli(
                "mark-restart",
                "--project",
                str(project),
                "--reason",
                "fixture restart",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )
            self.assertEqual(marker_result.returncode, 0, marker_result.stderr)

            result = run_cli(
                "status",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("# Codex Guardian Status", result.stdout)
            self.assertIn("## Current Health", result.stdout)
            self.assertIn("## Recovery State", result.stdout)
            self.assertIn("Restart marker:", result.stdout)
            self.assertIn("Post-restart status:", result.stdout)

    def test_health_separates_app_state_from_transport_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "health",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            health = report["health"]
            self.assertEqual(health["issue_type"], "app_state")
            self.assertTrue(health["restart_codex_now"])
            self.assertTrue(health["restart_recommended"])
            self.assertEqual(health["restart_timing"], "now_after_checkpoint")
            self.assertEqual(health["restart_decision"]["decision"], "restart_now_after_checkpoint")
            self.assertEqual(health["restart_decision"]["first_action"], "checkpoint")
            self.assertTrue(health["restart_decision"]["marker_recommended"])
            self.assertFalse(health["transport_unreliable"])
            self.assertIn("unknown_conversation", health["app_state_patterns"])

    def test_status_does_not_restart_for_single_app_state_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
            ])

            result = run_cli(
                "status",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            health = report["health"]
            self.assertEqual(health["issue_type"], "app_state")
            self.assertFalse(health["restart_codex_now"])
            self.assertFalse(health["restart_recommended"])
            self.assertEqual(health["restart_decision"]["decision"], "watch_for_repeat_before_restart")
            actions = report["status"]["next_actions"]
            self.assertTrue(any("recover-now --project . --hours 1 --no-mark-restart" in action for action in actions))
            self.assertTrue(any("restart only if app-state events repeat" in action for action in actions))
            self.assertFalse(any("post-restart --project . --hours 1" in action for action in actions))

    def test_health_marks_transport_failures_without_restart_primary_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, ["failed to send websocket request"])

            result = run_cli(
                "health",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            health = report["health"]
            self.assertEqual(health["issue_type"], "transport")
            self.assertTrue(health["transport_unreliable"])
            self.assertFalse(health["restart_codex_now"])
            self.assertFalse(health["restart_recommended"])
            self.assertEqual(health["restart_timing"], "not_first_action")
            self.assertIn("websocket_send_failed", health["transport_patterns"])

    def test_health_explains_mixed_restart_after_preserving_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "failed to send websocket request",
                "Received turn/started for unknown conversation",
            ])

            result = run_cli(
                "health",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            health = report["health"]
            self.assertEqual(health["issue_type"], "mixed")
            self.assertFalse(health["restart_codex_now"])
            self.assertTrue(health["restart_recommended"])
            self.assertEqual(health["restart_timing"], "after_state_preserved")
            self.assertIn("transport and app-state", health["restart_reason"])
            boundary = health["direct_fix_boundary"]
            self.assertFalse(boundary["direct_fix_available"])
            self.assertEqual(boundary["direct_fix_ceiling_score"], 3)
            self.assertEqual(boundary["recovery_tooling_ceiling_score"], 9)
            self.assertEqual(boundary["highest_local_recovery_command"], "doctor --project . --hours 1")
            self.assertEqual(boundary["full_bundle_command"], "recover-now --project . --hours 1")
            self.assertIn("cannot patch Codex app internals", boundary["boundary_reason"])

    def test_health_separates_auth_session_failures_from_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "POST /backend-api/codex/responses failed: 401 Unauthorized authentication required",
            ])

            result = run_cli(
                "health",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            health = report["health"]
            self.assertEqual(health["issue_type"], "auth_session")
            self.assertTrue(health["auth_session_unhealthy"])
            self.assertFalse(health["transport_unreliable"])
            self.assertFalse(health["app_state_unstable"])
            self.assertFalse(health["restart_codex_now"])
            self.assertFalse(health["restart_recommended"])
            self.assertEqual(health["restart_timing"], "not_first_action")
            self.assertIn("auth_session_failed", health["auth_session_patterns"])
            self.assertIn("Sign in", health["primary_action"])

    def test_status_actions_for_auth_session_preserve_state_then_reauth(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "request failed: 403 Forbidden authentication failed",
            ])

            result = run_cli(
                "status",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "auth_session")
            actions = report["status"]["next_actions"]
            self.assertTrue(any("recover-now --project . --hours 1 --no-mark-restart" in action for action in actions))
            self.assertTrue(any("Sign in" in action for action in actions))
            self.assertFalse(any("--mark-restart" in action for action in actions))

    def test_health_can_include_reachability_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "health",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                "--check-reachability",
                "--reachability-endpoint",
                "http://127.0.0.1:9/health",
                "--reachability-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertTrue(report["reachability"]["reachability"]["local_network_issue"])
            self.assertEqual(report["reachability"]["endpoint"], "http://127.0.0.1:9/health")

    def test_health_markdown_renders_restart_decision_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "failed to send websocket request",
                "Received turn/started for unknown conversation",
            ])

            result = run_cli(
                "health",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Restart recommended: `true`", result.stdout)
            self.assertIn("Restart timing: `after_state_preserved`", result.stdout)
            self.assertIn("Restart decision: `restart_after_state_preserved`", result.stdout)
            self.assertIn("Restart first action: `recover_now`", result.stdout)
            self.assertIn("Restart reason:", result.stdout)
            self.assertIn("## Direct-Fix Boundary", result.stdout)
            self.assertIn("Direct fix ceiling score: `3/10`", result.stdout)
            self.assertIn("Recovery tooling ceiling score: `9/10`", result.stdout)

    def test_health_markdown_renders_reachability_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "health",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--check-reachability",
                "--reachability-endpoint",
                "http://127.0.0.1:9/health",
                "--reachability-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("## Reachability", result.stdout)
            self.assertIn("Reachability status:", result.stdout)
            self.assertIn("Local network issue: `true`", result.stdout)

    def test_health_can_include_service_status_probe_without_claiming_failed_check_as_outage(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "health",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                "--check-service-status",
                "--service-status-endpoint",
                "http://127.0.0.1:9/status.json",
                "--service-status-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            service_status = report["service_status"]["service_status"]
            self.assertEqual(service_status["status"], "unknown")
            self.assertTrue(service_status["check_failed"])
            self.assertFalse(service_status["upstream_issue"])

    def test_health_markdown_renders_service_status_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "health",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--check-service-status",
                "--service-status-endpoint",
                "http://127.0.0.1:9/status.json",
                "--service-status-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("## Service Status", result.stdout)
            self.assertIn("Upstream status: `unknown`", result.stdout)
            self.assertIn("Check failed: `true`", result.stdout)

    def test_reachability_report_marks_probe_success(self):
        guardian = load_guardian_module()

        report = guardian.build_reachability_report(
            "https://chatgpt.com/backend-api/codex/responses",
            timeout=3.0,
            dns_probe=lambda _host, _port: [{"family": "AF_INET", "address": "127.0.0.1"}],
            http_probe=lambda _endpoint, _timeout: {"status": "reachable", "status_code": 204, "method": "HEAD"},
        )

        self.assertEqual(report["endpoint"], "https://chatgpt.com/backend-api/codex/responses")
        self.assertEqual(report["reachability"]["status"], "reachable")
        self.assertFalse(report["reachability"]["local_network_issue"])
        self.assertIn("current process", report["reachability"]["probe_scope"])
        self.assertEqual(report["checks"]["dns"]["status"], "ok")
        self.assertEqual(report["checks"]["http"]["status"], "reachable")
        self.assertEqual(report["checks"]["http"]["status_code"], 204)

    def test_reachability_command_supports_dns_only_without_live_http(self):
        result = run_cli(
            "reachability",
            "--endpoint",
            "http://127.0.0.1/health",
            "--dns-only",
            "--format",
            "json",
        )

        self.assertEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertEqual(report["reachability"]["status"], "dns_reachable")
        self.assertFalse(report["reachability"]["local_network_issue"])
        self.assertEqual(report["checks"]["dns"]["status"], "ok")
        self.assertEqual(report["checks"]["http"]["status"], "skipped")

    def test_reachability_error_classifier_uses_transport_failure_families(self):
        guardian = load_guardian_module()

        cases = [
            (socket.gaierror("failed to lookup address information"), "dns_resolution_failed"),
            (RuntimeError("TLS handshake failed: invalid peer certificate"), "tls_handshake_failed"),
            (ConnectionResetError("connection reset by peer"), "connection_reset"),
            (TimeoutError("operation timed out"), "request_timeout"),
        ]

        for exc, expected in cases:
            with self.subTest(expected=expected):
                report = guardian.classify_reachability_error(exc)
                self.assertEqual(report["code"], expected)
                self.assertTrue(report["local_network_issue"])

    def test_service_status_report_parses_statuspage_payload(self):
        guardian = load_guardian_module()

        operational = guardian.build_service_status_report(
            "https://status.openai.com/api/v2/status.json",
            timeout=1.0,
            status_probe=lambda _endpoint, _timeout: {
                "status": {
                    "indicator": "none",
                    "description": "All Systems Operational",
                }
            },
        )
        degraded = guardian.build_service_status_report(
            "https://status.openai.com/api/v2/status.json",
            timeout=1.0,
            status_probe=lambda _endpoint, _timeout: {
                "status": {
                    "indicator": "minor",
                    "description": "Partial System Outage",
                }
            },
        )

        self.assertEqual(operational["service_status"]["status"], "operational")
        self.assertFalse(operational["service_status"]["upstream_issue"])
        self.assertEqual(degraded["service_status"]["status"], "degraded")
        self.assertTrue(degraded["service_status"]["upstream_issue"])
        self.assertFalse(degraded["service_status"]["direct_fix_available"])

    def test_service_status_command_reports_check_failure_without_upstream_claim(self):
        result = run_cli(
            "service-status",
            "--endpoint",
            "http://127.0.0.1:9/status.json",
            "--timeout",
            "0.2",
            "--format",
            "json",
        )

        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report["service_status"]["status"], "unknown")
        self.assertTrue(report["service_status"]["check_failed"])
        self.assertFalse(report["service_status"]["upstream_issue"])

    def test_service_status_attention_only_for_upstream_issues(self):
        guardian = load_guardian_module()

        degraded = guardian.build_service_status_report(
            "https://status.openai.com/api/v2/status.json",
            timeout=1.0,
            status_probe=lambda _endpoint, _timeout: {
                "status": {
                    "indicator": "major",
                    "description": "Major Outage",
                }
            },
        )
        unknown = guardian.build_service_status_report(
            "https://status.openai.com/api/v2/status.json",
            timeout=1.0,
            status_probe=lambda _endpoint, _timeout: (_ for _ in ()).throw(RuntimeError("offline")),
        )

        self.assertTrue(guardian.service_status_needs_attention(degraded))
        self.assertFalse(guardian.service_status_needs_attention(unknown))

    def test_connection_triage_reports_local_fix_boundary_for_app_state_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "connection-triage",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "app_state")
            self.assertFalse(report["connection_triage"]["direct_fix_available"])
            self.assertEqual(report["connection_triage"]["local_fix_ceiling"], "recovery and local restart guidance")
            boundary = report["connection_triage"]["direct_fix_boundary"]
            self.assertFalse(boundary["direct_fix_available"])
            self.assertEqual(boundary["direct_fix_ceiling_score"], 3)
            self.assertEqual(boundary["recovery_tooling_ceiling_score"], 9)
            self.assertEqual(boundary["highest_local_recovery_command"], "doctor --project . --hours 1")
            self.assertEqual(boundary["full_bundle_command"], "recover-now --project . --hours 1")
            self.assertIn("cannot patch Codex app internals", boundary["boundary_reason"])
            escalation = report["connection_triage"]["escalation_packet"]
            self.assertFalse(escalation["local_direct_fix_available"])
            self.assertEqual(escalation["issue_type"], "app_state")
            self.assertIn("connection-triage.json", escalation["evidence_to_preserve"])
            self.assertIn("status.json", escalation["evidence_to_preserve"])
            self.assertTrue(any("auth tokens" in item for item in escalation["do_not_share"]))
            self.assertTrue(any("recover-now --project . --hours 1" in action for action in report["connection_triage"]["local_actions"]))
            self.assertTrue(any("post-restart --project . --hours 1" in action for action in report["connection_triage"]["local_actions"]))
            self.assertTrue(any("Cannot patch Codex app internals" in item for item in report["connection_triage"]["external_boundaries"]))
            markdown = run_cli(
                "connection-triage",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )
            self.assertEqual(markdown.returncode, 1)
            self.assertIn("## Direct-Fix Ceiling", markdown.stdout)
            self.assertIn("Direct fix ceiling score: `3/10`", markdown.stdout)
            self.assertIn("Recovery tooling ceiling score: `9/10`", markdown.stdout)

    def test_connection_triage_can_include_reachability_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "connection-triage",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                "--check-reachability",
                "--reachability-endpoint",
                "http://127.0.0.1:9/health",
                "--reachability-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertTrue(report["reachability"]["reachability"]["local_network_issue"])
            self.assertEqual(report["connection_triage"]["recovery_attention"], "reachability_failed")
            self.assertTrue(any("reachability" in action.lower() for action in report["connection_triage"]["local_actions"]))

    def test_connection_triage_markdown_renders_reachability_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "connection-triage",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--check-reachability",
                "--reachability-endpoint",
                "http://127.0.0.1:9/health",
                "--reachability-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Recovery attention: `reachability_failed`", result.stdout)
            self.assertIn("## Reachability", result.stdout)
            self.assertIn("Local network issue: `true`", result.stdout)

    def test_connection_triage_can_include_service_status_probe_without_claiming_failed_check_as_outage(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "connection-triage",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                "--check-service-status",
                "--service-status-endpoint",
                "http://127.0.0.1:9/status.json",
                "--service-status-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertEqual(report["service_status"]["service_status"]["status"], "unknown")
            self.assertTrue(report["service_status"]["service_status"]["check_failed"])
            self.assertFalse(report["service_status"]["service_status"]["upstream_issue"])
            self.assertEqual(report["connection_triage"]["recovery_attention"], "none")

    def test_connection_triage_markdown_renders_service_status_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "connection-triage",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--check-service-status",
                "--service-status-endpoint",
                "http://127.0.0.1:9/status.json",
                "--service-status-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("## Service Status", result.stdout)
            self.assertIn("Upstream status: `unknown`", result.stdout)

    def test_connection_triage_reports_recovery_attention_when_post_restart_is_unstable(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            marker_dir = project / ".codex-guardian"
            marker_dir.mkdir()
            now = int(time.time())
            marker_created_at = now - 7200
            (marker_dir / "restart-marker.json").write_text(
                json.dumps({
                    "schema": "codex-guardian.restart-marker.v1",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(marker_created_at)),
                    "reason": "fixture restart",
                }) + "\n",
                encoding="utf-8",
            )
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(marker_created_at + 60, "ERROR", "codex-test", "Received turn/started for unknown conversation")],
            )

            result = run_cli(
                "connection-triage",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertEqual(report["status"]["post_restart"]["status"], "still_unstable")
            self.assertEqual(report["connection_triage"]["recovery_attention"], "post_restart_still_unstable")
            self.assertTrue(any("recover-now --project . --hours 1" in action for action in report["connection_triage"]["local_actions"]))

            markdown = run_cli(
                "connection-triage",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(markdown.returncode, 1)
            self.assertIn("Recovery attention: `post_restart_still_unstable`", markdown.stdout)
            self.assertIn("## Escalation Packet", markdown.stdout)
            self.assertIn("Local direct fix available: `false`", markdown.stdout)
            self.assertIn("connection-triage.json", markdown.stdout)
            self.assertIn("Do not share", markdown.stdout)

    def test_connection_triage_reports_recovery_attention_when_post_restart_transport_is_unreliable(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            marker_dir = project / ".codex-guardian"
            marker_dir.mkdir()
            now = int(time.time())
            marker_created_at = now - 7200
            (marker_dir / "restart-marker.json").write_text(
                json.dumps({
                    "schema": "codex-guardian.restart-marker.v1",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(marker_created_at)),
                    "reason": "fixture restart",
                }) + "\n",
                encoding="utf-8",
            )
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(marker_created_at + 60, "ERROR", "codex-test", "failed to send websocket request")],
            )

            result = run_cli(
                "connection-triage",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertEqual(report["status"]["post_restart"]["status"], "transport_unreliable")
            self.assertTrue(report["status"]["fresh_recovery_bundle_recommended"])
            self.assertEqual(report["connection_triage"]["recovery_attention"], "post_restart_transport_unreliable")
            self.assertTrue(any("recover-now --project . --hours 1 --no-mark-restart" in action for action in report["connection_triage"]["local_actions"]))

    def test_health_recommends_restart_for_repeated_app_state_timeouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "turn/start timeout while waiting for app state",
                "turn/start timeout while waiting for app state",
            ])

            result = run_cli(
                "health",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            health = report["health"]
            self.assertEqual(health["issue_type"], "app_state")
            self.assertTrue(health["restart_codex_now"])
            self.assertFalse(health["transport_unreliable"])
            self.assertIn("turn_start_timeout", health["app_state_patterns"])

    def test_post_restart_reports_app_state_errors_after_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            since = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 50))
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [
                    (now - 100, "ERROR", "codex-test", "Received turn/started for unknown conversation"),
                    (now - 10, "ERROR", "codex-test", "Received turn/completed for unknown conversation"),
                ],
            )

            result = run_cli(
                "post-restart",
                "--codex-home",
                str(codex_home),
                "--since",
                since,
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["post_restart"]["status"], "still_unstable")
            self.assertEqual(report["post_restart"]["event_count_after_restart"], 1)
            self.assertIn("unknown_conversation", report["post_restart"]["app_state_patterns_after_restart"])

    def test_post_restart_is_clean_when_errors_are_before_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            since = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 50))
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [
                    (now - 100, "ERROR", "codex-test", "Received turn/completed for unknown conversation"),
                    (now - 10, "INFO", "codex-test", "normal post-restart activity"),
                ],
            )

            result = run_cli(
                "post-restart",
                "--codex-home",
                str(codex_home),
                "--since",
                since,
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["post_restart"]["status"], "clean")
            self.assertEqual(report["post_restart"]["event_count_after_restart"], 0)
            self.assertEqual(report["post_restart"]["activity_count_after_restart"], 1)
            self.assertIn("No app-state errors", report["post_restart"]["actions"][0])

    def test_post_restart_reports_transport_failures_after_marker_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            since = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 50))
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(now - 10, "ERROR", "codex-test", "failed to send websocket request")],
            )

            result = run_cli(
                "post-restart",
                "--codex-home",
                str(codex_home),
                "--since",
                since,
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["post_restart"]["status"], "transport_unreliable")
            self.assertEqual(report["post_restart"]["app_state_patterns_after_restart"], [])
            self.assertIn("websocket_send_failed", report["post_restart"]["transport_patterns_after_restart"])
            self.assertTrue(any("Transport errors remain" in action for action in report["post_restart"]["actions"]))

    def test_post_restart_reports_network_family_transport_after_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            since = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 50))
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [
                    (now - 10, "ERROR", "codex-test", "dns error: failed to lookup address information"),
                    (now - 11, "ERROR", "codex-test", "TLS handshake failed: invalid peer certificate"),
                    (now - 12, "ERROR", "codex-test", "connection reset by peer"),
                    (
                        now - 13,
                        "ERROR",
                        "codex-test",
                        "error sending request for url https://chatgpt.com/backend-api/codex/responses: operation timed out",
                    ),
                ],
            )

            result = run_cli(
                "post-restart",
                "--codex-home",
                str(codex_home),
                "--since",
                since,
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            transport_patterns = set(report["post_restart"]["transport_patterns_after_restart"])
            self.assertEqual(report["post_restart"]["status"], "transport_unreliable")
            self.assertEqual(report["post_restart"]["app_state_patterns_after_restart"], [])
            self.assertTrue(
                {
                    "dns_resolution_failed",
                    "tls_handshake_failed",
                    "connection_reset",
                    "request_timeout",
                }.issubset(transport_patterns)
            )

    def test_post_restart_reports_no_activity_after_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            since = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 50))
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(now - 100, "ERROR", "codex-test", "Received turn/completed for unknown conversation")],
            )

            result = run_cli(
                "post-restart",
                "--codex-home",
                str(codex_home),
                "--since",
                since,
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["post_restart"]["status"], "no_activity")
            self.assertEqual(report["post_restart"]["activity_count_after_restart"], 0)
            self.assertIn("No Codex log activity", report["post_restart"]["actions"][0])

    def test_mark_restart_writes_marker_and_post_restart_uses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            now = int(time.time())
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(now - 100, "ERROR", "codex-test", "Received turn/completed for unknown conversation")],
            )

            marker = run_cli(
                "mark-restart",
                "--project",
                str(project),
                "--reason",
                "Restart Codex for app-state recovery",
                "--format",
                "json",
            )

            self.assertEqual(marker.returncode, 0, marker.stderr)
            marker_report = json.loads(marker.stdout)
            marker_path = Path(marker_report["restart_marker"]["path"])
            self.assertTrue(marker_path.exists())
            marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker_payload["reason"], "Restart Codex for app-state recovery")
            self.assertEqual(marker_payload["source"], "mark-restart")
            self.assertEqual(marker_payload["issue_type"], "manual_restart")
            self.assertEqual(marker_payload["restart_decision"]["decision"], "manual_restart_after_checkpoint")
            self.assertEqual(marker_report["restart_marker"]["restart_decision"]["decision"], "manual_restart_after_checkpoint")
            marker_markdown = run_cli(
                "mark-restart",
                "--project",
                str(project),
                "--reason",
                "Restart Codex for app-state recovery",
            )
            self.assertEqual(marker_markdown.returncode, 0, marker_markdown.stderr)
            self.assertIn("Restart decision: `manual_restart_after_checkpoint`", marker_markdown.stdout)
            con = sqlite3.connect(codex_home / "logs_2.sqlite")
            con.execute(
                "insert into logs values (?, ?, ?, ?)",
                (int(time.time()) + 1, "INFO", "codex-test", "normal post-restart activity"),
            )
            con.commit()
            con.close()

            result = run_cli(
                "post-restart",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["post_restart"]["status"], "clean")
            self.assertEqual(report["post_restart"]["marker_path"], str(marker_path))
            self.assertEqual(report["restart_marker"]["restart_decision"]["decision"], "manual_restart_after_checkpoint")

    def test_post_restart_uses_marker_and_reports_new_app_state_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            marker_dir = project / ".codex-guardian"
            marker_dir.mkdir(parents=True)
            marker_time = "2026-06-12T09:30:00+00:00"
            marker_path = marker_dir / "restart-marker.json"
            marker_path.write_text(
                json.dumps({
                    "schema": "codex-guardian.restart-marker.v1",
                    "created_at": marker_time,
                    "reason": "fixture marker",
                    "source": "watch",
                    "issue_type": "mixed",
                    "restart_timing": "after_state_preserved",
                    "restart_reason": "Mixed transport and app-state failures appeared; preserve state before restarting Codex.",
                    "restart_recommended": True,
                    "restart_codex_now": False,
                }) + "\n",
                encoding="utf-8",
            )
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(1781256610, "ERROR", "codex-test", "Received turn/completed for unknown conversation")],
            )

            result = run_cli(
                "post-restart",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["post_restart"]["status"], "still_unstable")
            self.assertEqual(Path(report["post_restart"]["marker_path"]).resolve(), marker_path.resolve())
            self.assertEqual(Path(report["restart_marker"]["path"]).resolve(), marker_path.resolve())
            self.assertEqual(report["restart_marker"]["source"], "watch")
            self.assertEqual(report["restart_marker"]["issue_type"], "mixed")
            self.assertEqual(report["restart_marker"]["restart_timing"], "after_state_preserved")
            self.assertTrue(report["restart_marker"]["restart_recommended"])
            self.assertFalse(report["restart_marker"]["restart_codex_now"])
            self.assertEqual(report["restart_marker"]["restart_decision"]["decision"], "restart_after_state_preserved")
            self.assertIn("unknown_conversation", report["post_restart"]["app_state_patterns_after_restart"])

    def test_post_restart_markdown_includes_restart_marker_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            marker_dir = project / ".codex-guardian"
            marker_dir.mkdir(parents=True)
            marker_time = "2026-06-12T09:30:00+00:00"
            marker_path = marker_dir / "restart-marker.json"
            marker_path.write_text(
                json.dumps({
                    "schema": "codex-guardian.restart-marker.v1",
                    "created_at": marker_time,
                    "reason": "fixture marker",
                    "source": "doctor",
                    "issue_type": "app_state",
                    "restart_timing": "now_after_checkpoint",
                    "restart_reason": "App-state failures continued after checkpointing.",
                    "restart_recommended": True,
                    "restart_codex_now": True,
                }) + "\n",
                encoding="utf-8",
            )
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(1781256610, "INFO", "codex-test", "normal post-restart activity")],
            )

            result = run_cli(
                "post-restart",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("## Restart Marker", result.stdout)
            self.assertIn("- Path: `", result.stdout)
            self.assertIn("/.codex-guardian/restart-marker.json`", result.stdout)
            self.assertIn("- Source: `doctor`", result.stdout)
            self.assertIn("- Issue type: `app_state`", result.stdout)
            self.assertIn("- Restart timing: `now_after_checkpoint`", result.stdout)
            self.assertIn("- Restart Codex now: `True`", result.stdout)

    def test_post_restart_expands_lookback_to_cover_marker_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            marker_dir = project / ".codex-guardian"
            marker_dir.mkdir(parents=True)
            marker_path = marker_dir / "restart-marker.json"
            now = int(time.time())
            marker_time = now - 7200
            marker_path.write_text(
                json.dumps({
                    "schema": "codex-guardian.restart-marker.v1",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(marker_time)),
                    "reason": "old marker",
                    "project": str(project),
                }),
                encoding="utf-8",
            )
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(marker_time + 60, "ERROR", "codex-test", "Received turn/started for unknown conversation")],
            )

            result = run_cli(
                "post-restart",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1, result.stdout)
            report = json.loads(result.stdout)
            self.assertGreaterEqual(report["hours"], 2)
            self.assertEqual(report["post_restart"]["status"], "still_unstable")
            self.assertEqual(report["post_restart"]["event_count_after_restart"], 1)

    def test_doctor_creates_bundle_and_restart_plan_for_app_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "doctor",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "app_state")
            self.assertTrue(report["doctor"]["created_recovery_bundle"])
            bundle = Path(report["recovery_report"])
            self.assertTrue((bundle / "diagnosis.json").exists())
            self.assertTrue((bundle / "doctor.json").exists())
            self.assertTrue((bundle / "doctor.md").exists())
            doctor_bundle = json.loads((bundle / "doctor.json").read_text(encoding="utf-8"))
            self.assertEqual(doctor_bundle["doctor"]["actions"], report["doctor"]["actions"])
            self.assertIn("status", report)
            self.assertEqual(report["status"], doctor_bundle["status"])
            self.assertEqual(Path(report["status"]["latest_recovery_bundle"]).resolve(), bundle.resolve())
            doctor_markdown = (bundle / "doctor.md").read_text(encoding="utf-8")
            self.assertIn("Restart Codex", doctor_markdown)
            self.assertTrue(any("status.md" in action for action in report["doctor"]["actions"]))
            self.assertIn("status.md", doctor_markdown)
            self.assertIn("Checkpoint active work", report["doctor"]["actions"][0])
            self.assertTrue(any("Restart Codex" in action for action in report["doctor"]["actions"]))

    def test_doctor_can_write_preflight_checkpoint_before_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("ready\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "doctor",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--task",
                "Recover active task",
                "--touched",
                "target.txt",
                "--slice-minutes",
                "12",
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertTrue(report["doctor"]["created_preflight_checkpoint"])
            self.assertIn("preflight_checkpoint", report)
            self.assertIn("Preflight checkpoint written", report["doctor"]["actions"][0])
            checkpoint = json.loads((project / ".codex-guardian" / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["phase"], "preflight_done")
            self.assertEqual(checkpoint["task"], "Recover active task")
            self.assertEqual(checkpoint["slice_minutes"], 12)
            self.assertIn("touched file exists: target.txt", checkpoint["verified"])
            self.assertTrue(Path(report["preflight_checkpoint"]).exists())
            bundle = Path(report["recovery_report"])
            bundled_checkpoint = json.loads((bundle / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(bundled_checkpoint["task"], "Recover active task")
            doctor_bundle = json.loads((bundle / "doctor.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(doctor_bundle["preflight_checkpoint"]).resolve(), Path(report["preflight_checkpoint"]).resolve())

    def test_doctor_task_writes_preflight_checkpoint_even_when_health_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("ready\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "doctor",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--task",
                "Prepare healthy task",
                "--touched",
                "target.txt",
                "--slice-minutes",
                "11",
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["doctor"]["created_preflight_checkpoint"])
            self.assertFalse(report["doctor"]["created_recovery_bundle"])
            self.assertIn("preflight_checkpoint", report)
            self.assertNotIn("recovery_report", report)
            self.assertIn("Preflight checkpoint written", report["doctor"]["actions"][0])
            self.assertTrue(any("No known Codex connection failure" in action for action in report["doctor"]["actions"]))
            checkpoint = json.loads((project / ".codex-guardian" / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["phase"], "preflight_done")
            self.assertEqual(checkpoint["task"], "Prepare healthy task")
            self.assertEqual(checkpoint["slice_minutes"], 11)
            self.assertIn("touched file exists: target.txt", checkpoint["verified"])
            self.assertTrue(Path(report["preflight_checkpoint"]).exists())

    def test_doctor_can_mark_restart_for_app_state_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "Received turn/started for unknown conversation",
                "Received turn/completed for unknown conversation",
            ])

            result = run_cli(
                "doctor",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--mark-restart",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertTrue(report["doctor"]["created_restart_marker"])
            marker_path = project / ".codex-guardian" / "restart-marker.json"
            self.assertEqual(Path(report["restart_marker"]["path"]).resolve(), marker_path.resolve())
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["schema"], "codex-guardian.restart-marker.v1")
            self.assertIn("doctor", marker["reason"])
            self.assertTrue(any("post-restart" in action for action in report["doctor"]["actions"]))
            bundle = Path(report["recovery_report"])
            diagnosis = json.loads((bundle / "diagnosis.json").read_text(encoding="utf-8"))
            self.assertIn("restart_marker", diagnosis)
            self.assertEqual(Path(diagnosis["restart_marker"]["path"]).resolve(), marker_path.resolve())
            self.assertIn("## Restart Marker", (bundle / "diagnosis.md").read_text(encoding="utf-8"))

    def test_doctor_mark_restart_refreshes_marker_for_post_restart_instability(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            marker_dir = project / ".codex-guardian"
            marker_dir.mkdir()
            now = int(time.time())
            old_marker_time = now - 7200
            marker_path = marker_dir / "restart-marker.json"
            marker_path.write_text(
                json.dumps({
                    "schema": "codex-guardian.restart-marker.v1",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(old_marker_time)),
                    "reason": "fixture stale restart",
                    "source": "watch",
                    "issue_type": "mixed",
                    "restart_timing": "after_state_preserved",
                    "restart_recommended": True,
                    "restart_codex_now": False,
                }) + "\n",
                encoding="utf-8",
            )
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(old_marker_time + 60, "ERROR", "codex-test", "Received turn/completed for unknown conversation")],
            )

            result = run_cli(
                "doctor",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--mark-restart",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertTrue(report["doctor"]["created_restart_marker"])
            self.assertEqual(report["restart_marker"]["issue_type"], "post_restart_still_unstable")
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["source"], "doctor")
            self.assertEqual(marker["issue_type"], "post_restart_still_unstable")
            self.assertEqual(marker["restart_timing"], "after_state_preserved")
            self.assertTrue(marker["restart_recommended"])
            self.assertFalse(marker["restart_codex_now"])

    def test_doctor_creates_bundle_for_overdue_checkpoint_even_when_health_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            guardian = project / ".codex-guardian"
            guardian.mkdir()
            checkpoint = {
                "schema": "codex-guardian.checkpoint.v1",
                "created_at": "2026-06-12T00:00:00+00:00",
                "task": "Overdue recovery task",
                "phase": "preflight_done",
                "status": "in_progress",
                "next_action": "Continue",
                "touched": ["target.txt"],
                "verified": [],
                "notes": [],
                "cwd": str(project),
                "slice_minutes": 15,
                "checkpoint_due_at": "2026-06-12T00:01:00+00:00",
            }
            (guardian / "current.json").write_text(json.dumps(checkpoint) + "\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "doctor",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertTrue(report["checkpoint_attention"]["checkpoint_overdue"])
            self.assertTrue(report["doctor"]["created_recovery_bundle"])
            self.assertTrue(any("fresh checkpoint" in action.lower() for action in report["doctor"]["actions"]))
            bundle = Path(report["recovery_report"])
            diagnosis = json.loads((bundle / "diagnosis.json").read_text(encoding="utf-8"))
            self.assertTrue(diagnosis["checkpoint_attention"]["checkpoint_overdue"])

    def test_doctor_creates_bundle_for_unreadable_checkpoint_even_when_health_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            guardian = project / ".codex-guardian"
            guardian.mkdir()
            checkpoint_path = guardian / "current.json"
            checkpoint_path.write_text("{not valid json\n", encoding="utf-8")
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "doctor",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertEqual(Path(report["checkpoint_attention"]["checkpoint_read_error_path"]).resolve(), checkpoint_path.resolve())
            self.assertIn("checkpoint_read_error", report["checkpoint_attention"])
            self.assertTrue(report["doctor"]["created_recovery_bundle"])
            self.assertTrue(any("current.json" in action for action in report["doctor"]["actions"]))
            bundle = Path(report["recovery_report"])
            diagnosis = json.loads((bundle / "diagnosis.json").read_text(encoding="utf-8"))
            self.assertIn("checkpoint_read_error", diagnosis["checkpoint_attention"])

    def test_doctor_creates_bundle_for_post_restart_transport_attention_even_when_health_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            marker_dir = project / ".codex-guardian"
            marker_dir.mkdir()
            now = int(time.time())
            marker_created_at = now - 7200
            (marker_dir / "restart-marker.json").write_text(
                json.dumps({
                    "schema": "codex-guardian.restart-marker.v1",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(marker_created_at)),
                    "reason": "fixture restart",
                }) + "\n",
                encoding="utf-8",
            )
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log_rows(
                codex_home,
                "ts integer, level text, target text, feedback_log_body text",
                [(marker_created_at + 60, "ERROR", "codex-test", "failed to send websocket request")],
            )

            result = run_cli(
                "doctor",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertTrue(report["doctor"]["created_recovery_bundle"])
            self.assertEqual(report["status"]["post_restart"]["status"], "transport_unreliable")
            self.assertEqual(report["status"]["latest_recovery_bundle"], report["recovery_report"])
            self.assertTrue(any("recover-now --project . --hours 1 --no-mark-restart" in action for action in report["doctor"]["actions"]))
            bundle = Path(report["recovery_report"])
            triage = json.loads((bundle / "connection-triage.json").read_text(encoding="utf-8"))
            self.assertEqual(triage["connection_triage"]["recovery_attention"], "post_restart_transport_unreliable")

    def test_doctor_guides_auth_session_recovery_without_restart_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "request to /backend-api/codex/responses failed: session expired, authentication required",
            ])

            result = run_cli(
                "doctor",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "auth_session")
            self.assertTrue(report["doctor"]["created_recovery_bundle"])
            self.assertFalse(any("Restart Codex" in action for action in report["doctor"]["actions"]))
            self.assertTrue(any("Sign in" in action or "auth" in action.lower() for action in report["doctor"]["actions"]))

    def test_doctor_exits_cleanly_without_bundle_when_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "doctor",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertFalse(report["doctor"]["created_recovery_bundle"])
            self.assertNotIn("recovery_report", report)
            self.assertIn("No known Codex connection failure", report["doctor"]["actions"][0])

    def test_doctor_check_reachability_creates_bundle_when_endpoint_unreachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "doctor",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                "--check-reachability",
                "--reachability-endpoint",
                "http://127.0.0.1:9/health",
                "--reachability-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertTrue(report["reachability"]["reachability"]["local_network_issue"])
            self.assertTrue(report["doctor"]["created_recovery_bundle"])
            self.assertTrue(any("reachability" in action.lower() for action in report["doctor"]["actions"]))
            bundle = Path(report["recovery_report"])
            self.assertTrue((bundle / "reachability.json").exists())
            bundled_doctor = json.loads((bundle / "doctor.json").read_text(encoding="utf-8"))
            self.assertEqual(bundled_doctor["reachability"]["reachability"], report["reachability"]["reachability"])

    def test_doctor_check_service_status_creates_bundle_when_upstream_degraded(self):
        guardian = load_guardian_module()
        service_report = guardian.build_service_status_report(
            "https://status.example.test/status.json",
            timeout=1.0,
            status_probe=lambda _endpoint, _timeout: {
                "status": {
                    "indicator": "major",
                    "description": "Major Outage",
                }
            },
        )
        original_status_builder = guardian.build_service_status_report
        original_desktop_log_dirs = guardian.desktop_log_dirs
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            output = Path(tmp) / "doctor.json"
            args = argparse.Namespace(
                codex_home=str(codex_home),
                project=str(project),
                hours=1,
                limit=80,
                format="json",
                output=str(output),
                task=None,
                touched=None,
                slice_minutes=15,
                mark_restart=False,
                check_reachability=False,
                reachability_endpoint=guardian.DEFAULT_REACHABILITY_ENDPOINT,
                reachability_timeout=1.0,
                reachability_dns_only=False,
                check_service_status=True,
                service_status_endpoint="https://status.example.test/status.json",
                service_status_timeout=1.0,
            )
            try:
                guardian.build_service_status_report = lambda _endpoint, _timeout: service_report
                guardian.desktop_log_dirs = lambda _hours: []
                exit_code = guardian.cmd_doctor(args)
            finally:
                guardian.build_service_status_report = original_status_builder
                guardian.desktop_log_dirs = original_desktop_log_dirs

            self.assertEqual(exit_code, 1)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["health"]["issue_type"], "healthy")
            service_status = report["service_status"]["service_status"]
            self.assertEqual(service_status["status"], "degraded")
            self.assertTrue(service_status["upstream_issue"])
            self.assertTrue(report["doctor"]["created_recovery_bundle"])
            self.assertTrue(any("Upstream service status is degraded" in action for action in report["doctor"]["actions"]))
            bundle = Path(report["recovery_report"])
            bundled_status = json.loads((bundle / "service-status.json").read_text(encoding="utf-8"))
            self.assertEqual(bundled_status["service_status"], service_status)
            triage = json.loads((bundle / "connection-triage.json").read_text(encoding="utf-8"))
            self.assertEqual(triage["connection_triage"]["recovery_attention"], "upstream_degraded")

    def test_doctor_check_service_status_failed_check_stays_clean_when_logs_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            result = run_cli(
                "doctor",
                "--codex-home",
                str(codex_home),
                "--project",
                str(project),
                "--hours",
                "1",
                "--format",
                "json",
                "--check-service-status",
                "--service-status-endpoint",
                "http://127.0.0.1:9/status.json",
                "--service-status-timeout",
                "0.2",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            service_status = report["service_status"]["service_status"]
            self.assertEqual(report["health"]["issue_type"], "healthy")
            self.assertEqual(service_status["status"], "unknown")
            self.assertTrue(service_status["check_failed"])
            self.assertFalse(service_status["upstream_issue"])
            self.assertFalse(report["doctor"]["created_recovery_bundle"])
            self.assertNotIn("recovery_report", report)

    def test_wrap_writes_preflight_facts_before_guarded_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("ready\n", encoding="utf-8")

            result = run_cli(
                "wrap",
                "--project",
                str(project),
                "--task",
                "Guard long command",
                "--touched",
                "target.txt",
                "--slice-minutes",
                "20",
                "--",
                sys.executable,
                "-c",
                "print('ok')",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            checkpoints = sorted((project / ".codex-guardian" / "checkpoints").glob("*.json"))
            self.assertGreaterEqual(len(checkpoints), 2)
            start = next(
                json.loads(path.read_text(encoding="utf-8"))
                for path in checkpoints
                if json.loads(path.read_text(encoding="utf-8"))["phase"] == "preflight_done"
            )
            self.assertEqual(start["phase"], "preflight_done")
            self.assertEqual(start["slice_minutes"], 20)
            self.assertIn("target.txt", start["touched"])
            self.assertIn("touched file exists: target.txt", start["verified"])
            self.assertTrue(any(value.startswith("git status:") for value in start["verified"]))
            self.assertTrue(any(value.startswith("Command:") for value in start["verified"]))

    def test_redact_removes_private_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            write_sqlite_log(codex_home, [
                "unknown conversation Bearer abc.def-ghi sk-test_123456789012345 user kent@example.com "
                "conversationId=019ebaf2-d57c-71d3-8f20-5e5b3f48d1ba "
                "threadId=019ebaef-5249-7222-bf11-5a77e8c990e8 "
                "opaque=abcdefghijklmnopqrstuvwxyz1234567890",
            ])

            result = run_cli(
                "diagnose",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
                env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            text = result.stdout
            self.assertNotIn("kent@example.com", text)
            self.assertNotIn("019ebaf2-d57c-71d3-8f20-5e5b3f48d1ba", text)
            self.assertNotIn("019ebaef-5249-7222-bf11-5a77e8c990e8", text)
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz1234567890", text)
            self.assertIn("[REDACTED_EMAIL]", text)
            self.assertIn("conversationId=[REDACTED_ID]", text)

    def test_diagnose_handles_alternate_sqlite_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            now = int(time.time())
            write_sqlite_log_rows(
                codex_home,
                "created_at integer, severity text, module text, message text",
                [(now, "ERROR", "alt-target", "stream disconnected before completion")],
            )

            result = run_cli(
                "diagnose",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertGreaterEqual(report["summary"]["pattern_counts"].get("stream_disconnect", 0), 1)

    def test_diagnose_reports_unsupported_sqlite_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            write_sqlite_log_rows(codex_home, "only_id integer", [(1,)])

            result = run_cli(
                "diagnose",
                "--codex-home",
                str(codex_home),
                "--hours",
                "1",
                "--format",
                "json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["events"][0]["matches"][0]["code"], "sqlite_schema_unsupported")

    def test_preflight_records_touched_file_statuses_without_creating_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "exists.txt").write_text("ok\n", encoding="utf-8")
            missing = project / "missing.txt"

            result = run_cli(
                "preflight",
                "--project",
                str(project),
                "--task",
                "Check files",
                "--next-action",
                "Edit exists.txt",
                "--touched",
                "exists.txt",
                "--touched",
                "missing.txt",
                "--touched",
                "../outside.txt",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            checkpoint = json.loads((project / ".codex-guardian" / "current.json").read_text())
            facts = "\n".join(checkpoint["verified"] + checkpoint["notes"])
            self.assertIn("touched file exists: exists.txt", facts)
            self.assertIn("touched file missing: missing.txt", facts)
            self.assertIn("touched file outside project: ../outside.txt", facts)
            self.assertFalse(missing.exists())

    def test_validate_skill_uses_stdlib_frontmatter_check(self):
        result = run_cli("validate-skill", "--skill-dir", str(ROOT / "skills" / "codex-guardian"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Skill is valid", result.stdout)

    def test_validate_skill_rejects_incomplete_skill_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "codex-guardian"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: codex-guardian\n"
                "description: Local recovery workflow for Codex connection failures.\n"
                "---\n",
                encoding="utf-8",
            )

            result = run_cli("validate-skill", "--skill-dir", str(skill_dir))

            self.assertEqual(result.returncode, 1)
            self.assertIn("Skill is incomplete", result.stderr)
            self.assertIn("scripts/codex_guardian.py", result.stderr)

    def test_checkpoint_detects_no_progress_from_unchanged_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "target.txt").write_text("same\n", encoding="utf-8")

            start = run_cli(
                "checkpoint",
                "--project",
                str(project),
                "--task",
                "No progress slice",
                "--phase",
                "write_started",
                "--next-action",
                "Edit target.txt",
                "--touched",
                "target.txt",
                "--fingerprint",
            )
            self.assertEqual(start.returncode, 0, start.stderr)

            finish = run_cli(
                "checkpoint",
                "--project",
                str(project),
                "--task",
                "No progress slice",
                "--phase",
                "write_done",
                "--next-action",
                "Report no progress",
                "--touched",
                "target.txt",
                "--fingerprint",
                "--compare-fingerprint",
            )

            self.assertEqual(finish.returncode, 0, finish.stderr)
            checkpoint = json.loads((project / ".codex-guardian" / "current.json").read_text())
            self.assertEqual(checkpoint["status"], "no_progress")
            self.assertIn("no_progress: fingerprint unchanged", checkpoint["verified"])

    def test_self_test_runs_soak_corpus_without_private_leaks(self):
        result = run_cli("self-test", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "passed")
        self.assertGreaterEqual(report["checks_passed"], 6)
        self.assertIn("stream_disconnect", report["pattern_counts"])
        self.assertIn("responses_websocket_failure", report["pattern_counts"])
        self.assertEqual(report["pattern_counts"].get("unknown_conversation"), 1)
        text = result.stdout
        self.assertNotIn("kent@example.com", text)
        self.assertNotIn("019ebaf2-d57c-71d3-8f20-5e5b3f48d1ba", text)

    def test_self_test_uses_redacted_real_log_fixture_corpus(self):
        result = run_cli("self-test", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        check_names = {check["name"] for check in report["checks"]}
        self.assertIn("fixture corpus loaded", check_names)
        self.assertIn("fixture corpus detects desktop unknown conversation", check_names)
        self.assertIn("fixture corpus detects responses retry stream disconnected warning", check_names)
        self.assertIn("fixture corpus detects startup prewarm badrecordmac tls failure", check_names)
        self.assertIn("fixture corpus detects websocket send failure", check_names)
        self.assertIn("fixture corpus detects websocket closed before response completed", check_names)
        self.assertIn("fixture corpus detects auth session failure", check_names)
        self.assertIn("fixture corpus ignores streamed assistant quote", check_names)
        self.assertIn("fixture corpus ignores outbound response.create payload", check_names)
        self.assertIn("fixture corpus ignores app server goal payload quoting recovery terms", check_names)

    def test_self_test_checks_validate_skill_integrity(self):
        result = run_cli("self-test", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        checks = {check["name"]: check for check in report["checks"]}
        self.assertIn("validate skill required files and frontmatter", checks)
        self.assertTrue(checks["validate skill required files and frontmatter"]["passed"])
        self.assertEqual(checks["validate skill required files and frontmatter"]["missing"], [])
        self.assertIn("validate skill rejects missing required files", checks)
        self.assertTrue(checks["validate skill rejects missing required files"]["passed"])
        self.assertIn("scripts/codex_guardian.py", checks["validate skill rejects missing required files"]["missing"])

    def test_self_test_exercises_doctor_recovery_bundle_artifacts(self):
        result = run_cli("self-test", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        checks = {check["name"]: check for check in report["checks"]}
        self.assertIn("write doctor recovery bundle", checks)
        self.assertTrue(checks["write doctor recovery bundle"]["passed"])
        self.assertIn("write doctor recovery bundle status snapshot", checks)
        self.assertTrue(checks["write doctor recovery bundle status snapshot"]["passed"])
        self.assertIn("connection triage recovery attention", checks)
        self.assertTrue(checks["connection triage recovery attention"]["passed"])
        self.assertIn("connection triage escalation packet", checks)
        self.assertTrue(checks["connection triage escalation packet"]["passed"])
        self.assertIn("post-restart transport unreliable", checks)
        self.assertTrue(checks["post-restart transport unreliable"]["passed"])
        self.assertIn("doctor recovery bundle resume prompt status guidance", checks)
        self.assertTrue(checks["doctor recovery bundle resume prompt status guidance"]["passed"])
        self.assertIn("doctor recovery bundle reachability snapshot", checks)
        self.assertTrue(checks["doctor recovery bundle reachability snapshot"]["passed"])
        self.assertIn("doctor recovery bundle environment snapshot", checks)
        self.assertTrue(checks["doctor recovery bundle environment snapshot"]["passed"])
        self.assertIn("doctor recovery bundle service status snapshot", checks)
        self.assertTrue(checks["doctor recovery bundle service status snapshot"]["passed"])
        self.assertIn("reachability classifier transport families", checks)
        self.assertTrue(checks["reachability classifier transport families"]["passed"])
        self.assertIn("doctor reachability attention action", checks)
        self.assertTrue(checks["doctor reachability attention action"]["passed"])
        self.assertIn("health reachability report", checks)
        self.assertTrue(checks["health reachability report"]["passed"])
        self.assertIn("connection triage reachability boundary", checks)
        self.assertTrue(checks["connection triage reachability boundary"]["passed"])
        self.assertIn("service status parser boundary", checks)
        self.assertTrue(checks["service status parser boundary"]["passed"])

    def test_package_command_creates_clean_archive_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "dist"

            result = run_cli("package", "--output-dir", str(output_dir))

            self.assertEqual(result.returncode, 0, result.stderr)
            manifest_path = output_dir / "codex-guardian-package.json"
            archive_path = output_dir / "codex-guardian.tar.gz"
            self.assertTrue(manifest_path.exists())
            self.assertTrue(archive_path.exists())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["skill"], "codex-guardian")
            self.assertNotIn(str(ROOT), manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["archive_sha256"], manifest["archive_sha256"].lower())
            self.assertGreater(manifest["file_count"], 5)
            self.assertEqual(manifest["missing_required_files"], [])
            self.assertIn("codex-guardian/scripts/codex_guardian.py", manifest["required_files"])
            self.assertIn("codex-guardian/fixtures/redacted-real-log-corpus.json", manifest["required_files"])
            for required in manifest["required_files"]:
                self.assertIn(required, manifest["files"])
            with tarfile.open(archive_path, "r:gz") as archive:
                names = archive.getnames()
            self.assertIn("codex-guardian/SKILL.md", names)
            for required in manifest["required_files"]:
                self.assertIn(required, names)
            self.assertFalse(any(name.endswith(".DS_Store") for name in names))
            self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))


if __name__ == "__main__":
    unittest.main()
