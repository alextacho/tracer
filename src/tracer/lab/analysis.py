from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from .db import row_to_dict
from .traces import load_trace


HUMAN_NAME = "local reviewer"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def get_or_create_author(
    conn: sqlite3.Connection,
    author_type: str,
    name: str | None = None,
    model_identity: str | None = None,
) -> str:
    author_name = name or ("agent" if author_type == "agent" else HUMAN_NAME)
    existing = conn.execute(
        """
        SELECT id FROM authors
        WHERE author_type = ? AND name = ? AND COALESCE(model_identity, '') = COALESCE(?, '')
        """,
        (author_type, author_name, model_identity),
    ).fetchone()
    if existing:
        return existing["id"]
    author_id = new_id("auth")
    conn.execute(
        "INSERT INTO authors (id, author_type, name, model_identity) VALUES (?, ?, ?, ?)",
        (author_id, author_type, author_name, model_identity),
    )
    return author_id


def list_traces(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT t.*,
          (SELECT COUNT(*) FROM annotations a WHERE a.trace_id = t.id) AS annotation_count,
          (SELECT COUNT(*) FROM annotations a WHERE a.trace_id = t.id AND a.status = 'pending') AS pending_count
        FROM traces t
        ORDER BY COALESCE(started_at, imported_at) DESC
        """
    ).fetchall()
    traces = []
    for row in rows:
        item = dict(row)
        item["summary"] = json.loads(item.pop("summary_json") or "{}")
        traces.append(item)
    return traces


def get_trace(conn: sqlite3.Connection, trace_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM traces WHERE id = ?", (trace_id,)).fetchone()
    item = row_to_dict(row)
    if item:
        item["summary"] = json.loads(item.pop("summary_json") or "{}")
    return item


def list_annotations(conn: sqlite3.Connection, trace_id: str | None = None, *, reviewed_only: bool = False) -> list[dict]:
    where = []
    args: list[str] = []
    if trace_id:
        where.append("a.trace_id = ?")
        args.append(trace_id)
    if reviewed_only:
        where.append("a.status = 'accepted'")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""
        SELECT a.*, au.author_type, au.name AS author_name, au.model_identity,
          reviewer.name AS reviewer_name
        FROM annotations a
        JOIN authors au ON au.id = a.author_id
        LEFT JOIN authors reviewer ON reviewer.id = a.reviewed_by_author_id
        {clause}
        ORDER BY a.created_at DESC
        """,
        args,
    ).fetchall()
    return [dict(row) for row in rows]


def create_annotation(
    conn: sqlite3.Connection,
    trace_id: str,
    *,
    kind: str,
    body: str = "",
    label: str | None = None,
    target_type: str = "trace",
    target_id: str | None = None,
    author_type: str = "human",
    author_name: str | None = None,
    model_identity: str | None = None,
    status: str | None = None,
    origin_annotation_id: str | None = None,
) -> dict:
    annotation_id = new_id("ann")
    resolved_status = status or ("pending" if author_type == "agent" else "accepted")
    author_id = get_or_create_author(conn, author_type, author_name, model_identity)
    conn.execute(
        """
        INSERT INTO annotations (
          id, trace_id, target_type, target_id, kind, body, label, status,
          author_id, origin_annotation_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            annotation_id,
            trace_id,
            target_type,
            target_id,
            kind,
            body,
            label,
            resolved_status,
            author_id,
            origin_annotation_id,
        ),
    )
    conn.commit()
    return get_annotation(conn, annotation_id)


def get_annotation(conn: sqlite3.Connection, annotation_id: str) -> dict:
    row = conn.execute(
        """
        SELECT a.*, au.author_type, au.name AS author_name, au.model_identity
        FROM annotations a
        JOIN authors au ON au.id = a.author_id
        WHERE a.id = ?
        """,
        (annotation_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"annotation not found: {annotation_id}")
    return dict(row)


def accept_annotation(conn: sqlite3.Connection, annotation_id: str, reviewer_name: str = HUMAN_NAME) -> dict:
    reviewer_id = get_or_create_author(conn, "human", reviewer_name)
    conn.execute(
        """
        UPDATE annotations
        SET status = 'accepted', reviewed_by_author_id = ?, reviewed_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (reviewer_id, annotation_id),
    )
    conn.commit()
    return get_annotation(conn, annotation_id)


