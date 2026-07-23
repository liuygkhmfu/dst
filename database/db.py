from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    product_description TEXT NOT NULL DEFAULT '',
    is_series INTEGER NOT NULL DEFAULT 0,
    product_count INTEGER NOT NULL DEFAULT 1,
    custom_scene TEXT NOT NULL DEFAULT '',
    display_requirements TEXT NOT NULL DEFAULT '',
    product_dimensions TEXT NOT NULL DEFAULT '',
    input_product_path TEXT NOT NULL,
    input_series_path TEXT,
    output_dir TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS image_tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    task_name TEXT NOT NULL,
    task_kind TEXT NOT NULL DEFAULT 'workflow',
    prompt_group TEXT NOT NULL,
    original_prompt TEXT NOT NULL DEFAULT '',
    current_prompt TEXT NOT NULL DEFAULT '',
    reference_fields_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    selected_version_id TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, slot_id)
);
CREATE TABLE IF NOT EXISTS image_versions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    version_number INTEGER NOT NULL,
    mode TEXT NOT NULL,
    parent_version_id TEXT,
    file_path TEXT NOT NULL,
    prompt TEXT NOT NULL,
    change_request TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    size TEXT NOT NULL DEFAULT '',
    quality TEXT NOT NULL DEFAULT '',
    api_usage_json TEXT NOT NULL DEFAULT '{}',
    is_approved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, version_number)
);
CREATE TABLE IF NOT EXISTS extra_requests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    request_index INTEGER NOT NULL,
    requirement TEXT NOT NULL,
    reference_paths_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(project_id, request_index)
);
CREATE TABLE IF NOT EXISTS operation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    task_id TEXT,
    version_number INTEGER,
    operation TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status_code INTEGER,
    duration_ms INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    error_type TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT ''
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def connection(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    @staticmethod
    def one(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None
