from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .analysis import (
    assign_annotation_to_failure_mode,
    create_annotation,
    create_failure_mode,
    delete_failure_mode,
    eval_candidates,
    failure_mode_workspace,
    get_trace,
    list_annotations,
    list_failure_modes,
    list_traces,
    reviewed_context,
    trace_document_for_db_trace,
    update_failure_mode,
)
from .cli_support import start_detached_server
from .db import connect, init_db
from . import __version__
from .paths import LabContext, context_for
from .traces import discover_trace_paths, import_discovered


def ensure_imported(ctx: LabContext) -> None:
    init_db(ctx.db_path)
    with connect(ctx.db_path) as conn:
        import_discovered(conn, discover_trace_paths(ctx.project_root, ctx.direct_trace))


def tool_open(ctx: LabContext, cwd: str | None = None, trace_json: str | None = None, open_browser: bool = True) -> dict:
    target_ctx = context_for(cwd or ctx.cwd, trace_json)
    ensure_imported(target_ctx)
    url = start_detached_server(target_ctx, open_browser=open_browser)
    return {"url": url, "db": str(target_ctx.db_path)}


def tool_list_traces(ctx: LabContext, cwd: str | None = None) -> dict:
    target_ctx = context_for(cwd or ctx.cwd)
    ensure_imported(target_ctx)
    with connect(target_ctx.db_path) as conn:
        return {"db": str(target_ctx.db_path), "traces": list_traces(conn)}


def tool_get_trace(ctx: LabContext, trace_id: str, include_raw: bool = False) -> dict:
    ensure_imported(ctx)
    with connect(ctx.db_path) as conn:
        trace = get_trace(conn, trace_id)
        if trace is None:
            raise ValueError(f"trace not found: {trace_id}")
        payload = {"trace": trace, "annotations": list_annotations(conn, trace_id)}
        if include_raw:
            payload["trace_json"] = trace_document_for_db_trace(conn, trace_id)
        return payload


def tool_create_suggestion(
    ctx: LabContext,
    trace_id: str,
    kind: str,
    body: str = "",
    label: str | None = None,
    target_type: str = "trace",
    target_id: str | None = None,
    author_name: str = "Claude Code",
    model_identity: str | None = None,
) -> dict:
    ensure_imported(ctx)
    with connect(ctx.db_path) as conn:
        suggestion = create_annotation(
            conn,
            trace_id,
            kind=kind,
            body=body,
            label=label,
            target_type=target_type,
            target_id=target_id,
            author_type="agent",
            author_name=author_name,
            model_identity=model_identity,
            status="pending",
        )
        return {"suggestion": suggestion}


def tool_create_failure_mode(
    ctx: LabContext,
    title: str,
    description: str = "",
    annotation_ids: list[str] | None = None,
) -> dict:
    ensure_imported(ctx)
    with connect(ctx.db_path) as conn:
        mode = create_failure_mode(conn, title, description)
        for annotation_id in annotation_ids or []:
            assign_annotation_to_failure_mode(conn, mode["id"], annotation_id)
        return {"failure_mode": mode, "assigned_annotation_ids": annotation_ids or []}


def tool_update_failure_mode(
    ctx: LabContext,
    failure_mode_id: str,
    title: str,
    description: str = "",
    annotation_ids: list[str] | None = None,
) -> dict:
    ensure_imported(ctx)
    with connect(ctx.db_path) as conn:
        mode = update_failure_mode(conn, failure_mode_id, title=title, description=description)
        if annotation_ids is not None:
            for annotation_id in annotation_ids:
                assign_annotation_to_failure_mode(conn, failure_mode_id, annotation_id)
        return {"failure_mode": mode, "assigned_annotation_ids": annotation_ids}


def tool_assign_failure(ctx: LabContext, annotation_id: str, failure_mode_id: str | None = None) -> dict:
    ensure_imported(ctx)
    with connect(ctx.db_path) as conn:
        assign_annotation_to_failure_mode(conn, failure_mode_id, annotation_id)
        return {"annotation_id": annotation_id, "failure_mode_id": failure_mode_id}


def tool_delete_failure_mode(ctx: LabContext, failure_mode_id: str) -> dict:
    ensure_imported(ctx)
    with connect(ctx.db_path) as conn:
        delete_failure_mode(conn, failure_mode_id)
        return {"deleted_failure_mode_id": failure_mode_id}


