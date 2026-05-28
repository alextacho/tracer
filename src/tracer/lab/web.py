from __future__ import annotations

import html
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .analysis import (
    accept_annotation,
    assign_annotation_to_failure_mode,
    create_annotation,
    create_failure_mode,
    delete_failure_mode,
    edit_annotation,
    eval_candidates,
    failure_mode_workspace,
    get_trace,
    link_failure_mode,
    list_annotations,
    list_failure_modes,
    list_traces,
    reject_annotation,
    reviewed_context,
    trace_document_for_db_trace,
    update_failure_mode,
)
from .db import connect, init_db
from .paths import LabContext
from .traces import discover_trace_paths, flatten_turns, import_discovered


PACKAGE_DIR = Path(__file__).parent


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def post_value(data: dict[str, list[str]], key: str, default: str = "") -> str:
    return data.get(key, [default])[0]


def js(value: Any) -> str:
    return json.dumps("" if value is None else str(value))


def layout(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} · Tracer Lab</title>
  <link rel="stylesheet" href="/static/app.css">
</head>
<body>
  <header class="topbar">
    <div><a class="brand" href="/">Tracer Lab</a><span class="muted">local trace review</span></div>
    <nav><a href="/">Traces</a><a href="/failure-modes">Failure modes</a><a href="/exports/eval-candidates">Eval export</a></nav>
  </header>
  {body}
  <script>
    const filterStorageKey = 'tracer-lab:timeline-filters';
    function storedFilters() {{
      try {{
        return JSON.parse(localStorage.getItem(filterStorageKey) || '{{}}');
      }} catch (_error) {{
        return {{}};
      }}
    }}
    function saveFilter(type, on) {{
      const filters = storedFilters();
      filters[type] = on;
      localStorage.setItem(filterStorageKey, JSON.stringify(filters));
    }}
    function toggleRows(type, on) {{
      document.querySelectorAll('.trace-rows .t-' + type + ', .trace-rows .t-' + type + '-end').forEach(row => row.classList.toggle('hidden', !on));
    }}
    function setFilter(type, on) {{
      document.querySelectorAll('[data-row-filter="' + type + '"]').forEach(input => input.checked = on);
      toggleRows(type, on);
    }}
    function changeFilter(type, on) {{
      saveFilter(type, on);
      setFilter(type, on);
    }}
    function restoreFilters() {{
      const filters = storedFilters();
      document.querySelectorAll('[data-row-filter]').forEach(input => {{
        const type = input.dataset.rowFilter;
        const on = Object.prototype.hasOwnProperty.call(filters, type) ? filters[type] : input.checked;
        setFilter(type, on);
      }});
    }}
    function setAllDetails(open) {{
      document.querySelectorAll('.trace-rows details:not(.turn-section)').forEach(detail => detail.open = open);
    }}
    function prefillNoteTarget(targetType, targetId, label) {{
      const form = document.getElementById('note-form');
      if (!form) return;
      form.querySelector('[name="target_type"]').value = targetType || 'trace';
      form.querySelector('[name="target_id"]').value = targetId || '';
      const selected = document.getElementById('selected-target');
      if (selected) selected.textContent = label || (targetType + ': ' + targetId);
      const body = form.querySelector('[name="body"]');
      if (body) body.focus();
    }}
    restoreFilters();
  </script>
  <!-- AInnotator + Pindrop feedback bridge. Requires the AInnotator MCP receiver on http://127.0.0.1:9877. -->
  <link rel="stylesheet" href="http://127.0.0.1:9877/vendor/pindrop/style.css">
  <script id="ainnotator-config">
    window.__AINNOTATOR__ = {{"sessionId":"session_a3d7869ab631a2e89a5d21ce","token":"25270c2152af223d4d6b288014264d8520e912b3d74bb968","sourcePath":"","apiBase":"http://127.0.0.1:9877","instructions":"Add comments where the app needs changes, then click Send to Agent. Tell me when you are done annotating."}};
  </script>
  <script type="module" src="http://127.0.0.1:9877/widget.js"></script>
