#!/usr/bin/env python3
"""Trace artifact tool for coding-agent sessions.

Walks JSONL session logs (including nested subagent logs where available) and produces:
  - self-contained HTML trace (chronological view)
  - SQLite history append (track improvements over time)

Usage:
  tracer save    <jsonl-path-or-session-id> [--out DIR]
  tracer read    <ref-or-trace-json>
  tracer open    <ref-or-trace-json>
"""
from __future__ import annotations

import argparse
import dataclasses
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
__version__ = "0.1.3"

# Legacy central store retained for internal migration helpers.
LEGACY_TRACER_DB_ROOT = Path(
    os.environ.get(
        "TRACER_DB_ROOT",
        str(Path.home() / ".local" / "share" / "tracer-db"),
    )
).expanduser()

# Per-project storage lives in <project-root>/.tracer/.
TRACER_DIRNAME = ".tracer"
TRACER_TRACES = "traces"
TRACER_DB_FILE = "runs.db"
TRACER_CONFIG = "config.json"


def find_project_root(start: str | Path | None) -> Path | None:
    """Walk up from `start` looking for, in order:
       1. an existing .tracer/ dir
       2. a .git/ dir
       3. a recognized project marker (package.json, pyproject.toml, Cargo.toml, go.mod)
    Returns None if nothing is found before the filesystem root."""
    if not start:
        return None
    p = Path(start).expanduser()
    try:
        p = p.resolve()
    except Exception:
        return None
    if not p.exists():
        return None
    start_path = p
    home_root = Path.home().resolve()
    home_tracer = (Path.home() / TRACER_DIRNAME).resolve()
    markers = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")
    while True:
        if p == home_root and p != start_path:
            if p == p.parent:
                return None
            p = p.parent
            continue
        tracer_dir = p / TRACER_DIRNAME
        if tracer_dir.is_dir() and not (tracer_dir.resolve() == home_tracer and p != start_path):
            return p
        if (p / ".git").is_dir():
            return p
        for m in markers:
            if (p / m).exists():
                return p
        if p == p.parent:
            return None
        p = p.parent


def find_init_target_root(start: str | Path | None) -> Path | None:
    """Find where internal initialization should create .tracer/.

    Unlike normal trace resolution, initialization must not treat an ancestor .tracer/
    directory as the selected project. Otherwise a user-level ~/.tracer can
    capture initialization for arbitrary folders that do not yet have markers.
    """
    if not start:
        return None
    p = Path(start).expanduser()
    try:
        p = p.resolve()
    except Exception:
        return None
    if not p.exists():
        return None

    markers = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")
    cur = p
    while True:
        if (cur / TRACER_DIRNAME).is_dir() and cur == p:
            return cur
        if (cur / ".git").is_dir():
            return cur
        for m in markers:
            if (cur / m).exists():
                return cur
        if cur == cur.parent:
            return p
        cur = cur.parent


def project_tracer_dir(project_root: Path, create: bool = True) -> Path:
    d = project_root / TRACER_DIRNAME
    if create:
        (d / TRACER_TRACES).mkdir(parents=True, exist_ok=True)
    return d


def project_db_path(project_root: Path, create: bool = True) -> Path:
    return project_tracer_dir(project_root, create=create) / TRACER_DB_FILE


def project_traces_dir(project_root: Path, create: bool = True) -> Path:
    return project_tracer_dir(project_root, create=create) / TRACER_TRACES


def active_project_root(cwd: str | None = None, *, error_if_missing: bool = True) -> Path | None:
    """The project the user is currently 'in' (from the analyze-time cwd)."""
    root = find_project_root(cwd or os.getcwd())
    if root is None and error_if_missing:
        here = cwd or os.getcwd()
        raise SystemExit(
            f"not inside a tracer project: walked up from {here} and didn't find\n"
            f"  a .tracer/, .git/, or recognized project marker.\n"
            f"  Run `tracer save` from a project directory to create one."
        )
    return root


def session_project_root(session: "Session") -> Path:
    """The project the SESSION belongs to (its recorded cwd → git root)."""
    if session.cwd:
        r = find_project_root(session.cwd)
        if r:
            return r
        return Path(session.cwd).expanduser().resolve()
    return Path(os.getcwd())


def resolve_db(db_override: str | None, *, for_session: "Session | None" = None,
               cwd_override: str | None = None) -> Path:
    """Resolve the SQLite db path. Priority:
       1. explicit --db value
       2. for_session's project root → .tracer/runs.db
       3. cwd_override / current cwd → walk-up project root → .tracer/runs.db"""
    if db_override:
        return Path(db_override).expanduser()
    if for_session is not None:
        return project_db_path(session_project_root(for_session))
    root = active_project_root(cwd_override)
    return project_db_path(root)


# Cost weights — roughly track Anthropic API pricing ratios.
# Input cache_creation is 1.25× base input; cache_read is 0.1× base input; output ≈ 5× input.
W_INPUT = 1.0
W_CACHE_CREATE = 1.25
W_CACHE_READ = 0.10
W_OUTPUT = 5.0


# Back-compat alias: legacy default db path. New code uses project_db_path().
DEFAULT_DB = LEGACY_TRACER_DB_ROOT / "runs.db"


def cost_weighted(in_tok: int, cc: int, cr: int, out: int) -> float:
    return W_INPUT * in_tok + W_CACHE_CREATE * cc + W_CACHE_READ * cr + W_OUTPUT * out


# ─── Model pricing (USD per 1M tokens) ─────────────────────────────────────
# Tuple = (input, output, cache_write_5m, cache_write_1h, cache_read)
# Cache writes default to 5m (most common). Override via ~/.config/tracer/pricing.json.
MODEL_PRICING_DEFAULT: dict[str, tuple[float, float, float, float, float]] = {
    # Opus family
    "claude-opus-4-7":  (15.0, 75.0, 18.75, 30.0, 1.50),
    "claude-opus-4-6":  (15.0, 75.0, 18.75, 30.0, 1.50),
    "claude-opus-4":    (15.0, 75.0, 18.75, 30.0, 1.50),
    # Sonnet family
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 6.0, 0.30),
    "claude-sonnet-4-5": (3.0, 15.0, 3.75, 6.0, 0.30),
    "claude-sonnet-4":   (3.0, 15.0, 3.75, 6.0, 0.30),
    # Haiku
    "claude-haiku-4-5":          (1.0, 5.0, 1.25, 2.0, 0.10),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 1.25, 2.0, 0.10),
}


def _load_pricing() -> dict[str, tuple[float, float, float, float, float]]:
    """Merge defaults with optional user overrides at ~/.config/tracer/pricing.json."""
    table = dict(MODEL_PRICING_DEFAULT)
    p = Path.home() / ".config" / "tracer" / "pricing.json"
    if p.exists():
        try:
            data = json.loads(p.read_text())
            for model, vals in data.items():
                if isinstance(vals, list) and len(vals) == 5:
                    table[model] = tuple(float(v) for v in vals)  # type: ignore[assignment]
        except Exception as e:
            print(f"warning: failed to load pricing override at {p}: {e}", file=sys.stderr)
    return table


MODEL_PRICING = _load_pricing()


def dollars_for(
    model: str | None,
    input_tokens: int,
    cache_creation: int,
    cache_read: int,
    output_tokens: int,
) -> float | None:
    """USD cost for one call given its model + token counts. None if model is
    unknown (no pricing data)."""
    if not model:
        return None
    p = MODEL_PRICING.get(model)
    if p is None:
        return None
    inp_r, out_r, ccw5_r, _ccw1_r, cr_r = p
    return (
        input_tokens * inp_r
        + cache_creation * ccw5_r
        + cache_read * cr_r
        + output_tokens * out_r
    ) / 1_000_000


# ────────────────────────────────────────────────────────────────────────────
# Data model
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class ToolCall:
    name: str
    input_label: str                 # short human-readable summary of args
    result_tokens: int               # approx tokens of tool_result body (chars/4)
    tool_use_id: str
    input_full: dict = field(default_factory=dict)
    result_content: str = ""         # full tool_result body retained in trace.json
    result_preview: str = ""         # first ~3000 chars of tool result, for trace display
    result_full_chars: int = 0       # full result size, in chars
    timestamp: str = ""              # of the tool_use itself
    child_session: "Session | None" = None  # set for Agent calls


@dataclass
class UserMessage:
    id: str                          # "<request_id>:user:<idx>"
    text: str
    timestamp: str = ""


@dataclass
class TextBlock:
    id: str                          # "<request_id>:txt:<idx>"
    text: str


@dataclass
class ThinkingBlock:
    id: str                          # "<request_id>:think:<idx>"
    chars: int                       # length (content usually signed/opaque)


# An Event is one of: UserMessage | TextBlock | ThinkingBlock | ToolCall, retained in the
# order the model emitted them within a single turn.
Event = "UserMessage | TextBlock | ThinkingBlock | ToolCall"


@dataclass
class Turn:
    request_id: str
    timestamp: str
    input_tokens: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    output_tokens: int = 0
    model: str | None = None         # e.g. "claude-opus-4-7" — None if not recorded
    events: list = field(default_factory=list)  # ordered list of Event

    # ── Aggregate views ───────────────────────────────────────────────
    @property
    def user_messages(self) -> list[str]:
        return [e.text for e in self.events if isinstance(e, UserMessage)]

    @property
    def text_blocks(self) -> list[str]:
        return [e.text for e in self.events if isinstance(e, TextBlock)]

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [e for e in self.events if isinstance(e, ToolCall)]

    @property
    def text_chars(self) -> int:
        return sum(len(e.text) for e in self.events if isinstance(e, TextBlock))

    @property
    def thinking_chars(self) -> int:
        return sum(e.chars for e in self.events if isinstance(e, ThinkingBlock))

    @property
    def billable_input(self) -> int:
        return self.input_tokens + self.cache_creation + self.cache_read

    @property
    def cost(self) -> float:
        return cost_weighted(
            self.input_tokens, self.cache_creation, self.cache_read, self.output_tokens
        )

    @property
    def dollars(self) -> float | None:
        return dollars_for(
            self.model, self.input_tokens, self.cache_creation,
            self.cache_read, self.output_tokens,
        )


@dataclass
class Clip:
    """A range of root-session turns to include in totals and rendered views.
    Turns outside this range stay in the trace but are excluded from numbers."""
    start_turn: int | None = None    # 1-based, inclusive; None = from beginning
    end_turn: int | None = None      # 1-based, inclusive; None = to end
    matched_pattern: str | None = None  # if --clip-from was used, what matched
    reason: str | None = None


