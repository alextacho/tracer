from __future__ import annotations

import socket
import subprocess
import sys
import time
import webbrowser
from collections.abc import Sequence
from os import environ
from pathlib import Path

from .paths import LabContext


WATCH_SUFFIXES = {".py", ".css", ".js", ".html"}


def available_port(preferred: int = 8768) -> int:
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No available local port found")


def start_detached_server(ctx: LabContext, *, open_browser: bool = True) -> str:
    port = available_port()
    command = [
        sys.executable,
        "-m",
        "tracer.lab.cli",
        "open",
        "--port",
        str(port),
        "--no-browser",
    ]
    if ctx.direct_trace is not None:
        command.append(str(ctx.direct_trace))
    elif ctx.cwd is not None:
        command.extend(["--cwd", str(ctx.cwd)])
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return url


def watched_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        if path.suffix in WATCH_SUFFIXES:
            files.append(path)
    return files


def latest_mtime(paths: Sequence[Path]) -> float:
    latest = 0.0
    for path in paths:
        try:
            latest = max(latest, path.stat().st_mtime)
        except FileNotFoundError:
            return time.time()
    return latest


def run_reloader(command: list[str], *, watch_root: Path, url: str, open_browser: bool) -> int:
    print(f"Tracer Lab: reload watching {watch_root}")
    print(f"Tracer Lab: {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    env = dict(environ)
    env["TRACER_LAB_RELOAD_CHILD"] = "1"
    last_seen = latest_mtime(watched_files(watch_root))
    child: subprocess.Popen | None = None
    try:
        while True:
            child = subprocess.Popen(command, env=env)
            while child.poll() is None:
                time.sleep(0.5)
                current = latest_mtime(watched_files(watch_root))
                if current > last_seen:
                    last_seen = current
                    print("Tracer Lab: change detected, restarting")
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    break
            else:
                return child.returncode or 0
    except KeyboardInterrupt:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        return 0
