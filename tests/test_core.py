from pathlib import Path
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tracer import core


def make_session() -> core.Session:
    turn = core.Turn(
        request_id="req-1",
        timestamp="2026-05-19T10:00:00Z",
        input_tokens=1,
        output_tokens=1,
    )
    return core.Session(
        path=Path("/tmp/session.jsonl"),
        session_id="session-1234567890",
        agent_type=None,
        command=None,
        first_user_prompt="",
        cwd="/tmp",
        turns=[turn],
    )


class OutputDirTests(unittest.TestCase):
    def test_out_arg_does_not_compute_default_run_dir(self):
        session = make_session()

        with tempfile.TemporaryDirectory() as td:
            expected = Path(td, "trace-out").resolve()
            with patch("tracer.core.default_run_dir", side_effect=AssertionError("should not be called")):
                self.assertEqual(core.resolve_output_dir(session, str(expected), "label"), expected)

    def test_missing_out_arg_uses_default_run_dir(self):
        session = make_session()

        with patch("tracer.core.default_run_dir", return_value=Path("/tmp/default-trace")) as default_run_dir:
            self.assertEqual(core.resolve_output_dir(session, None, "label"), Path("/tmp/default-trace"))
            default_run_dir.assert_called_once_with(session, label_hint="label")


class PublicCliTests(unittest.TestCase):
    def test_help_exposes_lightweight_command_set(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), self.assertRaises(SystemExit) as cm:
            core.main(["--help"])

        self.assertEqual(cm.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("{save,ls,sessions,config,version,open,read,clip,mcp}", help_text)
        for command in ("track", "history", "render", "diff", "compare", "path", "init", "migrate"):
            self.assertNotIn(command, help_text)

    def test_ls_sessions_option_is_not_supported(self):
        stderr = io.StringIO()
        with patch("sys.stderr", stderr), self.assertRaises(SystemExit) as cm:
            core.main(["ls", "--sessions"])

        self.assertEqual(cm.exception.code, 2)
        self.assertIn("unrecognized arguments: --sessions", stderr.getvalue())

    def test_ls_displays_turn_count_from_saved_trace_summary(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            project.mkdir()
            (project / "pyproject.toml").write_text("[project]\nname = 'example'\n")

            session = make_session()
            session.cwd = str(project)
            session.turns.append(core.Turn(
                request_id="req-2",
                timestamp="2026-05-19T10:01:00Z",
                input_tokens=2,
                output_tokens=2,
            ))
            trace_dir = project / ".tracer" / "traces" / "sample"
            trace_dir.mkdir(parents=True)
            core.emit_trace_json(session, trace_dir / "trace.json")
            db_path = core.project_db_path(project, create=True)
            core.track(session, db_path, label="sample", trace_dir=trace_dir)

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                core.main(["ls", "--cwd", str(project)])

        output = stdout.getvalue()
        self.assertIn("sample", output)
        self.assertIn("    2 ", output)

    def test_config_set_writes_project_config(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            project.mkdir()
            (project / "pyproject.toml").write_text("[project]\nname = 'example'\n")

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                core.main([
                    "config",
                    "set",
                    "claude_config_dir",
                    "~/.claude-pk",
                    "--cwd",
                    str(project),
                ])

            config_path = project / ".tracer" / "config.json"
            data = json.loads(config_path.read_text())

        self.assertEqual(data["claude_config_dir"], "~/.claude-pk")
        self.assertIn("updated:", stdout.getvalue())


class InitRootTests(unittest.TestCase):
    def test_project_root_ignores_user_home_tracer_for_child_directory(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td, "home")
            child = home / "scratch"
            (home / ".tracer").mkdir(parents=True)
            child.mkdir(parents=True)

            with patch("pathlib.Path.home", return_value=home):
                self.assertIsNone(core.find_project_root(child))

    def test_project_root_ignores_user_home_project_markers_for_child_directory(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td, "home")
            child = home / "scratch"
            home.mkdir(parents=True)
            child.mkdir(parents=True)
            (home / "package.json").write_text("{}")

            with patch("pathlib.Path.home", return_value=home):
                self.assertIsNone(core.find_project_root(child))

    def test_session_project_root_falls_back_to_session_cwd_when_only_home_tracer_exists(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td, "home")
            child = home / "scratch"
            (home / ".tracer").mkdir(parents=True)
            child.mkdir(parents=True)
            session = make_session()
            session.cwd = str(child)

            with patch("pathlib.Path.home", return_value=home):
                self.assertEqual(core.session_project_root(session), child.resolve())

    def test_init_root_ignores_ancestor_tracer_dir_without_project_marker(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td, "home")
            project = home / "work" / "unmarked-project"
            (home / ".tracer").mkdir(parents=True)
            project.mkdir(parents=True)

            self.assertEqual(core.find_init_target_root(project), project.resolve())

    def test_init_root_uses_project_marker_before_ancestor_tracer_dir(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td, "home")
            project = home / "work" / "marked-project"
            nested = project / "src"
            (home / ".tracer").mkdir(parents=True)
            nested.mkdir(parents=True)
            (project / "pyproject.toml").write_text("[project]\nname = 'example'\n")

            self.assertEqual(core.find_init_target_root(nested), project.resolve())

    def test_cmd_init_creates_tracer_in_current_unmarked_directory(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td, "home")
            project = home / "work" / "unmarked-project"
            (home / ".tracer").mkdir(parents=True)
            project.mkdir(parents=True)
            old_cwd = os.getcwd()
            try:
                os.chdir(project)
                core.cmd_init(here=False, skip_gitignore_prompt=True)
            finally:
                os.chdir(old_cwd)

            self.assertTrue((project / ".tracer" / "traces").is_dir())
            self.assertFalse((home / ".tracer" / "traces").exists())


class SessionDiscoveryTests(unittest.TestCase):
    def test_project_config_can_override_claude_config_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "pyproject.toml").write_text("[project]\nname = 'example'\n")
            (project / ".tracer").mkdir()
            (project / ".tracer" / "config.json").write_text(json.dumps({
                "claude_config_dir": str(root / ".claude-pk")
            }))

            custom_projects = root / ".claude-pk" / "projects" / "encoded-project"
            custom_projects.mkdir(parents=True)
            session_path = custom_projects / "session-pk.jsonl"
            session_path.write_text(json.dumps({
                "type": "user",
                "cwd": str(project),
                "message": {"content": "hello"},
            }) + "\n")

            matches = core.find_sessions_for_cwd(str(project), source="claude")

        self.assertEqual([m.path for m in matches], [session_path])
        self.assertEqual(matches[0].session_id, "session-pk")

    def test_resolve_input_uses_project_claude_projects_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "pyproject.toml").write_text("[project]\nname = 'example'\n")
            (project / ".tracer").mkdir()

            custom_root = root / "claude-projects"
            session_dir = custom_root / "encoded-project"
            session_dir.mkdir(parents=True)
            session_path = session_dir / "session-custom.jsonl"
            session_path.write_text("{}\n")
            (project / ".tracer" / "config.json").write_text(json.dumps({
                "claude_projects_dir": str(custom_root)
            }))

            resolved = core.resolve_input("session-custom", source="claude", cwd=project)

        self.assertEqual(resolved, session_path)


class TraceJsonTests(unittest.TestCase):
    def test_tool_result_content_round_trips(self):
        full = "0123456789" * 400
        session = make_session()
        session.turns[0].events.append(
            core.ToolCall(
                name="Bash",
                input_label="printf",
                result_tokens=1000,
                tool_use_id="toolu-test",
                input_full={"command": "printf long"},
                result_content=full,
                result_preview=full[:3000],
                result_full_chars=len(full),
            )
        )

        with tempfile.TemporaryDirectory() as td:
            trace_json = Path(td, "trace.json")
            core.emit_trace_json(session, trace_json)
            loaded = core.load_trace_json(trace_json)

        loaded_tool = loaded.turns[0].tool_calls[0]
        self.assertEqual(loaded_tool.result_content, full)
        self.assertEqual(loaded_tool.result_preview, full[:3000])
        self.assertEqual(loaded_tool.result_full_chars, len(full))

    def test_legacy_preview_loads_as_content(self):
        legacy = {
            "schema_version": core.SCHEMA_VERSION,
            "task": {},
            "session": {
                "id": "session-legacy",
                "agent_type": None,
                "command": None,
                "cwd": "/tmp",
                "first_user_prompt": "",
                "label": "session: legacy",
                "clip": None,
                "totals": {},
                "turns": [
                    {
                        "id": "req-1",
                        "n": 1,
                        "timestamp": "2026-05-19T10:00:00Z",
                        "model": None,
                        "usage": {},
                        "events": [
                            {
                                "kind": "tool_use",
                                "id": "toolu-legacy",
                                "name": "Bash",
                                "input_label": "echo",
                                "input": {"command": "echo legacy"},
                                "timestamp": "2026-05-19T10:00:00Z",
                                "result": {
                                    "tokens": 3,
                                    "full_chars": 14,
                                    "preview": "legacy result",
                                },
                            }
                        ],
                    }
                ],
            },
            "annotations": {},
        }

        with tempfile.TemporaryDirectory() as td:
            trace_json = Path(td, "trace.json")
            trace_json.write_text(__import__("json").dumps(legacy))
            loaded = core.load_trace_json(trace_json)

        loaded_tool = loaded.turns[0].tool_calls[0]
        self.assertEqual(loaded_tool.result_content, "legacy result")
        self.assertEqual(loaded_tool.result_preview, "legacy result")

    def test_user_message_round_trips(self):
        session = make_session()
        session.turns[0].events.append(core.UserMessage(
            id="req-1:user:0",
            text="please fix this",
            timestamp="2026-05-19T09:59:59Z",
        ))

        with tempfile.TemporaryDirectory() as td:
            trace_json = Path(td, "trace.json")
            core.emit_trace_json(session, trace_json)
            loaded = core.load_trace_json(trace_json)

        self.assertEqual(loaded.turns[0].user_messages, ["please fix this"])


class ClaudeParserTests(unittest.TestCase):
    def test_claude_session_preserves_user_messages_on_turns(self):
        rows = [
            {
                "type": "user",
                "timestamp": "2026-05-19T09:59:59Z",
                "cwd": "/tmp/project",
                "message": {"content": "first prompt"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-19T10:00:00Z",
                "requestId": "req-1",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                    "content": [{"type": "text", "text": "first answer"}],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-05-19T10:00:01Z",
                "cwd": "/tmp/project",
                "message": {"content": [{"type": "text", "text": "second prompt"}]},
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-19T10:00:02Z",
                "requestId": "req-2",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                    "content": [{"type": "text", "text": "second answer"}],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "session-claude.jsonl")
            path.write_text("\n".join(json.dumps(r) for r in rows))
            session = core.parse_session(path)

        self.assertEqual(session.first_user_prompt, "first prompt")
        self.assertEqual([t.user_messages for t in session.turns], [["first prompt"], ["second prompt"]])
        self.assertEqual([t.text_blocks for t in session.turns], [["first answer"], ["second answer"]])


class CodexParserTests(unittest.TestCase):
    def test_codex_rollout_maps_to_common_trace_model(self):
        rows = [
            {
                "timestamp": "2026-05-19T10:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "019e3fc3-6b0e-7f53-9f97-449f61e406ee",
                    "timestamp": "2026-05-19T10:00:00.000Z",
                    "cwd": "/tmp/project",
                    "model": "gpt-5.5",
                },
            },
            {
                "timestamp": "2026-05-19T10:00:01.000Z",
                "type": "turn_context",
                "payload": {"turn_id": "turn-1", "cwd": "/tmp/project", "model": "gpt-5.5"},
            },
            {
                "timestamp": "2026-05-19T10:00:02.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            },
            {
                "timestamp": "2026-05-19T10:00:03.000Z",
                "type": "response_item",
                "payload": {"type": "reasoning", "content": "thinking"},
            },
            {
                "timestamp": "2026-05-19T10:00:04.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": json.dumps({"cmd": "ls", "workdir": "/tmp/project"}),
                },
            },
            {
                "timestamp": "2026-05-19T10:00:05.000Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "call-1", "output": "file.txt\n"},
            },
            {
                "timestamp": "2026-05-19T10:00:06.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            },
            {
                "timestamp": "2026-05-19T10:00:07.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 25,
                            "output_tokens": 8,
                        }
                    },
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "rollout-2026-05-19T10-00-00-019e3fc3-6b0e-7f53-9f97-449f61e406ee.jsonl")
            path.write_text("\n".join(json.dumps(r) for r in rows))
            session = core.parse_session(path)

        self.assertEqual(session.source, "codex")
        self.assertEqual(session.session_id, "019e3fc3-6b0e-7f53-9f97-449f61e406ee")
        self.assertEqual(session.cwd, "/tmp/project")
        self.assertEqual(session.first_user_prompt, "hello")
        self.assertEqual(len(session.turns), 1)
        self.assertEqual(session.turns[0].input_tokens, 75)
        self.assertEqual(session.turns[0].cache_read, 25)
        self.assertEqual(session.turns[0].output_tokens, 8)
        self.assertEqual(session.turns[0].model, "gpt-5.5")
        self.assertEqual(session.turns[0].user_messages, ["hello"])
        self.assertEqual(session.turns[0].text_blocks, ["done"])
        self.assertEqual(session.turns[0].tool_calls[0].name, "exec_command")
        self.assertEqual(session.turns[0].tool_calls[0].result_content, "file.txt\n")

    def test_codex_rollout_preserves_all_user_messages(self):
        rows = [
            {
                "timestamp": "2026-05-19T10:00:00.000Z",
                "type": "session_meta",
                "payload": {"id": "session-1", "cwd": "/tmp/project", "model": "gpt-5.5"},
            },
            {
                "timestamp": "2026-05-19T10:00:01.000Z",
                "type": "turn_context",
                "payload": {"turn_id": "turn-1", "cwd": "/tmp/project"},
            },
            {
                "timestamp": "2026-05-19T10:00:02.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "first prompt"}],
                },
            },
            {
                "timestamp": "2026-05-19T10:00:03.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "first answer"}],
                },
            },
            {
                "timestamp": "2026-05-19T10:00:04.000Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"last_token_usage": {}}},
            },
            {
                "timestamp": "2026-05-19T10:00:05.000Z",
                "type": "turn_context",
                "payload": {"turn_id": "turn-2", "cwd": "/tmp/project"},
            },
            {
                "timestamp": "2026-05-19T10:00:06.000Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "second prompt"},
            },
            {
                "timestamp": "2026-05-19T10:00:07.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "second answer"}],
                },
            },
            {
                "timestamp": "2026-05-19T10:00:08.000Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"last_token_usage": {}}},
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "rollout-2026-05-19T10-00-00-019e3fc3-6b0e-7f53-9f97-449f61e406ee.jsonl")
            path.write_text("\n".join(json.dumps(r) for r in rows))
            session = core.parse_session(path)
            trace_json = Path(td, "trace.json")
            core.emit_trace_json(session, trace_json)
            doc = json.loads(trace_json.read_text())

        self.assertEqual(session.first_user_prompt, "first prompt")
        self.assertEqual([t.user_messages for t in session.turns], [["first prompt"], ["second prompt"]])
        events = [e for turn in doc["session"]["turns"] for e in turn["events"] if e["kind"] == "user"]
        self.assertEqual([e["text"] for e in events], ["first prompt", "second prompt"])