</body>
</html>""".encode("utf-8")


class LabHandler(BaseHTTPRequestHandler):
    context: LabContext

    def db(self):
        return connect(self.context.db_path)

    def send_html(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path: str) -> None:
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def not_found(self) -> None:
        self.send_html(layout("Not found", "<main class='empty'><h1>Not found</h1></main>"), 404)

    def read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8")
        return parse_qs(raw, keep_blank_values=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/static/app.css":
            css = (PACKAGE_DIR / "static" / "app.css").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(css)))
            self.end_headers()
            self.wfile.write(css)
            return

        if path == "/":
            with self.db() as conn:
                traces = list_traces(conn)
            if traces:
                self.redirect(f"/traces/{traces[0]['id']}")
            else:
                self.send_html(render_empty(self.context))
            return

        if path.startswith("/traces/"):
            self.show_trace(path.removeprefix("/traces/"))
            return

        if path.startswith("/artifacts/") and path.endswith("/html"):
            trace_id = path.split("/")[2]
            with self.db() as conn:
                trace = get_trace(conn, trace_id)
            if not trace or not trace.get("html_path"):
                self.not_found()
                return
            artifact = Path(trace["html_path"])
            if not artifact.exists():
                self.not_found()
                return
            body = artifact.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/failure-modes":
            with self.db() as conn:
                self.send_html(render_failure_modes(self.context, failure_mode_workspace(conn)))
            return

        if path == "/exports/eval-candidates":
            with self.db() as conn:
                self.send_json(eval_candidates(conn))
            return

        self.not_found()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        data = self.read_form()
        with self.db() as conn:
            if path == "/import":
                paths = discover_trace_paths(self.context.project_root, self.context.direct_trace)
                manual = post_value(data, "trace_path").strip()
                if manual:
                    paths.append(Path(manual).expanduser())
                import_discovered(conn, paths)
                self.redirect("/")
                return

            if path.startswith("/traces/") and path.endswith("/annotations"):
                trace_id = path.split("/")[2]
                create_annotation(
                    conn,
                    trace_id,
                    kind=post_value(data, "kind", "note"),
                    body=post_value(data, "body").strip(),
                    label=post_value(data, "label").strip() or None,
                    target_type=post_value(data, "target_type", "trace"),
                    target_id=post_value(data, "target_id").strip() or None,
                )
                self.redirect(f"/traces/{trace_id}")
                return

            if path.startswith("/annotations/"):
                annotation_id = path.split("/")[2]
                trace_id = post_value(data, "trace_id")
                if path.endswith("/accept"):
                    accept_annotation(conn, annotation_id)
                elif path.endswith("/reject"):
                    reject_annotation(conn, annotation_id)
                elif path.endswith("/edit"):
                    edit_annotation(
                        conn,
                        annotation_id,
                        body=post_value(data, "body").strip(),
                        label=post_value(data, "label").strip() or None,
                    )
                self.redirect(f"/traces/{trace_id}")
                return

            if path == "/failure-modes":
                create_failure_mode(conn, post_value(data, "title").strip(), post_value(data, "description").strip())
                trace_id = post_value(data, "trace_id")
                self.redirect(f"/traces/{trace_id}" if trace_id else "/failure-modes")
                return

            if path.startswith("/failure-modes/") and path.endswith("/edit"):
                failure_mode_id = path.split("/")[2]
                update_failure_mode(
                    conn,
                    failure_mode_id,
                    title=post_value(data, "title").strip(),
                    description=post_value(data, "description").strip(),
                )
                self.redirect("/failure-modes")
                return

            if path.startswith("/failure-modes/") and path.endswith("/delete"):
                failure_mode_id = path.split("/")[2]
                delete_failure_mode(conn, failure_mode_id)
                self.redirect("/failure-modes")
                return

            if path == "/failure-modes/assign":
                annotation_id = post_value(data, "annotation_id").strip()
                if annotation_id:
                    assign_annotation_to_failure_mode(conn, post_value(data, "failure_mode_id").strip() or None, annotation_id)
                self.redirect("/failure-modes")
                return

            if path.startswith("/failure-modes/") and path.endswith("/link"):
                failure_mode_id = path.split("/")[2]
                trace_id = post_value(data, "trace_id")
                link_failure_mode(
                    conn,
                    failure_mode_id,
                    trace_id=trace_id or None,
                    annotation_id=post_value(data, "annotation_id") or None,
                )
                self.redirect(f"/traces/{post_value(data, 'return_trace_id') or trace_id}")
                return
        self.not_found()

    def show_trace(self, trace_id: str) -> None:
        with self.db() as conn:
            trace = get_trace(conn, trace_id)
            if trace is None:
                self.not_found()
                return
            traces = list_traces(conn)
            modes = list_failure_modes(conn)
            annotations = list_annotations(conn, trace_id)
            doc = trace_document_for_db_trace(conn, trace_id)
            turns = flatten_turns(doc.get("session", {}) or {})
        self.send_html(render_trace(self.context, traces, trace, modes, annotations, turns))


def render_empty(context: LabContext) -> bytes:
    body = f"""
