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
        self.assertIn("{save,ls,sessions,config,version,open,read,clip,label,mcp}", help_text)
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
            session.first_user_prompt = "session-1234567890 prompt"
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
        self.assertIn("tokens", output)
        self.assertNotIn("prompt", output)
        self.assertNotIn("session-1234567890 prompt", output)
        self.assertIn("6", output)

    def test_ls_filters_by_label(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            project.mkdir()
            (project / "pyproject.toml").write_text("[project]\nname = 'example'\n")
            db_path = core.project_db_path(project, create=True)

            first = make_session()
            first.cwd = str(project)
            first.session_id = "session-first"
            first.path = Path(td, "first.jsonl")
            first_dir = project / ".tracer" / "traces" / "first"
            first_dir.mkdir(parents=True)
            core.emit_trace_json(first, first_dir / "trace.json")
            core.track(first, db_path, label="keep", trace_dir=first_dir)

            second = make_session()
            second.cwd = str(project)
            second.session_id = "session-second"
            second.path = Path(td, "second.jsonl")
            second_dir = project / ".tracer" / "traces" / "second"
            second_dir.mkdir(parents=True)
            core.emit_trace_json(second, second_dir / "trace.json")
            core.track(second, db_path, label="drop", trace_dir=second_dir)

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                core.main(["ls", "--cwd", str(project), "--label", "keep"])

        output = stdout.getvalue()
        self.assertIn("label=keep", output)
        self.assertIn("keep", output)
        self.assertNotIn("drop", output)

    def test_label_command_updates_existing_trace_label(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            project.mkdir()
            (project / "pyproject.toml").write_text("[project]\nname = 'example'\n")
            session = make_session()
            session.cwd = str(project)
            trace_dir = project / ".tracer" / "traces" / "sample"
            trace_dir.mkdir(parents=True)
            core.emit_trace_json(session, trace_dir / "trace.json")
            db_path = core.project_db_path(project, create=True)
            core.track(session, db_path, label="old", note="keep note", trace_dir=trace_dir)

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                core.main([
                    "label",
                    session.session_id,
                    "new",
                    "--cwd",
                    str(project),
                ])

            rows = core._run_rows(db_path, label="new", limit=10)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "new")
        self.assertEqual(rows[0]["note"], "keep note")
        self.assertIn("label set:", stdout.getvalue())

    def test_claude_session_display_details_are_fast_identifiers(self):
        rows = [
            {
                "type": "user",
                "timestamp": "2026-05-19T10:00:00Z",
                "cwd": "/tmp/project",
                "message": {"content": "please review this pull request"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-19T10:02:03Z",
                "requestId": "req-1",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [{"type": "text", "text": "done"}],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td, "session.jsonl")
            path.write_text("\n".join(json.dumps(r) for r in rows))
            prompt, duration = core._raw_session_display_details(path, "claude")

        self.assertEqual(prompt, "please review this pull request")
        self.assertEqual(duration, 123)

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


class CostDisplayTests(unittest.TestCase):
    def test_opus_4_8_is_priced(self):
        dollars = core.dollars_for(
            "claude-opus-4-8",
            input_tokens=52_685,
            cache_creation=417_664,
            cache_read=1_321_032,
            output_tokens=35_569,
        )

        self.assertIsNotNone(dollars)
        self.assertAlmostEqual(dollars or 0, 13.270698)

    def test_trace_html_shows_total_raw_tokens(self):
        session = make_session()
        session.turns[0].input_tokens = 10
        session.turns[0].cache_creation = 20
        session.turns[0].cache_read = 30
        session.turns[0].output_tokens = 40

        with tempfile.TemporaryDirectory() as td:
            trace_html = Path(td, "trace.html")
            core.emit_trace_html(session, trace_html)
            html = trace_html.read_text()

        self.assertIn("<b>tokens:</b>", html)
        self.assertIn('<span class="num">100</span>', html)

    def test_turn_timeline_timestamp_uses_earliest_user_event(self):
        turn = core.Turn(
            request_id="req-1",
            timestamp="2026-05-19T10:00:02Z",
            input_tokens=1,
            output_tokens=1,
        )
        turn.events.append(core.UserMessage(
            id="req-1:user:0",
            text="prompt",
            timestamp="2026-05-19T10:00:01Z",
        ))

        self.assertEqual(core._turn_timeline_timestamp(turn), "2026-05-19T10:00:01Z")

    def test_trace_json_includes_pricing_breakdown_metadata(self):
        session = make_session()
        session.turns[0].model = "claude-opus-4-8"
        session.turns[0].input_tokens = 52_685
        session.turns[0].cache_creation = 417_664
        session.turns[0].cache_read = 1_321_032
        session.turns[0].output_tokens = 35_569

        with tempfile.TemporaryDirectory() as td:
            trace_json = Path(td, "trace.json")
            core.emit_trace_json(session, trace_json)
            doc = json.loads(trace_json.read_text())

        pricing = doc["task"]["totals"]["pricing"]
        self.assertEqual(pricing["rows"][0]["model"], "claude-opus-4-8")
        self.assertEqual(pricing["rows"][0]["label"], "Opus 4.8")
        self.assertEqual(pricing["rows"][0]["input"], 52_685)
        self.assertEqual(pricing["rows"][0]["output"], 35_569)
        self.assertEqual(pricing["rows"][0]["cache_creation"], 417_664)
        self.assertEqual(pricing["rows"][0]["cache_read"], 1_321_032)
        self.assertAlmostEqual(pricing["rows"][0]["dollars"], 13.270698)
        self.assertEqual(pricing["total"]["tokens"], 1_826_950)

    def test_trace_html_includes_pricing_breakdown_table(self):
        session = make_session()
        session.turns[0].model = "claude-opus-4-8"
        session.turns[0].input_tokens = 52_685
        session.turns[0].cache_creation = 417_664
        session.turns[0].cache_read = 1_321_032
        session.turns[0].output_tokens = 35_569

        with tempfile.TemporaryDirectory() as td:
            trace_html = Path(td, "trace.html")
            core.emit_trace_html(session, trace_html)
            html = trace_html.read_text()

        self.assertIn('class="pricing-table"', html)
        self.assertIn("<th>Model</th><th>Input</th><th>Output</th>", html)
        self.assertIn("Opus 4.8", html)
        self.assertIn("52,685", html)
        self.assertIn("417,664", html)
        self.assertIn("$13.27", html)


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

    def test_subagents_link_by_tool_use_id_not_file_mtime(self):
        main_rows = [
            {
                "type": "assistant",
                "timestamp": "2026-05-19T10:00:00Z",
                "requestId": "req-main",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu-first",
                            "name": "Agent",
                            "input": {"description": "first", "subagent_type": "general-purpose"},
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu-second",
                            "name": "Agent",
                            "input": {"description": "second", "subagent_type": "general-purpose"},
                        },
                    ],
                },
            },
        ]

        def sub_rows(prompt: str) -> list[dict]:
            return [
                {
                    "type": "user",
                    "timestamp": "2026-05-19T10:00:01Z",
                    "cwd": "/tmp/project",
                    "message": {"content": prompt},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-05-19T10:00:02Z",
                    "requestId": f"req-{prompt}",
                    "message": {
                        "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "content": [{"type": "text", "text": f"answer {prompt}"}],
                    },
                },
            ]

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main_path = root / "session-main.jsonl"
            main_path.write_text("\n".join(json.dumps(r) for r in main_rows))
            sub_dir = root / "session-main" / "subagents"
            sub_dir.mkdir(parents=True)

            first_path = sub_dir / "agent-first.jsonl"
            second_path = sub_dir / "agent-second.jsonl"
            first_path.write_text("\n".join(json.dumps(r) for r in sub_rows("first prompt")))
            second_path.write_text("\n".join(json.dumps(r) for r in sub_rows("second prompt")))
            (sub_dir / "agent-first.meta.json").write_text(json.dumps({
                "agentType": "general-purpose",
                "description": "first",
                "toolUseId": "toolu-first",
            }))
            (sub_dir / "agent-second.meta.json").write_text(json.dumps({
                "agentType": "general-purpose",
                "description": "second",
                "toolUseId": "toolu-second",
            }))

            # Deliberately make mtime order disagree with spawn order.
            os.utime(second_path, (1, 1))
            os.utime(first_path, (2, 2))

            session = core.parse_session(main_path)

        calls = session.turns[0].tool_calls
        self.assertEqual(calls[0].tool_use_id, "toolu-first")
        self.assertEqual(calls[0].child_session.first_user_prompt, "first prompt")
        self.assertEqual(calls[1].tool_use_id, "toolu-second")
        self.assertEqual(calls[1].child_session.first_user_prompt, "second prompt")


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
