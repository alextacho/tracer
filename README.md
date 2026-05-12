# Tracer

Tracer analyzes Claude Code session logs and turns them into stored trace artifacts:

- `trace.json` as the canonical machine-readable trace
- `trace.html` as a chronological browser view
- `.tracer/runs.db` as per-project history for comparing runs over time

It can be used as a CLI or as a local MCP server for Claude Code.

## Install

Recommended install from GitHub:

```bash
pipx install "git+https://github.com/alextacho/tracer.git"
```

For a specific branch or tag:

```bash
pipx install "git+https://github.com/alextacho/tracer.git@main"
pipx install "git+https://github.com/alextacho/tracer.git@v0.1.0"
```

For a private repo over SSH:

```bash
pipx install "git+ssh://git@github.com/alextacho/tracer.git@main"
```

Plain `pip` also works, though `pipx` is better for CLI tools:

```bash
pip install "git+https://github.com/alextacho/tracer.git"
```

After installation:

```bash
tracer --help
```

For local development, clone or copy this repository, then run Tracer from the repository directory:

```bash
./tracer --help
```

Optional shell install from the repository directory:

```bash
mkdir -p ~/.local/bin
ln -s "$PWD/tracer" ~/.local/bin/tracer
```

After that, make sure `~/.local/bin` is on your `PATH`.

Alternatively, keep Tracer unlinked and run commands as `./tracer ...` from the repository.

## Distribute

Tracer is packaged as a Python CLI. You can distribute it without publishing to PyPI by installing directly from GitHub:

```bash
pipx install "git+https://github.com/alextacho/tracer.git@main"
```

To distribute a stable release, create a git tag and install that tag:

```bash
git tag v0.1.0
git push origin v0.1.0
pipx install "git+https://github.com/alextacho/tracer.git@v0.1.0"
```

Users can upgrade with:

```bash
pipx upgrade claude-code-tracer
```

The installed command is:

```bash
tracer
```

Recommended team flow:

1. Put this repository in git.
2. Tag releases.
3. Ask users to install with `pipx install "git+https://github.com/alextacho/tracer.git@<tag>"`.
4. Ask each user to run `tracer mcp install --scope user` or `tracer mcp install --scope local` on their machine.

Do not commit a user-specific MCP registration that points at one person's checkout path. MCP stdio commands are executed locally, so the path must exist on the machine running Claude Code.

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

List recent Claude Code sessions for the current directory:

```bash
tracer ls
```

Analyze the latest session for the current directory:

```bash
tracer analyze --label "my-experiment" --note "What changed in this attempt"
```

Analyze a specific session:

```bash
tracer analyze <session-id-or-jsonl-path> --label "baseline"
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

Clip irrelevant setup or trailing turns out of an existing trace:

```bash
tracer clip baseline --start 3 --reason "ignore initial setup"
tracer clip baseline --from "tool:Edit" --reason "start at implementation"
tracer clip baseline --reset
```

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

- Tracer reads Claude Code logs from `~/.claude/projects`.
- `trace.json` is the source of truth; `trace.html` can be regenerated with `tracer render`.
- MCP `trace_open` uses macOS `open`. In headless environments it may return the path without successfully opening the artifact.