<main class="empty">
  <h1>No traces found</h1>
  <p>Point Tracer Lab at a project with <code>.tracer/traces</code>, or import a direct <code>trace.json</code>.</p>
  <form method="post" action="/import" class="inline-form">
    <input name="trace_path" placeholder="/path/to/trace.json">
    <button type="submit">Import</button>
  </form>
  <p class="small">Database: <code>{esc(context.db_path)}</code></p>
</main>"""
    return layout("No traces", body)


def render_trace(
    context: LabContext,
    traces: list[dict],
    active: dict,
    modes: list[dict],
    annotations: list[dict],
    turns: list[dict],
) -> bytes:
    trace_cards = "\n".join(
        f"""<a class="trace-card {'active' if t['id'] == active['id'] else ''}" href="/traces/{esc(t['id'])}">
  <strong>{esc(t.get('command') or t.get('first_user_prompt') or t.get('session_id') or 'trace')}</strong>
  <span>{esc(t.get('started_at') or t.get('imported_at'))}</span>
  <span>{esc((t.get('summary') or {}).get('turns') or '?')} turns · {esc(t.get('pending_count', 0))} pending</span>
</a>"""
        for t in traces
    )
    summary = active.get("summary") or {}
    model_mix = render_model_mix(summary.get("model_mix") or {}, turns)
    html_link = (
        f"""<a class="button secondary" href="/artifacts/{esc(active['id'])}/html" target="_blank">trace.html</a>"""
        if active.get("html_path")
        else ""
    )
    timeline = render_timeline(active, turns)
    annotation_html = "\n".join(render_annotation(active["id"], item) for item in annotations)

    body = f"""
