from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tracer.lab.analysis import (
    accept_annotation,
    assign_annotation_to_failure_mode,
    create_annotation,
    create_failure_mode,
    edit_annotation,
    eval_candidates,
    failure_mode_workspace,
    link_failure_mode,
    list_annotations,
    update_failure_mode,
)
from tracer.lab.cli import build_parser
from tracer.lab.cli_support import watched_files
from tracer.lab.db import connect, init_db
from tracer.lab.paths import context_for
from tracer.lab.traces import flatten_turns, import_trace
from tracer.lab.web import render_timeline


def write_sample_trace(path: Path) -> None:
    doc = {
        "schema_version": 1,
        "task": {
            "command": "/test",
            "cwd": str(path.parent),
            "started_at": "2026-05-18T10:00:00Z",
            "wall_seconds": 12,
            "first_user_prompt": "Run the test workflow",
            "totals": {
                "sessions": 1,
                "turns": 1,
                "billable_input": 10,
                "output": 5,
                "cost_weighted": 35,
                "dollars": 0.01,
                "model_mix": {"claude-test": 1},
            },
        },
        "session": {
            "id": "session-1",
            "agent_type": None,
            "command": "/test",
            "cwd": str(path.parent),
            "first_user_prompt": "Run the test workflow",
            "label": "session: session-1",
            "turns": [
                {
                    "id": "turn-1",
                    "n": 1,
                    "timestamp": "2026-05-18T10:00:00Z",
                    "model": "claude-test",
                    "usage": {"input": 1, "cache_creation": 2, "cache_read": 3, "output": 4},
                    "events": [
                        {
                            "kind": "user",
                            "id": "turn-1:user:0",
                            "text": "Run the test workflow",
                            "timestamp": "2026-05-18T09:59:59Z",
                        },
                        {"kind": "text", "id": "turn-1:txt:0", "text": "I will inspect the code."},
                        {
                            "kind": "tool_use",
                            "id": "tool-1",
                            "name": "Read",
                            "input_label": "app.py",
                            "input": {"file_path": "app.py"},
                            "timestamp": "2026-05-18T10:00:01Z",
                            "result": {"tokens": 3, "full_chars": 12, "preview": "print('hi')"},
                            "child_session": {
                                "id": "agent-session-1",
                                "agent_type": "explorer",
                                "label": "agent: explorer",
                                "turns": [
                                    {
                                        "id": "agent-turn-1",
                                        "n": 1,
                                        "timestamp": "2026-05-18T10:00:02Z",
                                        "model": "claude-test",
                                        "usage": {},
                                        "events": [
                                            {
                                                "kind": "text",
                                                "id": "agent-turn-1:txt:0",
                                                "text": "Subagent finding.",
                                            }
                                        ],
                                    }
                                ],
                            },
                        },
                    ],
                }
            ],
        },
        "annotations": {"by_event": {}, "by_turn": {}, "by_session": {}, "global": []},
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


class CoreTests(unittest.TestCase):
    def test_import_trace_and_flatten_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.json"
            write_sample_trace(trace_path)
            db_path = Path(tmp) / "lab.db"
            init_db(db_path)
            with connect(db_path) as conn:
                meta = import_trace(conn, trace_path)
                conn.commit()
                self.assertEqual(meta["session_id"], "session-1")
                rows = conn.execute("SELECT * FROM traces").fetchall()
                self.assertEqual(len(rows), 1)

            turns = flatten_turns(json.loads(trace_path.read_text())["session"])
            self.assertEqual(len(turns), 2)
            self.assertEqual(turns[0]["depth"], 0)
            self.assertEqual(turns[0]["turn_id"], "turn-1")
            self.assertTrue(turns[0]["has_user"])
            self.assertTrue(turns[0]["has_child_session"])
            self.assertEqual(turns[1]["depth"], 1)
            self.assertEqual(turns[1]["agent_type"], "explorer")
            self.assertEqual(turns[1]["parent_tool_id"], "tool-1")
            self.assertEqual(turns[1]["turn_id"], "agent-turn-1")
            self.assertEqual(turns[0]["events"][2]["result_preview"], "print('hi')")
            self.assertEqual(turns[0]["events"][2]["result_full_chars"], 12)
            self.assertEqual(turns[0]["events"][2]["result_preview_chars"], 11)

    def test_timeline_renders_all_user_message_events(self) -> None:
        active = {
            "id": "trace-1",
            "started_at": "2026-05-18T10:00:00Z",
            "first_user_prompt": "first prompt",
        }
        turns = [
            {
                "depth": 0,
                "turn_id": "turn-1",
                "n": 1,
                "timestamp": "2026-05-18T10:00:01Z",
                "usage": {},
                "events": [
                    {"kind": "user", "id": "turn-1:user:0", "text": "first prompt"},
                    {"kind": "text", "id": "turn-1:txt:0", "text": "first answer"},
                ],
            },
            {
                "depth": 0,
                "turn_id": "turn-2",
                "n": 2,
                "timestamp": "2026-05-18T10:01:01Z",
                "usage": {},
                "events": [
                    {"kind": "user", "id": "turn-2:user:0", "text": "second prompt"},
                    {"kind": "text", "id": "turn-2:txt:0", "text": "second answer"},
                ],
            },
        ]

        html = render_timeline(active, turns)

        self.assertEqual(html.count("first prompt"), 1)
        self.assertIn("second prompt", html)
        self.assertIn("t-user", html)

    def test_agent_suggestion_review_and_edit_preserve_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.json"
            write_sample_trace(trace_path)
            db_path = Path(tmp) / "lab.db"
            init_db(db_path)
            with connect(db_path) as conn:
                trace = import_trace(conn, trace_path)
                conn.commit()
                suggestion = create_annotation(
                    conn,
                    trace["id"],
                    kind="label",
                    label="serial tool loop",
                    body="The agent serializes independent reads.",
                    author_type="agent",
                    author_name="Claude Code",
                    model_identity="claude-opus-test",
                )
                self.assertEqual(suggestion["status"], "pending")
                self.assertEqual(suggestion["author_type"], "agent")

                accepted = accept_annotation(conn, suggestion["id"])
                self.assertEqual(accepted["status"], "accepted")
                edited = edit_annotation(
                    conn,
                    suggestion["id"],
                    label="under-batched reads",
                    body="The agent should batch independent reads before reasoning.",
                )
                self.assertEqual(edited["origin_annotation_id"], suggestion["id"])
                self.assertEqual(edited["author_type"], "human")

                annotations = list_annotations(conn, trace["id"])
                self.assertEqual(len(annotations), 2)

    def test_failure_mode_eval_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.json"
            write_sample_trace(trace_path)
            db_path = Path(tmp) / "lab.db"
            init_db(db_path)
            with connect(db_path) as conn:
                trace = import_trace(conn, trace_path)
                conn.commit()
                note = create_annotation(conn, trace["id"], kind="note", body="Failed to use parallel reads.")
                mode = create_failure_mode(conn, "Serial tool loop", "Independent reads happen one at a time.")
                link_failure_mode(conn, mode["id"], trace_id=trace["id"], annotation_id=note["id"])
                exported = eval_candidates(conn)
                self.assertEqual(exported["eval_candidates"][0]["failure_mode"]["title"], "Serial tool loop")
                self.assertEqual(exported["eval_candidates"][0]["support"][0]["annotation_id"], note["id"])

    def test_failure_mode_workspace_and_reassignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.json"
            write_sample_trace(trace_path)
            db_path = Path(tmp) / "lab.db"
            init_db(db_path)
            with connect(db_path) as conn:
                trace = import_trace(conn, trace_path)
                conn.commit()
                note = create_annotation(conn, trace["id"], kind="note", body="Failed to use parallel reads.")
                first = create_failure_mode(conn, "Serial tool loop", "Independent reads happen one at a time.")
                second = create_failure_mode(conn, "Weak planning", "")

                assign_annotation_to_failure_mode(conn, first["id"], note["id"])
                workspace = failure_mode_workspace(conn)
                self.assertEqual(workspace["failures"][0]["failure_mode_id"], first["id"])
                self.assertEqual(len(workspace["failures_by_mode"][first["id"]]), 1)

                assign_annotation_to_failure_mode(conn, second["id"], note["id"])
                workspace = failure_mode_workspace(conn)
                self.assertEqual(len(workspace["failures_by_mode"][first["id"]]), 0)
                self.assertEqual(len(workspace["failures_by_mode"][second["id"]]), 1)

                assign_annotation_to_failure_mode(conn, None, note["id"])
                workspace = failure_mode_workspace(conn)
                self.assertEqual(workspace["unassigned_failures"][0]["id"], note["id"])

                updated = update_failure_mode(conn, second["id"], title="Planning gap", description="Too little planning.")
                self.assertEqual(updated["title"], "Planning gap")

    def test_open_parser_accepts_reload(self) -> None:
        args = build_parser().parse_args(["open", "--reload", "--no-browser", "--port", "8768"])
        self.assertTrue(args.reload)
        self.assertFalse(args.browser)
        self.assertEqual(args.port, 8768)

    def test_watched_files_include_source_and_static_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("", encoding="utf-8")
            (root / "static.css").write_text("", encoding="utf-8")
            (root / "notes.txt").write_text("", encoding="utf-8")
            pycache = root / "__pycache__"
            pycache.mkdir()
            (pycache / "cached.py").write_text("", encoding="utf-8")

            names = {path.name for path in watched_files(root)}
            self.assertEqual(names, {"app.py", "static.css"})

    def test_context_for_project_uses_unified_tracer_lab_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            nested = project / "src"
            (project / ".tracer").mkdir(parents=True)
            nested.mkdir()

            ctx = context_for(nested)

            self.assertEqual(ctx.project_root, project.resolve())
            self.assertEqual(ctx.db_path, project.resolve() / ".tracer" / "lab.db")

    def test_context_for_direct_trace_without_project_marker_uses_trace_local_tracer_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.json"
            write_sample_trace(trace_path)

            ctx = context_for(None, trace_path)

            self.assertIsNone(ctx.project_root)
            self.assertEqual(ctx.direct_trace, trace_path.resolve())
            self.assertEqual(ctx.db_path, trace_path.parent.resolve() / ".tracer" / "lab.db")

    def test_context_for_direct_trace_prefers_trace_local_db_over_ancestor_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home_like = Path(tmp) / "home"
            project = home_like / "project"
            trace_path = project / "trace.json"
            (home_like / ".tracer").mkdir(parents=True)
            project.mkdir(parents=True)
            write_sample_trace(trace_path)

            ctx = context_for(project, trace_path)

            self.assertEqual(ctx.project_root, home_like.resolve())
            self.assertEqual(ctx.direct_trace, trace_path.resolve())
            self.assertEqual(ctx.db_path, trace_path.parent.resolve() / ".tracer" / "lab.db")

    def test_context_for_trace_artifact_directory_uses_trace_local_tracer_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace.json"
            write_sample_trace(trace_path)

            ctx = context_for(tmp)

            self.assertIsNone(ctx.project_root)
            self.assertEqual(ctx.direct_trace, trace_path.resolve())
            self.assertEqual(ctx.db_path, trace_path.parent.resolve() / ".tracer" / "lab.db")


if __name__ == "__main__":
    unittest.main()
