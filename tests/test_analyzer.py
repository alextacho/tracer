from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import analyzer


def make_session() -> analyzer.Session:
    turn = analyzer.Turn(
        request_id="req-1",
        timestamp="2026-05-19T10:00:00Z",
        input_tokens=1,
        output_tokens=1,
    )
    return analyzer.Session(
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
            with patch("analyzer.default_run_dir", side_effect=AssertionError("should not be called")):
                self.assertEqual(analyzer.resolve_output_dir(session, str(expected), "label"), expected)

    def test_missing_out_arg_uses_default_run_dir(self):
        session = make_session()

        with patch("analyzer.default_run_dir", return_value=Path("/tmp/default-trace")) as default_run_dir:
            self.assertEqual(analyzer.resolve_output_dir(session, None, "label"), Path("/tmp/default-trace"))
            default_run_dir.assert_called_once_with(session, label_hint="label")


class InitRootTests(unittest.TestCase):
    def test_init_root_ignores_ancestor_tracer_dir_without_project_marker(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td, "home")
            project = home / "work" / "unmarked-project"
            (home / ".tracer").mkdir(parents=True)
            project.mkdir(parents=True)

            self.assertEqual(analyzer.find_init_target_root(project), project.resolve())

    def test_init_root_uses_project_marker_before_ancestor_tracer_dir(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td, "home")
            project = home / "work" / "marked-project"
            nested = project / "src"
            (home / ".tracer").mkdir(parents=True)
            nested.mkdir(parents=True)
            (project / "pyproject.toml").write_text("[project]\nname = 'example'\n")

            self.assertEqual(analyzer.find_init_target_root(nested), project.resolve())

    def test_cmd_init_creates_tracer_in_current_unmarked_directory(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td, "home")
            project = home / "work" / "unmarked-project"
            (home / ".tracer").mkdir(parents=True)
            project.mkdir(parents=True)
            old_cwd = os.getcwd()
            try:
                os.chdir(project)
                analyzer.cmd_init(here=False, skip_gitignore_prompt=True)
            finally:
                os.chdir(old_cwd)

            self.assertTrue((project / ".tracer" / "traces").is_dir())
            self.assertFalse((home / ".tracer" / "traces").exists())


class TraceJsonTests(unittest.TestCase):
    def test_tool_result_content_round_trips(self):
        full = "0123456789" * 400
        session = make_session()
        session.turns[0].events.append(
            analyzer.ToolCall(
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
            analyzer.emit_trace_json(session, trace_json)
            loaded = analyzer.load_trace_json(trace_json)

        loaded_tool = loaded.turns[0].tool_calls[0]
        self.assertEqual(loaded_tool.result_content, full)
        self.assertEqual(loaded_tool.result_preview, full[:3000])
        self.assertEqual(loaded_tool.result_full_chars, len(full))

    def test_legacy_preview_loads_as_content(self):
        legacy = {
            "schema_version": analyzer.SCHEMA_VERSION,
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
            loaded = analyzer.load_trace_json(trace_json)

        loaded_tool = loaded.turns[0].tool_calls[0]
        self.assertEqual(loaded_tool.result_content, "legacy result")
        self.assertEqual(loaded_tool.result_preview, "legacy result")


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
            session = analyzer.parse_session(path)

        self.assertEqual(session.source, "codex")
        self.assertEqual(session.session_id, "019e3fc3-6b0e-7f53-9f97-449f61e406ee")
        self.assertEqual(session.cwd, "/tmp/project")
        self.assertEqual(session.first_user_prompt, "hello")
        self.assertEqual(len(session.turns), 1)
        self.assertEqual(session.turns[0].input_tokens, 75)
        self.assertEqual(session.turns[0].cache_read, 25)
        self.assertEqual(session.turns[0].output_tokens, 8)
        self.assertEqual(session.turns[0].model, "gpt-5.5")
        self.assertEqual(session.turns[0].text_blocks, ["done"])
        self.assertEqual(session.turns[0].tool_calls[0].name, "exec_command")
        self.assertEqual(session.turns[0].tool_calls[0].result_content, "file.txt\n")


if __name__ == "__main__":
    unittest.main()
