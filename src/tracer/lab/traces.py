from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterable


def trace_id_for(path: Path) -> str:
    resolved = str(path.expanduser().resolve())
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]


def load_trace(path: str | Path) -> dict:
    trace_path = Path(path).expanduser().resolve()
    with trace_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def summarize_trace(doc: dict) -> dict:
    task = doc.get("task", {}) or {}
    session = doc.get("session", {}) or {}
    totals = task.get("totals", {}) or {}
    return {
        "schema_version": doc.get("schema_version"),
        "sessions": totals.get("sessions"),
        "turns": totals.get("turns") or session.get("totals", {}).get("turns"),
        "billable_input": totals.get("billable_input"),
        "output": totals.get("output"),
        "cost_weighted": totals.get("cost_weighted"),
        "dollars": totals.get("dollars"),
        "model_mix": totals.get("model_mix") or session.get("totals", {}).get("model_mix") or {},
    }


def metadata_for_trace(path: str | Path) -> dict:
    trace_path = Path(path).expanduser().resolve()
    doc = load_trace(trace_path)
    task = doc.get("task", {}) or {}
    session = doc.get("session", {}) or {}
    html_path = trace_path.with_name("trace.html")
    return {
        "id": trace_id_for(trace_path),
        "path": str(trace_path),
        "html_path": str(html_path) if html_path.exists() else None,
        "session_id": session.get("id"),
        "cwd": task.get("cwd") or session.get("cwd"),
        "command": task.get("command") or session.get("command"),
        "started_at": task.get("started_at"),
        "first_user_prompt": task.get("first_user_prompt") or session.get("first_user_prompt"),
        "summary_json": json.dumps(summarize_trace(doc), ensure_ascii=False),
    }


def discover_trace_paths(project_root: Path | None, direct_trace: Path | None = None) -> list[Path]:
    found: dict[str, Path] = {}
    if direct_trace is not None and direct_trace.exists():
        found[str(direct_trace.resolve())] = direct_trace.resolve()

    if project_root is None:
        return list(found.values())

    tracer_dir = project_root / ".tracer"
    runs_db = tracer_dir / "runs.db"
    if runs_db.exists():
        try:
            conn = sqlite3.connect(runs_db)
            for row in conn.execute("SELECT trace_dir FROM runs ORDER BY started_at DESC"):
                trace_dir = row[0]
                if trace_dir:
                    candidate = Path(trace_dir).expanduser() / "trace.json"
                    if candidate.exists():
                        found[str(candidate.resolve())] = candidate.resolve()
        except sqlite3.Error:
            pass

    traces_dir = tracer_dir / "traces"
    if traces_dir.exists():
        for candidate in traces_dir.glob("*/trace.json"):
            found[str(candidate.resolve())] = candidate.resolve()

    return list(found.values())


def import_trace(conn: sqlite3.Connection, path: str | Path) -> dict:
    meta = metadata_for_trace(path)
    conn.execute(
        """
        INSERT INTO traces (
          id, path, html_path, session_id, cwd, command, started_at,
          first_user_prompt, summary_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
          html_path=excluded.html_path,
          session_id=excluded.session_id,
          cwd=excluded.cwd,
          command=excluded.command,
          started_at=excluded.started_at,
          first_user_prompt=excluded.first_user_prompt,
          summary_json=excluded.summary_json,
          updated_at=CURRENT_TIMESTAMP
        """,
        (
            meta["id"],
            meta["path"],
            meta["html_path"],
            meta["session_id"],
            meta["cwd"],
            meta["command"],
            meta["started_at"],
            meta["first_user_prompt"],
            meta["summary_json"],
        ),
    )
    return meta


def import_discovered(conn: sqlite3.Connection, paths: Iterable[Path]) -> list[dict]:
    imported = []
    for path in paths:
        try:
            imported.append(import_trace(conn, path))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    conn.commit()
    return imported


def flatten_turns(
    session: dict,
    prefix: str = "main",
    *,
    depth: int = 0,
    parent_tool_id: str | None = None,
    session_path: str | None = None,
) -> list[dict]:
    rows: list[dict] = []
    session_label = session.get("label") or session.get("id") or prefix
    current_path = session_path or session_label
    for turn in session.get("turns", []):
        child_rows: list[dict] = []
        turn_row = {
            "session_label": session_label,
            "session_id": session.get("id"),
            "agent_type": session.get("agent_type"),
            "depth": depth,
            "parent_tool_id": parent_tool_id,
            "session_path": current_path,
            "turn_id": turn.get("id"),
            "n": turn.get("n"),
            "timestamp": turn.get("timestamp"),
            "model": turn.get("model"),
            "usage": turn.get("usage", {}) or {},
            "events": [],
            "has_text": False,
            "has_tool": False,
            "has_thinking": False,
            "has_child_session": False,
        }
        for event in turn.get("events", []):
            item = dict(event)
            kind = item.get("kind")
            if kind == "user":
                turn_row["has_user"] = True
            elif kind == "text":
                turn_row["has_text"] = True
            elif kind == "thinking":
                turn_row["has_thinking"] = True
            elif kind == "tool_use":
                turn_row["has_tool"] = True
            if item.get("kind") == "tool_use":
                result = item.get("result") or {}
                stored_preview = result.get("preview") or ""
                item["result_preview"] = stored_preview
                item["result_preview_chars"] = len(stored_preview)
                item["result_full_chars"] = result.get("full_chars") or len(stored_preview)
                if item.get("child_session"):
                    turn_row["has_child_session"] = True
                    item["has_child_session"] = True
                    child_session = item["child_session"]
                    child_label = child_session.get("label") or child_session.get("id") or item.get("id")
                    child_rows.extend(
                        flatten_turns(
                            child_session,
                            prefix=f"{prefix}:{item.get('id')}",
                            depth=depth + 1,
                            parent_tool_id=item.get("id"),
                            session_path=f"{current_path} / {child_label}",
                        )
                    )
                    item["has_child_session"] = True
                    item.pop("child_session", None)
            turn_row["events"].append(item)
        rows.append(turn_row)
        rows.extend(child_rows)
    return rows