<main class="workspace">
  <aside class="pane trace-list">
    <div class="pane-header"><h2>Traces</h2><form method="post" action="/import"><button type="submit">Rescan</button></form></div>
    <form method="post" action="/import" class="stacked-form"><input name="trace_path" placeholder="Import trace.json path"></form>
    <div class="items">{trace_cards}</div>
    <p class="small db-path">DB <code>{esc(context.db_path)}</code></p>
  </aside>
  <section class="pane detail">
    <div class="pane-header trace-title"><div><h1>{esc(active.get('command') or 'Trace')}</h1><p>{esc(active.get('first_user_prompt') or active.get('session_id'))}</p></div>{html_link}</div>
    <section class="stats">
      <div><span>Turns</span><strong>{esc(summary.get('turns') or '?')}</strong></div>
      <div><span>Sessions</span><strong>{esc(summary.get('sessions') or '?')}</strong></div>
      <div><span>Cost tokens</span><strong>{esc(round(summary.get('cost_weighted') or 0))}</strong></div>
      <div class="stat-models"><span>Models</span><strong>{model_mix}</strong></div>
    </section>
    <section class="timeline-controls">
      <button type="button" onclick="setAllDetails(true)">Expand details</button>
      <button type="button" onclick="setAllDetails(false)">Collapse details</button>
      <label><input type="checkbox" checked data-row-filter="user" onchange="changeFilter('user', this.checked)"> user messages</label>
      <label><input type="checkbox" checked data-row-filter="assistant" onchange="changeFilter('assistant', this.checked)"> assistant text</label>
      <label><input type="checkbox" checked data-row-filter="thinking" onchange="changeFilter('thinking', this.checked)"> thinking</label>
      <label><input type="checkbox" checked data-row-filter="tool" onchange="changeFilter('tool', this.checked)"> tool calls</label>
      <label><input type="checkbox" checked data-row-filter="divider" onchange="changeFilter('divider', this.checked)"> turn markers</label>
      <label><input type="checkbox" checked data-row-filter="agent" onchange="changeFilter('agent', this.checked)"> agent rows</label>
    </section>
    <section class="timeline">{timeline}</section>
  </section>
  <aside class="pane review">
    <section class="review-block">
      <h2>Add reviewed analysis</h2>
      <form id="note-form" method="post" action="/traces/{esc(active['id'])}/annotations" class="stacked-form">
        <label>Kind<select name="kind"><option value="note">Note</option><option value="label">Label</option></select></label>
        <label>Label<input name="label" placeholder="optional short label"></label>
        <p class="selected-target">Target: <span id="selected-target">trace</span></p>
        <label>Target<select name="target_type"><option value="trace">Trace</option><option value="turn">Turn</option><option value="event">Event</option><option value="session">Session</option></select></label>
        <label>Target id<input name="target_id" placeholder="turn or event id"></label>
        <label>Note<textarea name="body" rows="4" placeholder="What failed, why it matters, or what to try next"></textarea></label>
        <button type="submit">Save</button>
      </form>
    </section>
    <section class="review-block">
      <h2>Notes, labels, suggestions</h2>
      {annotation_html}
    </section>
  </aside>
</main>"""
    return layout("Trace", body)


def render_model_mix(summary_mix: dict, turns: list[dict]) -> str:
    turn_counts: dict[str, int] = {}
    for turn in turns:
        model = turn.get("model")
        if model:
            turn_counts[model] = turn_counts.get(model, 0) + 1
    counts = turn_counts or {str(model): int(count or 0) for model, count in summary_mix.items()}
    if not counts:
        return "unknown"
    return " ".join(
        f"""<span class="model-chip">{esc(short_model(model))}<span>{fmt_num(count)} turns</span></span>"""
        for model, count in counts.items()
    )


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def relative_time(value: str | None, started_at: datetime | None) -> str:
    current = parse_timestamp(value)
    if current is None or started_at is None:
        return ""
    seconds = max(0, int((current - started_at).total_seconds()))
    if seconds < 60:
        return f"+{seconds:.1f}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"+{minutes}m{rem:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"+{hours}h{minutes:02d}m"


def fmt_num(value: int | float | None) -> str:
    if value is None:
        return "0"
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return esc(value)


def fmt_dollars(value: int | float | None) -> str:
    if value is None:
        return "$0.000"
    try:
        return f"${float(value):.3f}"
    except (TypeError, ValueError):
        return esc(value)


def short_model(model: str | None) -> str:
    if not model:
        return "unknown"
    return model.replace("claude-", "").replace("sonnet-", "sonnet-").replace("opus-", "opus-")


def render_timeline(active: dict, turns: list[dict]) -> str:
    started_at = parse_timestamp(active.get("started_at"))
    trace_id = active["id"]
    rows: list[str] = []
    has_user_events = any(
        event.get("kind") == "user"
        for turn in turns
        for event in turn.get("events", [])
    )
    if active.get("first_user_prompt") and not has_user_events:
        rows.append(
            render_trace_row(
                "user",
                active.get("started_at"),
                started_at,
                0,
                "▸",
                f"""<span class="label"><span class="label-name">user</span> {esc(active.get("first_user_prompt"))}</span>""",
                "",
            )
        )

    previous_depth = 0
    previous_key: tuple[int, str | None, str | None] | None = None
    for index, turn in enumerate(turns):
        depth = int(turn.get("depth") or 0)
        current_key = (depth, turn.get("parent_tool_id"), turn.get("session_id"))
        if depth > 0 and (depth > previous_depth or previous_key != current_key):
            rows.append(render_agent_boundary("agent", turn.get("timestamp"), started_at, depth, "▶", "Agent started", turn))

        rows.append(render_turn_section(turn, trace_id, started_at))

        next_depth = int(turns[index + 1].get("depth") or 0) if index + 1 < len(turns) else 0
        next_key = (
            next_depth,
            turns[index + 1].get("parent_tool_id"),
            turns[index + 1].get("session_id"),
        ) if index + 1 < len(turns) else None
        if depth > 0 and next_depth == depth and next_key != current_key:
            rows.append(render_agent_boundary("agent-end", turn.get("timestamp"), started_at, depth, "◀", "Agent returned", turn))
        while depth > next_depth:
            rows.append(render_agent_boundary("agent-end", turn.get("timestamp"), started_at, depth, "◀", "Agent returned", turn))
            depth -= 1
        previous_depth = next_depth if next_depth < int(turn.get("depth") or 0) else int(turn.get("depth") or 0)
        previous_key = current_key

    return f"""<div class="trace-rows">{"".join(rows)}</div>"""


def render_turn_section(turn: dict, trace_id: str, started_at: datetime | None) -> str:
    depth = int(turn.get("depth") or 0)
    depth_class = f" d{min(max(depth, 0), 4)}" if depth else ""
    events = "".join(render_event(event, trace_id, turn, started_at) for event in turn.get("events", []))
    return f"""<section class="turn-section{depth_class}">
  {render_turn_marker_content(turn, started_at, trace_id)}
  <div class="turn-section-events">{events}</div>
