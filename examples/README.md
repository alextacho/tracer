# Tracer Examples

This directory contains small public fixtures for testing Tracer without private agent logs.

## Lightweight Core Smoke

Create trace artifacts from the sample Codex session:

```bash
tracer save examples/codex-smoke-session.jsonl \
  --source codex \
  --out /tmp/tracer-sample \
  --no-track \
  --label sample \
  --ascii
```

Inspect the AI-readable summary:

```bash
tracer read /tmp/tracer-sample/trace.json --summary
```

Inspect the human-readable trace:

```bash
tracer open /tmp/tracer-sample/trace.json
```

Open it in Lab for review:

```bash
tracer-lab open /tmp/tracer-sample/trace.json
```

