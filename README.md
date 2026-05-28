# Tracer

Tracer turns coding-agent sessions into durable traces you can save, open, and read.

Use it when Claude Code, Codex, or another coding agent says a task is done and you need a stable artifact of what actually happened: prompts, tool calls, evidence, test output, and cost/context shape.

Tracer Core is intentionally small:

- `tracer save` creates `trace.json` and `trace.html` artifacts.
- `tracer ls` lists saved traces with useful metadata.
- `tracer sessions` lists raw Claude Code and Codex sessions available to save.
- `tracer open` opens the human-readable `trace.html`.
- `tracer read` prints the machine-readable `trace.json` for agents.
- `tracer clip` trims traces after creation.

Tracer Lab is the bundled experimental review/error-analysis tool:

- `tracer-lab open` imports traces and opens the local review surface.
- `tracer-lab mcp serve` exposes annotations, suggestions, and failure-mode clustering to agents.

## Install

Recommended install from GitHub:

```bash
pipx install "git+https://github.com/alextacho/tracer.git"
```

or:

```bash
pip install "git+https://github.com/alextacho/tracer.git"
```

After installation:

```bash
tracer --help
tracer version
```

## Quickstart

From a project you want to trace:

```bash
tracer ls
tracer save --label baseline --note "first reviewable run"
tracer open baseline
```

This creates project-local storage:

```text
.tracer/
  runs.db
  traces/
    <run>/
      trace.json
      trace.html
      ascii.txt
```

`trace.json` is the source of truth. `trace.html` is the chronological browser view.

## Sample

From a source checkout, run Tracer against the included public sample session:

```bash
tracer save examples/codex-smoke-session.jsonl \
  --source codex \
  --out /tmp/tracer-sample \
  --no-track \
  --label sample \
  --ascii
tracer read /tmp/tracer-sample/trace.json --summary
```

The same generated `trace.json` can be opened in Lab:

```bash
tracer-lab open /tmp/tracer-sample/trace.json
```

## CLI

List saved traces for the current project:

```bash
tracer ls
```

List recent raw Claude Code and Codex sessions for the current directory:

```bash
tracer sessions
tracer sessions --source codex
```

Save the latest session as trace artifacts:

```bash
tracer save --label "my-experiment" --note "What changed in this attempt"
```

Save a specific session:

```bash
tracer save <session-id-or-jsonl-path> --label baseline
tracer save --source codex --label codex-run
```

Open saved artifacts:

```bash
tracer open baseline --view trace
tracer open baseline --view json
tracer open baseline --view dir
```

Read the trace JSON for an agent or script:

```bash
tracer read baseline
tracer read baseline --summary
```

Clip irrelevant setup or trailing turns after a trace has been created:

```bash
tracer clip baseline --start 3 --reason "ignore initial setup"
tracer clip baseline --from "tool:Edit" --reason "start at implementation"
tracer clip baseline --reset
```

Clipping rewrites the trace to keep only the selected root turns. `--from` uses the most recent matching turn, so repeated text anchors clip from the latest occurrence. Subagent output attached to kept turns is preserved.

Tool results inside the retained clip are stored fully in `trace.json`. `trace.html` shows compact previews by default and exposes full results on demand, so the JSON artifact remains faithful even if the original logs or files read during the session are later removed or changed.

## Lab

Tracer Lab is the bundled experimental local review and error-analysis surface. It imports saved traces and lets humans or agents create:

- notes
- labels
- pending suggestions
- failure observations
- failure-mode clusters
- eval candidate exports

Agent-authored suggestions are pending until a human accepts, edits, or rejects them.

Run it separately from Tracer Core:

```bash
tracer-lab open
tracer-lab open /path/to/trace.json
```

Lab review state is stored at:

```text
.tracer/lab.db
```

## MCP Servers

Each tool has its own MCP server.

Tracer Core MCP:

```bash
tracer mcp serve
```

Tracer Lab MCP:

```bash
tracer-lab mcp serve
```

Install Tracer Core into Claude Code:

```bash
tracer mcp install --scope local
tracer mcp install --scope user
tracer mcp install --scope project
```

Install Tracer Lab into Claude Code:

```bash
tracer-lab mcp install --scope local
tracer-lab mcp install --scope user
tracer-lab mcp install --scope project
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

## Core MCP Tools

`tracer mcp serve` exposes only core trace storage and access:

- `trace_save`: create/store trace artifacts.
- `trace_list`: list saved traces and metadata.
- `trace_read`: read a stored `trace.json`.
- `trace_open`: open a stored trace artifact with the local OS.
- `trace_clip`: clip an existing stored trace or create a clipped trace from a session source.

## Lab MCP Tools

`tracer-lab mcp serve` exposes review and analysis workflows:

- `tracer_lab_open`: open Lab for a project or trace.
- `tracer_lab_list_traces`: list imported/discovered traces.
- `tracer_lab_get_trace`: get trace metadata, annotations, and optionally raw `trace.json`.
- `tracer_lab_create_suggestion`: create an agent-authored pending note, label, or failure suggestion.
- `tracer_lab_list_failure_modes`: list failure modes.
- `tracer_lab_failure_mode_workspace`: list reviewed failures and cluster state.
- `tracer_lab_create_failure_mode`: create a failure-mode cluster.
- `tracer_lab_update_failure_mode`: update a failure-mode cluster.
- `tracer_lab_assign_failure`: assign or unassign reviewed failure annotations.
- `tracer_lab_export_eval_candidates`: export failure-mode support data.

Example agent requests after MCP is connected:

```text
Use tracer to save the latest session and read the resulting trace JSON.
```

```text
Use tracer-lab to review the latest trace and add pending suggestions for evidence gaps.
```

```text
Use tracer to clip the latest trace from the first Edit tool call, then reopen it in Lab.
```

## Notes

- Tracer reads Claude Code logs from `~/.claude/projects` and Codex rollout logs from `~/.codex/sessions`.
- `trace.json` is the source of truth and includes full retained tool results.
- MCP `trace_open` uses macOS `open`; in headless environments it may return the path without successfully opening the artifact.