</section>"""


def render_turn_marker_content(turn: dict, started_at: datetime | None, trace_id: str) -> str:
    usage = turn.get("usage") or {}
    billable_input = (usage.get("input") or 0) + (usage.get("cache_creation") or 0) + (usage.get("cache_read") or 0)
    output = usage.get("output") or 0
    cost = usage.get("cost_weighted")
    dollars = usage.get("dollars")
    session_hint = ""
    if int(turn.get("depth") or 0) > 0:
        session_hint = f""" <span class="turn-session">{esc(turn.get("session_label"))}</span>"""
    body = (
        f"""<span class="turn-marker">turn {esc(turn.get("n"))}{session_hint} """
        f"""(in {fmt_num(billable_input)} · out {fmt_num(output)} · cost {fmt_num(cost)})</span>"""
    )
    meta = (
        f"""<span class="turn-marker">{esc(short_model(turn.get("model")))} · {fmt_dollars(dollars)}</span>"""
    )
    return render_trace_row(
        "divider",
        turn.get("timestamp"),
        started_at,
        int(turn.get("depth") or 0),
        "·",
        body,
        meta,
        target_type="turn",
        target_id=turn.get("turn_id"),
        target_label=f"turn {turn.get('n')}",
    )


def render_agent_boundary(
    row_type: str,
    timestamp: str | None,
    started_at: datetime | None,
    depth: int,
    icon: str,
    label: str,
    turn: dict,
) -> str:
    body = (
        f"""<span class="label"><span class="label-name">{esc(label)}</span> """
        f"""<span class="muted">({esc(turn.get("session_label"))})</span></span>"""
    )
    meta = f"""<span class="turn-marker">{esc(turn.get("parent_tool_id") or "")}</span>"""
    return render_trace_row(row_type, timestamp, started_at, depth, icon, body, meta)


def render_trace_row(
    row_type: str,
    timestamp: str | None,
    started_at: datetime | None,
    depth: int,
    icon: str,
    body: str,
    meta: str,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    target_label: str | None = None,
) -> str:
    depth_class = f" d{min(max(depth, 0), 4)}" if depth else ""
    click = ""
    if target_type and target_id:
        click = (
            f""" onclick='prefillNoteTarget({js(target_type)}, {js(target_id)}, {js(target_label or target_id)})' """
            f"""role="button" tabindex="0" """
        )
    meta_html = f"""<span class="row-meta">{meta}</span>""" if meta else ""
    return f"""<div class="trace-row t-{esc(row_type)}{depth_class}"{click}>
  <div class="time">{esc(relative_time(timestamp, started_at))}</div>
  <div class="row-main">
    <div class="row-heading"><span class="icon">{esc(icon)}</span><div class="row-body">{body}</div>{meta_html}</div>
  </div>
