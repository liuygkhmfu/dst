from __future__ import annotations

import json
import mimetypes
import os
import sys
import traceback
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from config.runtime_config import PROMPT_GROUPS
from config.settings import get_settings
from config.task_definitions import TASK_DEFINITIONS
from database.db import Database
from database.repositories import Repository
from services.generation_service import GenerationService
from services.prompt_service import PROMPT_VARIABLES
from services.storage_service import StorageService, read_image_info


ROOT = Path(__file__).resolve().parent
SETTINGS = get_settings(ROOT)
DB = Database(SETTINGS.db_path)
REPO = Repository(DB)
STORAGE = StorageService(SETTINGS.output_root)
GENERATOR = GenerationService(SETTINGS, REPO, STORAGE)

EDITABLE_ENV_KEYS = {
    "IMAGE_API_BASE_URL", "IMAGE_API_KEY", "IMAGE_MODEL", "IMAGE_SIZE", "IMAGE_QUALITY", "IMAGE_OUTPUT_FORMAT", "IMAGE_BACKGROUND", "IMAGE_MODERATION",
    "PROMPT_API_BASE_URL", "PROMPT_API_KEY", "PROMPT_MODEL", "MAX_UPLOAD_MB", "GENERATION_CONCURRENCY", "API_TIMEOUT_SECONDS", "MOCK_MODE",
    "OUTPUT_ROOT",
}


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")


def clean_task(task: dict) -> dict:
    task = dict(task)
    for version in task.get("versions", []):
        version["is_approved"] = bool(version.get("is_approved"))
    return task


def project_payload(project_id: str) -> dict:
    project = REPO.get_project(project_id)
    if not project:
        raise KeyError("项目不存在")
    tasks = [clean_task(t) for t in REPO.get_tasks(project_id)]
    return {**project, "tasks": tasks, "extra_requests": REPO.get_extra_requests(project_id)}


def masked_secret(value: str) -> str:
    value = str(value or "")
    return f"••••{value[-4:]}" if value else ""


def content_disposition(disposition: str, filename: str) -> str:
    """Build a browser-safe header for Chinese filenames without raw Unicode bytes."""
    suffix = Path(filename).suffix or ".bin"
    fallback = f"download{suffix.lower()}"
    encoded = quote(str(filename), safe="")
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def settings_payload() -> dict:
    definitions = GENERATOR.prompt_service.task_definitions()
    return {
        "image_api_base_url": SETTINGS.image_api_base_url,
        "output_root": str(SETTINGS.output_root),
        "image_model": SETTINGS.image_model,
        "image_size": SETTINGS.image_size,
        "image_quality": SETTINGS.image_quality,
        "image_output_format": SETTINGS.image_output_format,
        "image_background": SETTINGS.image_background,
        "image_moderation": SETTINGS.image_moderation,
        "prompt_api_base_url": SETTINGS.prompt_api_base_url,
        "prompt_model": SETTINGS.prompt_model,
        "generation_concurrency": SETTINGS.generation_concurrency,
        "api_timeout_seconds": SETTINGS.api_timeout_seconds,
        "max_upload_mb": SETTINGS.max_upload_mb,
        "mock_mode": SETTINGS.mock_mode,
        "image_key_configured": bool(SETTINGS.image_api_key),
        "prompt_key_configured": bool(SETTINGS.prompt_api_key),
        "image_key_masked": masked_secret(SETTINGS.image_api_key),
        "prompt_key_masked": masked_secret(SETTINGS.prompt_api_key),
        "group_constraints": GENERATOR.config_store.group_constraints(),
        "prompt_groups": [
            {"key": key, **PROMPT_GROUPS[key]}
            for key in ("size", "atmosphere", "scene")
        ],
        "prompt_variables": PROMPT_VARIABLES,
        "task_templates": definitions,
    }


def save_env_values(updates: dict[str, str]) -> None:
    """Update only whitelisted local .env values without returning secrets to the UI."""
    env_path = ROOT / ".env"
    existing: dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            existing[key.strip()] = value.strip()
    existing.update({key: str(value) for key, value in updates.items() if key in EDITABLE_ENV_KEYS})
    content = "\n".join(f"{key}={value}" for key, value in existing.items()) + "\n"
    STORAGE.atomic_write(env_path, content.encode("utf-8"))
    for key, value in updates.items():
        if key in EDITABLE_ENV_KEYS:
            if value == "":
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)


