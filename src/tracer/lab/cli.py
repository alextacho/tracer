from __future__ import annotations

import argparse
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from .cli_support import available_port, run_reloader
from .db import connect, init_db
from .mcp_server import serve as serve_mcp
from .paths import context_for
from .traces import discover_trace_paths, import_discovered
from .web import create_server


def cmd_open(args: argparse.Namespace) -> int:
    trace_path = Path(args.trace).expanduser().resolve() if args.trace else None
    ctx = context_for(args.cwd, trace_path)
    port = args.port or available_port()
    if args.reload and os.environ.get("TRACER_LAB_RELOAD_CHILD") != "1":
        command = [
            sys.executable,
            "-m",
            "tracer.lab.cli",
            "open",
            "--port",
            str(port),
            "--no-browser",
        ]
        if trace_path is not None:
            command.append(str(trace_path))
        elif args.cwd:
            command.extend(["--cwd", args.cwd])
        return run_reloader(
            command,
            watch_root=Path(__file__).parent,
            url=f"http://127.0.0.1:{port}",
            open_browser=args.browser,
        )

    server = create_server(ctx, port=port)
    url = f"http://127.0.0.1:{port}"
    print(f"Tracer Lab: db {ctx.db_path}")
    print(f"Tracer Lab: {url}")
    if args.browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    trace_path = Path(args.trace).expanduser().resolve() if args.trace else None
    ctx = context_for(args.cwd, trace_path)
    init_db(ctx.db_path)
    with connect(ctx.db_path) as conn:
        imported = import_discovered(conn, discover_trace_paths(ctx.project_root, ctx.direct_trace))
    print(f"imported {len(imported)} trace(s) into {ctx.db_path}")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    ctx = context_for(args.cwd)
    serve_mcp(ctx)
    return 0


def cmd_install_mcp(args: argparse.Namespace) -> int:
    command = [sys.executable, "-m", "tracer.lab.cli", "mcp", "serve"]
    subprocess.run(["claude", "mcp", "add", args.name, "--scope", args.scope, "--"] + command, check=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracer-lab")
    sub = parser.add_subparsers(dest="cmd")

    open_parser = sub.add_parser("open", help="start the local Tracer Lab UI")
    open_parser.add_argument("trace", nargs="?", help="optional direct path to trace.json")
    open_parser.add_argument("--cwd", help="project directory to inspect")
    open_parser.add_argument("--port", type=int)
    open_parser.add_argument("--reload", action="store_true", help="restart the server when Tracer Lab source files change")
    open_parser.add_argument("--no-browser", dest="browser", action="store_false")
    open_parser.set_defaults(func=cmd_open, browser=True)

    import_parser = sub.add_parser("import", help="import traces without starting the UI")
    import_parser.add_argument("trace", nargs="?")
    import_parser.add_argument("--cwd")
    import_parser.set_defaults(func=cmd_import)

    mcp_parser = sub.add_parser("mcp", help="MCP server commands")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_cmd", required=True)
    serve_parser = mcp_sub.add_parser("serve", help="run stdio MCP server")
    serve_parser.add_argument("--cwd")
    serve_parser.set_defaults(func=cmd_mcp)

    install_parser = mcp_sub.add_parser("install", help="install Tracer Lab into Claude Code MCP config")
    install_parser.add_argument("--scope", default="local", choices=["local", "project", "user"])
    install_parser.add_argument("--name", default="tracer-lab")
    install_parser.set_defaults(func=cmd_install_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        args = parser.parse_args(["open"] + (argv or []))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