</div>"""


def render_event(event: dict, trace_id: str, turn: dict | None = None, started_at: datetime | None = None) -> str:
    kind = event.get("kind")
    timestamp = event.get("timestamp") or (turn or {}).get("timestamp")
    depth = int((turn or {}).get("depth") or 0)
    if kind == "user":
        text = event.get("text") or ""
        body = (
            f"""<span class="label"><span class="label-name">user</span> """
            f"""{render_text_body(text)}</span>"""
        )
        return render_trace_row(
            "user",
            timestamp,
            started_at,
            depth,
            "▸",
            body,
            "",
            target_type="event",
            target_id=event.get("id"),
            target_label="user message",
        )
    if kind == "text":
        text = event.get("text") or ""
        body = render_text_body(text)
        return render_trace_row(
            "assistant",
            timestamp,
            started_at,
            depth,
            "≡",
            body,
            "",
            target_type="event",
            target_id=event.get("id"),
            target_label="assistant message",
        )
    elif kind == "thinking":
        body = f"""<span class="label muted">thinking block · {esc(event.get("chars"))} chars</span>"""
        return render_trace_row("thinking", timestamp, started_at, depth, "…", body, "")
    elif kind == "tool_use":
        preview = esc(event.get("result_preview")) if event.get("result_preview") else ""
        if event.get("tool_output_hidden"):
            result_details = "<p class='muted'>tool output hidden by filter</p>"
        else:
            result_details = render_result_details(event, preview)
        input_details = render_tool_input(event)
        child = " <span class='pill'>spawned subagent</span>" if event.get("has_child_session") else ""
        meta = f"""~{fmt_num((event.get("result") or {}).get("tokens"))} tok"""
        body = (
            f"""<div class="tool-heading"><span class="label"><span class="label-name">{esc(event.get("name"))}</span>"""
            f"""(<span class="tool-label">{esc(event.get("input_label"))}</span>)</span>{child}"""
            f"""<span class="row-meta">{meta}</span></div>"""
            f"""{input_details}{result_details}"""
        )
        return render_trace_row(
            "tool",
            timestamp,
            started_at,
            depth,
            "●",
            body,
            "",
            target_type="event",
            target_id=event.get("id"),
            target_label=f"{event.get('name')} output",
        )
    return ""


def render_text_body(text: str) -> str:
    if len(text) <= 220:
        return f"""<span class="label">{esc(text)}</span>"""
    short = text[:220].rstrip() + "..."
    return (
        f"""<span class="label">{esc(short)}</span>"""
        f"""<details><summary>full text ({fmt_num(len(text))} chars)</summary><pre>{esc(text)}</pre></details>"""
    )


def render_tool_input(event: dict) -> str:
    tool_input = event.get("input") or {}
    if event.get("name") == "Read" and tool_input.get("file_path"):
        rendered = f"file: {tool_input.get('file_path')}"
    elif event.get("name") == "Bash" and tool_input.get("command"):
        rendered = tool_input.get("command")
    elif event.get("name") in ("Edit", "Write") and tool_input.get("file_path"):
        rendered = "\n".join(f"{key}: {value}" for key, value in tool_input.items())
    else:
        rendered = json.dumps(tool_input, ensure_ascii=False, indent=2)
    event_id = event.get("id")
    label = f"{event.get('name')} input"
    return (
        f"""<details><summary onclick='event.stopPropagation(); prefillNoteTarget("event", {js(str(event_id) + '#input')}, {js(label)})'>input</summary>"""
        f"""<pre onclick='event.stopPropagation(); prefillNoteTarget("event", {js(str(event_id) + '#input')}, {js(label)})'>{esc(rendered)}</pre></details>"""
    )