@dataclass
class Session:
    path: Path
    session_id: str
    agent_type: str | None           # None for main session
    command: str | None              # parsed slash command if any, e.g. "/ci:run"
    first_user_prompt: str           # truncated
    cwd: str = ""                    # working directory where session was invoked
    turns: list[Turn] = field(default_factory=list)
    clip: Clip | None = None         # optional clip range on this session
    source: str = "claude"           # "claude" or "codex"

    @property
    def label(self) -> str:
        if self.agent_type:
            return f"agent: {self.agent_type}"
        if self.command:
            return f"{self.command}"
        return f"session: {self.session_id[:8]}"

    @property
    def included_turns(self) -> list[Turn]:
        """Turns inside the active clip range (or all turns if no clip)."""
        if self.clip is None:
            return list(self.turns)
        start = (self.clip.start_turn or 1) - 1
        end = self.clip.end_turn if self.clip.end_turn is not None else len(self.turns)
        return list(self.turns[start:end])

    @property
    def clipped_turn_indices(self) -> tuple[set[int], set[int]]:
        """Return (prefix_indices, suffix_indices) — 0-based — that are excluded.
        Useful for renderers to gray out clipped rows."""
        if self.clip is None:
            return (set(), set())
        n = len(self.turns)
        start = (self.clip.start_turn or 1) - 1
        end = self.clip.end_turn if self.clip.end_turn is not None else n
        return (set(range(0, start)), set(range(end, n)))

    @property
    def own_billable_input(self) -> int:
        return sum(t.billable_input for t in self.included_turns)

    @property
    def own_output(self) -> int:
        return sum(t.output_tokens for t in self.included_turns)

    @property
    def own_cost(self) -> float:
        return sum(t.cost for t in self.included_turns)

    @property
    def own_dollars(self) -> tuple[float, int]:
        """(total_dollars, n_turns_with_unknown_pricing) for this session's
        included turns. Unknown-priced turns contribute 0."""
        total = 0.0
        unknown = 0
        for t in self.included_turns:
            d = t.dollars
            if d is None:
                unknown += 1
            else:
                total += d
        return (total, unknown)

    @property
    def own_model_mix(self) -> dict[str, int]:
        mix: dict[str, int] = {}
        for t in self.included_turns:
            key = t.model or "(unknown)"
            mix[key] = mix.get(key, 0) + 1
        return mix

    def _children(self) -> list["Session"]:
        # Legacy soft clips exclude subagents spawned by clipped-out turns.
        return [tc.child_session for t in self.included_turns
                for tc in t.tool_calls if tc.child_session]

    @property
    def total_billable_input(self) -> int:
        return self.own_billable_input + sum(s.total_billable_input for s in self._children())

    @property
    def total_output(self) -> int:
        return self.own_output + sum(s.total_output for s in self._children())

    @property
    def total_cost(self) -> float:
        return self.own_cost + sum(s.total_cost for s in self._children())

    @property
    def total_dollars(self) -> tuple[float, int]:
        own_d, own_u = self.own_dollars
        total_d = own_d
        total_u = own_u
        for s in self._children():
            d, u = s.total_dollars
            total_d += d
            total_u += u
        return (total_d, total_u)

    @property
    def total_model_mix(self) -> dict[str, int]:
        mix = dict(self.own_model_mix)
        for s in self._children():
            for k, v in s.total_model_mix.items():
                mix[k] = mix.get(k, 0) + v
        return mix

    @property
    def wall_seconds(self) -> float:
        if not self.turns:
            return 0.0
        try:
            t0 = datetime.fromisoformat(self.turns[0].timestamp.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(self.turns[-1].timestamp.replace("Z", "+00:00"))
            return (t1 - t0).total_seconds()
        except Exception:
            return 0.0

    def all_sessions(self) -> Iterable["Session"]:
        yield self
        for turn in self.turns:
            for tc in turn.tool_calls:
                if tc.child_session is not None:
                    yield from tc.child_session.all_sessions()


# ────────────────────────────────────────────────────────────────────────────
# Parsing
# ────────────────────────────────────────────────────────────────────────────


def _approx_tokens(s: str | list | dict) -> int:
    if isinstance(s, (list, dict)):
        s = json.dumps(s)
    elif not isinstance(s, str):
        s = str(s)
    return max(1, len(s) // 4)


def _tool_input_label(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    if name == "Read":
        return Path(inp.get("file_path", "")).name
    if name == "Bash":
        cmd = inp.get("command", "")
        return (cmd[:60] + "…") if len(cmd) > 60 else cmd
    if name == "Edit" or name == "Write":
        return Path(inp.get("file_path", "")).name
    if name == "Grep":
        return f'pattern={inp.get("pattern","")!r}'
    if name == "Glob":
        return inp.get("pattern", "")
    if name == "Agent":
        return inp.get("subagent_type") or inp.get("description", "")
    if name == "Skill":
        return inp.get("skill", "")
    # fallback: first short stringy value
    for v in inp.values():
        if isinstance(v, str) and 0 < len(v) <= 80:
            return v
    return ""


def detect_session_source(path: Path) -> str:
    try:
        with path.open() as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("type") == "session_meta" and isinstance(row.get("payload"), dict):
                    return "codex"
                if row.get("type") in {"user", "assistant"} and "message" in row:
                    return "claude"
                break
    except Exception:
        pass
    if CODEX_SESSIONS in path.expanduser().resolve().parents:
        return "codex"
    return "claude"


def parse_session(path: Path) -> Session:
    if detect_session_source(path) == "codex":
        return parse_codex_session(path)
    return parse_claude_session(path)


def parse_claude_session(path: Path) -> Session:
    """Parse a session JSONL and any nested subagent JSONLs."""
    with path.open() as fh:
        rows = [json.loads(line) for line in fh]

    session_id = path.stem
    agent_type = None
    command = None
    first_user_prompt = ""
    cwd = ""

    # If this is a subagent JSONL, its meta.json sits next to it.
    meta_path = path.with_suffix(".meta.json")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        agent_type = meta.get("agentType")

    # Walk rows once. Track:
    #   - turns by requestId (one model call = one turn)
    #   - tool_use blocks (assistant messages)
    #   - tool_results (user messages) by tool_use_id → size
    turns_by_req: dict[str, Turn] = {}
    tool_use_to_turn: dict[str, ToolCall] = {}
    tool_results: dict[str, tuple[int, str, str, int]] = {}   # tool_use_id → (result_tokens, content, preview, full_chars)
    pending_user_messages: list[tuple[str, str]] = []
    PREVIEW_CHARS = 3000

    def capture_user_message(text: str, ts: str = ""):
        nonlocal first_user_prompt, command
        if not text.strip():
            return
        if not first_user_prompt:
            m = re.search(r"<command-name>([^<]+)</command-name>", text)
            if m:
                command = m.group(1).strip()
            first_user_prompt = text[:200]
        pending_user_messages.append((ts, text))

    def ensure_claude_turn(rid: str, timestamp: str, usage: dict, model: str | None) -> Turn:
        turn = turns_by_req.get(rid)
        if turn is None:
            turn = Turn(
                request_id=rid,
                timestamp=timestamp,
                input_tokens=usage.get("input_tokens", 0) or 0,
                cache_creation=usage.get("cache_creation_input_tokens", 0) or 0,
                cache_read=usage.get("cache_read_input_tokens", 0) or 0,
                output_tokens=usage.get("output_tokens", 0) or 0,
                model=model,
            )
            for idx, (user_ts, text) in enumerate(pending_user_messages):
                turn.events.append(UserMessage(
                    id=f"{rid}:user:{idx}",
                    text=text,
                    timestamp=user_ts,
                ))
            pending_user_messages.clear()
            turns_by_req[rid] = turn
        return turn

    for r in rows:
        rtype = r.get("type")
        msg = r.get("message", {}) or {}
        content = msg.get("content")
        if not cwd and r.get("cwd"):
            cwd = r["cwd"]

        if rtype == "user":
            # Capture first slash-command or first user prompt.
            if isinstance(content, str):
                capture_user_message(content, r.get("timestamp", ""))
            elif isinstance(content, list):
                for c in content:
                    if c.get("type") == "tool_result":
                        body = c.get("content", "")
                        body_str = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                        # tool_result content can also be a list of content blocks; flatten text
                        if isinstance(body, list):
                            parts = []
                            for blk in body:
                                if isinstance(blk, dict) and blk.get("type") == "text":
                                    parts.append(blk.get("text", ""))
                                else:
                                    parts.append(json.dumps(blk, ensure_ascii=False))
                            body_str = "\n".join(parts)
                        tool_results[c.get("tool_use_id", "")] = (
                            _approx_tokens(body_str),
                            body_str,
                            body_str[:PREVIEW_CHARS],
                            len(body_str),
                        )
                    elif c.get("type") == "text":
                        capture_user_message(c.get("text") or "", r.get("timestamp", ""))

        elif rtype == "assistant":
            rid = r.get("requestId")
            if not rid:
                continue
            usage = msg.get("usage", {}) or {}
            turn = ensure_claude_turn(rid, r.get("timestamp", ""), usage, msg.get("model"))
            if isinstance(content, list):
                # Track per-turn counters for synthesizing stable IDs.
                txt_idx = sum(1 for e in turn.events if isinstance(e, TextBlock))
                think_idx = sum(1 for e in turn.events if isinstance(e, ThinkingBlock))
                for c in content:
                    ctype = c.get("type")
                    if ctype == "tool_use":
                        tc = ToolCall(
                            name=c.get("name", "?"),
                            input_label=_tool_input_label(c.get("name", ""), c.get("input", {}) or {}),
                            result_tokens=0,  # filled below
                            tool_use_id=c.get("id", ""),
                            input_full=c.get("input", {}) or {},
                            timestamp=r.get("timestamp", ""),
                        )
                        turn.events.append(tc)
                        tool_use_to_turn[tc.tool_use_id] = tc
                    elif ctype == "text":
                        txt = c.get("text") or ""
                        if txt.strip():
                            turn.events.append(TextBlock(
                                id=f"{rid}:txt:{txt_idx}",
                                text=txt,
                            ))
                            txt_idx += 1
                    elif ctype == "thinking":
                        thinking_text = c.get("thinking") or ""
                        turn.events.append(ThinkingBlock(
                            id=f"{rid}:think:{think_idx}",
                            chars=len(thinking_text),
                        ))
                        think_idx += 1

    # Stamp tool_result sizes onto matching tool calls.
    for tuid, (sz, content, preview, full_chars) in tool_results.items():
        tc = tool_use_to_turn.get(tuid)
        if tc is not None:
            tc.result_tokens = sz
            tc.result_content = content
            tc.result_preview = preview
            tc.result_full_chars = full_chars

    # Order turns by timestamp.
    turns = sorted(turns_by_req.values(), key=lambda t: t.timestamp)

    sess = Session(
        path=path,
        session_id=session_id,
        agent_type=agent_type,
        command=command,
        first_user_prompt=first_user_prompt,
        cwd=cwd,
        turns=turns,
        source="claude",
    )

    # Link subagents: each Agent tool_use should have a corresponding JSONL in
    # <session-dir>/subagents/. We match by order of appearance.
    sub_dir = path.parent / session_id / "subagents"
    if sub_dir.exists():
        # Read meta.json for each agent file to know its agentType.
        sub_files: list[Path] = sorted(sub_dir.glob("agent-*.jsonl"))
        # Build queue of Agent tool calls in this session, in temporal order.
        agent_calls: list[ToolCall] = [
            tc for turn in turns for tc in turn.tool_calls if tc.name == "Agent"
        ]
        # Match: subagent files are listed alphabetically; we re-sort by mtime
        # to match temporal order more reliably.
        sub_files.sort(key=lambda p: p.stat().st_mtime)
        for tc, sub_path in zip(agent_calls, sub_files):
            try:
                tc.child_session = parse_claude_session(sub_path)
            except Exception as e:
                print(f"warning: failed to parse subagent {sub_path}: {e}", file=sys.stderr)

    return sess


def _coerce_codex_arguments(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return {"arguments": raw}
    return {"arguments": raw}


def _codex_message_text(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text") or block.get("output_text")
            if text:
                parts.append(str(text))
        return "\n".join(parts)
    message = payload.get("message")
    if isinstance(message, str):
        return message
    return ""


def _is_codex_bootstrap_user_message(text: str) -> bool:
    stripped = text.lstrip()
    return (
        stripped.startswith("# AGENTS.md instructions")
        or stripped.startswith("<environment_context>")
    )


def _codex_output_text(payload: dict) -> str:
    output = payload.get("output", "")
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False)


def parse_codex_session(path: Path) -> Session:
    """Parse a Codex rollout JSONL into the common trace model.

    Codex emits a stream of response items and separate token_count events.
    Each token_count's last_token_usage is treated as the usage for the turn
    that just completed.
    """
    with path.open() as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    session_id = path.stem
    if session_id.startswith("rollout-"):
        session_id = session_id.rsplit("-", 5)[-5] + "-" + session_id.rsplit("-", 5)[-4] + "-" + session_id.rsplit("-", 5)[-3] + "-" + session_id.rsplit("-", 5)[-2] + "-" + session_id.rsplit("-", 5)[-1]

    cwd = ""
    model: str | None = None
    first_user_prompt = ""
    start_timestamp = ""
    command = None
    current_turn_id = ""
    current_timestamp = ""
    current_events: list[Event] = []
    turns: list[Turn] = []
    tool_use_to_call: dict[str, ToolCall] = {}
    user_idx = 0
    txt_idx = 0
    think_idx = 0
    PREVIEW_CHARS = 3000

    def ensure_turn_id(ts: str = "") -> str:
        nonlocal current_turn_id, current_timestamp
        if not current_turn_id:
            current_turn_id = f"{session_id}:turn:{len(turns) + 1}"
        if not current_timestamp:
            current_timestamp = ts or start_timestamp
        return current_turn_id

    def append_user_message(text: str, ts: str = ""):
        nonlocal first_user_prompt, command, user_idx
        if not text or _is_codex_bootstrap_user_message(text):
            return
        if current_events and isinstance(current_events[-1], UserMessage) and current_events[-1].text == text:
            return
        if not first_user_prompt:
            first_user_prompt = text[:200]
            m = re.search(r"<command-name>([^<]+)</command-name>", text)
            if m:
                command = m.group(1).strip()
        rid = ensure_turn_id(ts)
        current_events.append(UserMessage(
            id=f"{rid}:user:{user_idx}",
            text=text,
            timestamp=ts,
        ))
        user_idx += 1

    def finish_turn(usage: dict | None, ts: str = ""):
        nonlocal current_turn_id, current_timestamp, current_events, user_idx, txt_idx, think_idx
        if not current_events and not usage:
            return
        rid = ensure_turn_id(ts)
        last = usage or {}
        input_tokens = int(last.get("input_tokens", 0) or 0)
        cache_read = int(last.get("cached_input_tokens", 0) or 0)
        output_tokens = int(last.get("output_tokens", 0) or 0)
        turns.append(Turn(
            request_id=rid,
            timestamp=current_timestamp or ts or start_timestamp,
            input_tokens=max(0, input_tokens - cache_read),
            cache_read=cache_read,
            output_tokens=output_tokens,
            model=model,
            events=current_events,
        ))
        current_turn_id = ""
        current_timestamp = ""
        current_events = []
        user_idx = 0
        txt_idx = 0
        think_idx = 0

    for row in rows:
        rtype = row.get("type")
        ts = row.get("timestamp", "")
        payload = row.get("payload", {}) or {}

        if rtype == "session_meta":
            meta = payload
            session_id = meta.get("id") or session_id
            cwd = meta.get("cwd", "") or cwd
            model = meta.get("model") or meta.get("model_provider") or model
            start_timestamp = meta.get("timestamp") or ts or start_timestamp
            continue

        if rtype == "turn_context":
            current_turn_id = payload.get("turn_id") or current_turn_id
            cwd = payload.get("cwd", "") or cwd
            model = payload.get("model") or model
            current_timestamp = ts or current_timestamp
            continue

        if rtype == "response_item":
            ptype = payload.get("type")
            if ptype == "message":
                role = payload.get("role")
                text = _codex_message_text(payload)
                if role == "user":
                    append_user_message(text, ts)
                elif role == "assistant" and text.strip():
                    rid = ensure_turn_id(ts)
                    current_events.append(TextBlock(id=f"{rid}:txt:{txt_idx}", text=text))
                    txt_idx += 1
            elif ptype == "reasoning":
                content = payload.get("content")
                if content is None:
                    content = payload.get("encrypted_content") or payload.get("summary") or ""
                chars = len(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
                if chars:
                    rid = ensure_turn_id(ts)
                    current_events.append(ThinkingBlock(id=f"{rid}:think:{think_idx}", chars=chars))
                    think_idx += 1
            elif ptype == "function_call":
                name = payload.get("name", "?")
                inp = _coerce_codex_arguments(payload.get("arguments", {}))
                call_id = payload.get("call_id") or payload.get("id") or f"call-{len(tool_use_to_call) + 1}"
                tc = ToolCall(
                    name=name,
                    input_label=_tool_input_label(name, inp),
                    result_tokens=0,
                    tool_use_id=call_id,
                    input_full=inp,
                    timestamp=ts,
                )
                ensure_turn_id(ts)
                current_events.append(tc)
                tool_use_to_call[call_id] = tc
            elif ptype == "function_call_output":
                call_id = payload.get("call_id") or payload.get("id") or ""
                body = _codex_output_text(payload)
                tc = tool_use_to_call.get(call_id)
                if tc is not None:
                    tc.result_tokens = _approx_tokens(body)
                    tc.result_content = body
                    tc.result_preview = body[:PREVIEW_CHARS]
                    tc.result_full_chars = len(body)
            continue

        if rtype == "event_msg":
            etype = payload.get("type")
            if etype == "user_message":
                text = str(payload.get("message", ""))
                append_user_message(text, ts)
            elif etype == "token_count":
                usage = ((payload.get("info") or {}).get("last_token_usage") or {})
                finish_turn(usage, ts)

    finish_turn(None, rows[-1].get("timestamp", "") if rows else "")

    return Session(
        path=path,
        session_id=session_id,
        agent_type=None,
        command=command,
        first_user_prompt=first_user_prompt,
        cwd=cwd,
        turns=turns,
        source="codex",
    )


def resolve_input(arg: str, source: str = "auto") -> Path:
    """Accept a file path, a full session id, or an 8-char (or longer) prefix.
    Searches ~/.claude/projects."""
    p = Path(arg).expanduser()
    if p.is_file():
        return p
    roots = []
    if source in ("auto", "claude"):
        roots.append(CLAUDE_PROJECTS)
    if source in ("auto", "codex"):
        roots.append(CODEX_SESSIONS)
    candidates = []
    for root in roots:
        if not root.is_dir():
            continue
        candidates.extend(root.rglob(f"{arg}.jsonl"))
    if not candidates:
        for root in roots:
            if not root.is_dir():
                continue
            candidates.extend(root.rglob(f"*{arg}*.jsonl"))
    candidates = [m for m in candidates if "subagents" not in m.parts]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise SystemExit(f"ambiguous session id {arg!r}, matches:\n  "
                         + "\n  ".join(map(str, candidates)))
    raise SystemExit(f"could not find session for {arg!r}")


# ────────────────────────────────────────────────────────────────────────────
# Optimization-opportunity hints for MCP summaries
# ────────────────────────────────────────────────────────────────────────────


def _fmt_num(n: float | int) -> str:
    return f"{n:,.0f}" if isinstance(n, float) else f"{n:,}"


def _build_hints(root: Session) -> str:
    """Surface a handful of likely-actionable optimization findings."""
    hints: list[str] = []

    sessions = list(root.all_sessions())
    total_cost = root.total_cost

    # 1. Biggest subagent
    subagents = [s for s in sessions if s.agent_type]
    if subagents:
        biggest = max(subagents, key=lambda s: s.total_cost)
        share = 100 * biggest.total_cost / total_cost if total_cost else 0
        if share > 30:
            hints.append(
                f"Subagent <code>{biggest.label}</code> consumes "
                f"<b>{share:.0f}%</b> of total cost ({_fmt_num(biggest.total_cost)} weighted). "
                f"This is the highest-leverage place to optimize."
            )

    # 2. O(n²) re-read growth
    for s in sessions:
        if len(s.turns) >= 10:
            first_cr = s.turns[0].cache_read
            last_cr = s.turns[-1].cache_read
            if last_cr > 4 * max(first_cr, 1):
                hints.append(
                    f"In <code>{s.label}</code>, cache_read grew from "
                    f"{_fmt_num(first_cr)} → {_fmt_num(last_cr)} tokens across "
                    f"{len(s.turns)} turns (≈{(last_cr-first_cr)*len(s.turns)//2:,} extra "
                    f"context-tokens paid for the growth alone). "
                    f"Consider batching tool calls or splitting the work to shrink the loop."
                )
                break

    # 3. Many tiny-output turns (under-batched tool calls)
    for s in sessions:
        if len(s.turns) >= 8:
            tiny = sum(1 for t in s.turns if t.output_tokens < 10)
            if tiny / len(s.turns) > 0.5:
                hints.append(
                    f"<code>{s.label}</code> has {tiny}/{len(s.turns)} turns with "
                    f"under 10 output tokens — suggests serial tool calls that could be parallelized."
                )
                break

    # 4. Big tool results
    big_results: list[tuple[Session, Turn, ToolCall]] = []
    for s in sessions:
        for t in s.turns:
            for tc in t.tool_calls:
                if tc.result_tokens > 1500 and tc.child_session is None:
                    big_results.append((s, t, tc))
    big_results.sort(key=lambda x: -x[2].result_tokens)
    if big_results:
        items = []
        for s, _, tc in big_results[:3]:
            items.append(
                f"<li><code>{tc.name}({tc.input_label})</code> in "
                f"<code>{s.label}</code> → {_fmt_num(tc.result_tokens)} tok "
                f"(persists in every subsequent cache_read for this session)</li>"
            )
        hints.append(
            "Largest single tool results — these are read repeatedly:<ul>"
            + "".join(items) + "</ul>"
        )

    # 5. System prompt baseline (first-turn cache_creation)
    for s in sessions:
        if s.turns:
            base = s.turns[0].cache_creation
            if base > 10000:
                hints.append(
                    f"<code>{s.label}</code> opens with a <b>{_fmt_num(base)}</b>-token "
                    f"system prompt baseline (cache_creation on turn 1). "
                    f"That's re-read on every subsequent turn at 10% cost. "
                    f"Trim the agent's system prompt / skill descriptions / tool list to cut this."
                )

    if not hints:
        return ""
    return "<ul>" + "".join(f"<li>{h}</li>" for h in hints) + "</ul>"


def _ascii_lines(sess: Session, prefix: str = "", connector: str = "", is_root: bool = False) -> list[str]:
    out: list[str] = []
    out.append(
        f"{prefix}{connector}● {sess.label}  "
        f"[cost {_fmt_num(sess.total_cost)} · {len(sess.turns)} turns]"
    )
    child_prefix = prefix + ("" if is_root else ("    " if connector.startswith("└") else "│   "))
    n = len(sess.turns)
    for ti, turn in enumerate(sess.turns):
        t_last = ti == n - 1
        t_conn = "└── " if t_last else "├── "
        t_next_prefix = child_prefix + ("    " if t_last else "│   ")
        out.append(
            f"{child_prefix}{t_conn}◇ turn {ti+1}  "
            f"[cost {_fmt_num(turn.cost)} · in {_fmt_num(turn.billable_input)} · "
            f"out {_fmt_num(turn.output_tokens)} · {len(turn.tool_calls)} tools]"
        )
        m = len(turn.tool_calls)
        for ci, tc in enumerate(turn.tool_calls):
            c_last = ci == m - 1
            c_conn = "└── " if c_last else "├── "
            if tc.child_session:
                out.append(f"{t_next_prefix}{c_conn}▶ Agent")
                sub_prefix = t_next_prefix + ("    " if c_last else "│   ")
                out.extend(_ascii_lines(tc.child_session, sub_prefix, "└── ", is_root=False))
            else:
                label = tc.input_label[:55].replace("\n", " ")
                out.append(
                    f"{t_next_prefix}{c_conn}• {tc.name}({label})  ~{_fmt_num(tc.result_tokens)} tok"
                )
    return out


def emit_ascii(root: Session, out_path: Path):
    out_path.write_text("\n".join(_ascii_lines(root, is_root=True)) + "\n")


# ────────────────────────────────────────────────────────────────────────────
# Trace (chronological timeline) HTML emitter
# ────────────────────────────────────────────────────────────────────────────


import html as _html


def _esc(s: str) -> str:
    return _html.escape(s or "", quote=False)


def _attr(s: str) -> str:
    return _html.escape(s or "", quote=True)


def _rel_seconds(ts: str, origin: datetime | None) -> str:
    if not ts or origin is None:
        return ""
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        delta = (t - origin).total_seconds()
        if delta < 60:
            return f"+{delta:5.1f}s"
        m, s = divmod(int(delta), 60)
        if m < 60:
            return f"+{m:>2}m{s:02d}s"
        h, m = divmod(m, 60)
        return f"+{h}h{m:02d}m"
    except Exception:
        return ""


def _input_pretty(name: str, inp: dict) -> str:
    """Render tool input details for the trace expansion panel."""
    if not isinstance(inp, dict) or not inp:
        return ""
    if name == "Bash":
        return _esc(inp.get("command", ""))
    if name in ("Read", "Edit", "Write"):
        parts = [f"file: {inp.get('file_path','')}"]
        if name == "Edit":
            parts.append(f"\n--- old ---\n{inp.get('old_string','')[:600]}")
            parts.append(f"\n--- new ---\n{inp.get('new_string','')[:600]}")
        elif name == "Write":
            parts.append(f"\n--- content ({len(inp.get('content',''))} chars) ---\n{inp.get('content','')[:600]}")
        return _esc("\n".join(parts))
    if name == "Grep":
        return _esc(json.dumps(inp, indent=2))
    if name == "Agent":
        return _esc(
            f"subagent_type: {inp.get('subagent_type','')}\n"
            f"description: {inp.get('description','')}\n\n"
            f"prompt:\n{inp.get('prompt','')[:2000]}"
        )
    return _esc(json.dumps(inp, indent=2, ensure_ascii=False)[:2000])


TRACE_HTML_HEAD = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Trace — __NAME__</title>
<style>
  :root {
    --bg: #fafaf8;
    --row-hover: #f0f0ea;
    --text: #222;
    --muted: #888;
    --rule: #e3e3dc;
    --user: #2b6cb0;
    --assistant: #4a5568;
    --tool: #2f855a;
    --result: #38a169;
    --agent: #c05621;
    --skill: #6b46c1;
    --warn: #d69e2e;
  }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--text);
    font: 13px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro", sans-serif;
    margin: 0; padding: 18px 22px; }
  h1 { font-size: 17px; margin: 0 0 8px; }
  .topbar { background: #fff; border: 1px solid var(--rule); border-radius: 6px;
    padding: 10px 14px; margin-bottom: 14px; }
  .topbar span { display: inline-block; margin-right: 18px; }
  .num { font-variant-numeric: tabular-nums; }
  .controls { margin: 8px 0; }
  .controls button { font: inherit; padding: 4px 10px; margin-right: 6px;
    border: 1px solid var(--rule); background: #fff; border-radius: 4px; cursor: pointer; }
  .controls button:hover { background: var(--row-hover); }
  .controls label { margin-right: 14px; font-size: 12px; color: var(--muted); cursor: pointer; }
  .result-open { font: inherit; font-size: 12px; color: #2c5282; padding: 0;
    border: 0; background: transparent; cursor: pointer; }
  .result-open:hover { text-decoration: underline; }
  dialog { width: min(1100px, calc(100vw - 48px)); height: min(760px, calc(100vh - 48px));
    border: 1px solid var(--rule); border-radius: 6px; padding: 0; }
  dialog::backdrop { background: rgba(0, 0, 0, 0.32); }
  .modal-head { display: flex; justify-content: space-between; align-items: center;
    gap: 16px; padding: 10px 12px; border-bottom: 1px solid var(--rule); background: #fff; }
  .modal-head strong { font-size: 13px; }
  .modal-head button { font: inherit; padding: 3px 8px; border: 1px solid var(--rule);
    background: #fff; border-radius: 4px; cursor: pointer; }
  #result-modal pre { height: calc(100% - 43px); margin: 0; padding: 12px;
    overflow: auto; background: #f6f5ef; font: 12px/1.45 "SF Mono", Menlo, monospace;
    white-space: pre-wrap; }
  .trace { background: #fff; border: 1px solid var(--rule); border-radius: 6px; }
  .row { display: grid;
    grid-template-columns: 70px 18px 1fr 230px;
    gap: 8px; padding: 4px 12px; border-bottom: 1px solid var(--rule);
    position: relative; }
  .row:hover { background: var(--row-hover); }
  .row:last-child { border-bottom: 0; }
  .time { font: 11px/1.4 "SF Mono", Menlo, monospace; color: var(--muted); padding-top: 1px; }
  .icon { font-weight: 700; text-align: center; padding-top: 0; }
  .body { min-width: 0; word-break: break-word; }
  .meta { font: 11px/1.4 "SF Mono", Menlo, monospace; color: var(--muted);
    text-align: right; padding-top: 1px; }
  .label { font: 13px/1.4 "SF Mono", Menlo, monospace; }
  .label-name { font-weight: 600; }
  details { margin: 4px 0 0 0; }
  details > summary { cursor: pointer; color: #2c5282; font-size: 12px; }
  details > summary::-webkit-details-marker { display: none; }
  details > summary::before { content: "▸ "; }
  details[open] > summary::before { content: "▾ "; }
  details pre { background: #f6f5ef; border: 1px solid var(--rule); border-radius: 4px;
    padding: 8px 10px; margin: 4px 0 0 0; max-height: 360px; overflow: auto;
    font: 12px/1.45 "SF Mono", Menlo, monospace; white-space: pre-wrap; }
  /* Depth bars on the left, one column per nesting level */
  .row.d1 { padding-left: 28px; }
  .row.d2 { padding-left: 44px; }
  .row.d3 { padding-left: 60px; }
  .row.d4 { padding-left: 76px; }
  .row::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0;
    border-left: 0 solid transparent; }
  .row.d1::before { left: 14px; border-left: 2px solid #e7eef8; }
  .row.d2::before { left: 30px; border-left: 2px solid #e7eef8; }
  .row.d3::before { left: 46px; border-left: 2px solid #e7eef8; }
  .row.d4::before { left: 62px; border-left: 2px solid #e7eef8; }
  /* Color stripes by type */
  .row.t-user .icon { color: var(--user); }
  .row.t-assistant .icon { color: var(--assistant); }
  .row.t-tool .icon { color: var(--tool); }
  .row.t-agent .icon { color: var(--agent); }
  .row.t-agent { background: #fffaf0; }
  .row.t-agent-end { background: #fffaf0; border-top: 1px dashed var(--agent); }
  .row.t-divider { background: #fcfcf8; }
  .row.t-divider .label { color: var(--muted); font-size: 11px; }
  .turn-marker { font-size: 11px; color: var(--muted); font-family: "SF Mono", Menlo, monospace; }
  .hidden { display: none; }
  .clip-banner { background: #fff7d6; border: 1px solid #f0c400; border-radius: 4px;
    padding: 8px 12px; margin: 0 0 12px 0; font-size: 12px; color: #604c00; }
  .row.t-clipped { opacity: 0.35; }
  .row.t-clipped:hover { opacity: 0.7; }
</style>
</head><body>
<h1>Trace — <code>__NAME__</code></h1>
<div class="topbar">
  <span><b>cost-weighted:</b> <span class="num">__TOTAL_COST__</span></span>
  <span><b>dollars:</b> <span class="num">__TOTAL_DOLLARS__</span></span>
  <span><b>model:</b> <span class="num">__MODEL_MIX__</span></span>
  <span><b>sessions:</b> __N_SESSIONS__</span>
  <span><b>turns:</b> __N_TURNS__</span>
  <span><b>wall:</b> __WALL__</span>
</div>
<div class="controls">
  <button onclick="document.querySelectorAll('.trace details').forEach(d=>d.open=true)">Expand all</button>
  <button onclick="document.querySelectorAll('.trace details').forEach(d=>d.open=false)">Collapse all</button>
  <label><input type="checkbox" id="filt-text" checked onchange="toggleType('assistant', this.checked)"> assistant text</label>
  <label><input type="checkbox" id="filt-tool" checked onchange="toggleType('tool', this.checked)"> tool calls</label>
  <label><input type="checkbox" id="filt-divider" checked onchange="toggleType('divider', this.checked)"> turn markers</label>
</div>
__CLIP_BANNER__
__FINDINGS__
<div class="trace">
__ROWS__
</div>
<dialog id="result-modal">
  <div class="modal-head">
    <strong id="result-modal-title">Tool result</strong>
    <button onclick="document.getElementById('result-modal').close()">Close</button>
  </div>
  <pre id="result-modal-body"></pre>
</dialog>
<script>
function toggleType(t, on) {
  document.querySelectorAll('.row.t-'+t).forEach(r => r.classList.toggle('hidden', !on));
}
function openResult(button) {
  const dialog = document.getElementById('result-modal');
  const title = document.getElementById('result-modal-title');
  const body = document.getElementById('result-modal-body');
  const template = button.nextElementSibling;
  title.textContent = button.dataset.title || 'Tool result';
  body.textContent = template ? template.content.textContent : '';
  dialog.showModal();
}
</script>
</body></html>
"""


def _emit_row(time_str: str, icon: str, body_html: str, meta_html: str, depth: int, type_class: str) -> str:
    d_class = f"d{depth}" if depth else ""
    return (
        f'<div class="row {type_class} {d_class}">'
        f'<div class="time">{time_str}</div>'
        f'<div class="icon">{icon}</div>'
        f'<div class="body">{body_html}</div>'
        f'<div class="meta">{meta_html}</div>'
        f'</div>'
    )


def _short_num(n: float | int) -> str:
    n = float(n)
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"{n/1_000:.1f}k"
    return f"{n:.0f}"


def _expanded_turn_totals(turn: Turn) -> tuple[int, int, float]:
    """Tokens billed for this turn + any subagents spawned in it."""
    in_tok = turn.billable_input
    out_tok = turn.output_tokens
    cost = turn.cost
    for tc in turn.tool_calls:
        if tc.child_session is not None:
            in_tok += tc.child_session.total_billable_input
            out_tok += tc.child_session.total_output
            cost += tc.child_session.total_cost
    return in_tok, out_tok, cost


def _text_followup_costs(sess: Session) -> dict[tuple[int, int], tuple[int, int, float]]:
    """For each (turn_idx, text_idx) in `sess`, return (in, out, cost) summed over
    every turn that runs after this message and up to and including the turn that
    produces the next assistant message in the same session. Costs include any
    spawned subagents. For the last text, follow-up = all remaining turns."""
    events: list[tuple[int, int]] = []
    for ti, turn in enumerate(sess.turns):
        for tx_idx in range(len(turn.text_blocks)):
            events.append((ti, tx_idx))

    result: dict[tuple[int, int], tuple[int, int, float]] = {}
    for i, (ti, tx_idx) in enumerate(events):
        if i + 1 < len(events):
            # Include the turn that produces the next text (its compute happens
            # between this message and the next message).
            end_exclusive = events[i + 1][0] + 1
        else:
            end_exclusive = len(sess.turns)
        start = ti + 1
        if end_exclusive <= start:
            result[(ti, tx_idx)] = (0, 0, 0.0)
            continue
        in_tok = out_tok = 0
        cost = 0.0
        for tt in sess.turns[start:end_exclusive]:
            i2, o2, c2 = _expanded_turn_totals(tt)
            in_tok += i2
            out_tok += o2
            cost += c2
        result[(ti, tx_idx)] = (in_tok, out_tok, cost)
    return result


def _trace_rows(sess: Session, depth: int, origin: datetime | None) -> list[str]:
    rows: list[str] = []
    followups = _text_followup_costs(sess)
    clipped_prefix, clipped_suffix = sess.clipped_turn_indices
    for ti, turn in enumerate(sess.turns):
        is_clipped = (ti in clipped_prefix) or (ti in clipped_suffix)
        clip_class = " t-clipped" if is_clipped else ""
        # Per-turn meta: model + dollars
        turn_meta_bits: list[str] = []
        if turn.model:
            turn_meta_bits.append(_shorten_model(turn.model))
        td = turn.dollars
        if td is not None:
            turn_meta_bits.append(f"${td:.4f}" if td < 0.01 else f"${td:.3f}")
        turn_meta = " · ".join(turn_meta_bits)
        # Turn divider
        rows.append(_emit_row(
            _rel_seconds(turn.timestamp, origin),
            "·",
            f'<span class="turn-marker">── turn {ti+1}'
            + (' [CLIPPED]' if is_clipped else '')
            + f' (in {turn.billable_input:,} · out {turn.output_tokens:,} · '
            f'cost {turn.cost:,.0f}) ──</span>',
            f'<span class="turn-marker">{turn_meta}</span>' if turn_meta else "",
            depth,
            "t-divider" + clip_class,
        ))

        # User messages that prompted this turn.
        for e in turn.events:
            if not isinstance(e, UserMessage):
                continue
            preview = e.text.strip().replace("\n", " ")
            short = preview[:200] + ("…" if len(preview) > 200 else "")
            body = f'<span class="label"><span class="label-name">user</span> {_esc(short)}</span>'
            if len(e.text) > 200:
                body += (
                    f'<details><summary>full user message ({len(e.text):,} chars)</summary>'
                    f'<pre>{_esc(e.text)}</pre></details>'
                )
            rows.append(_emit_row(
                _rel_seconds(e.timestamp or turn.timestamp, origin),
                "▸",
                body,
                "",
                depth,
                "t-user" + clip_class,
            ))

        # Assistant text blocks for this turn (model output)
        for tx_idx, txt in enumerate(turn.text_blocks):
            preview = txt.strip().replace("\n", " ")
            short = preview[:200] + ("…" if len(preview) > 200 else "")
            body = f'<span class="label">{_esc(short)}</span>'
            if len(txt) > 200:
                body += (
                    f'<details><summary>full text ({len(txt):,} chars)</summary>'
                    f'<pre>{_esc(txt)}</pre></details>'
                )
            f_in, f_out, f_cost = followups.get((ti, tx_idx), (0, 0, 0.0))
            if f_cost > 0:
                meta = (
                    f'<span title="Sum of all turns (and their subagents) between '
                    f'this assistant message and the next one in this session.">'
                    f'↓ {_short_num(f_cost)} cost · {_short_num(f_in)} in / '
                    f'{_short_num(f_out)} out</span>'
                )
            else:
                meta = '<span style="color:#bbb" title="no follow-up turns before next assistant message">↓ 0</span>'
            rows.append(_emit_row(
                _rel_seconds(turn.timestamp, origin),
                "≡",
                body,
                meta,
                depth,
                "t-assistant" + clip_class,
            ))

        # Tool calls
        for tc in turn.tool_calls:
            ts = _rel_seconds(tc.timestamp or turn.timestamp, origin)
            if tc.child_session is not None:
                # Agent boundary: open, recurse, close
                sub = tc.child_session
                rows.append(_emit_row(
                    ts,
                    "▶",
                    f'<span class="label"><span class="label-name">▶ Agent</span> '
                    f'<span style="color:var(--muted)">→ {_esc(sub.label)}</span></span>'
                    + (f'<details><summary>spawn prompt</summary>'
                       f'<pre>{_input_pretty("Agent", tc.input_full)}</pre></details>'
                       if tc.input_full else ""),
                    f"{sub.total_cost:,.0f} cost · {len(sub.turns)} turns",
                    depth,
                    "t-agent" + clip_class,
                ))
                rows.extend(_trace_rows(sub, depth + 1, origin))
                return_body = (
                    f'<span class="label"><span class="label-name">◀ Agent returned</span> '
                    f'<span style="color:var(--muted)">({_esc(sub.label)})</span></span>'
                )
                if tc.result_preview:
                    truncated_note = (
                        f" (showing first {len(tc.result_preview):,} of {tc.result_full_chars:,} chars)"
                        if tc.result_full_chars > len(tc.result_preview) else ""
                    )
                    return_body += (
                        f'<details><summary>result ~{tc.result_tokens:,} tok{truncated_note}</summary>'
                        f'<pre>{_esc(tc.result_preview)}</pre></details>'
                    )
                    if tc.result_content and tc.result_full_chars > len(tc.result_preview):
                        return_body += (
                            f'<button class="result-open" data-title="Agent result '
                            f'({tc.result_full_chars:,} chars)" onclick="openResult(this)">'
                            f'Show full result</button>'
                            f'<template>{_esc(tc.result_content)}</template>'
                        )
                rows.append(_emit_row(
                    "",
                    "◀",
                    return_body,
                    f"result ~{tc.result_tokens:,} tok",
                    depth,
                    "t-agent-end" + clip_class,
                ))
            else:
                # Plain tool call
                inp_pretty = _input_pretty(tc.name, tc.input_full)
                body = (
                    f'<span class="label">'
                    f'<span class="label-name">{_esc(tc.name)}</span>'
                    f'(<span style="color:#555">{_esc(tc.input_label)}</span>)'
                    f'</span>'
                )
                if inp_pretty:
                    body += (
                        f'<details><summary>input</summary>'
                        f'<pre>{inp_pretty}</pre></details>'
                    )
                if tc.result_preview:
                    truncated_note = (
                        f" (showing first {len(tc.result_preview):,} of {tc.result_full_chars:,} chars)"
                        if tc.result_full_chars > len(tc.result_preview) else ""
                    )
                    body += (
                        f'<details><summary>result ~{tc.result_tokens:,} tok{truncated_note}</summary>'
                        f'<pre>{_esc(tc.result_preview)}</pre></details>'
                    )
                    if tc.result_content and tc.result_full_chars > len(tc.result_preview):
                        body += (
                            f'<button class="result-open" data-title="{_attr(tc.name)} result '
                            f'({tc.result_full_chars:,} chars)" onclick="openResult(this)">'
                            f'Show full result</button>'
                            f'<template>{_esc(tc.result_content)}</template>'
                        )
                rows.append(_emit_row(
                    ts,
                    "●",
                    body,
                    f"~{tc.result_tokens:,} tok",
                    depth,
                    "t-tool" + clip_class,
                ))
    return rows


def _clip_banner_html(root: Session) -> str:
    if root.clip is None:
        return ""
    n = len(root.turns)
    kept = len(root.included_turns)
    start = root.clip.start_turn or 1
    end = root.clip.end_turn or n
    bits = [
        f"<b>legacy soft clip active</b>: showing turns <b>{start}..{end}</b> ({kept}/{n} turns); "
        f"clipped turns are visually grayed and excluded from totals."
    ]
    if root.clip.reason:
        bits.append(f"reason: {_esc(root.clip.reason)}")
    if root.clip.matched_pattern:
        bits.append(f"matched: <code>{_esc(root.clip.matched_pattern)}</code>")
    return ('<div class="clip-banner">' + " — ".join(bits) + "</div>")


def emit_trace_html(root: Session, out_path: Path):
    # Origin = first turn timestamp of root
    origin = None
    if root.turns:
        try:
            origin = datetime.fromisoformat(root.turns[0].timestamp.replace("Z", "+00:00"))
        except Exception:
            pass

    rows = []
    # Lead row: the user prompt that started the session
    has_user_events = any(
        isinstance(e, UserMessage)
        for turn in root.turns
        for e in turn.events
    )
    if root.first_user_prompt and not has_user_events:
        rows.append(_emit_row(
            "+0.0s",
            "▸",
            f'<span class="label"><span class="label-name">user</span> '
            f'{_esc(root.first_user_prompt[:300])}</span>',
            "",
            0,
            "t-user",
        ))
    rows.extend(_trace_rows(root, 0, origin))

    sessions = list(root.all_sessions())
    n_turns = sum(len(s.turns) for s in sessions)
    wall = root.wall_seconds
    wall_str = f"{int(wall // 60)}m {int(wall % 60)}s" if wall else "—"

    total_d, total_u = root.total_dollars
    mix = root.total_model_mix
    if not mix:
        model_disp = "—"
    elif len(mix) == 1:
        model_disp = _shorten_model(next(iter(mix)))
    else:
        model_disp = "mix: " + ", ".join(
            f"{_shorten_model(k)}×{v}" for k, v in
            sorted(mix.items(), key=lambda kv: -kv[1])
        )
    dollar_disp = f"${total_d:.2f}" if total_d > 0 else "—"
    if total_u > 0:
        dollar_disp += f" ({total_u} unpriced)"

    html_out = TRACE_HTML_HEAD
    html_out = html_out.replace("__NAME__", _esc(root.command or root.session_id[:8]))
    html_out = html_out.replace("__TOTAL_COST__", _fmt_num(root.total_cost))
    html_out = html_out.replace("__TOTAL_DOLLARS__", dollar_disp)
    html_out = html_out.replace("__MODEL_MIX__", _esc(model_disp))
    html_out = html_out.replace("__N_SESSIONS__", str(len(sessions)))
    html_out = html_out.replace("__N_TURNS__", str(n_turns))
    html_out = html_out.replace("__WALL__", wall_str)
    html_out = html_out.replace("__CLIP_BANNER__", _clip_banner_html(root))
    html_out = html_out.replace("__FINDINGS__", "")
    html_out = html_out.replace("__ROWS__", "\n".join(rows))
    out_path.write_text(html_out)


# ────────────────────────────────────────────────────────────────────────────
# SQLite tracking
# ────────────────────────────────────────────────────────────────────────────


def _ensure_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY,
          session_id TEXT NOT NULL,
          command TEXT,
          first_prompt TEXT,
          started_at TEXT,
          wall_seconds REAL,
          n_sessions INTEGER,
          n_turns INTEGER,
          billable_input INTEGER,
          cache_creation INTEGER,
          cache_read INTEGER,
          output_tokens INTEGER,
          cost_weighted REAL,
          UNIQUE(session_id)
        );
        CREATE INDEX IF NOT EXISTS runs_cmd_idx ON runs(command, started_at);
        """
    )
    # Idempotent column adds for backwards-compatible schema evolution.
    for col_decl in ("label TEXT", "note TEXT", "trace_dir TEXT", "cwd TEXT",
                     "dollars REAL", "n_unpriced_turns INTEGER", "model_mix TEXT",
                     "source TEXT"):
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col_decl}")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS runs_label_idx ON runs(label)")
    conn.execute("CREATE INDEX IF NOT EXISTS runs_cwd_idx ON runs(cwd)")
    return conn


def git_label_for(cwd: str) -> str | None:
    """Return '<branch>@<short-sha>[-dirty]' for the given dir, or None."""
    import subprocess
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
        branch = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, env=env,
        ).strip()
        sha = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, env=env,
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", cwd, "status", "--porcelain"],
            text=True, stderr=subprocess.DEVNULL, env=env,
        ).strip()
        dirty = "-dirty" if status else ""
        return f"{branch}@{sha}{dirty}"
    except Exception:
        return None


def track(
    root: Session,
    db_path: Path,
    label: str | None = None,
    note: str | None = None,
    trace_dir: Path | None = None,
):
    sessions = list(root.all_sessions())
    started_at = root.turns[0].timestamp if root.turns else ""
    total_d, total_u = root.total_dollars
    model_mix = json.dumps(root.total_model_mix)
    conn = _ensure_db(db_path)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO runs "
            "(session_id, command, first_prompt, started_at, wall_seconds, "
            "n_sessions, n_turns, billable_input, cache_creation, cache_read, "
            "output_tokens, cost_weighted, label, note, trace_dir, cwd, "
            "dollars, n_unpriced_turns, model_mix, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                root.session_id,
                root.command,
                root.first_user_prompt,
                started_at,
                root.wall_seconds,
                len(sessions),
                sum(len(s.turns) for s in sessions),
                sum(s.own_billable_input for s in sessions),
                sum(sum(t.cache_creation for t in s.turns) for s in sessions),
                sum(sum(t.cache_read for t in s.turns) for s in sessions),
                sum(s.own_output for s in sessions),
                root.total_cost,
                label,
                note,
                str(trace_dir) if trace_dir else None,
                root.cwd or None,
                total_d if total_d > 0 else None,
                total_u,
                model_mix,
                root.source,
            ),
        )
    tag = f" [{label}]" if label else ""
    print(f"tracked: {root.session_id}{tag} → {db_path}")


def history(db_path: Path, skill: str | None = None):
    """Show all runs in this project's db. (Per-project storage already
    scopes to project — no cwd filter needed.)"""
    if not db_path.exists():
        print(f"no history db at {db_path}")
        return
    conn = _ensure_db(db_path)
    cur = conn.cursor()

    where: list[str] = []
    args: list = []
    if skill:
        where.append("command = ?")
        args.append(skill)
    scope = f"db={db_path}"

    q = ("SELECT session_id, started_at, command, label, cwd, n_turns, "
         "billable_input, cost_weighted, dollars, model_mix FROM runs")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY started_at"

    rows = cur.execute(q, args).fetchall()
    if not rows:
        print(f"(no runs · {scope})")
        return
    print(f"scope: {scope}")
    print(f"{'ref':<10}{'started_at':<18}{'command':<14}{'label':<22}{'project':<13}"
          f"{'model':<14}{'turns':>5}{'cost':>10}{'$$':>8}  {'Δ$$ vs prev':>14}")
    print("-" * 140)
    prev_dollars_by_cmd: dict[str, float] = {}
    for sid, ts, cmd, lbl, run_cwd, nturns, in_tok, cost, dollars, mix_json in rows:
        ref = (sid or "")[:8]
        cmd_s = cmd or "—"
        lbl_s = (lbl or "—")[:21]
        proj = (os.path.basename(run_cwd.rstrip("/")) if run_cwd else "—")[:12]
        try:
            mix = json.loads(mix_json or "{}")
        except Exception:
            mix = {}
        primary_model = _shorten_model(_dominant_model(mix))
        if len(mix) > 1:
            primary_model = primary_model + "+"
        dollar_str = f"${dollars:.2f}" if dollars else "—"
        delta = ""
        if cmd and dollars and cmd in prev_dollars_by_cmd:
            d = dollars - prev_dollars_by_cmd[cmd]
            pct = 100 * d / prev_dollars_by_cmd[cmd] if prev_dollars_by_cmd[cmd] else 0
            sign = "+" if d >= 0 else ""
            delta = f"{sign}${d:.2f} ({sign}{pct:>3.0f}%)"
        if dollars:
            prev_dollars_by_cmd[cmd] = dollars
        ts_short = ts[:16] if ts else ""
        print(f"{ref:<10}{ts_short:<18}{cmd_s:<14}{lbl_s:<22}{proj:<13}"
              f"{primary_model:<14}{nturns:>5}{cost:>10,.0f}{dollar_str:>8}"
              f"  {delta:>14}")


# ────────────────────────────────────────────────────────────────────────────
# Clip helpers
# ────────────────────────────────────────────────────────────────────────────


def find_clip_from(turns: list[Turn], pattern: str) -> tuple[int | None, str]:
    """Locate the most recent turn whose content matches `pattern`. Returns
    (1-based turn index or None, what-matched)."""
    n = len(turns)
    if pattern.startswith("tool:"):
        target = pattern[len("tool:"):]
        for offset, t in enumerate(reversed(turns)):
            for tc in t.tool_calls:
                if tc.name == target:
                    i = n - offset
                    return (i, f"tool {tc.name}({tc.input_label})")
        return (None, "")
    p = pattern.lower()
    for offset, t in enumerate(reversed(turns)):
        i = n - offset
        for um in t.user_messages:
            if p in um.lower():
                return (i, f"user message in turn {i}")
        for tb in t.text_blocks:
            if p in tb.lower():
                return (i, f"text in turn {i}")
        for tc in t.tool_calls:
            if p in tc.input_label.lower() or p in tc.name.lower():
                return (i, f"tool {tc.name}({tc.input_label}) in turn {i}")
    return (None, "")


def apply_clip_flags(
    root: Session,
    clip_start: int | None,
    clip_end: int | None,
    clip_from: str | None,
    clip_reason: str | None,
) -> Clip | None:
    """Resolve --clip-* flags and hard-trim root turns.

    Only the root session is sliced; child sessions attached to kept root turns
    are preserved in full.
    """
    if clip_start is None and clip_end is None and clip_from is None and clip_reason is None:
        return None
    clip = Clip(reason=clip_reason)
    total = len(root.turns)
    if clip_start is not None:
        clip.start_turn = clip_start
    if clip_from is not None:
        idx, matched = find_clip_from(root.turns, clip_from)
        if idx is None:
            raise SystemExit(f"--clip-from: no turn matched pattern {clip_from!r}")
        clip.start_turn = idx
        clip.matched_pattern = f"{clip_from} → {matched}"
    if clip_end is not None:
        # --clip-end M means "drop the last M turns of root"
        keep = max(0, total - clip_end)
        clip.end_turn = keep
    start = max(1, clip.start_turn or 1)
    end = min(total, clip.end_turn if clip.end_turn is not None else total)
    root.turns = root.turns[start - 1:end] if start <= end else []
    root.clip = None
    return clip


# ────────────────────────────────────────────────────────────────────────────
# trace.json (canonical artifact) — emit + load
# ────────────────────────────────────────────────────────────────────────────


SCHEMA_VERSION = 1


def _event_to_json(e: Event) -> dict:
    if isinstance(e, UserMessage):
        return {"kind": "user", "id": e.id, "text": e.text, "timestamp": e.timestamp}
    if isinstance(e, TextBlock):
        return {"kind": "text", "id": e.id, "text": e.text}
    if isinstance(e, ThinkingBlock):
        return {"kind": "thinking", "id": e.id, "chars": e.chars}
    if isinstance(e, ToolCall):
        d: dict = {
            "kind": "tool_use",
            "id": e.tool_use_id,
            "name": e.name,
            "input_label": e.input_label,
            "input": e.input_full,
            "timestamp": e.timestamp,
            "result": {
                "tokens": e.result_tokens,
                "full_chars": e.result_full_chars,
                "content": e.result_content,
                "preview": e.result_preview,
            },
        }
        if e.child_session is not None:
            d["child_session"] = _session_to_json(e.child_session)
        return d
    raise TypeError(f"unknown event type: {type(e)!r}")


def _turn_to_json(t: Turn, n: int) -> dict:
    return {
        "id": t.request_id,
        "n": n,
        "timestamp": t.timestamp,
        "model": t.model,
        "usage": {
            "input": t.input_tokens,
            "cache_creation": t.cache_creation,
            "cache_read": t.cache_read,
            "output": t.output_tokens,
            "cost_weighted": t.cost,
            "dollars": t.dollars,
        },
        "events": [_event_to_json(e) for e in t.events],
    }


def _session_to_json(s: Session) -> dict:
    own_d, own_u = s.own_dollars
    tot_d, tot_u = s.total_dollars
    return {
        "id": s.session_id,
        "source": s.source,
        "agent_type": s.agent_type,
        "command": s.command,
        "cwd": s.cwd,
        "first_user_prompt": s.first_user_prompt,
        "label": s.label,
        "clip": (
            {
                "start_turn": s.clip.start_turn,
                "end_turn": s.clip.end_turn,
                "matched_pattern": s.clip.matched_pattern,
                "reason": s.clip.reason,
            }
            if s.clip is not None else None
        ),
        "totals": {
            "own_billable_input": s.own_billable_input,
            "own_output": s.own_output,
            "own_cost_weighted": s.own_cost,
            "own_dollars": own_d,
            "own_dollars_n_unpriced_turns": own_u,
            "total_billable_input": s.total_billable_input,
            "total_output": s.total_output,
            "total_cost_weighted": s.total_cost,
            "total_dollars": tot_d,
            "total_dollars_n_unpriced_turns": tot_u,
            "turns": len(s.turns),
            "included_turns": len(s.included_turns),
            "model_mix": s.total_model_mix,
        },
        "turns": [_turn_to_json(t, i + 1) for i, t in enumerate(s.turns)],
    }


def emit_trace_json(root: Session, out_path: Path):
    sessions = list(root.all_sessions())
    total_d, total_u = root.total_dollars
    doc = {
        "schema_version": SCHEMA_VERSION,
        "task": {
            "command": root.command,
            "cwd": root.cwd,
            "started_at": root.turns[0].timestamp if root.turns else "",
            "wall_seconds": root.wall_seconds,
            "first_user_prompt": root.first_user_prompt,
            "totals": {
                "sessions": len(sessions),
                "turns": sum(len(s.turns) for s in sessions),
                "billable_input": sum(s.own_billable_input for s in sessions),
                "output": sum(s.own_output for s in sessions),
                "cost_weighted": root.total_cost,
                "dollars": total_d,
                "dollars_n_unpriced_turns": total_u,
                "model_mix": root.total_model_mix,
            },
        },
        "session": _session_to_json(root),
        "annotations": {
            "by_event": {},
            "by_turn": {},
            "by_session": {},
            "global": [],
        },
    }
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2))


def _event_from_json(d: dict) -> Event:
    k = d.get("kind")
    if k == "user":
        return UserMessage(id=d["id"], text=d["text"], timestamp=d.get("timestamp", ""))
    if k == "text":
        return TextBlock(id=d["id"], text=d["text"])
    if k == "thinking":
        return ThinkingBlock(id=d["id"], chars=d.get("chars", 0))
    if k == "tool_use":
        res = d.get("result", {}) or {}
        tc = ToolCall(
            name=d["name"],
            input_label=d.get("input_label", ""),
            result_tokens=res.get("tokens", 0),
            tool_use_id=d["id"],
            input_full=d.get("input", {}) or {},
            result_content=res.get("content", res.get("preview", "")),
            result_preview=res.get("preview", ""),
            result_full_chars=res.get("full_chars", 0),
            timestamp=d.get("timestamp", ""),
        )
        if tc.result_content and not tc.result_preview:
            tc.result_preview = tc.result_content[:3000]
        if tc.result_content and not tc.result_full_chars:
            tc.result_full_chars = len(tc.result_content)
        if d.get("child_session"):
            tc.child_session = _session_from_json(d["child_session"])
        return tc
    raise ValueError(f"unknown event kind: {k!r}")


def _turn_from_json(d: dict) -> Turn:
    u = d.get("usage", {}) or {}
    t = Turn(
        request_id=d["id"],
        timestamp=d.get("timestamp", ""),
        input_tokens=u.get("input", 0),
        cache_creation=u.get("cache_creation", 0),
        cache_read=u.get("cache_read", 0),
        output_tokens=u.get("output", 0),
        model=d.get("model"),
    )
    t.events = [_event_from_json(e) for e in d.get("events", [])]
    return t


def _session_from_json(d: dict) -> Session:
    s = Session(
        path=Path(""),
        session_id=d["id"],
        agent_type=d.get("agent_type"),
        command=d.get("command"),
        first_user_prompt=d.get("first_user_prompt", ""),
        cwd=d.get("cwd", ""),
        turns=[_turn_from_json(t) for t in d.get("turns", [])],
        source=d.get("source", "claude"),
    )
    c = d.get("clip")
    if c:
        s.clip = Clip(
            start_turn=c.get("start_turn"),
            end_turn=c.get("end_turn"),
            matched_pattern=c.get("matched_pattern"),
            reason=c.get("reason"),
        )
    return s


def load_trace_json(path: Path) -> Session:
    doc = json.loads(path.read_text())
    if doc.get("schema_version") != SCHEMA_VERSION:
        print(
            f"warning: trace.json schema_version {doc.get('schema_version')} "
            f"!= tracer schema version {SCHEMA_VERSION}",
            file=sys.stderr,
        )
    return _session_from_json(doc["session"])


# ────────────────────────────────────────────────────────────────────────────
# Per-project storage paths
# ────────────────────────────────────────────────────────────────────────────


def cmd_init(here: bool, skip_gitignore_prompt: bool):
    """Create .tracer/ in the current project root."""
    cwd = os.getcwd()
    if here:
        target = Path(cwd).resolve()
    else:
        target = find_init_target_root(cwd)
        if target is None:
            print(f"no project root found above {cwd}", file=sys.stderr)
            print("hint: pass here=True to initialize the current directory anyway",
                  file=sys.stderr)
            raise SystemExit(2)
    tdir = target / TRACER_DIRNAME
    if tdir.exists():
        print(f"already initialized: {tdir}")
    else:
        (tdir / TRACER_TRACES).mkdir(parents=True, exist_ok=True)
        # Touch config.json with empty contents to make the dir's role obvious.
        (tdir / TRACER_CONFIG).write_text("{}\n")
        print(f"created: {tdir}")
        print(f"  ├── runs.db        (created on first save)")
        print(f"  ├── config.json    (per-project overrides — currently empty)")
        print(f"  └── traces/        (run dirs)")

    if skip_gitignore_prompt:
        return
    gi = target / ".gitignore"
    if not gi.exists():
        return
    gi_text = gi.read_text()
    if ".tracer/traces" in gi_text or ".tracer/" in gi_text:
        return
    print()
    print("Optional: trace artifacts can be large.")
    print(f"Add `.tracer/traces/` to {gi}? [y/N] ", end="", flush=True)
    try:
        ans = input().strip().lower()
    except EOFError:
        ans = ""
    if ans in ("y", "yes"):
        with gi.open("a") as fh:
            if not gi_text.endswith("\n"):
                fh.write("\n")
            fh.write("# tracer trace artifacts (commit if you want to share)\n.tracer/traces/\n")
        print(f"added to {gi}")
    else:
        print("(skipped — trace artifacts will be visible to git)")


def cmd_migrate(from_dir: Path, dry_run: bool):
    """Distribute runs from a legacy central tracer-db/ into per-project .tracer/."""
    legacy_db = from_dir / "runs.db"
    if not legacy_db.exists():
        print(f"no legacy db at {legacy_db}")
        return
    conn = sqlite3.connect(legacy_db)
    cur = conn.cursor()
    # Pull every row; we use trace_dir + cwd to route.
    cur.execute("SELECT session_id, command, label, note, trace_dir, cwd FROM runs")
    rows = cur.fetchall()
    print(f"found {len(rows)} legacy runs in {legacy_db}")
    moved = 0
    skipped: list[tuple[str, str]] = []
    by_project: dict[Path, int] = {}
    for sid, cmd, lbl, note, trace_dir, cwd in rows:
        if not trace_dir or not Path(trace_dir).is_dir():
            skipped.append((sid, "trace_dir missing"))
            continue
        if not cwd:
            skipped.append((sid, "no cwd recorded"))
            continue
        proj = find_project_root(cwd)
        if proj is None:
            skipped.append((sid, f"no project root for cwd {cwd}"))
            continue
        src = Path(trace_dir)
        dst = project_traces_dir(proj, create=not dry_run) / src.name
        action = "DRY-RUN" if dry_run else "moving"
        print(f"  [{action}] {sid[:8]}  →  {proj} (.tracer/traces/{src.name})")
        by_project[proj] = by_project.get(proj, 0) + 1
        if dry_run:
            continue
        if dst.exists():
            print(f"    skipped (destination exists): {dst}")
            continue
        import shutil
        shutil.move(str(src), str(dst))
        # Insert into project's db.
        # Recompute totals from the moved trace.json (safest).
        tj = dst / "trace.json"
        if tj.exists():
            try:
                root = load_trace_json(tj)
                pdb = project_db_path(proj)
                track(root, pdb, label=lbl, note=note, trace_dir=dst)
            except Exception as e:
                print(f"    warning: could not retrack {sid[:8]}: {e}")
        moved += 1
    print()
    if dry_run:
        print(f"(dry-run) would move {len(rows) - len(skipped)} runs across "
              f"{len(by_project)} project(s).")
    else:
        print(f"moved {moved} runs to {len(by_project)} project(s).")
    if skipped:
        print(f"skipped {len(skipped)}:")
        for sid, reason in skipped[:10]:
            print(f"  - {sid[:8]}: {reason}")
        if len(skipped) > 10:
            print(f"  …and {len(skipped) - 10} more")
    if not dry_run and moved > 0:
        print()
        print(f"original tracer-db preserved at {from_dir}")
        print("delete it manually when you're satisfied:")
        print(f"  rm -rf {from_dir}")


def default_run_dir(root: Session, label_hint: str | None = None) -> Path:
    """<project-root>/.tracer/traces/<iso>__<sid8>__<label>/"""
    proj_root = session_project_root(root)
    iso = (root.turns[0].timestamp if root.turns else datetime.utcnow().isoformat())
    iso_safe = re.sub(r"[:.]", "-", iso.split("+")[0]).split("Z")[0][:19]
    sid8 = root.session_id[:8]
    if label_hint:
        # Slugify the label for filesystem safety.
        safe = re.sub(r"[^A-Za-z0-9._@-]", "-", label_hint).strip("-")[:48]
        return project_traces_dir(proj_root) / f"{iso_safe}__{sid8}__{safe}"
    return project_traces_dir(proj_root) / f"{iso_safe}__{sid8}"


def resolve_output_dir(root: Session, out_arg: str | None, label_hint: str | None = None) -> Path:
    """Resolve artifact output dir without touching project storage when --out is set."""
    if out_arg:
        return Path(out_arg).expanduser().resolve()
    return default_run_dir(root, label_hint=label_hint)


# ────────────────────────────────────────────────────────────────────────────
# diff / compare across runs
# ────────────────────────────────────────────────────────────────────────────


def _resolve_run_target(ref: str, db_path: Path) -> Path:
    """Resolve a reference (path, session id, or label) to a trace.json path."""
    # 1. Direct path to trace.json
    p = Path(ref).expanduser()
    if p.is_file() and p.name.endswith(".json"):
        return p.resolve()
    # 2. Path to a run dir
    if p.is_dir() and (p / "trace.json").exists():
        return (p / "trace.json").resolve()
    # 3. Lookup in db: session id (exact or prefix) → trace_dir
    if not db_path.exists():
        raise SystemExit(f"cannot resolve {ref!r}: no db at {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT session_id, label, trace_dir FROM runs "
        "WHERE session_id = ? OR session_id LIKE ? OR label = ? "
        "ORDER BY started_at DESC LIMIT 1",
        (ref, f"{ref}%", ref),
    )
    row = cur.fetchone()
    if row:
        _, _, td = row
        if td and (Path(td) / "trace.json").exists():
            return (Path(td) / "trace.json").resolve()
    raise SystemExit(f"could not resolve {ref!r} to a trace.json")


def _summarize_trace(tj_path: Path) -> dict:
    """Compute headline stats and tool/file frequency tables from a trace.json."""
    from collections import Counter
    doc = json.loads(tj_path.read_text())
    sessions = []

    def walk(sess: dict):
        sessions.append(sess)
        for t in sess.get("turns", []):
            for e in t.get("events", []):
                if e.get("kind") == "tool_use" and e.get("child_session"):
                    walk(e["child_session"])
    walk(doc["session"])
    task_cwd = doc.get("task", {}).get("cwd") or doc.get("session", {}).get("cwd")

    tool_counts: Counter[str] = Counter()
    files_read: Counter[str] = Counter()
    files_written: Counter[str] = Counter()
    subagent_types: Counter[str] = Counter()
    total_turns = 0
    total_tools = 0
    for sess in sessions:
        if sess.get("agent_type"):
            subagent_types[sess["agent_type"]] += 1
        for t in sess.get("turns", []):
            total_turns += 1
            for e in t.get("events", []):
                if e.get("kind") != "tool_use":
                    continue
                total_tools += 1
                name = e.get("name", "?")
                tool_counts[name] += 1
                inp = e.get("input", {}) or {}
                if name == "Read":
                    files_read[os.path.basename(inp.get("file_path", ""))] += 1
                elif name in ("Write", "Edit"):
                    files_written[os.path.basename(inp.get("file_path", ""))] += 1

    return {
        "label": doc.get("task", {}).get("first_user_prompt", "")[:60],
        "command": doc.get("task", {}).get("command"),
        "cwd": task_cwd,
        "started_at": doc.get("task", {}).get("started_at"),
        "wall_seconds": doc.get("task", {}).get("wall_seconds", 0),
        "cost_weighted": doc.get("task", {}).get("totals", {}).get("cost_weighted", 0),
        "billable_input": doc.get("task", {}).get("totals", {}).get("billable_input", 0),
        "output": doc.get("task", {}).get("totals", {}).get("output", 0),
        "n_sessions": len(sessions),
        "n_turns": total_turns,
        "n_tools": total_tools,
        "tool_counts": dict(tool_counts),
        "files_read": dict(files_read),
        "files_written": dict(files_written),
        "subagent_types": dict(subagent_types),
    }


def _fmt_delta(a: float, b: float, *, pct: bool = True) -> str:
    d = b - a
    if d == 0:
        return "0"
    sign = "+" if d > 0 else ""
    if pct and a:
        return f"{sign}{d:,.0f} ({sign}{100*d/a:.0f}%)"
    return f"{sign}{d:,.0f}"


def diff_runs(ref_a: str, ref_b: str, db_path: Path):
    pa = _resolve_run_target(ref_a, db_path)
    pb = _resolve_run_target(ref_b, db_path)
    a = _summarize_trace(pa)
    b = _summarize_trace(pb)
    print(f"A: {ref_a}  →  {pa}")
    print(f"   cwd: {a.get('cwd') or '—'}")
    print(f"B: {ref_b}  →  {pb}")
    print(f"   cwd: {b.get('cwd') or '—'}")
    if a.get("cwd") and b.get("cwd") and a["cwd"] != b["cwd"]:
        print(
            "\n  ⚠ cross-project diff: cwds differ — complexity is not comparable.\n"
            "    Treat absolute numbers with care; structural deltas (tool counts,\n"
            "    files read/written, subagents) are still meaningful."
        )
    print()
    rows = [
        ("turns",       a["n_turns"],         b["n_turns"]),
        ("sessions",    a["n_sessions"],      b["n_sessions"]),
        ("tool calls",  a["n_tools"],         b["n_tools"]),
        ("billable in", a["billable_input"],  b["billable_input"]),
        ("output",      a["output"],          b["output"]),
        ("cost (wt)",   a["cost_weighted"],   b["cost_weighted"]),
        ("wall (s)",    a["wall_seconds"],    b["wall_seconds"]),
    ]
    print(f"{'metric':<14}{'A':>14}{'B':>14}    {'Δ (B−A)':>22}")
    print("-" * 70)
    for label, av, bv in rows:
        print(f"{label:<14}{av:>14,.0f}{bv:>14,.0f}    {_fmt_delta(av, bv):>22}")

    def print_freq(title: str, ka: dict, kb: dict):
        keys = sorted(set(ka) | set(kb), key=lambda k: -(ka.get(k, 0) + kb.get(k, 0)))
        if not keys:
            return
        print(f"\n{title}")
        print("-" * 70)
        for k in keys:
            av, bv = ka.get(k, 0), kb.get(k, 0)
            if av == bv == 0:
                continue
            d = bv - av
            mark = "  " if d == 0 else ("+ " if d > 0 else "− ")
            print(f"{mark}{k:<40}{av:>6,}{bv:>8,}  {_fmt_delta(av, bv, pct=False):>10}")

    print_freq("tool calls by name", a["tool_counts"], b["tool_counts"])
    print_freq("files read", a["files_read"], b["files_read"])
    print_freq("files written/edited", a["files_written"], b["files_written"])
    print_freq("subagents spawned (by type)", a["subagent_types"], b["subagent_types"])


def compare_runs(db_path: Path, skill: str | None, limit: int):
    """Rank recent runs in this project's db by cost-weighted total."""
    if not db_path.exists():
        print(f"no db at {db_path}")
        return
    conn = _ensure_db(db_path)
    cur = conn.cursor()

    where: list[str] = []
    args: list = []
    if skill:
        where.append("command = ?")
        args.append(skill)
    scope = f"db={db_path}"

    q = ("SELECT session_id, label, started_at, command, cwd, n_turns, "
         "billable_input, output_tokens, cost_weighted, dollars, model_mix FROM runs")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)

    rows = cur.execute(q, args).fetchall()
    if not rows:
        print(f"(no runs · {scope})")
        return
    best = min(r[8] for r in rows)
    print(f"scope: {scope}")
    print(f"{'ref':<10}{'label':<22}{'started_at':<18}{'command':<14}{'model':<14}"
          f"{'turns':>5}{'cost':>10}{'$$':>8}  {'vs best':>10}")
    print("-" * 130)
    rows.sort(key=lambda r: r[8])  # cheapest first
    for sid, lbl, ts, cmd, run_cwd, nturns, inn, outt, cost, dollars, mix_json in rows:
        ref = (sid or "")[:8]
        lbl_s = (lbl or "—")[:21]
        cmd_s = cmd or "—"
        try:
            mix = json.loads(mix_json or "{}")
        except Exception:
            mix = {}
        model_disp = _shorten_model(_dominant_model(mix))
        if len(mix) > 1:
            model_disp += "+"
        dollar_str = f"${dollars:.2f}" if dollars else "—"
        pct = 100 * (cost - best) / best if best else 0
        vs = "← best" if cost == best else f"+{pct:.0f}%"
        ts_short = (ts or "")[:16]
        print(f"{ref:<10}{lbl_s:<22}{ts_short:<18}{cmd_s:<14}{model_disp:<14}"
              f"{nturns:>5}{cost:>10,.0f}{dollar_str:>8}  {vs:>10}")


# ────────────────────────────────────────────────────────────────────────────
# MCP support
# ────────────────────────────────────────────────────────────────────────────


def _strip_tags(s: str) -> str:
    s = re.sub(r"<li>", "- ", s)
    s = re.sub(r"</li>", "\n", s)
    s = re.sub(r"</?(ul|b|code|details|summary|div)[^>]*>", "", s)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _run_rows(db_path: Path, command: str | None = None, limit: int = 20) -> list[dict]:
    if not db_path.exists():
        return []
    conn = _ensure_db(db_path)
    where: list[str] = []
    args: list = []
    if command:
        where.append("command = ?")
        args.append(command)
    q = ("SELECT session_id, label, started_at, command, cwd, n_turns, "
         "billable_input, output_tokens, cost_weighted, dollars, model_mix, "
         "trace_dir, note FROM runs")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    rows = []
    for r in conn.execute(q, args).fetchall():
        (sid, label, started_at, cmd, cwd, turns, billable_input, output_tokens,
         cost_weighted, dollars, model_mix, trace_dir, note) = r
        try:
            mix = json.loads(model_mix or "{}")
        except Exception:
            mix = {}
        rows.append({
            "session_id": sid,
            "ref": (sid or "")[:8],
            "label": label,
            "started_at": started_at,
            "command": cmd,
            "cwd": cwd,
            "turns": turns,
            "billable_input": billable_input,
            "output_tokens": output_tokens,
            "cost_weighted": cost_weighted,
            "dollars": dollars,
            "model_mix": mix,
            "trace_dir": trace_dir,
            "trace_json": str(Path(trace_dir) / "trace.json") if trace_dir else None,
            "trace_html": str(Path(trace_dir) / "trace.html") if trace_dir else None,
            "note": note,
        })
    return rows


def _mcp_db_path(cwd: str | None = None, *, create: bool = False) -> Path:
    root = active_project_root(cwd, error_if_missing=False)
    if root is None:
        here = Path(cwd or os.getcwd()).expanduser().resolve()
        root = here if here.is_dir() else here.parent
    return project_db_path(root, create=create)


def _latest_trace_ref(db_path: Path, command: str | None = None) -> str:
    rows = _run_rows(db_path, command=command, limit=1)
    if not rows:
        raise SystemExit("no analyzed traces found")
    return rows[0]["session_id"]


def _resolve_mcp_ref(ref: str | None, db_path: Path) -> Path:
    if not ref or ref == "latest":
        ref = _latest_trace_ref(db_path)
    return _resolve_run_target(ref, db_path)


def _summary_for_path(tj: Path) -> dict:
    s = _summarize_trace(tj)
    root = load_trace_json(tj)
    total_d, total_u = root.total_dollars
    tools = sorted(s["tool_counts"].items(), key=lambda kv: -kv[1])[:10]
    return {
        "session_id": root.session_id,
        "command": s.get("command"),
        "cwd": s.get("cwd"),
        "started_at": s.get("started_at"),
        "wall_seconds": s.get("wall_seconds"),
        "sessions": s.get("n_sessions"),
        "turns": s.get("n_turns"),
        "tool_calls": s.get("n_tools"),
        "billable_input": s.get("billable_input"),
        "output_tokens": s.get("output"),
        "cost_weighted": s.get("cost_weighted"),
        "dollars": total_d,
        "unpriced_turns": total_u,
        "model_mix": root.total_model_mix,
        "top_tools": [{"name": name, "count": count} for name, count in tools],
        "findings": _mcp_suggestions(root, s),
    }


def _mcp_suggestions(root: Session, summary: dict | None = None) -> list[dict]:
    suggestions: list[dict] = []
    sessions = list(root.all_sessions())
    total_cost = root.total_cost

    subagents = [s for s in sessions if s.agent_type]
    if subagents and total_cost:
        biggest = max(subagents, key=lambda s: s.total_cost)
        share = 100 * biggest.total_cost / total_cost
        if share > 30:
            suggestions.append({
                "kind": "subagent_cost_share",
                "impact": "high",
                "evidence": {
                    "agent": biggest.label,
                    "share_percent": round(share, 1),
                    "cost_weighted": biggest.total_cost,
                },
                "suggestion": "Start optimization with this subagent's prompt, tool access, and task scope.",
            })

    for s in sessions:
        if len(s.turns) >= 8:
            tiny = sum(1 for t in s.turns if t.output_tokens < 10)
            if tiny / len(s.turns) > 0.5:
                suggestions.append({
                    "kind": "serial_tool_loop",
                    "impact": "medium",
                    "evidence": {
                        "session": s.label,
                        "tiny_output_turns": tiny,
                        "turns": len(s.turns),
                    },
                    "suggestion": "Batch independent reads/searches and use parallel tool calls before reasoning again.",
                })
                break

    big_results: list[tuple[Session, ToolCall]] = []
    for s in sessions:
        for t in s.turns:
            for tc in t.tool_calls:
                if tc.result_tokens > 1500 and tc.child_session is None:
                    big_results.append((s, tc))
    big_results.sort(key=lambda x: -x[1].result_tokens)
    if big_results:
        suggestions.append({
            "kind": "large_tool_results",
            "impact": "medium",
            "evidence": [
                {
                    "session": s.label,
                    "tool": tc.name,
                    "input": tc.input_label,
                    "result_tokens": tc.result_tokens,
                }
                for s, tc in big_results[:5]
            ],
            "suggestion": "Narrow reads/searches or summarize large outputs before they become repeated context.",
        })

    for s in sessions:
        if s.turns and s.turns[0].cache_creation > 10000:
            suggestions.append({
                "kind": "large_prompt_baseline",
                "impact": "medium",
                "evidence": {
                    "session": s.label,
                    "cache_creation_turn_1": s.turns[0].cache_creation,
                },
                "suggestion": "Trim loaded skills, tool descriptions, or agent instructions for this workflow.",
            })
            break

    if not suggestions:
        html_hints = _build_hints(root)
        if html_hints:
            suggestions.append({
                "kind": "trace_findings",
                "impact": "info",
                "evidence": _strip_tags(html_hints),
                "suggestion": "Review the trace summary for the detailed optimization note.",
            })
    return suggestions


def mcp_trace_create(
    session: str | None = None,
    cwd: str | None = None,
    source: str = "auto",
    label: str | None = None,
    note: str | None = None,
    clip_start: int | None = None,
    clip_end: int | None = None,
    clip_from: str | None = None,
    clip_reason: str | None = None,
    emit_ascii_artifact: bool = False,
) -> dict:
    if session:
        path = resolve_input(session, source=source)
    else:
        cwd_for_lookup = cwd or os.getcwd()
        matches = find_sessions_for_cwd(cwd_for_lookup, source=source)
        if not matches:
            raise SystemExit(f"no sessions found for cwd: {cwd_for_lookup}")
        path = matches[0].path
    root = parse_session(path)
    apply_clip_flags(root, clip_start, clip_end, clip_from, clip_reason)
    db_path = resolve_db(None, for_session=root)
    label = label if label is not None else git_label_for(root.cwd)
    out_dir = default_run_dir(root, label_hint=label)
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_json = out_dir / "trace.json"
    trace_html = out_dir / "trace.html"
    emit_trace_json(root, trace_json)
    emit_trace_html(root, trace_html)
    ascii_path = None
    if emit_ascii_artifact:
        ascii_path = out_dir / "ascii.txt"
        emit_ascii(root, ascii_path)
    track(root, db_path, label=label, note=note, trace_dir=out_dir)
    return {
        "session_id": root.session_id,
        "source": root.source,
        "label": label,
        "project_root": str(session_project_root(root)),
        "db": str(db_path),
        "trace_dir": str(out_dir),
        "trace_json": str(trace_json),
        "trace_html": str(trace_html),
        "ascii": str(ascii_path) if ascii_path else None,
        "summary": _summary_for_path(trace_json),
    }


def mcp_trace_history(cwd: str | None = None, command: str | None = None, limit: int = 20) -> dict:
    db_path = _mcp_db_path(cwd, create=False)
    rows = _run_rows(db_path, command=command, limit=limit)
    best = min((r["cost_weighted"] for r in rows if r["cost_weighted"] is not None), default=None)
    for r in rows:
        if best and r.get("cost_weighted") is not None:
            r["vs_best_percent"] = round(100 * (r["cost_weighted"] - best) / best, 1)
            r["is_best"] = r["cost_weighted"] == best
    return {"db": str(db_path), "runs": rows}


def mcp_trace_compare(baseline: str, candidate: str, cwd: str | None = None) -> dict:
    db_path = _mcp_db_path(cwd, create=False)
    pa = _resolve_mcp_ref(baseline, db_path)
    pb = _resolve_mcp_ref(candidate, db_path)
    a = _summarize_trace(pa)
    b = _summarize_trace(pb)
    metric_map = {
        "turns": "n_turns",
        "sessions": "n_sessions",
        "tool_calls": "n_tools",
        "billable_input": "billable_input",
        "output_tokens": "output",
        "cost_weighted": "cost_weighted",
        "wall_seconds": "wall_seconds",
    }
    deltas = {name: (b[key] or 0) - (a[key] or 0) for name, key in metric_map.items()}

    def freq_delta(name: str) -> dict:
        keys = sorted(set(a[name]) | set(b[name]))
        return {k: b[name].get(k, 0) - a[name].get(k, 0) for k in keys if b[name].get(k, 0) != a[name].get(k, 0)}

    return {
        "baseline": {"ref": baseline, "trace_json": str(pa), "summary": a},
        "candidate": {"ref": candidate, "trace_json": str(pb), "summary": b},
        "deltas": deltas,
        "tool_call_deltas": freq_delta("tool_counts"),
        "file_read_deltas": freq_delta("files_read"),
        "file_write_deltas": freq_delta("files_written"),
        "subagent_deltas": freq_delta("subagent_types"),
        "cross_project": bool(a.get("cwd") and b.get("cwd") and a.get("cwd") != b.get("cwd")),
    }


def mcp_trace_progress(
    ref: str | None = "latest",
    compare_to: str = "previous_same_command",
    baseline: str | None = None,
    cwd: str | None = None,
    limit: int = 20,
) -> dict:
    db_path = _mcp_db_path(cwd, create=False)
    candidate_path = _resolve_mcp_ref(ref, db_path)
    candidate = _summary_for_path(candidate_path)
    if baseline:
        baseline_ref = baseline
    else:
        command = candidate.get("command") if compare_to in ("previous_same_command", "best_same_command") else None
        rows = _run_rows(db_path, command=command, limit=limit)
        rows = [r for r in rows if r.get("trace_json") and Path(r["trace_json"]).resolve() != candidate_path.resolve()]
        if not rows:
            return {
                "verdict": "insufficient_data",
                "candidate": candidate,
                "progress": [],
                "regressions": [],
                "improvement_opportunities": candidate.get("findings", []),
                "next_experiment": "Capture another comparable trace after changing one workflow variable.",
            }
        if compare_to == "best_same_command":
            row = min(rows, key=lambda r: r.get("cost_weighted") or float("inf"))
        else:
            row = rows[0]
        baseline_ref = row["session_id"]
    comp = mcp_trace_compare(baseline_ref, str(candidate_path), cwd=cwd)
    deltas = comp["deltas"]
    progress: list[str] = []
    regressions: list[str] = []
    for key, label_text in (
        ("cost_weighted", "cost-weighted tokens"),
        ("tool_calls", "tool calls"),
        ("turns", "turns"),
        ("wall_seconds", "wall seconds"),
    ):
        d = deltas.get(key, 0)
        if d < 0:
            progress.append(f"{label_text} decreased by {abs(d):,.0f}")
        elif d > 0:
            regressions.append(f"{label_text} increased by {d:,.0f}")
    verdict = "mixed"
    if progress and not regressions:
        verdict = "improved"
    elif regressions and not progress:
        verdict = "regressed"
    return {
        "verdict": verdict,
        "baseline_ref": baseline_ref,
        "candidate": candidate,
        "comparison": comp,
        "progress": progress,
        "regressions": regressions,
        "improvement_opportunities": candidate.get("findings", []),
        "next_experiment": "Change one high-impact item above, capture a new labeled trace, then compare against this candidate.",
    }


def mcp_trace_path(ref: str | None = "latest", view: str = "json", cwd: str | None = None) -> dict:
    db_path = _mcp_db_path(cwd, create=False)
    tj = _resolve_mcp_ref(ref, db_path)
    run_dir = tj.parent
    targets = {
        "json": tj,
        "trace": run_dir / "trace.html",
        "html": run_dir / "trace.html",
        "ascii": run_dir / "ascii.txt",
        "dir": run_dir,
    }
    if view not in targets:
        raise SystemExit(f"unknown view {view!r}")
    return {"ref": ref or "latest", "view": view, "path": str(targets[view])}


def mcp_trace_open(ref: str | None = "latest", view: str = "trace", cwd: str | None = None) -> dict:
    resolved = mcp_trace_path(ref=ref, view=view, cwd=cwd)
    target = Path(resolved["path"])
    if not target.exists():
        raise SystemExit(f"missing artifact: {target}")
    try:
        subprocess.run(["open", str(target)], check=True)
        opened = True
        error = None
    except Exception as e:
        opened = False
        error = str(e)
    return {**resolved, "opened": opened, "error": error}


def mcp_trace_read(ref: str | None = "latest", cwd: str | None = None, include_summary: bool = True) -> dict:
    resolved = mcp_trace_path(ref=ref, view="json", cwd=cwd)
    trace_json = Path(resolved["path"])
    doc = json.loads(trace_json.read_text(encoding="utf-8"))
    payload = {"ref": ref or "latest", "trace_json": str(trace_json), "document": doc}
    if include_summary:
        payload["summary"] = _summary_for_path(trace_json)
    return payload


def mcp_trace_clip(
    ref: str | None = "latest",
    cwd: str | None = None,
    start: int | None = None,
    end: int | None = None,
    from_pattern: str | None = None,
    reason: str | None = None,
    reset: bool = False,
    session: str | None = None,
    source: str = "auto",
    label: str | None = None,
    note: str | None = None,
) -> dict:
    if session:
        return mcp_trace_create(
            session=session,
            cwd=cwd,
            source=source,
            label=label,
            note=note,
            clip_start=start,
            clip_end=end,
            clip_from=from_pattern,
            clip_reason=reason,
        )

    db_path = _mcp_db_path(cwd, create=False)
    tj = _resolve_mcp_ref(ref, db_path)
    root = load_trace_json(tj)
    if reset:
        root.clip = None
    else:
        clip = apply_clip_flags(root, clip_start=start, clip_end=end, clip_from=from_pattern, clip_reason=reason)
        if clip is None:
            raise SystemExit("no clip flags given")
    prev_label = prev_note = None
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT label, note FROM runs WHERE session_id = ?", (root.session_id,)).fetchone()
        if row:
            prev_label, prev_note = row
    emit_trace_json(root, tj)
    emit_trace_html(root, tj.parent / "trace.html")
    track(root, db_path, label=prev_label, note=prev_note, trace_dir=tj.parent)
    return {
        "ref": ref or "latest",
        "trace_json": str(tj),
        "trace_html": str(tj.parent / "trace.html"),
        "reset": reset,
        "summary": _summary_for_path(tj),
    }


def mcp_trace_suggest_improvements(ref: str | None = "latest", cwd: str | None = None) -> dict:
    db_path = _mcp_db_path(cwd, create=False)
    tj = _resolve_mcp_ref(ref, db_path)
    root = load_trace_json(tj)
    summary = _summarize_trace(tj)
    return {
        "ref": ref or "latest",
        "trace_json": str(tj),
        "summary": _summary_for_path(tj),
        "improvement_opportunities": _mcp_suggestions(root, summary),
    }


MCP_TOOLS = {
    "trace_save": {
        "description": "Save a Claude Code or Codex session as trace.json/trace.html and track it in project history.",
        "schema": {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "Session id, JSONL path, or omitted for latest session in cwd."},
                "cwd": {"type": "string", "description": "Project cwd used when session is omitted."},
                "source": {"type": "string", "enum": ["auto", "claude", "codex"], "default": "auto"},
                "label": {"type": "string"},
                "note": {"type": "string"},
                "clip_start": {"type": "integer"},
                "clip_end": {"type": "integer"},
                "clip_from": {"type": "string"},
                "clip_reason": {"type": "string"},
                "emit_ascii_artifact": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "handler": mcp_trace_create,
    },
    "trace_list": {
        "description": "List stored traces for the current project with metadata such as turns, tokens, models, labels, and artifact paths.",
        "schema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "command": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "additionalProperties": False,
        },
        "handler": mcp_trace_history,
    },
    "trace_read": {
        "description": "Read a stored trace.json document for AI inspection.",
        "schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "default": "latest"},
                "cwd": {"type": "string"},
                "include_summary": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": mcp_trace_read,
    },
    "trace_open": {
        "description": "Open a stored trace artifact in the local OS and return its path.",
        "schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "default": "latest"},
                "view": {"type": "string", "enum": ["trace", "json", "ascii", "dir"], "default": "trace"},
                "cwd": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": mcp_trace_open,
    },
    "trace_clip": {
        "description": "Clip an existing stored trace in hindsight, or create a clipped trace from a session source.",
        "schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "default": "latest"},
                "cwd": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
                "from_pattern": {"type": "string"},
                "reason": {"type": "string"},
                "reset": {"type": "boolean", "default": False},
                "session": {"type": "string", "description": "Optional session id or JSONL path to create a new clipped trace."},
                "source": {"type": "string", "enum": ["auto", "claude", "codex"], "default": "auto"},
                "label": {"type": "string"},
                "note": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": mcp_trace_clip,
    },
}


def _mcp_write(obj: dict):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _mcp_result(req_id, result: dict):
    _mcp_write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _mcp_error(req_id, code: int, message: str):
    _mcp_write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def mcp_serve():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            method = req.get("method")
            req_id = req.get("id")
            params = req.get("params") or {}

            if method == "initialize":
                _mcp_result(req_id, {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "serverInfo": {"name": "tracer", "version": tracer_version()},
                    "capabilities": {"tools": {}},
                })
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                _mcp_result(req_id, {
                    "tools": [
                        {
                            "name": name,
                            "description": spec["description"],
                            "inputSchema": spec["schema"],
                        }
                        for name, spec in MCP_TOOLS.items()
                    ]
                })
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                if name not in MCP_TOOLS:
                    _mcp_error(req_id, -32602, f"unknown tool: {name}")
                    continue
                try:
                    payload = MCP_TOOLS[name]["handler"](**args)
                    _mcp_result(req_id, {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(payload, ensure_ascii=False, indent=2),
                        }]
                    })
                except BaseException as e:
                    _mcp_result(req_id, {
                        "isError": True,
                        "content": [{"type": "text", "text": str(e)}],
                    })
            else:
                if req_id is not None:
                    _mcp_error(req_id, -32601, f"method not found: {method}")
        except Exception as e:
            _mcp_error(None, -32700, str(e))


def cmd_mcp_install(scope: str, name: str):
    invoked_path = Path(sys.argv[0]).expanduser().resolve()
    if invoked_path.exists() and os.access(invoked_path, os.X_OK):
        serve_command = [str(invoked_path), "mcp", "serve"]
    else:
        serve_command = [sys.executable, str(Path(__file__).resolve()), "mcp", "serve"]
    cmd = [
        "claude", "mcp", "add",
        "--transport", "stdio",
        "--scope", scope,
        name,
        "--",
        *serve_command,
    ]
    subprocess.run(cmd, check=True)
    print(f"installed MCP server {name!r} ({scope})")
    print("restart Claude Code or run /mcp to verify it is connected")


def cmd_mcp_status(name: str):
    subprocess.run(["claude", "mcp", "get", name], check=True)


def cmd_mcp_remove(scope: str | None, name: str):
    cmd = ["claude", "mcp", "remove"]
    if scope:
        cmd.extend(["--scope", scope])
    cmd.append(name)
    subprocess.run(cmd, check=True)


# ────────────────────────────────────────────────────────────────────────────
# Session discovery (find sessions for a given cwd)
# ────────────────────────────────────────────────────────────────────────────


def _iter_claude_session_files() -> Iterable[Path]:
    """Yield every top-level session JSONL across all Claude Code projects."""
    if not CLAUDE_PROJECTS.is_dir():
        return
    for proj in CLAUDE_PROJECTS.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            if "subagents" in f.parts:
                continue
            yield f


def _iter_codex_session_files() -> Iterable[Path]:
    """Yield Codex rollout JSONL files."""
    if not CODEX_SESSIONS.is_dir():
        return
    yield from CODEX_SESSIONS.rglob("rollout-*.jsonl")


def _iter_session_files(source: str = "auto") -> Iterable[tuple[str, Path]]:
    if source in ("auto", "claude"):
        for f in _iter_claude_session_files():
            yield ("claude", f)
    if source in ("auto", "codex"):
        for f in _iter_codex_session_files():
            yield ("codex", f)


def _session_cwd_quick(path: Path, max_lines: int = 30) -> str:
    """Read the first few rows of a JSONL and return the recorded cwd, or ''."""
    try:
        with path.open() as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("cwd"):
                    return r["cwd"]
    except Exception:
        pass
    return ""


def _session_first_command(path: Path, max_lines: int = 30) -> str:
    """Best-effort: return the first <command-name> tag found, or ''."""
    try:
        with path.open() as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                m = re.search(r"<command-name>([^<]+)</command-name>", line)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return ""


def _codex_session_quick(path: Path, max_lines: int = 80) -> tuple[str, str]:
    cwd = ""
    prompt = ""
    try:
        with path.open() as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                payload = r.get("payload", {}) or {}
                if r.get("type") == "session_meta":
                    cwd = payload.get("cwd", "") or cwd
                elif r.get("type") == "turn_context":
                    cwd = payload.get("cwd", "") or cwd
                elif r.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                    text = _codex_message_text(payload)
                    if text and not _is_codex_bootstrap_user_message(text):
                        prompt = text[:120]
                elif r.get("type") == "event_msg" and payload.get("type") == "user_message":
                    text = str(payload.get("message", ""))
                    if text and not _is_codex_bootstrap_user_message(text):
                        prompt = text[:120]
                if cwd and prompt:
                    break
    except Exception:
        pass
    return cwd, prompt


@dataclass
class SessionSummary:
    path: Path
    session_id: str
    cwd: str
    command: str
    mtime: float
    source: str = "claude"


def find_sessions_for_cwd(cwd: str, source: str = "auto") -> list[SessionSummary]:
    """Return sessions whose recorded cwd matches `cwd`, sorted newest-first."""
    cwd_n = os.path.realpath(os.path.expanduser(cwd)).rstrip("/")
    results: list[SessionSummary] = []
    for src, f in _iter_session_files(source):
        if src == "codex":
            sc, command = _codex_session_quick(f)
            sid = f.stem.rsplit("-", 5)
            session_id = "-".join(sid[-5:]) if len(sid) >= 6 else f.stem
        else:
            sc = _session_cwd_quick(f)
            command = _session_first_command(f)
            session_id = f.stem
        if not sc:
            continue
        if os.path.realpath(sc).rstrip("/") != cwd_n:
            continue
        results.append(SessionSummary(
            path=f,
            session_id=session_id,
            cwd=sc,
            command=command,
            mtime=f.stat().st_mtime,
            source=src,
        ))
    results.sort(key=lambda s: -s.mtime)
    return results


def _format_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _dominant_model(mix: dict[str, int]) -> str:
    if not mix:
        return "—"
    return max(mix.items(), key=lambda kv: kv[1])[0]


def _shorten_model(name: str) -> str:
    """claude-opus-4-7 → opus-4-7. claude-sonnet-4-6 → sonnet-4-6. Etc."""
    if not name or name == "—" or name == "(unknown)":
        return name or "—"
    s = name
    if s.startswith("claude-"):
        s = s[len("claude-"):]
    if len(s) > 13:
        s = s[:13]
    return s


def _find_latest_run_dir(session_id: str, project_root: Path | None = None) -> Path | None:
    """Locate the rendered run dir for a given session id. Searches the active
    project's .tracer/traces/ by default; pass project_root to override."""
    if project_root is None:
        project_root = active_project_root(error_if_missing=False)
        if project_root is None:
            return None
    traces = project_traces_dir(project_root, create=False)
    if not traces.is_dir():
        return None
    sid8 = session_id[:8]
    # Run dirs are <iso>__<sid8> or <iso>__<sid8>__<label>
    matches = list(traces.glob(f"*__{sid8}")) + list(traces.glob(f"*__{sid8}__*"))
    if matches:
        matches.sort(key=lambda p: -p.stat().st_mtime)
        return matches[0]
    return None


def print_sessions_for_cwd(cwd: str, *, source: str = "auto", limit: int = 20) -> None:
    """Print raw Claude Code/Codex sessions available to save for a cwd."""
    matches = find_sessions_for_cwd(cwd, source=source)
    if not matches:
        print(f"no sessions for cwd: {cwd}")
        return
    seen: set[str] = set()
    proj_root = find_project_root(cwd)
    if proj_root is not None:
        db = project_db_path(proj_root, create=False)
        if db.exists():
            conn = sqlite3.connect(db)
            try:
                seen = {r[0] for r in conn.execute("SELECT session_id FROM runs")}
            finally:
                conn.close()
    print(f"sessions for {cwd}:")
    print(f"  {'when':<18}{'source':<8}{'session id':<20}{'command':<22}{'saved':<10}")
    print("  " + "-" * 78)
    for s in matches[:limit]:
        mark = "✓" if s.session_id in seen else ""
        print(f"  {_format_mtime(s.mtime):<18}{s.source:<8}{s.session_id[:18]:<20}"
              f"{(s.command or '—'):<22}{mark:<10}")


def tracer_version() -> str:
    return __version__


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────


def main(argv: list[str]):
    parser = argparse.ArgumentParser(prog="tracer")
    parser.add_argument("--version", action="version", version=f"%(prog)s {tracer_version()}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_an = sub.add_parser("save", help="create and store trace.json + trace.html for a coding-agent session")
    p_an.add_argument("session", nargs="?", default=None,
                      help="path to JSONL or session id (default: newest session for cwd)")
    p_an.add_argument("--source", default="auto", choices=["auto", "claude", "codex"],
                      help="session source to search when resolving input (default: auto)")
    p_an.add_argument("--out", default=None,
                      help="output dir (default: <project>/.tracer/traces/<run-dir>/)")
    p_an.add_argument("--no-track", action="store_true",
                      help="skip writing the run summary to SQLite (tracking is on by default)")
    p_an.add_argument("--db", default=None, help="path to SQLite db (default: current project's .tracer/runs.db)")
    p_an.add_argument("--label", default=None,
                      help="short label for this run (default: git <branch>@<short-sha>[-dirty])")
    p_an.add_argument("--note", default=None,
                      help="longer free-text note describing what changed")
    p_an.add_argument("--ascii", action="store_true", help="also emit an ASCII tree (ascii.txt)")
    p_an.add_argument("--print-ascii", action="store_true", help="print ASCII tree to stdout")
    p_an.add_argument("--clip-start", type=int, default=None,
                      help="hard clip to start at root turn N")
    p_an.add_argument("--clip-end", type=int, default=None,
                      help="hard clip by dropping last N root turns")
    p_an.add_argument("--clip-from", default=None,
                      help='hard clip to start at most recent turn matching pattern '
                           '(substring or "tool:Name")')
    p_an.add_argument("--clip-reason", default=None, help="note printed with the clip summary")

    p_ls = sub.add_parser("ls", help="list saved traces for the current project")
    p_ls.add_argument("--cwd", default=None, help="override cwd (default: $PWD)")
    p_ls.add_argument("--limit", type=int, default=20)

    p_sessions = sub.add_parser("sessions", help="list raw Claude Code/Codex sessions available to save")
    p_sessions.add_argument("--cwd", default=None, help="override cwd (default: $PWD)")
    p_sessions.add_argument("--source", default="auto", choices=["auto", "claude", "codex"])
    p_sessions.add_argument("--limit", type=int, default=20)

    sub.add_parser("version", help="print tracer version")

    p_op = sub.add_parser("open", help="open a saved trace run (ref or current cwd's latest)")
    p_op.add_argument("ref", nargs="?", default=None,
                      help="session id, label, run dir, or trace.json path "
                           "(default: latest run for current cwd)")
    p_op.add_argument("--cwd", default=None)
    p_op.add_argument("--view", default="trace",
                      choices=["trace", "json", "dir"],
                      help="which artifact to open")
    p_op.add_argument("--db", default=None, help="path to SQLite db (default: current project's .tracer/runs.db)")

    p_rd = sub.add_parser("read", help="print trace.json for a saved trace ref")
    p_rd.add_argument("ref", nargs="?", default="latest",
                      help="session id, label, run dir, trace.json path, or latest")
    p_rd.add_argument("--cwd", default=None)
    p_rd.add_argument("--summary", action="store_true", help="print a compact summary instead of full trace.json")
    p_rd.add_argument("--db", default=None, help="path to SQLite db (default: current project's .tracer/runs.db)")

    p_cl = sub.add_parser("clip", help="hard clip an existing trace.json")
    p_cl.add_argument("ref", help="run reference (session id, label, or path)")
    p_cl.add_argument("--start", type=int, default=None,
                      help="hard clip to start at root turn N")
    p_cl.add_argument("--end", type=int, default=None,
                      help="hard clip by dropping last N root turns")
    p_cl.add_argument("--from", dest="from_pattern", default=None,
                      help='hard clip to start at most recent turn matching pattern')
    p_cl.add_argument("--reason", default=None)
    p_cl.add_argument("--reset", action="store_true",
                      help="clear legacy soft-clip metadata; cannot restore hard-clipped turns")
    p_cl.add_argument("--db", default=None, help="path to SQLite db (default: current project's .tracer/runs.db)")

    p_mcp = sub.add_parser("mcp", help="serve or install the Tracer MCP server")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_cmd", required=True)
    mcp_sub.add_parser("serve", help="run the stdio MCP server")
    p_mcp_install = mcp_sub.add_parser("install", help="install Tracer into Claude Code MCP config")
    p_mcp_install.add_argument("--scope", default="local", choices=["local", "project", "user"])
    p_mcp_install.add_argument("--name", default="tracer")
    p_mcp_status = mcp_sub.add_parser("status", help="show Claude Code MCP status for Tracer")
    p_mcp_status.add_argument("--name", default="tracer")
    p_mcp_remove = mcp_sub.add_parser("remove", help="remove Tracer from Claude Code MCP config")
    p_mcp_remove.add_argument("--scope", default=None, choices=["local", "project", "user"])
    p_mcp_remove.add_argument("--name", default="tracer")

    args = parser.parse_args(argv)

    if args.cmd == "mcp":
        if args.mcp_cmd == "serve":
            mcp_serve()
        elif args.mcp_cmd == "install":
            cmd_mcp_install(args.scope, args.name)
        elif args.mcp_cmd == "status":
            cmd_mcp_status(args.name)
        elif args.mcp_cmd == "remove":
            cmd_mcp_remove(args.scope, args.name)

    elif args.cmd == "save":
        if args.session:
            path = resolve_input(args.session, source=args.source)
        else:
            cwd = os.getcwd()
            matches = find_sessions_for_cwd(cwd, source=args.source)
            if not matches:
                raise SystemExit(
                    f"no sessions found for cwd: {cwd}\n"
                    f"(walked {CLAUDE_PROJECTS} and {CODEX_SESSIONS})"
                )
            path = matches[0].path
            print(f"latest {matches[0].source} session for {cwd}: {matches[0].session_id} "
                  f"({_format_mtime(matches[0].mtime)}, {matches[0].command or 'no command'})")
        root = parse_session(path)
        # Apply --clip-* flags before computing totals or writing trace.json.
        total_before_clip = len(root.turns)
        clip = apply_clip_flags(
            root,
            clip_start=args.clip_start,
            clip_end=args.clip_end,
            clip_from=args.clip_from,
            clip_reason=args.clip_reason,
        )
        if clip:
            kept = len(root.turns)
            print(f"hard clip applied: kept original root turns "
                  f"{clip.start_turn or 1}..{clip.end_turn or total_before_clip} "
                  f"({kept}/{total_before_clip} turns kept)"
                  + (f" — reason: {clip.reason}" if clip.reason else "")
                  + (f" — matched: {clip.matched_pattern}" if clip.matched_pattern else ""))
        # Resolve label early so we can put it in the run-dir name.
        db_path = resolve_db(args.db, for_session=root) if (not args.no_track or args.db) else None
        label = args.label
        note = args.note
        if db_path is not None and (label is None or note is None) and db_path.exists():
            try:
                conn_l = _ensure_db(db_path)
                row = conn_l.execute(
                    "SELECT label, note FROM runs WHERE session_id = ?",
                    (root.session_id,),
                ).fetchone()
                if row:
                    prev_label, prev_note = row
                    if label is None and prev_label:
                        label = prev_label
                        print(f"label (preserved): {label}")
                    if note is None and prev_note:
                        note = prev_note
            except Exception:
                pass
        if label is None:
            label = git_label_for(root.cwd)
            if label:
                print(f"label (auto): {label}")

        # Resolve output dir: --out wins; else project's .tracer/traces/<run>.
        out_dir = resolve_output_dir(root, args.out, label_hint=label)
        out_dir.mkdir(parents=True, exist_ok=True)
        trace_json = out_dir / "trace.json"
        trace = out_dir / "trace.html"
        emit_trace_json(root, trace_json)
        emit_trace_html(root, trace)
        project_root_disp = session_project_root(root)
        print(f"project:   {project_root_disp}")
        print(f"out dir:   {out_dir}")
        print(f"  trace.json: source of truth")
        print(f"  trace.html: chronological view")
        if args.ascii:
            ascii_path = out_dir / "ascii.txt"
            emit_ascii(root, ascii_path)
            print(f"  ascii.txt")
        if args.print_ascii:
            print()
            print("\n".join(_ascii_lines(root, is_root=True)))
        sessions = list(root.all_sessions())
        print(
            f"\nsummary: {len(sessions)} session(s), "
            f"{sum(len(s.turns) for s in sessions)} turn(s), "
            f"cost-weighted {root.total_cost:,.0f} tok"
        )
        if not args.no_track and db_path is not None:
            track(root, db_path, label=label, note=note, trace_dir=out_dir)

    elif args.cmd == "ls":
        cwd = args.cwd or os.getcwd()
        db = project_db_path(active_project_root(cwd), create=False)
        rows = _run_rows(db, limit=args.limit)
        if not rows:
            print(f"no saved traces for project: {active_project_root(cwd, error_if_missing=False) or cwd}")
            return
        print(f"saved traces for {active_project_root(cwd)}:")
        print(f"  {'when':<18}{'label':<22}{'turns':>5} {'tokens':>10} {'model':<13} path")
        print("  " + "-" * 96)
        for row in rows:
            summary = _summary_for_path(Path(row["trace_json"])) if row.get("trace_json") else {}
            print(
                f"  {(row.get('started_at') or '')[:16]:<18}"
                f"{(row.get('label') or row.get('session_id') or '')[:21]:<22}"
                f"{summary.get('turns') or 0:>5} "
                f"{int(summary.get('cost_weighted') or 0):>10,} "
                f"{_shorten_model(_dominant_model(summary.get('model_mix') or {})):<13} "
                f"{row.get('trace_json') or ''}"
            )

    elif args.cmd == "sessions":
        cwd = args.cwd or os.getcwd()
        print_sessions_for_cwd(cwd, source=args.source, limit=args.limit)

    elif args.cmd == "version":
        print(tracer_version())

    elif args.cmd == "read":
        tj = _resolve_run_target(args.ref, resolve_db(args.db))
        if args.summary:
            print(json.dumps(_summary_for_path(tj), ensure_ascii=False, indent=2))
        else:
            print(tj.read_text(encoding="utf-8"))

    elif args.cmd == "clip":
        db_path = resolve_db(args.db)
        tj = _resolve_run_target(args.ref, db_path)
        root = load_trace_json(tj)
        if args.reset:
            root.clip = None
            print(f"clip reset on {tj}")
        else:
            total_before_clip = len(root.turns)
            clip = apply_clip_flags(
                root,
                clip_start=args.start,
                clip_end=args.end,
                clip_from=args.from_pattern,
                clip_reason=args.reason,
            )
            if clip is None:
                raise SystemExit("no clip flags given (use --start, --end, --from, or --reset)")
            kept = len(root.turns)
            print(f"hard clip applied to {tj}: kept original root turns "
                  f"{clip.start_turn or 1}..{clip.end_turn or total_before_clip} "
                  f"({kept}/{total_before_clip} kept)")
        # Preserve label/note from existing db row.
        prev_label = prev_note = None
        if db_path.exists():
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT label, note FROM runs WHERE session_id = ?",
                (root.session_id,),
            ).fetchone()
            if row:
                prev_label, prev_note = row
        # Re-emit JSON + re-render views in place.
        emit_trace_json(root, tj)
        out_dir = tj.parent
        emit_trace_html(root, out_dir / "trace.html")
        track(root, db_path, label=prev_label, note=prev_note, trace_dir=out_dir)
        print(f"re-rendered views in {out_dir}")

    elif args.cmd == "open":
        import subprocess
        if args.ref:
            tj = _resolve_run_target(args.ref, resolve_db(args.db))
            run_dir = tj.parent
        else:
            cwd = args.cwd or os.getcwd()
            matches = find_sessions_for_cwd(cwd)
            if not matches:
                raise SystemExit(f"no sessions for cwd: {cwd}")
            run_dir = _find_latest_run_dir(matches[0].session_id)
            if run_dir is None:
                raise SystemExit(
                    f"session {matches[0].session_id} not yet saved — "
                    f"run `tracer save` first"
                )
        target_map = {
            "trace": run_dir / "trace.html",
            "json": run_dir / "trace.json",
            "dir": run_dir,
        }
        target = target_map[args.view]
        if not target.exists():
            raise SystemExit(f"missing artifact: {target}")
        subprocess.run(["open", str(target)])
        print(f"opened: {target}")


def main_entry():
    main(sys.argv[1:])


if __name__ == "__main__":
    main_entry()
