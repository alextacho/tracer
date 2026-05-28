from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_MARKERS = (".tracer", ".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")
TRACER_DIR = ".tracer"
LAB_DB_FILE = "lab.db"


@dataclass(frozen=True)
class LabContext:
    cwd: Path
    project_root: Path | None
    db_path: Path
    direct_trace: Path | None = None


def find_project_root(start: str | Path | None) -> Path | None:
    if start is None:
        start_path = Path.cwd()
    else:
        start_path = Path(start).expanduser()
    try:
        path = start_path.resolve()
    except OSError:
        return None
    if path.is_file():
        path = path.parent

    while True:
        if any((path / marker).exists() for marker in PROJECT_MARKERS):
            return path
        if path == path.parent:
            return None
        path = path.parent


def context_for(cwd: str | Path | None = None, trace_json: str | Path | None = None) -> LabContext:
    direct_trace = Path(trace_json).expanduser().resolve() if trace_json else None
    base = direct_trace.parent if direct_trace else Path(cwd or Path.cwd()).expanduser().resolve()
    project_root = find_project_root(base)

    if direct_trace is None and project_root is None and base.is_dir() and (base / "trace.json").exists():
        direct_trace = (base / "trace.json").resolve()

    if direct_trace is not None:
        db_path = direct_trace.parent / TRACER_DIR / LAB_DB_FILE
    elif project_root is not None:
        db_path = project_root / TRACER_DIR / LAB_DB_FILE
    else:
        db_path = Path.home() / TRACER_DIR / LAB_DB_FILE

    return LabContext(cwd=base, project_root=project_root, db_path=db_path, direct_trace=direct_trace)