TOOLS = {
    "tracer_lab_open": {
        "description": "Start/open the local Tracer Lab UI for a project or trace.json.",
        "schema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "trace_json": {"type": "string"},
                "open_browser": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": tool_open,
    },
    "tracer_lab_list_traces": {
        "description": "List imported/discovered traces.",
        "schema": {"type": "object", "properties": {"cwd": {"type": "string"}}, "additionalProperties": False},
        "handler": tool_list_traces,
    },
    "tracer_lab_get_trace": {
        "description": "Get one trace metadata and its analysis annotations.",
        "schema": {
            "type": "object",
            "properties": {"trace_id": {"type": "string"}, "include_raw": {"type": "boolean", "default": False}},
            "required": ["trace_id"],
            "additionalProperties": False,
        },
        "handler": tool_get_trace,
    },
    "tracer_lab_list_failure_modes": {
        "description": "List failure modes with support counts.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": lambda ctx: {"failure_modes": list_failure_modes(connect(ctx.db_path))},
    },
    "tracer_lab_failure_mode_workspace": {
        "description": "List reviewed failures, assignment state, and failure-mode clusters.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": lambda ctx: failure_mode_workspace(connect(ctx.db_path)),
    },
    "tracer_lab_create_failure_mode": {
        "description": "Create a failure-mode cluster and optionally assign reviewed failure annotation ids to it.",
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "annotation_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        "handler": tool_create_failure_mode,
    },
    "tracer_lab_update_failure_mode": {
        "description": "Rename/describe a failure-mode cluster and optionally assign reviewed failure annotation ids to it.",
        "schema": {
            "type": "object",
            "properties": {
                "failure_mode_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "annotation_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["failure_mode_id", "title"],
            "additionalProperties": False,
        },
        "handler": tool_update_failure_mode,
    },
    "tracer_lab_assign_failure": {
        "description": "Assign or unassign a reviewed failure annotation to a failure-mode cluster.",
        "schema": {
            "type": "object",
            "properties": {
                "annotation_id": {"type": "string"},
                "failure_mode_id": {"type": "string"},
            },
            "required": ["annotation_id"],
            "additionalProperties": False,
        },
        "handler": tool_assign_failure,
    },
    "tracer_lab_delete_failure_mode": {
        "description": "Delete a failure-mode cluster and leave its failures unassigned.",
        "schema": {
            "type": "object",
            "properties": {"failure_mode_id": {"type": "string"}},
            "required": ["failure_mode_id"],
            "additionalProperties": False,
        },
        "handler": tool_delete_failure_mode,
    },
    "tracer_lab_get_reviewed_context": {
        "description": "Return reviewed labels/notes/failure modes separated from suggestions.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": lambda ctx: reviewed_context(connect(ctx.db_path)),
    },
    "tracer_lab_create_suggestion": {
        "description": "Create an agent-authored pending note, label, or failure-mode assignment suggestion.",
        "schema": {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["note", "label", "failure_mode_assignment"]},
                "body": {"type": "string"},
                "label": {"type": "string"},
                "target_type": {"type": "string", "enum": ["trace", "session", "turn", "event"], "default": "trace"},
                "target_id": {"type": "string"},
                "author_name": {"type": "string", "default": "Claude Code"},
                "model_identity": {"type": "string"},
            },
            "required": ["trace_id", "kind"],
            "additionalProperties": False,
        },
        "handler": tool_create_suggestion,
    },
    "tracer_lab_export_eval_candidates": {
        "description": "Export failure-mode support data for later eval creation.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": lambda ctx: eval_candidates(connect(ctx.db_path)),
    },
}


def write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(req_id, payload: dict) -> None:
    write({"jsonrpc": "2.0", "id": req_id, "result": payload})


def error(req_id, code: int, message: str) -> None:
    write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def serve(ctx: LabContext) -> None:
    ensure_imported(ctx)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            method = req.get("method")
            req_id = req.get("id")
            params = req.get("params") or {}

            if method == "initialize":
                result(
                    req_id,
                    {
                        "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                        "serverInfo": {"name": "tracer-lab", "version": __version__},
                        "capabilities": {"tools": {}},
                    },
                )
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                result(
                    req_id,
                    {
                        "tools": [
                            {"name": name, "description": spec["description"], "inputSchema": spec["schema"]}
                            for name, spec in TOOLS.items()
                        ]
                    },
                )
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                if name not in TOOLS:
                    error(req_id, -32602, f"unknown tool: {name}")
                    continue
                payload = TOOLS[name]["handler"](ctx, **args)
                result(req_id, {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]})
            else:
                if req_id is not None:
                    error(req_id, -32601, f"method not found: {method}")
        except Exception as exc:
            result(req.get("id") if "req" in locals() else None, {"isError": True, "content": [{"type": "text", "text": str(exc)}]})
