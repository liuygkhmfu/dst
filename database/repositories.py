from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import Database


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


class Repository:
    def __init__(self, db: Database):
        self.db = db

    def create_project(self, project: dict[str, Any]) -> None:
        with self.db.connection() as conn:
            conn.execute(
                """INSERT INTO projects
                (id, product_name, product_description, is_series, product_count,
                 custom_scene, display_requirements, product_dimensions,
                 input_product_path, input_series_path, output_dir, status, created_at, updated_at)
                VALUES (:id,:product_name,:product_description,:is_series,:product_count,
                 :custom_scene,:display_requirements,:product_dimensions,
                 :input_product_path,:input_series_path,:output_dir,:status,:created_at,:updated_at)""",
                project,
            )

    def create_task(self, task: dict[str, Any]) -> None:
        with self.db.connection() as conn:
            conn.execute(
                """INSERT INTO image_tasks
                (id,project_id,slot_id,task_name,task_kind,prompt_group,original_prompt,current_prompt,
                 reference_fields_json,status,selected_version_id,last_error,created_at,updated_at)
                VALUES (:id,:project_id,:slot_id,:task_name,:task_kind,:prompt_group,:original_prompt,:current_prompt,
                 :reference_fields_json,:status,:selected_version_id,:last_error,:created_at,:updated_at)""",
                task,
            )

    def create_extra_request(self, item: dict[str, Any]) -> None:
        with self.db.connection() as conn:
            conn.execute(
                """INSERT INTO extra_requests
                (id,project_id,request_index,requirement,reference_paths_json,created_at)
                VALUES (:id,:project_id,:request_index,:requirement,:reference_paths_json,:created_at)""",
                item,
            )

    def list_projects(self) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projects WHERE deleted_at IS NULL ORDER BY created_at DESC")]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND deleted_at IS NULL", (project_id,)).fetchone()
            return self.db.one(row)

    def get_tasks(self, project_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            tasks = [dict(r) for r in conn.execute("SELECT * FROM image_tasks WHERE project_id=? ORDER BY slot_id", (project_id,))]
            for task in tasks:
                task["reference_fields"] = _loads(task.pop("reference_fields_json", "[]"), [])
                task["versions"] = [dict(v) for v in conn.execute("SELECT * FROM image_versions WHERE task_id=? ORDER BY version_number", (task["id"],))]
                for version in task["versions"]:
                    version["is_approved"] = bool(version["is_approved"])
            return tasks

    def get_extra_requests(self, project_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            result = []
            for row in conn.execute("SELECT * FROM extra_requests WHERE project_id=? ORDER BY request_index", (project_id,)):
                item = dict(row)
                item["reference_paths"] = _loads(item.pop("reference_paths_json", "[]"), [])
                result.append(item)
            return result

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM image_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            task = dict(row)
            task["reference_fields"] = _loads(task.pop("reference_fields_json", "[]"), [])
            task["versions"] = [dict(v) for v in conn.execute("SELECT * FROM image_versions WHERE task_id=? ORDER BY version_number", (task_id,))]
            return task

    def update_project(self, project_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        columns = ", ".join(f"{key} = :{key}" for key in fields)
        fields["project_id"] = project_id
        with self.db.connection() as conn:
            conn.execute(f"UPDATE projects SET {columns} WHERE id=:project_id", fields)

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "reference_fields" in fields:
            fields["reference_fields_json"] = json.dumps(fields.pop("reference_fields"), ensure_ascii=False)
        fields["updated_at"] = now_iso()
        columns = ", ".join(f"{key} = :{key}" for key in fields)
        fields["task_id"] = task_id
        with self.db.connection() as conn:
            conn.execute(f"UPDATE image_tasks SET {columns} WHERE id=:task_id", fields)

    def next_version_number(self, task_id: str) -> int:
        with self.db.connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(version_number),0)+1 AS n FROM image_versions WHERE task_id=?", (task_id,)).fetchone()
            return int(row["n"])

    def create_version(self, version: dict[str, Any]) -> None:
        with self.db.connection() as conn:
            conn.execute(
                """INSERT INTO image_versions
                (id,task_id,version_number,mode,parent_version_id,file_path,prompt,change_request,model,size,quality,api_usage_json,is_approved,created_at)
                VALUES (:id,:task_id,:version_number,:mode,:parent_version_id,:file_path,:prompt,:change_request,:model,:size,:quality,:api_usage_json,:is_approved,:created_at)""",
                version,
            )

    def select_version(self, task_id: str, version_id: str) -> None:
        with self.db.connection() as conn:
            conn.execute("UPDATE image_versions SET is_approved=0 WHERE task_id=?", (task_id,))
            conn.execute("UPDATE image_versions SET is_approved=1 WHERE id=? AND task_id=?", (version_id, task_id))
            conn.execute("UPDATE image_tasks SET selected_version_id=?, status='approved', updated_at=? WHERE id=?", (version_id, now_iso(), task_id))

    def create_log(self, item: dict[str, Any]) -> int:
        with self.db.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO operation_logs
                (project_id,task_id,version_number,operation,model,started_at,finished_at,status_code,duration_ms,attempt_count,error_type,error_message)
                VALUES (:project_id,:task_id,:version_number,:operation,:model,:started_at,:finished_at,:status_code,:duration_ms,:attempt_count,:error_type,:error_message)""",
                item,
            )
            return int(cursor.lastrowid)
