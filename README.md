# Tracer

Tracer analyzes Claude Code and Codex session logs and turns them into stored trace artifacts:

- `trace.json` as the canonical machine-readable trace
- `trace.html` as a chronological browser view
- `.tracer/runs.db` as per-project history for comparing runs over time

It can be used as a CLI or as a local MCP server for Claude Code.

## Install

Recommended install from GitHub:

```bash
pipx install "git+https://github.com/alextacho/tracer.git"
```
or

```bash
pip install "git+https://github.com/alextacho/tracer.git"
```

After installation:

```bash
tracer --help
tracer version
```

If every user installs `tracer` on `PATH`, a shared project MCP config can use:

```json
{
  "mcpServers": {
    "tracer": {
      "type": "stdio",
      "command": "tracer",
      "args": ["mcp", "serve"]
    }
  }
}
```

Because the package installs a console script named `tracer`, the MCP command stays stable as `tracer mcp serve`.

## Project Setup

From a project you want to trace:

```bash
tracer init
```

This creates:

```text
.tracer/
  runs.db
  traces/
```

Trace artifacts can be large, so `tracer init` can add `.tracer/traces/` to `.gitignore`.

## CLI Usage

List recent Claude Code and Codex sessions for the current directory:

```bash
tracer ls
tracer ls --source codex
```

Analyze the latest session for the current directory:

```bash
tracer analyze --label "my-experiment" --note "What changed in this attempt"
```

Analyze a specific session:

```bash
tracer analyze <session-id-or-jsonl-path> --label "baseline"
tracer analyze --source codex --label "codex-run"
```

Open the latest analyzed trace:

```bash
tracer open
```

Open a specific artifact:

```bash
tracer open baseline --view trace
tracer open baseline --view json
tracer open baseline --view dir
```

Show stored history:

```bash
tracer history
```

Compare two stored runs:

```bash
tracer diff baseline candidate
```

Rank recent runs by cost:

```bash
tracer compare
```

Resolve artifact paths:

```bash
tracer path baseline --view json
tracer path baseline --view trace
tracer path baseline --view dir
```

Hard clip irrelevant setup or trailing turns out of an existing trace:

```bash
tracer clip baseline --start 3 --reason "ignore initial setup"
tracer clip baseline --from "tool:Edit" --reason "start at implementation"
tracer clip baseline --reset
```

Clipping rewrites the trace to keep only the selected root turns. `--from`
uses the most recent matching turn, so repeated text anchors clip from the
latest occurrence. Subagent output attached to kept turns is preserved.

Tool results inside the retained clip are stored fully in `trace.json`.
`trace.html` shows compact previews by default and exposes full results on
demand, so the JSON artifact remains faithful even if the original Claude
JSONL logs or files read during the session are later removed or changed.

## MCP Server

Tracer includes a stdio MCP server for Claude Code.

Start it manually:

```bash
tracer mcp serve
```

Normally you do not run this by hand. Claude Code starts it after installation.

## Install MCP In Claude Code

Install for the current Claude Code project/session:

```bash
tracer mcp install --scope local
```

Install for your user:

```bash
tracer mcp install --scope user
```

Install into project MCP config:

```bash
tracer mcp install --scope project
```

Then verify in Claude Code:

```text
/mcp
```

Check or remove the registration:

```bash
tracer mcp status
tracer mcp remove
```

## MCP Tools

The MCP server exposes these tools to Claude Code:

- `trace_create`: analyze a Claude Code session, emit `trace.json` and `trace.html`, and store it in history
- `trace_history`: list stored traces for the current project
- `trace_compare`: compare two stored traces by label, session id, run dir, or `trace.json` path
- `trace_progress`: compare one trace against a previous or best comparable trace and summarize progress/regressions
- `trace_suggest_improvements`: return optimization suggestions for a stored trace
- `trace_path`: resolve a stored trace reference to an artifact path
- `trace_open`: open a stored trace artifact with the local OS

Example Claude Code requests after MCP is connected:

```text
Use tracer to create a trace for the latest session and summarize it.
```

```text
Use tracer to compare the latest trace against the previous same-command trace.
```

```text
Use tracer to open the latest trace HTML.
```

## MCP Tool Inputs

Common references accepted by MCP tools:

- a session id or session id prefix
- a stored label
- a run directory
- a direct `trace.json` path
- `latest`

`trace_create` accepts:

```json
{
  "session": "optional session id or JSONL path",
  "cwd": "optional project cwd",
  "label": "short label",
  "note": "what changed",
  "clip_start": 3,
  "clip_end": 1,
  "clip_from": "tool:Edit",
  "clip_reason": "ignore setup",
  "emit_ascii_artifact": false
}
```

`clip_from` starts at the most recent matching root turn. Clip options hard
trim the emitted trace rather than hiding excluded turns.

`trace_open` accepts:

```json
{
  "ref": "latest",
  "view": "trace"
}
```

`view` can be `trace`, `json`, `ascii`, or `dir`.

## Storage Model

Tracer stores history per project:

```text
<project>/.tracer/
  runs.db
  traces/
    <timestamp>__<session-prefix>__<label>/
      trace.json
      trace.html
      ascii.txt
```

The SQLite database stores run metadata and paths. The trace directory stores the detailed artifacts.

## Notes

- Tracer reads Claude Code logs from `~/.claude/projects` and Codex rollout logs from `~/.codex/sessions`.
- `trace.json` is the source of truth, including full retained tool results; `trace.html` can be regenerated with `tracer render`.
- MCP `trace_open` uses macOS `open`. In headless environments it may return the path without successfully opening the artifact.