def render_result_details(event: dict, preview: str) -> str:
    result = event.get("result") or {}
    tokens = result.get("tokens")
    full_chars = event.get("result_full_chars") or len(event.get("result_preview") or "")
    preview_chars = event.get("result_preview_chars") or len(event.get("result_preview") or "")
    showing = ""
    if full_chars and preview_chars and full_chars > preview_chars:
        showing = f" (showing first {fmt_num(preview_chars)} of {fmt_num(full_chars)} chars)"
    if not preview:
        return ""
    event_id = event.get("id")
    label = f"{event.get('name')} output"
    return (
        f"""<details><summary onclick='event.stopPropagation(); prefillNoteTarget("event", {js(str(event_id) + '#output')}, {js(label)})'>result ~{fmt_num(tokens)} tok{showing}</summary>"""
        f"""<pre onclick='event.stopPropagation(); prefillNoteTarget("event", {js(str(event_id) + '#output')}, {js(label)})'>{preview}</pre></details>"""
    )


def render_annotation(trace_id: str, item: dict) -> str:
    actions = ""
    if item["status"] == "pending":
        actions = f"""<div class="actions">
  <form method="post" action="/annotations/{esc(item['id'])}/accept"><input type="hidden" name="trace_id" value="{esc(trace_id)}"><button type="submit">Accept</button></form>
  <form method="post" action="/annotations/{esc(item['id'])}/reject"><input type="hidden" name="trace_id" value="{esc(trace_id)}"><button type="submit" class="danger">Reject</button></form>
</div>"""
    robot = '<span class="robot">[robot]</span> ' if item["author_type"] == "agent" else ""
    origin = f"<p class='small'>Edited from <code>{esc(item['origin_annotation_id'])}</code></p>" if item.get("origin_annotation_id") else ""
    return f"""<article class="annotation {esc(item['status'])} {esc(item['author_type'])}">
  <header><span class="kind">{esc(item['kind'])}</span><span class="status">{esc(item['status'])}</span></header>
  {f"<strong>{esc(item['label'])}</strong>" if item.get('label') else ""}
  {f"<p>{esc(item['body'])}</p>" if item.get('body') else ""}
  <p class="small">{robot}{esc(item['author_type'])} · {esc(item['author_name'])}{' · ' + esc(item['model_identity']) if item.get('model_identity') else ''} · {esc(item['target_type'])}{': <code>' + esc(item['target_id']) + '</code>' if item.get('target_id') else ''}</p>
  {origin}
  {actions}
  <details><summary>Edit</summary>
    <form method="post" action="/annotations/{esc(item['id'])}/edit" class="stacked-form compact">
      <input type="hidden" name="trace_id" value="{esc(trace_id)}">
      <input name="label" value="{esc(item.get('label') or '')}">
      <textarea name="body" rows="3">{esc(item.get('body') or '')}</textarea>
      <button type="submit">Save edit</button>
    </form>
  </details>
</article>"""