def reject_annotation(conn: sqlite3.Connection, annotation_id: str, reviewer_name: str = HUMAN_NAME) -> dict:
    reviewer_id = get_or_create_author(conn, "human", reviewer_name)
    conn.execute(
        """
        UPDATE annotations
        SET status = 'rejected', reviewed_by_author_id = ?, reviewed_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (reviewer_id, annotation_id),
    )
    conn.commit()
    return get_annotation(conn, annotation_id)


def edit_annotation(
    conn: sqlite3.Connection,
    annotation_id: str,
    *,
    body: str,
    label: str | None = None,
    reviewer_name: str = HUMAN_NAME,
) -> dict:
    original = get_annotation(conn, annotation_id)
    reviewer_id = get_or_create_author(conn, "human", reviewer_name)
    if original["author_type"] == "agent":
        conn.execute(
            """
            UPDATE annotations
            SET status = 'accepted', reviewed_by_author_id = ?, reviewed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (reviewer_id, annotation_id),
        )
        return create_annotation(
            conn,
            original["trace_id"],
            kind=original["kind"],
            body=body,
            label=label,
            target_type=original["target_type"],
            target_id=original["target_id"],
            author_type="human",
            author_name=reviewer_name,
            status="accepted",
            origin_annotation_id=annotation_id,
        )

    conn.execute(
        """
        UPDATE annotations
        SET body = ?, label = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (body, label, annotation_id),
    )
    conn.commit()
    return get_annotation(conn, annotation_id)


def list_failure_modes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT fm.*,
          COUNT(DISTINCT fml.trace_id) AS trace_count,
          COUNT(DISTINCT fml.annotation_id) AS annotation_count
        FROM failure_modes fm
        LEFT JOIN failure_mode_links fml ON fml.failure_mode_id = fm.id
        GROUP BY fm.id
        ORDER BY fm.updated_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def create_failure_mode(conn: sqlite3.Connection, title: str, description: str = "") -> dict:
    mode_id = new_id("fm")
    conn.execute(
        "INSERT INTO failure_modes (id, title, description) VALUES (?, ?, ?)",
        (mode_id, title, description),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM failure_modes WHERE id = ?", (mode_id,)).fetchone())


def update_failure_mode(conn: sqlite3.Connection, failure_mode_id: str, *, title: str, description: str = "") -> dict:
    conn.execute(
        """
        UPDATE failure_modes
        SET title = ?, description = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (title, description, failure_mode_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM failure_modes WHERE id = ?", (failure_mode_id,)).fetchone()
    if row is None:
        raise KeyError(f"failure mode not found: {failure_mode_id}")
    return dict(row)


def delete_failure_mode(conn: sqlite3.Connection, failure_mode_id: str) -> None:
    conn.execute("DELETE FROM failure_modes WHERE id = ?", (failure_mode_id,))
    conn.commit()


def link_failure_mode(
    conn: sqlite3.Connection,
    failure_mode_id: str,
    *,
    trace_id: str | None = None,
    annotation_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO failure_mode_links (failure_mode_id, trace_id, annotation_id)
        VALUES (?, ?, ?)
        """,
        (failure_mode_id, trace_id, annotation_id),
    )
    conn.commit()


def assign_annotation_to_failure_mode(conn: sqlite3.Connection, failure_mode_id: str | None, annotation_id: str) -> None:
    annotation = get_annotation(conn, annotation_id)
    conn.execute("DELETE FROM failure_mode_links WHERE annotation_id = ?", (annotation_id,))
    if failure_mode_id:
        link_failure_mode(conn, failure_mode_id, trace_id=annotation["trace_id"], annotation_id=annotation_id)
    else:
        conn.commit()


def failure_mode_workspace(conn: sqlite3.Connection) -> dict:
    modes = list_failure_modes(conn)
    rows = conn.execute(
        """
        SELECT a.*, au.author_type, au.name AS author_name, au.model_identity,
          t.first_user_prompt, t.command, t.started_at,
          fm.id AS failure_mode_id, fm.title AS failure_mode_title
        FROM annotations a
        JOIN authors au ON au.id = a.author_id
        JOIN traces t ON t.id = a.trace_id
        LEFT JOIN failure_mode_links fml ON fml.annotation_id = a.id
        LEFT JOIN failure_modes fm ON fm.id = fml.failure_mode_id
        WHERE a.status = 'accepted'
          AND (a.kind IN ('note', 'label', 'failure_mode_assignment'))
        ORDER BY a.created_at DESC
        """
    ).fetchall()
    failures = [dict(row) for row in rows]
    by_mode: dict[str, list[dict]] = {mode["id"]: [] for mode in modes}
    unassigned: list[dict] = []
    for failure in failures:
        mode_id = failure.get("failure_mode_id")
        if mode_id and mode_id in by_mode:
            by_mode[mode_id].append(failure)
        else:
            unassigned.append(failure)
    return {"failure_modes": modes, "failures": failures, "failures_by_mode": by_mode, "unassigned_failures": unassigned}


def reviewed_context(conn: sqlite3.Connection) -> dict:
    return {
        "failure_modes": list_failure_modes(conn),
        "reviewed_annotations": list_annotations(conn, reviewed_only=True),
        "pending_suggestions": [
            item for item in list_annotations(conn) if item["status"] == "pending" and item["author_type"] == "agent"
        ],
        "rejected_suggestions": [
            item for item in list_annotations(conn) if item["status"] == "rejected" and item["author_type"] == "agent"
        ],
    }


def eval_candidates(conn: sqlite3.Connection) -> dict:
    modes = list_failure_modes(conn)
    candidates = []
    for mode in modes:
        rows = conn.execute(
            """
            SELECT fml.failure_mode_id, t.id AS trace_id, t.path, t.html_path, t.first_user_prompt,
              a.id AS annotation_id, a.body, a.label, a.target_type, a.target_id,
              au.author_type, au.name AS author_name, au.model_identity
            FROM failure_mode_links fml
            LEFT JOIN traces t ON t.id = fml.trace_id
            LEFT JOIN annotations a ON a.id = fml.annotation_id
            LEFT JOIN authors au ON au.id = a.author_id
            WHERE fml.failure_mode_id = ?
            ORDER BY fml.created_at DESC
            """,
            (mode["id"],),
        ).fetchall()
        candidates.append({"failure_mode": mode, "support": [dict(row) for row in rows]})
    return {"eval_candidates": candidates}


def trace_document_for_db_trace(conn: sqlite3.Connection, trace_id: str) -> dict:
    trace = get_trace(conn, trace_id)
    if trace is None:
        raise KeyError(f"trace not found: {trace_id}")
    return load_trace(Path(trace["path"]))