class SampleSetupTests(unittest.TestCase):
    def test_public_sample_saves_and_reads_summary(self):
        sample = Path(__file__).resolve().parents[1] / "examples" / "codex-smoke-session.jsonl"
        self.assertTrue(sample.exists())

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "sample-trace"
            save_stdout = io.StringIO()
            with patch("sys.stdout", save_stdout):
                core.main([
                    "save",
                    str(sample),
                    "--source",
                    "codex",
                    "--out",
                    str(out_dir),
                    "--no-track",
                    "--label",
                    "sample",
                    "--ascii",
                ])

            trace_json = out_dir / "trace.json"
            self.assertTrue(trace_json.exists())
            self.assertTrue((out_dir / "trace.html").exists())
            self.assertTrue((out_dir / "ascii.txt").exists())

            read_stdout = io.StringIO()
            with patch("sys.stdout", read_stdout):
                core.main(["read", str(trace_json), "--summary"])

            summary = json.loads(read_stdout.getvalue())
            self.assertEqual(summary["session_id"], "019f-tracer-sample-session")
            self.assertEqual(summary["turns"], 1)
            self.assertEqual(summary["tool_calls"], 2)
            self.assertEqual(summary["model_mix"], {"gpt-5.5": 1})


if __name__ == "__main__":
    unittest.main()
