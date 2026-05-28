from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS traces (
  id TEXT PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  html_path TEXT,
  session_id TEXT,
  cwd TEXT,
  command TEXT,
  started_at TEXT,
  first_user_prompt TEXT,
  summary_json TEXT NOT NULL DEFAULT '{}',
  imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS authors (
  id TEXT PRIMARY KEY,
  author_type TEXT NOT NULL CHECK (author_type IN ('human', 'agent')),
  name TEXT NOT NULL,
  model_identity TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(author_type, name, model_identity)
);

CREATE TABLE IF NOT EXISTS annotations (
  id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
  target_type TEXT NOT NULL CHECK (target_type IN ('trace', 'session', 'turn', 'event')),
  target_id TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('note', 'label', 'failure_mode_assignment')),
  body TEXT NOT NULL DEFAULT '',
  label TEXT,
  status TEXT NOT NULL CHECK (status IN ('accepted', 'pending', 'rejected')) DEFAULT 'accepted',
  author_id TEXT NOT NULL REFERENCES authors(id),
  origin_annotation_id TEXT REFERENCES annotations(id),
  reviewed_by_author_id TEXT REFERENCES authors(id),
  reviewed_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS failure_modes (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS failure_mode_links (
  failure_mode_id TEXT NOT NULL REFERENCES failure_modes(id) ON DELETE CASCADE,
  trace_id TEXT REFERENCES traces(id) ON DELETE CASCADE,
  annotation_id TEXT REFERENCES annotations(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (failure_mode_id, trace_id, annotation_id)
);

CREATE INDEX IF NOT EXISTS idx_annotations_trace ON annotations(trace_id, status, kind);
CREATE INDEX IF NOT EXISTS idx_annotations_origin ON annotations(origin_annotation_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