def render_failure_modes(context: LabContext, workspace: dict) -> bytes:
    modes = workspace["failure_modes"]
    failures = workspace["failures"]
    failures_by_mode = workspace["failures_by_mode"]
    unassigned = workspace["unassigned_failures"]
    failure_items = "\n".join(render_failure_item(item, modes) for item in failures) or "<p class='empty-note'>No reviewed failures yet.</p>"
    unassigned_items = "\n".join(render_compact_failure(item) for item in unassigned) or "<p class='empty-note'>All failures are assigned.</p>"
    mode_cards = "\n".join(render_failure_mode_card(mode, failures_by_mode.get(mode["id"], [])) for mode in modes)
    if not mode_cards:
        mode_cards = "<p class='empty-note'>No failure modes yet. Create one or ask an agent to propose clusters through MCP.</p>"
    body = f"""<main class="failure-workspace">
  <aside class="pane failure-list-pane">
    <div class="pane-header"><div><h1>Failures</h1><p>{len(failures)} reviewed</p></div></div>
    <div class="failure-list">{failure_items}</div>
  </aside>
  <section class="pane failure-cluster-pane">
    <div class="pane-header"><div><h1>Failure modes</h1><p>{len(modes)} clusters · {len(unassigned)} unassigned</p></div></div>
    <form method="post" action="/failure-modes" class="cluster-create-form">
      <input name="title" placeholder="New failure mode">
      <textarea name="description" rows="2" placeholder="Definition and boundaries"></textarea>
      <button type="submit">Create</button>
    </form>
    <section class="unassigned-block">
      <h2>Unassigned failures</h2>
      <div class="compact-failure-list">{unassigned_items}</div>
    </section>
    <div class="mode-clusters">{mode_cards}</div>
  </section>
</main>"""
    return layout("Failure modes", body)


def failure_title(item: dict) -> str:
    return item.get("label") or item.get("body") or item.get("kind") or "failure"


def failure_trace_label(item: dict) -> str:
    return item.get("command") or item.get("first_user_prompt") or item.get("trace_id") or "trace"


def render_failure_item(item: dict, modes: list[dict]) -> str:
    options = ["<option value=''>Unassigned</option>"] + [
        f"""<option value="{esc(mode['id'])}" {'selected' if mode['id'] == item.get('failure_mode_id') else ''}>{esc(mode['title'])}</option>"""
        for mode in modes
    ]
    assignment = item.get("failure_mode_title") or "Unassigned"
    return f"""<article class="failure-item">
  <div>
    <strong>{esc(failure_title(item))}</strong>
    {f"<p>{esc(item.get('body'))}</p>" if item.get("body") and item.get("label") else ""}
    <p class="small">{esc(failure_trace_label(item))} · {esc(item.get('target_type'))}{': ' + esc(item.get('target_id')) if item.get('target_id') else ''}</p>
  </div>
  <form method="post" action="/failure-modes/assign" class="assignment-form">
    <input type="hidden" name="annotation_id" value="{esc(item['id'])}">
    <label>Mode<select name="failure_mode_id">{''.join(options)}</select></label>
    <button type="submit" {'disabled' if not modes else ''}>Assign</button>
  </form>
  <span class="assignment-badge">{esc(assignment)}</span>
</article>"""


def render_compact_failure(item: dict) -> str:
    return f"""<a class="compact-failure" href="/traces/{esc(item['trace_id'])}">
  <strong>{esc(failure_title(item))}</strong>
  <span>{esc(failure_trace_label(item))}</span>
</a>"""


def render_failure_mode_card(mode: dict, failures: list[dict]) -> str:
    assigned = "\n".join(render_compact_failure(item) for item in failures) or "<p class='empty-note'>No assigned failures.</p>"
    return f"""<article class="mode-cluster">
  <form method="post" action="/failure-modes/{esc(mode['id'])}/edit" class="cluster-edit-form">
    <input name="title" value="{esc(mode['title'])}">
    <textarea name="description" rows="2">{esc(mode.get('description') or '')}</textarea>
    <div class="cluster-actions">
      <span class="small">{len(failures)} assigned</span>
      <button type="submit">Save</button>
    </div>
  </form>
  <div class="compact-failure-list">{assigned}</div>
  <form method="post" action="/failure-modes/{esc(mode['id'])}/delete" class="delete-cluster-form">
    <button type="submit" class="danger">Delete mode</button>
  </form>
</article>"""


def create_server(context: LabContext, host: str = "127.0.0.1", port: int = 8768) -> ThreadingHTTPServer:
    init_db(context.db_path)
    with connect(context.db_path) as conn:
        import_discovered(conn, discover_trace_paths(context.project_root, context.direct_trace))

    class Handler(LabHandler):
        pass

    Handler.context = context
    return ThreadingHTTPServer((host, port), Handler)