def apply_settings(payload: dict) -> dict:
    global SETTINGS, STORAGE
    env_updates: dict[str, str] = {}
    mapping = {
        "output_root": "OUTPUT_ROOT",
        "image_api_base_url": "IMAGE_API_BASE_URL", "image_model": "IMAGE_MODEL", "image_size": "IMAGE_SIZE", "image_quality": "IMAGE_QUALITY",
        "image_output_format": "IMAGE_OUTPUT_FORMAT", "image_background": "IMAGE_BACKGROUND", "image_moderation": "IMAGE_MODERATION",
        "prompt_api_base_url": "PROMPT_API_BASE_URL", "prompt_model": "PROMPT_MODEL", "generation_concurrency": "GENERATION_CONCURRENCY",
        "api_timeout_seconds": "API_TIMEOUT_SECONDS", "max_upload_mb": "MAX_UPLOAD_MB", "mock_mode": "MOCK_MODE",
    }
    for field, env_key in mapping.items():
        if field in payload and payload[field] is not None:
            value = payload[field]
            if field == "mock_mode":
                value = "true" if bool(value) else "false"
            env_updates[env_key] = str(value).strip()
    for field, env_key, clear_key in [("image_api_key", "IMAGE_API_KEY", "clear_image_key"), ("prompt_api_key", "PROMPT_API_KEY", "clear_prompt_key")]:
        if payload.get(clear_key):
            env_updates[env_key] = ""
        elif str(payload.get(field, "")).strip():
            env_updates[env_key] = str(payload[field]).strip()
    save_env_values(env_updates)
    runtime = payload.get("runtime", {}) if isinstance(payload.get("runtime"), dict) else payload
    task_briefs = runtime.get("task_briefs")
    group_constraints = runtime.get("group_constraints")
    if task_briefs is not None or group_constraints is not None:
        GENERATOR.config_store.save(group_constraints=group_constraints, task_briefs=task_briefs)
    SETTINGS = get_settings(ROOT)
    STORAGE = StorageService(SETTINGS.output_root)
    GENERATOR.storage = STORAGE
    GENERATOR.settings = SETTINGS
    GENERATOR.prompt_service.settings = SETTINGS
    GENERATOR.image_service.settings = SETTINGS
    return settings_payload()


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, list[str]], dict[str, list[dict]]]:
    message = BytesParser(policy=default).parsebytes((f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
    fields: dict[str, list[str]] = {}
    files: dict[str, list[dict]] = {}
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        data = part.get_payload(decode=True) or b""
        if filename is not None:
            files.setdefault(name, []).append({"filename": filename, "content": data, "content_type": part.get_content_type()})
        else:
            charset = part.get_content_charset() or "utf-8"
            fields.setdefault(name, []).append(data.decode(charset, errors="replace"))
    return fields, files


class Handler(BaseHTTPRequestHandler):
    server_version = "EcommerceImageWorkbench/1.0"

    def log_message(self, format: str, *args) -> None:
        sys.stdout.write("[web] " + (format % args) + "\n")

    def send_data(self, data: bytes, content_type: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, value: object, status: int = 200) -> None:
        self.send_data(json_bytes(value), "application/json; charset=utf-8", status)

    def error(self, message: str, status: int = 400) -> None:
        self.send_json({"error": message}, status)

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        max_bytes = SETTINGS.max_upload_mb * 1024 * 1024 * 40
        if length > max_bytes:
            raise ValueError("请求体过大")
        return self.rfile.read(length)

    def read_json(self) -> dict:
        raw = self.read_body()
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path in {"/", "/index.html"}:
                self.send_data((ROOT / "ui" / "index.html").read_bytes(), "text/html; charset=utf-8")
                return
            if path == "/api/projects":
                self.send_json(REPO.list_projects())
                return
            if path == "/api/task-definitions":
                self.send_json(GENERATOR.prompt_service.task_definitions())
                return
            if path == "/api/settings":
                self.send_json(settings_payload())
                return
            if path.startswith("/api/projects/"):
                project_id = path.rsplit("/", 1)[-1]
                self.send_json(project_payload(project_id))
                return
            if path == "/api/files":
                query = parse_qs(parsed.query)
                project_id = query.get("project", [""])[0]
                relative = unquote(query.get("path", [""])[0])
                project = REPO.get_project(project_id)
                if not project:
                    self.error("项目不存在", 404)
                    return
                file_path = STORAGE.resolve_relative(Path(project["output_dir"]), relative)
                if not file_path.exists() or not file_path.is_file():
                    self.error("文件不存在", 404)
                    return
                self.send_data(file_path.read_bytes(), mimetypes.guess_type(file_path.name)[0] or "application/octet-stream", headers={"Content-Disposition": content_disposition("inline", file_path.name)})
                return
            if path == "/api/export":
                query = parse_qs(parsed.query)
                project_id = query.get("project", [""])[0]
                project = REPO.get_project(project_id)
                if not project:
                    self.error("项目不存在", 404)
                    return
                tasks = REPO.get_tasks(project_id)
                files = []
                project_dir = Path(project["output_dir"])
                for task in tasks:
                    selected = task.get("selected_version_id")
                    versions = task.get("versions", [])
                    # 已确认时导出确认版；未确认时导出当前最新成功版本，避免得到空 ZIP。
                    version = next((v for v in versions if v["id"] == selected), None) or (versions[-1] if versions else None)
                    if version:
                        files.append(STORAGE.resolve_relative(project_dir, version["file_path"]))
                archive = STORAGE.export_final_zip(project_dir, files)
                self.send_data(archive.read_bytes(), "application/zip", headers={"Content-Disposition": content_disposition("attachment", archive.name)})
                return
            self.error("Not Found", 404)
        except KeyError as exc:
            self.error(str(exc.args[0]), 404)
        except Exception as exc:
            traceback.print_exc()
            self.error(str(exc), 500)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/projects":
                fields, files = parse_multipart(self.headers.get("Content-Type", ""), self.read_body())
                form = {key: values[-1] for key, values in fields.items()}
                product = (files.get("product_image") or [None])[0]
                series = (files.get("series_image") or [None])[0]
                for item in [product, series, *[f for key, vals in files.items() if key.startswith("extra_") for f in vals]]:
                    if item and item.get("content"):
                        read_image_info(item["content"])
                        if len(item["content"]) > SETTINGS.max_upload_mb * 1024 * 1024:
                            raise ValueError(f"{item.get('filename', '图片')} 超过 {SETTINGS.max_upload_mb}MB")
                extra_uploads = {key: values for key, values in files.items() if key.startswith("extra_")}
                project_id = GENERATOR.create_project(form, product, series, extra_uploads)
                GENERATOR.start_initial_generation(project_id)
                self.send_json({"id": project_id})
                return
            if path == "/api/settings":
                self.send_json(apply_settings(self.read_json()))
                return
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "projects":
                project_id, action = parts[2], parts[3]
                if action == "refresh-prompts":
                    prompts = GENERATOR.refresh_prompts(project_id)
                    self.send_json({"ok": True, "prompt_count": len(prompts)})
                    return
                if action == "retry-failed":
                    GENERATOR.retry_failed(project_id)
                    self.send_json({"ok": True})
                    return
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "tasks":
                task_id, action = parts[2], parts[3]
                payload = self.read_json()
                if action == "regenerate":
                    GENERATOR.regenerate(task_id, str(payload.get("prompt", "")))
                elif action == "edit":
                    GENERATOR.edit(task_id, str(payload.get("change_request", "")))
                elif action == "approve":
                    GENERATOR.approve(task_id, str(payload.get("version_id", "")))
                elif action == "retry":
                    GENERATOR.retry(task_id)
                else:
                    self.error("不支持的任务操作", 404)
                    return
                self.send_json({"ok": True})
                return
            self.error("Not Found", 404)
        except ValueError as exc:
            self.error(str(exc), 400)
        except KeyError as exc:
            self.error(str(exc.args[0]), 404)
        except Exception as exc:
            traceback.print_exc()
            self.error(str(exc), 500)

    def do_DELETE(self) -> None:
        try:
            path = urlparse(self.path).path
            if path.startswith("/api/projects/"):
                project_id = path.rsplit("/", 1)[-1]
                if not REPO.get_project(project_id):
                    self.error("项目不存在", 404)
                    return
                from database.repositories import now_iso
                REPO.update_project(project_id, deleted_at=now_iso(), status="deleted")
                self.send_json({"ok": True})
                return
            self.error("Not Found", 404)
        except Exception as exc:
            self.error(str(exc), 500)


def main() -> None:
    SETTINGS.data_dir.mkdir(parents=True, exist_ok=True)
    SETTINGS.output_root.mkdir(parents=True, exist_ok=True)
    port = int(__import__("os").getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"电商图 AI 工作台已启动: http://127.0.0.1:{port}")
    print(f"数据目录: {SETTINGS.data_dir}")
    print(f"模式: {'模拟模式' if SETTINGS.mock_mode or not SETTINGS.image_api_key else '真实图片 API'}")
    GENERATOR.migrate_task_metadata()
    GENERATOR.resume_pending()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
