from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from config.task_definitions import TASK_DEFINITIONS
from config.runtime_config import RuntimeConfigStore
from database.repositories import Repository, now_iso
from .image_service import ImageService, ImageServiceError
from .prompt_service import PromptService
from .storage_service import StorageService


class GenerationService:
    def __init__(self, settings, repo: Repository, storage: StorageService):
        self.settings = settings
        self.repo = repo
        self.storage = storage
        self.config_store = RuntimeConfigStore(settings.data_dir)
        self.prompt_service = PromptService(settings, self.config_store)
        self.image_service = ImageService(settings)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _task_lock(self, task_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(task_id, threading.Lock())

    def create_project(self, form: dict[str, Any], product_file: dict[str, Any], series_file: dict[str, Any] | None, extra_uploads: dict[str, list[dict[str, Any]]]) -> str:
        if not product_file or not product_file.get("content"):
            raise ValueError("stt 手托比例参考图为必填项")
        is_series = str(form.get("is_series", "0")) in {"1", "true", "on"}
        if is_series and not (series_file and series_file.get("content")):
            raise ValueError("系列品必须上传 cpt 系列外观参考图")
        product_name = str(form.get("product_name") or "未命名产品").strip()
        function_template = self.config_store.function_template(str(form.get("size_template_id") or "size-01"))
        project_id = uuid.uuid4().hex[:12]
        project_dir = self.storage.create_project_dir(project_id, product_name)
        product_path = self.storage.save_upload(project_dir, "input", product_file.get("filename", "product.png"), product_file["content"], product_file.get("content_type", ""))
        series_path = None
        if series_file and series_file.get("content"):
            series_path = self.storage.save_upload(project_dir, "input", series_file.get("filename", "series.png"), series_file["content"], series_file.get("content_type", ""))
        created = now_iso()
        project = {
            "id": project_id, "product_name": product_name, "product_description": str(form.get("product_description", "")).strip(),
            "is_series": 1 if is_series else 0,
            "product_count": max(1, int(form.get("product_count") or 1)), "custom_scene": str(form.get("custom_scene", "")).strip(),
            "display_requirements": str(form.get("display_requirements", "")).strip(), "product_dimensions": str(form.get("product_dimensions", "")).strip(),
            "size_template_id": function_template["id"],
            "input_product_path": self.storage.relative(project_dir, product_path), "input_series_path": self.storage.relative(project_dir, series_path) if series_path else None,
            "output_dir": str(project_dir), "status": "created", "created_at": created, "updated_at": created,
        }
        self.repo.create_project(project)
        enabled_ids = self._parse_enabled(form.get("enabled_tasks"))
        prompts = self.prompt_service.generate_prompts(project, product_path, enabled_ids, series_path)
        task_definitions = self.prompt_service.task_definitions()
        for item in task_definitions:
            if item["id"] not in enabled_ids:
                continue
            task_id = uuid.uuid4().hex
            task_name = function_template["name"] if item["id"] == "13" else item["name"]
            self.repo.create_task({
                "id": task_id, "project_id": project_id, "slot_id": item["id"], "task_name": task_name, "task_kind": "workflow", "prompt_group": item["prompt_group"],
                "original_prompt": prompts[item["id"]], "current_prompt": prompts[item["id"]], "reference_fields_json": json.dumps(item["reference_fields"], ensure_ascii=False), "status": "queued", "selected_version_id": None, "last_error": "", "created_at": created, "updated_at": created,
            })
        function_defs = self._parse_function_requests(form.get("function_requests"))
        for index, item in enumerate(function_defs, 1):
            template = self.config_store.function_template(str(item.get("template_id") or ""))
            task_name = str(item.get("name") or template["name"]).strip() or template["name"]
            prompt = self.prompt_service.generate_function_prompt(project, template, product_path, series_path)
            task_id = uuid.uuid4().hex
            self.repo.create_task({
                "id": task_id, "project_id": project_id, "slot_id": f"FN-{index:02d}", "task_name": task_name, "task_kind": "function", "prompt_group": "function",
                "original_prompt": prompt, "current_prompt": prompt, "reference_fields_json": json.dumps(["stt", "cpt"], ensure_ascii=False), "status": "queued", "selected_version_id": None, "last_error": "", "created_at": created, "updated_at": created,
            })
        extra_defs = self._parse_extra_requests(form.get("extra_requests"))
        for index, extra in enumerate(extra_defs, 1):
            key = str(extra.get("upload_key") or f"extra_{index - 1}")
            saved: list[str] = []
            for upload in extra_uploads.get(key, []):
                p = self.storage.save_upload(project_dir, f"extra_requests/extra_{index:03d}", upload.get("filename", "reference.png"), upload["content"], upload.get("content_type", ""))
                saved.append(self.storage.relative(project_dir, p))
            request_id = uuid.uuid4().hex
            self.repo.create_extra_request({"id": request_id, "project_id": project_id, "request_index": index, "requirement": str(extra.get("requirement", "")).strip(), "reference_paths_json": json.dumps(saved, ensure_ascii=False), "created_at": created})
            prompt = self.prompt_service.build_extra_prompt(project, str(extra.get("requirement", "")))
            task_id = uuid.uuid4().hex
            self.repo.create_task({
                "id": task_id, "project_id": project_id, "slot_id": f"XR-{index:02d}", "task_name": f"额外参考图需求 {index}", "task_kind": "extra", "prompt_group": "extra_reference",
                "original_prompt": prompt, "current_prompt": prompt, "reference_fields_json": json.dumps(["product_image", *saved], ensure_ascii=False), "status": "queued", "selected_version_id": None, "last_error": "", "created_at": created, "updated_at": created,
            })
        self._write_manifest(project_id)
        return project_id

    @staticmethod
    def _parse_enabled(value: Any) -> list[str]:
        if value is None or value == "":
            return [item["id"] for item in TASK_DEFINITIONS]
        try:
            if isinstance(value, str):
                value = json.loads(value)
            selected = {str(item).zfill(2) for item in value}
            return [item["id"] for item in TASK_DEFINITIONS if item["id"] in selected]
        except (TypeError, ValueError, json.JSONDecodeError):
            return [item["id"] for item in TASK_DEFINITIONS]

    @staticmethod
    def _parse_extra_requests(value: Any) -> list[dict[str, Any]]:
        if not value:
            return []
        try:
            items = json.loads(value) if isinstance(value, str) else value
            return [item for item in items if str(item.get("requirement", "")).strip()]
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    @staticmethod
    def _parse_function_requests(value: Any) -> list[dict[str, Any]]:
        if not value:
            return []
        try:
            items = json.loads(value) if isinstance(value, str) else value
            return [item for item in items if isinstance(item, dict) and str(item.get("template_id", "")).strip()]
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    def start_initial_generation(self, project_id: str) -> None:
        self.repo.update_project(project_id, status="generating")
        threading.Thread(target=self._run_initial, args=(project_id,), daemon=True, name=f"generate-{project_id}").start()

    def _run_initial(self, project_id: str) -> None:
        tasks = [task for task in self.repo.get_tasks(project_id) if task["status"] in {"queued", "generating"}]
        with ThreadPoolExecutor(max_workers=self.settings.generation_concurrency) as executor:
            futures = [executor.submit(self.generate_task, task["id"], "initial", task.get("current_prompt", ""), "") for task in tasks]
            for future in futures:
                try:
                    future.result()
                except Exception:
                    pass
        self._refresh_project_status(project_id)

    def resume_pending(self) -> None:
        """Recover tasks left queued/generating after a process restart."""
        for project in self.repo.list_projects():
            tasks = self.repo.get_tasks(project["id"])
            if any(task["status"] in {"queued", "generating"} for task in tasks):
                self.start_initial_generation(project["id"])

    def migrate_task_metadata(self) -> None:
        """Keep persisted projects aligned and remove legacy internal text from visible prompts."""
        definitions = {item["id"]: item for item in self.prompt_service.task_definitions()}
        for project in self.repo.list_projects():
            project_changed = False
            for task in self.repo.get_tasks(project["id"]):
                definition = definitions.get(task["slot_id"])
                updates: dict[str, Any] = {}
                if task["task_kind"] == "workflow" and definition:
                    expected_name = (
                        self.config_store.function_template(project.get("size_template_id"))["name"]
                        if task["slot_id"] == "13"
                        else definition["name"]
                    )
                    if task["task_name"] != expected_name:
                        updates["task_name"] = expected_name
                    if task["prompt_group"] != definition["prompt_group"]:
                        updates["prompt_group"] = definition["prompt_group"]
                    expected_reference_fields = list(definition.get("reference_fields") or [])
                    if task.get("reference_fields") != expected_reference_fields:
                        updates["reference_fields"] = expected_reference_fields
                for field in ("original_prompt", "current_prompt"):
                    prompt = self.prompt_service.strip_reference_variable_contract(str(task.get(field) or ""))
                    if prompt != str(task.get(field) or ""):
                        updates[field] = prompt
                if updates:
                    self.repo.update_task(task["id"], **updates)
                    project_changed = True
            if project_changed:
                self._write_manifest(project["id"])

    def refresh_prompts(self, project_id: str) -> dict[str, str]:
        """Re-analyze the product and replace every workflow task with a complete prompt."""
        project = self.repo.get_project(project_id)
        if not project:
            raise ValueError("项目不存在")
        project_dir = Path(project["output_dir"]).resolve()
        product_path = self.storage.resolve_relative(project_dir, project["input_product_path"])
        cpt_path = self.storage.resolve_relative(project_dir, project["input_series_path"]) if project.get("input_series_path") else None
        tasks = [task for task in self.repo.get_tasks(project_id) if task["task_kind"] == "workflow"]
        ids = [task["slot_id"] for task in tasks]
        prompts = self.prompt_service.generate_prompts(project, product_path, ids, cpt_path)
        definition_map = {item["id"]: item for item in self.prompt_service.task_definitions()}
        for task in tasks:
            prompt = str(prompts.get(task["slot_id"], "")).strip()
            if not prompt:
                raise ValueError(f"图{task['slot_id']}没有生成有效描述词")
            definition = definition_map.get(task["slot_id"], {})
            task_name = (
                self.config_store.function_template(project.get("size_template_id"))["name"]
                if task["slot_id"] == "13"
                else definition.get("name", task["task_name"])
            )
            self.repo.update_task(
                task["id"],
                task_name=task_name,
                prompt_group=definition.get("prompt_group", task["prompt_group"]),
                original_prompt=prompt,
                current_prompt=prompt,
                last_error="",
            )
        self._write_manifest(project_id)
        return prompts

    def retry_failed(self, project_id: str) -> None:
        """Queue all failed tasks; the worker only runs queued/generating tasks."""
        project = self.repo.get_project(project_id)
        if not project:
            raise ValueError("项目不存在")
        failed = [task for task in self.repo.get_tasks(project_id) if task["status"] == "failed"]
        if not failed:
            return
        for task in failed:
            self.repo.update_task(task["id"], status="queued", last_error="")
        self.start_initial_generation(project_id)

    def _project_and_task(self, task_id: str) -> tuple[dict[str, Any], dict[str, Any], Path]:
        task = self.repo.get_task(task_id)
        if not task:
            raise ValueError("任务不存在")
        project = self.repo.get_project(task["project_id"])
        if not project:
            raise ValueError("项目不存在")
        project_dir = Path(project["output_dir"]).resolve()
        return project, task, project_dir

    def _reference_inputs(self, project: dict[str, Any], task: dict[str, Any], project_dir: Path, mode: str) -> list[dict[str, Any]]:
        inputs: list[dict[str, Any]] = []

        def add(path: Path, label: str, purpose: str) -> None:
            path = path.resolve()
            if path.exists() and all(item["path"] != path for item in inputs):
                inputs.append({"path": path, "label": label, "purpose": purpose})

        if mode == "edit" and task.get("versions"):
            selected = task.get("selected_version_id")
            versions = task.get("versions", [])
            current = next((v for v in versions if v["id"] == selected), None) or versions[-1]
            if current:
                add(
                    self.storage.resolve_relative(project_dir, current["file_path"]),
                    "待编辑成品图",
                    "这是本次局部编辑的画布，只能按修改要求调整指定内容，其他像素级关系尽量保持不变。",
                )
        stt_path = self.storage.resolve_relative(project_dir, project["input_product_path"])
        if project.get("is_series"):
            if not project.get("input_series_path"):
                raise ValueError("系列品缺少 cpt 系列外观参考图")
            cpt_path = self.storage.resolve_relative(project_dir, project["input_series_path"])
            add(
                cpt_path,
                "系列外观参考图",
                "这是系列品外观的唯一最高优先级依据，锁定款式、颜色、造型、图案、结构、表面细节和材质。",
            )
            add(
                stt_path,
                "手托比例参考图",
                "只锁定手与产品的真实大小比例，不得覆盖系列外观参考图中的产品外观。",
            )
        else:
            add(
                stt_path,
                "手托比例参考图",
                "单品同时以此图锁定产品外观和手与产品的真实大小比例。",
            )
        if task.get("task_kind") == "extra":
            for index, ref in enumerate(task.get("reference_fields", [])[1:], 1):
                try:
                    add(
                        self.storage.resolve_relative(project_dir, ref),
                        f"额外需求参考图{index}",
                        "仅用于本条需求的构图、动作、风格、道具或氛围，不得覆盖产品身份。",
                    )
                except ValueError:
                    continue
        return inputs

    def _reference_paths(self, project: dict[str, Any], task: dict[str, Any], project_dir: Path, mode: str) -> list[Path]:
        return [item["path"] for item in self._reference_inputs(project, task, project_dir, mode)]

    @staticmethod
    def _runtime_reference_contract(reference_inputs: list[dict[str, Any]]) -> str:
        if not reference_inputs:
            raise ValueError("任务缺少可用参考图")
        mappings = [
            f"输入参考图{index}＝【{item['label']}】：{item['purpose']}"
            for index, item in enumerate(reference_inputs, 1)
        ]
        labels = {item["label"] for item in reference_inputs}
        if "系列外观参考图" in labels:
            priority = (
                "【产品外观参考图】固定等同于输入中的【系列外观参考图】；系列品外观必须以它为最高优先级；"
                "【手托比例参考图】只控制手与产品的大小比例，"
                "不得覆盖或改写系列外观。"
            )
        else:
            priority = "【产品外观参考图】固定等同于输入中的【手托比例参考图】；单品的外观和手托比例都必须以这张图为最高优先级。"
        return (
            "【输入参考图对应关系】"
            + "".join(mappings)
            + "【参考优先级】"
            + priority
            + "任何文字要求或额外需求参考图都不得覆盖上述规则；"
            "不得猜测、交换或合并各参考图角色，不得把参考图变量名称绘制到画面中。"
        )

    def generate_task(self, task_id: str, mode: str, prompt: str, change_request: str) -> str:
        lock = self._task_lock(task_id)
        if not lock.acquire(blocking=False):
            raise ValueError("该任务正在执行，请稍后再试")
        started = time.perf_counter()
        project_id = task_id
        try:
            project, task, project_dir = self._project_and_task(task_id)
            project_id = project["id"]
            if mode not in {"initial", "regenerate", "edit"}:
                raise ValueError("不支持的版本模式")
            prompt = str(prompt or task.get("current_prompt", "")).strip()
            if not prompt:
                raise ValueError("提示词不能为空")
            if mode == "edit":
                if not change_request.strip():
                    raise ValueError("局部修改要求不能为空")
                appearance_reference = self.prompt_service.template_values(project)["产品外观参考图"]
                scale_reference = self.prompt_service.template_values(project)["手托比例参考图"]
                prompt = f"基于当前成品图进行局部编辑，使用{appearance_reference}锁定产品外观、使用{scale_reference}锁定真实大小比例。本次只执行以下修改：" + change_request.strip() + "。保持原有构图、机位、裁切、背景、道具和光线不变；除本次明确要求外，其他内容全部保持不变。额外参考图不得改变产品的设计、颜色、造型、比例、图案、结构和表面细节。变量名称不得出现在画面中。"
            version_number = self.repo.next_version_number(task_id)
            self.repo.update_task(task_id, status="generating", last_error="")
            reference_inputs = self._reference_inputs(project, task, project_dir, mode)
            refs = [item["path"] for item in reference_inputs]
            effective_prompt = self._runtime_reference_contract(reference_inputs) + "【画面生成要求】" + prompt
            content, usage = self.image_service.generate(effective_prompt, refs, seed=f"{project_id}:{task['slot_id']}:{version_number}:{mode}:{effective_prompt}")
            usage = dict(usage or {})
            usage["reference_inputs"] = [
                {"index": index, "label": item["label"], "filename": item["path"].name}
                for index, item in enumerate(reference_inputs, 1)
            ]
            ext = ".jpg" if self.settings.image_output_format == "jpeg" else "." + self.settings.image_output_format
            target = self.storage.version_path(project_dir, task["slot_id"], task["task_name"], project["product_name"], version_number, mode, ext)
            self.storage.atomic_write(target, content)
            relative = self.storage.relative(project_dir, target)
            parent = None
            versions = task.get("versions", [])
            if versions:
                selected = task.get("selected_version_id")
                parent = next((v["id"] for v in versions if v["id"] == selected), None) or versions[-1]["id"]
            version_id = uuid.uuid4().hex
            self.repo.create_version({"id": version_id, "task_id": task_id, "version_number": version_number, "mode": mode, "parent_version_id": parent, "file_path": relative, "prompt": effective_prompt, "change_request": change_request, "model": self.settings.image_model, "size": self.settings.image_size, "quality": self.settings.image_quality, "api_usage_json": json.dumps(usage, ensure_ascii=False), "is_approved": 0, "created_at": now_iso()})
            self.repo.update_task(task_id, current_prompt=prompt, status="ready", last_error="")
            self.repo.create_log({"project_id": project_id, "task_id": task_id, "version_number": version_number, "operation": mode, "model": self.settings.image_model, "started_at": now_iso(), "finished_at": now_iso(), "status_code": usage.get("status_code", 200), "duration_ms": int((time.perf_counter() - started) * 1000), "attempt_count": usage.get("attempt_count", 1), "error_type": "", "error_message": ""})
            self._write_manifest(project_id)
            self._refresh_project_status(project_id)
            return version_id
        except Exception as exc:
            if project_id != task_id:
                try:
                    self.repo.update_task(task_id, status="failed", last_error=str(exc))
                    self.repo.create_log({"project_id": project_id, "task_id": task_id, "version_number": None, "operation": mode, "model": self.settings.image_model, "started_at": now_iso(), "finished_at": now_iso(), "status_code": getattr(exc, "status_code", None), "duration_ms": int((time.perf_counter() - started) * 1000), "attempt_count": 1, "error_type": getattr(exc, "error_type", type(exc).__name__), "error_message": str(exc)[:1000]})
                except Exception:
                    pass
            raise
        finally:
            lock.release()

    def regenerate(self, task_id: str, prompt: str) -> None:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("提示词不能为空")
        if not self.repo.get_task(task_id):
            raise ValueError("任务不存在")
        # 先持久化用户编辑，再启动耗时生图；轮询绝不能在生成完成前回传旧描述词。
        self.repo.update_task(task_id, current_prompt=prompt, status="queued", last_error="")
        threading.Thread(target=self._run_one, args=(task_id, "regenerate", prompt, ""), daemon=True).start()

    def edit(self, task_id: str, change_request: str) -> None:
        threading.Thread(target=self._run_one, args=(task_id, "edit", "", change_request), daemon=True).start()

    def retry(self, task_id: str) -> None:
        task = self.repo.get_task(task_id)
        if task:
            threading.Thread(target=self._run_one, args=(task_id, "regenerate", task.get("current_prompt", ""), ""), daemon=True).start()

    def _run_one(self, task_id: str, mode: str, prompt: str, change_request: str) -> None:
        try:
            self.generate_task(task_id, mode, prompt, change_request)
        except Exception:
            pass

    def approve(self, task_id: str, version_id: str) -> None:
        task = self.repo.get_task(task_id)
        if not task or not any(v["id"] == version_id for v in task.get("versions", [])):
            raise ValueError("版本不存在")
        self.repo.select_version(task_id, version_id)
        self._refresh_project_status(task["project_id"])
        self._write_manifest(task["project_id"])

    def _refresh_project_status(self, project_id: str) -> None:
        tasks = self.repo.get_tasks(project_id)
        if not tasks:
            return
        statuses = [task["status"] for task in tasks]
        if all(status == "approved" for status in statuses):
            self.repo.update_project(project_id, status="completed", completed_at=now_iso())
        elif any(status == "generating" for status in statuses):
            self.repo.update_project(project_id, status="generating")
        elif any(status == "failed" for status in statuses):
            self.repo.update_project(project_id, status="has_failures")
        else:
            self.repo.update_project(project_id, status="review")

    def _write_manifest(self, project_id: str) -> None:
        project = self.repo.get_project(project_id)
        if not project:
            return
        project_dir = Path(project["output_dir"])
        manifest = {"project": project, "tasks": self.repo.get_tasks(project_id), "extra_requests": self.repo.get_extra_requests(project_id), "generated_at": now_iso()}
        self.storage.write_manifest(project_dir, manifest)
