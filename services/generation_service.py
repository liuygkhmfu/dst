from __future__ import annotations

import json
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from config.task_definitions import TASK_DEFINITIONS
from config.runtime_config import RuntimeConfigStore
from database.repositories import Repository, now_iso
from .agent_service import AgentStore
from .image_service import ImageService, ImageServiceError
from .postprocess_service import PostprocessService
from .prompt_service import PromptService
from .storage_service import StorageService


class GenerationService:
    def __init__(self, settings, repo: Repository, storage: StorageService):
        self.settings = settings
        self.repo = repo
        self.storage = storage
        self.config_store = RuntimeConfigStore(settings.data_dir)
        self.agent_store = AgentStore(settings.data_dir)
        self.prompt_service = PromptService(settings, self.config_store)
        self.image_service = ImageService(settings)
        self.postprocess_service = PostprocessService(storage)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._postprocess_locks: dict[str, threading.Lock] = {}
        self._postprocess_locks_guard = threading.Lock()

    def _task_lock(self, task_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(task_id, threading.Lock())

    def _postprocess_lock(self, project_id: str) -> threading.Lock:
        with self._postprocess_locks_guard:
            return self._postprocess_locks.setdefault(project_id, threading.Lock())

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
        enabled_ids = self._parse_enabled(form.get("enabled_tasks"))
        not_edible_enabled = self._form_checked(form.get("enable_not_edible_watermark"))
        collage_enabled = self._form_checked(form.get("enable_scene_collage"))
        postprocess: dict[str, Any] = {
            "not_edible": {
                "enabled": not_edible_enabled,
                "status": "pending" if not_edible_enabled else "disabled",
                "reference_path": "",
                "transparent_asset_path": "",
                "outputs": {},
                "error": "",
            },
            "collage": {
                "enabled": collage_enabled,
                "status": "pending" if collage_enabled else "disabled",
                "template_agent_id": str(form.get("collage_template_agent_id") or "").strip(),
                "text_agent_id": str(form.get("collage_text_agent_id") or "").strip(),
                "scene_slots": [slot for slot in enabled_ids if slot in {"07", "08", "09", "10", "11", "12"}],
                "package_path": "",
                "error": "",
            },
        }
        if not_edible_enabled:
            bundled_reference = Path(__file__).resolve().parents[1] / "assets" / "not_edible_reference.png"
            if not bundled_reference.is_file():
                raise ValueError("项目缺少内置的禁止食用水印参考图")
            saved_reference = self.storage.save_upload(
                project_dir,
                "postprocess/references",
                bundled_reference.name,
                bundled_reference.read_bytes(),
                "image/png",
            )
            postprocess["not_edible"]["reference_path"] = self.storage.relative(project_dir, saved_reference)
        if collage_enabled:
            if len(postprocess["collage"]["scene_slots"]) < 4:
                raise ValueError("场景拼图素材包至少需要勾选 4 张使用场景图")
            if not postprocess["collage"]["template_agent_id"] or not postprocess["collage"]["text_agent_id"]:
                raise ValueError("场景拼图素材包必须分别选择场景模板 Agent 和艺术文字 Agent")
            for label, agent_id in (
                ("场景拼图模板", postprocess["collage"]["template_agent_id"]),
                ("艺术文字", postprocess["collage"]["text_agent_id"]),
            ):
                agent = self.agent_store.get(agent_id)
                if not agent:
                    raise ValueError(f"{label}所选 Agent 不存在或已被删除")
                variables = set(agent.get("project_variables", []))
                if variables.intersection({"cpt", "系列外观参考图"}) and not is_series:
                    raise ValueError(f"{label} Agent 引用了 cpt；当前项目必须选择系列品并上传 cpt")
                if "Agent参考图" in variables and not self.agent_store.reference_files(agent):
                    raise ValueError(
                        f"{label} Agent 使用了 {{Agent参考图}}，请先在 Agent 工作台绑定固定参考图"
                    )
        created = now_iso()
        project = {
            "id": project_id, "product_name": product_name, "product_description": str(form.get("product_description", "")).strip(),
            "is_series": 1 if is_series else 0,
            "product_count": max(1, int(form.get("product_count") or 1)), "custom_scene": str(form.get("custom_scene", "")).strip(),
            "display_requirements": str(form.get("display_requirements", "")).strip(), "product_dimensions": str(form.get("product_dimensions", "")).strip(),
            "size_template_id": function_template["id"],
            "input_product_path": self.storage.relative(project_dir, product_path), "input_series_path": self.storage.relative(project_dir, series_path) if series_path else None,
            "output_dir": str(project_dir), "postprocess": postprocess,
            "status": "created", "created_at": created, "updated_at": created,
        }
        self.repo.create_project(project)
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
        if not_edible_enabled:
            watermark_prompt = (
                "以【禁止食用水印底稿】为唯一画布、人物、构图和警示设计底稿。完整保留底稿中的孩子、"
                "手部姿势、脸部朝向、白色背景、红色禁止符号、白色虚线轮廓以及英文“Not Edible”的"
                "字体、颜色、位置和整体版式，禁止重画或改动这些内容。只把孩子手指间原本拿着的小物体"
                "替换成【产品外观参考图】中的当前捏捏产品：单品严格以手托比例参考图同时锁定外观与比例；"
                "系列品严格以系列外观参考图锁定产品外观，以手托比例参考图锁定产品和手的真实比例。"
                "产品被孩子手指自然捏住并靠近嘴边，但不得进入口中、不得出现吞咽或咬食动作。只允许在手指"
                "真实施力接触点产生轻微、连续、符合PU慢回弹材质的局部压缩形变；产品其余部分的轮廓、颜色、"
                "五官、图案、结构、表面细节和身份必须保持不变，禁止扭曲、融化、增生、缺失或AI自创细节。"
                "最终仍是一张清晰完整的英文禁止食用警示贴图，不新增任何中文或其他文字，正方形构图。"
            )
            self.repo.create_task({
                "id": uuid.uuid4().hex,
                "project_id": project_id,
                "slot_id": "WM-01",
                "task_name": "禁止食用水印",
                "task_kind": "watermark_asset",
                "prompt_group": "postprocess",
                "original_prompt": watermark_prompt,
                "current_prompt": watermark_prompt,
                "reference_fields_json": json.dumps(
                    [postprocess["not_edible"]["reference_path"], "stt", "cpt"],
                    ensure_ascii=False,
                ),
                "generation_size": "1024x1024",
                "generation_quality": "high",
                "status": "queued",
                "selected_version_id": None,
                "last_error": "",
                "created_at": created,
                "updated_at": created,
            })
        if collage_enabled:
            self._create_suite_agent_task(
                project,
                product_path,
                series_path,
                postprocess["collage"]["template_agent_id"],
                "COL-TEMPLATE",
                "场景拼图模板",
                "collage_template",
                created,
            )
            self._create_suite_agent_task(
                project,
                product_path,
                series_path,
                postprocess["collage"]["text_agent_id"],
                "COL-TEXT",
                "艺术文字贴图",
                "collage_text",
                created,
            )
        function_defs = self._parse_function_requests(form.get("function_requests"))
        for index, item in enumerate(function_defs, 1):
            source_type = str(item.get("source_type") or "template").strip().lower()
            generation_size = ""
            generation_quality = ""
            agent_id = ""
            task_kind = "function"
            saved_agent_refs: list[str] = []
            analysis_refs: list[Path] = []
            agent_inputs: dict[str, str] = {}
            if source_type == "agent":
                agent = self.agent_store.get(str(item.get("agent_id") or ""))
                if not agent:
                    raise ValueError("所选 Agent 不存在或已被删除")
                agent_id = agent["id"]
                template = {"id": agent_id, "name": agent["name"], "prompt": agent["prompt"]}
                task_kind = "agent"
                generation_size = agent["image_size"]
                generation_quality = agent["image_quality"]
                agent_variables = set(agent.get("project_variables", []))
                if agent_variables.intersection({"cpt", "系列外观参考图"}) and not is_series:
                    raise ValueError(
                        f"Agent“{agent['name']}”引用了 cpt 系列外观参考图；"
                        "请在新建项目中选择“系列品”并上传 cpt 图片"
                    )
                project_values = self.prompt_service.template_values(project)
                agent_inputs = {
                    name: project_values.get(name, "")
                    for name in agent.get("project_variables", [])
                }
                for source in self.agent_store.reference_files(agent):
                    copied = self.storage.save_upload(
                        project_dir,
                        f"agent_requests/agent_{index:03d}",
                        source.name,
                        source.read_bytes(),
                    )
                    saved_agent_refs.append(self.storage.relative(project_dir, copied))
                    analysis_refs.append(copied)
                upload_key = str(item.get("reference_upload_key") or f"agent_ref_{index - 1}")
                for upload in extra_uploads.get(upload_key, [])[:8]:
                    copied = self.storage.save_upload(
                        project_dir,
                        f"agent_requests/agent_{index:03d}",
                        upload.get("filename", "reference.png"),
                        upload["content"],
                        upload.get("content_type", ""),
                    )
                    saved_agent_refs.append(self.storage.relative(project_dir, copied))
                    analysis_refs.append(copied)
                if "Agent参考图" in agent_variables and not analysis_refs:
                    raise ValueError(f"Agent“{agent['name']}”至少需要上传 1 张本次任务参考图")
            else:
                template = self.config_store.function_template(str(item.get("template_id") or ""))
            task_name = str(item.get("name") or template["name"]).strip() or template["name"]
            prompt = self.prompt_service.generate_function_prompt(
                project,
                template,
                product_path,
                series_path,
                additional_reference_paths=analysis_refs,
                template_variables=agent_inputs,
            )
            task_id = uuid.uuid4().hex
            self.repo.create_task({
                "id": task_id, "project_id": project_id, "slot_id": f"FN-{index:02d}", "task_name": task_name, "task_kind": task_kind, "prompt_group": "function",
                "original_prompt": prompt, "current_prompt": prompt,
                "reference_fields_json": json.dumps(["stt", "cpt", *saved_agent_refs], ensure_ascii=False),
                "generation_size": generation_size, "generation_quality": generation_quality, "agent_id": agent_id,
                "agent_inputs_json": json.dumps(agent_inputs, ensure_ascii=False),
                "status": "queued", "selected_version_id": None, "last_error": "", "created_at": created, "updated_at": created,
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
    def _form_checked(value: Any) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on", "是"}

    def _create_suite_agent_task(
        self,
        project: dict[str, Any],
        product_path: Path,
        series_path: Path | None,
        agent_id: str,
        slot_id: str,
        task_name: str,
        task_kind: str,
        created: str,
    ) -> None:
        agent = self.agent_store.get(agent_id)
        if not agent:
            raise ValueError(f"{task_name}所选 Agent 不存在或已被删除")
        variables = set(agent.get("project_variables", []))
        if variables.intersection({"cpt", "系列外观参考图"}) and not project.get("is_series"):
            raise ValueError(f"{task_name} Agent 引用了 cpt；当前项目必须选择系列品并上传 cpt")
        fixed_references = self.agent_store.reference_files(agent)
        if "Agent参考图" in variables and not fixed_references:
            raise ValueError(f"{task_name} Agent 使用了 {{Agent参考图}}，请先在 Agent 工作台绑定固定参考图")
        project_dir = Path(project["output_dir"]).resolve()
        copied_references: list[str] = []
        analysis_references: list[Path] = []
        for source in fixed_references:
            copied = self.storage.save_upload(
                project_dir,
                f"agent_requests/{slot_id.lower()}",
                source.name,
                source.read_bytes(),
            )
            copied_references.append(self.storage.relative(project_dir, copied))
            analysis_references.append(copied)
        project_values = self.prompt_service.template_values(project)
        agent_inputs = {
            name: project_values.get(name, "")
            for name in agent.get("project_variables", [])
        }
        prompt = self.prompt_service.generate_function_prompt(
            project,
            {"id": agent["id"], "name": agent["name"], "prompt": agent["prompt"]},
            product_path,
            series_path,
            additional_reference_paths=analysis_references,
            template_variables=agent_inputs,
        )
        self.repo.create_task({
            "id": uuid.uuid4().hex,
            "project_id": project["id"],
            "slot_id": slot_id,
            "task_name": task_name,
            "task_kind": task_kind,
            "prompt_group": "postprocess",
            "original_prompt": prompt,
            "current_prompt": prompt,
            "reference_fields_json": json.dumps(["stt", "cpt", *copied_references], ensure_ascii=False),
            "generation_size": agent["image_size"],
            "generation_quality": agent["image_quality"],
            "agent_id": agent["id"],
            "agent_inputs_json": json.dumps(agent_inputs, ensure_ascii=False),
            "status": "queued",
            "selected_version_id": None,
            "last_error": "",
            "created_at": created,
            "updated_at": created,
        })

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
            return [
                item
                for item in items
                if isinstance(item, dict)
                and (
                    str(item.get("template_id", "")).strip()
                    or str(item.get("agent_id", "")).strip()
                )
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    def start_initial_generation(self, project_id: str) -> None:
        self.repo.update_project(project_id, status="generating")
        threading.Thread(target=self._run_initial, args=(project_id,), daemon=True, name=f"generate-{project_id}").start()

    def run_agent(
        self,
        agent_id: str,
        supplied_inputs: dict[str, Any],
        agent_uploads: list[dict[str, Any]],
        stt_upload: dict[str, Any] | None = None,
        cpt_upload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one Agent outside a project with temporary, role-bound image inputs."""
        agent = self.agent_store.get(agent_id)
        if not agent:
            raise ValueError("Agent 不存在或已被删除")
        if not (stt_upload and stt_upload.get("content")):
            raise ValueError("独立运行 Agent 必须上传 stt 手托比例参考图")
        usable_agent_uploads = [item for item in agent_uploads[:8] if item.get("content")]
        fixed_agent_references = self.agent_store.reference_files(agent)
        agent_variables = set(agent.get("project_variables", []))
        if "Agent参考图" in agent_variables and not (fixed_agent_references or usable_agent_uploads):
            raise ValueError("Agent 描述词引用了 {{Agent参考图}}，请先绑定固定参考图或上传本次临时任务参考图")
        supplied_inputs = supplied_inputs or {}
        series_value = str(supplied_inputs.get("是否系列品") or "").strip().lower()
        is_series = bool(cpt_upload and cpt_upload.get("content")) or series_value in {
            "1", "true", "yes", "on", "是", "系列品",
        }
        if is_series and not (cpt_upload and cpt_upload.get("content")):
            raise ValueError("系列品独立测试必须上传 cpt 系列外观参考图")
        if agent_variables.intersection({"cpt", "系列外观参考图"}) and not (
            cpt_upload and cpt_upload.get("content")
        ):
            raise ValueError("Agent 描述词引用了 cpt 系列外观参考图，请上传实际 cpt 图片")
        resolved_inputs = {
            str(key): str(value).strip()
            for key, value in supplied_inputs.items()
            if str(key) in agent_variables
        }
        synthetic_project = {
            "product_name": resolved_inputs.get("产品名") or "Agent 独立测试",
            "product_description": resolved_inputs.get("产品文字信息") or resolved_inputs.get("产品描述") or "",
            "product_count": resolved_inputs.get("产品数量") or 1,
            "display_requirements": resolved_inputs.get("摆放展示要求") or "",
            "custom_scene": resolved_inputs.get("自定义使用场景") or "",
            "product_dimensions": resolved_inputs.get("产品尺寸") or "",
            "is_series": is_series,
            "input_series_path": "agent-test-cpt" if is_series else None,
        }
        rendered_values = self.prompt_service.template_values(synthetic_project)
        rendered_values.update(resolved_inputs)
        resolved_inputs = {
            name: rendered_values.get(name, "")
            for name in agent.get("project_variables", [])
        }
        resolved_prompt = self.prompt_service.render_template(
            agent["prompt"],
            synthetic_project,
            resolved_inputs,
        )
        if not resolved_prompt.strip():
            raise ValueError("Agent 渲染后的描述词为空")
        with tempfile.TemporaryDirectory(prefix="agent-run-") as temp_name:
            temp_dir = Path(temp_name)
            reference_inputs: list[dict[str, Any]] = []

            def save_reference(upload: dict[str, Any], stem: str, label: str, purpose: str) -> None:
                suffix = Path(str(upload.get("filename") or "")).suffix.lower()
                if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                    suffix = ".png"
                path = temp_dir / f"{stem}{suffix}"
                path.write_bytes(upload["content"])
                reference_inputs.append({"path": path, "label": label, "purpose": purpose})

            if is_series and cpt_upload:
                save_reference(
                    cpt_upload,
                    "cpt_series_appearance",
                    "系列外观参考图",
                    "这是系列品外观的唯一最高优先级依据，锁定款式、颜色、造型、图案、结构、表面细节和材质。",
                )
                save_reference(
                    stt_upload,
                    "stt_hand_scale",
                    "手托比例参考图",
                    "只锁定手与产品的真实大小比例，不得覆盖系列外观参考图中的产品外观。",
                )
            else:
                save_reference(
                    stt_upload,
                    "stt_product_and_scale",
                    "手托比例参考图",
                    "单品同时以此图锁定产品外观和手与产品的真实大小比例。",
                )
            for index, path in enumerate(fixed_agent_references, 1):
                reference_inputs.append(
                    {
                        "path": path,
                        "label": f"智能体任务参考图{index}",
                        "purpose": "这是固定绑定在当前 Agent 定义中的任务参考图；追加到套图时会自动参与本 Agent 任务，但不得覆盖产品身份与比例参考。",
                    }
                )
            for index, upload in enumerate(usable_agent_uploads, 1):
                reference_index = len(fixed_agent_references) + index
                save_reference(
                    upload,
                    f"agent_task_reference_{reference_index:02d}",
                    f"智能体任务参考图{reference_index}",
                    "这是本次独立测试临时追加的 Agent 任务参考图；仅用于当前任务，不得覆盖产品身份与比例参考。",
                )
            refs = [item["path"] for item in reference_inputs]
            effective_prompt = self._runtime_reference_contract(reference_inputs)
            if fixed_agent_references or usable_agent_uploads:
                effective_prompt += (
                    "【智能体任务参考图】统指上述全部【智能体任务参考图1…N】，"
                    "按上传顺序使用，不得把它们误认成 stt 或 cpt。"
                )
            effective_prompt += "【画面生成要求】" + resolved_prompt
            content, usage = self.image_service.generate(
                effective_prompt,
                refs,
                seed=f"agent-run:{agent_id}:{effective_prompt}",
                image_size=agent["image_size"],
                image_quality=agent["image_quality"],
            )
            usage = dict(usage or {})
            usage["reference_inputs"] = [
                {"index": index, "label": item["label"], "filename": item["path"].name}
                for index, item in enumerate(reference_inputs, 1)
            ]
        suffix = ".jpg" if self.settings.image_output_format == "jpeg" else "." + self.settings.image_output_format
        relative = self.agent_store.save_run_output(agent_id, content, suffix)
        return {
            "agent_id": agent_id,
            "file_path": relative,
            "resolved_prompt": resolved_prompt,
            "effective_prompt": effective_prompt,
            "inputs": resolved_inputs,
            "image_size": agent["image_size"],
            "image_quality": agent["image_quality"],
            "usage": usage,
        }

    def _run_initial(self, project_id: str) -> None:
        tasks = [task for task in self.repo.get_tasks(project_id) if task["status"] in {"queued", "generating"}]
        with ThreadPoolExecutor(max_workers=self.settings.generation_concurrency) as executor:
            futures = [executor.submit(self.generate_task, task["id"], "initial", task.get("current_prompt", ""), "") for task in tasks]
            for future in futures:
                try:
                    future.result()
                except Exception:
                    pass
        self.finalize_project_assets(project_id)
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
        if task.get("task_kind") == "watermark_asset":
            reference_fields = task.get("reference_fields", [])
            if not reference_fields:
                raise ValueError("禁止食用水印任务缺少固定底稿")
            add(
                self.storage.resolve_relative(project_dir, reference_fields[0]),
                "禁止食用水印底稿",
                "这是唯一画布和构图底稿；必须保留孩子、动作、红色禁止符号、虚线轮廓、Not Edible英文文字及其版式，只替换手指间的小物体。",
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
        if task.get("task_kind") in {"agent", "collage_template", "collage_text"}:
            for index, ref in enumerate(task.get("reference_fields", [])[2:], 1):
                try:
                    add(
                        self.storage.resolve_relative(project_dir, ref),
                        f"智能体任务参考图{index}",
                        "这是创建本项目时为当前 Agent 任务上传的参考图，仅用于本任务的构图、风格、动作、材质表现或功能要求，不得覆盖产品身份与比例参考。",
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
            generation_size = str(task.get("generation_size") or self.settings.image_size)
            generation_quality = str(task.get("generation_quality") or self.settings.image_quality)
            content, usage = self.image_service.generate(
                effective_prompt,
                refs,
                seed=f"{project_id}:{task['slot_id']}:{version_number}:{mode}:{effective_prompt}",
                image_size=generation_size,
                image_quality=generation_quality,
            )
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
            self.repo.create_version({"id": version_id, "task_id": task_id, "version_number": version_number, "mode": mode, "parent_version_id": parent, "file_path": relative, "prompt": effective_prompt, "change_request": change_request, "model": self.settings.image_model, "size": generation_size, "quality": generation_quality, "api_usage_json": json.dumps(usage, ensure_ascii=False), "is_approved": 0, "created_at": now_iso()})
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
            task = self.repo.get_task(task_id)
            if task:
                self.finalize_project_assets(task["project_id"])
        except Exception:
            pass

    @staticmethod
    def _current_version(task: dict[str, Any]) -> dict[str, Any] | None:
        versions = task.get("versions", [])
        selected = task.get("selected_version_id")
        return next((version for version in versions if version["id"] == selected), None) or (
            versions[-1] if versions else None
        )

    def final_relative_path(
        self,
        project: dict[str, Any],
        task: dict[str, Any],
        version: dict[str, Any],
    ) -> str:
        """Return the user-visible/exportable derivative without hiding the source version."""
        not_edible = (project.get("postprocess") or {}).get("not_edible") or {}
        output = (not_edible.get("outputs") or {}).get(str(task.get("slot_id")))
        if (
            not_edible.get("enabled")
            and output
            and output.get("source_version_id") == version.get("id")
            and output.get("file_path")
        ):
            return str(output["file_path"])
        return str(version["file_path"])

    def finalize_project_assets(self, project_id: str) -> None:
        """Rebuild local derivatives after initial generation, regeneration or version approval."""
        lock = self._postprocess_lock(project_id)
        with lock:
            project = self.repo.get_project(project_id)
            if not project:
                return
            postprocess = json.loads(json.dumps(project.get("postprocess") or {}, ensure_ascii=False))
            tasks = self.repo.get_tasks(project_id)
            task_by_slot = {task["slot_id"]: task for task in tasks}
            project_dir = Path(project["output_dir"]).resolve()

            not_edible = postprocess.setdefault("not_edible", {"enabled": False})
            if not_edible.get("enabled"):
                try:
                    watermark_task = task_by_slot.get("WM-01")
                    watermark_version = self._current_version(watermark_task or {})
                    placement_sources: list[dict[str, Any]] = []
                    for slot_id in ("03", "04", "05", "06"):
                        task = task_by_slot.get(slot_id)
                        version = self._current_version(task or {})
                        if not task or not version:
                            continue
                        placement_sources.append(
                            {
                                "slot_id": slot_id,
                                "task_name": task["task_name"],
                                "source_path": self.storage.resolve_relative(project_dir, version["file_path"]),
                                "source_version_id": version["id"],
                                "watermark_version_id": watermark_version["id"] if watermark_version else "",
                            }
                        )
                    if watermark_task and watermark_task.get("status") == "failed":
                        not_edible["status"] = "failed"
                        not_edible["error"] = watermark_task.get("last_error", "")
                        not_edible["transparent_asset_path"] = ""
                        not_edible["outputs"] = {}
                    elif not watermark_version:
                        not_edible["status"] = (
                            "waiting"
                        )
                        not_edible["error"] = (
                            watermark_task.get("last_error", "") if watermark_task else "禁止食用水印任务不存在"
                        )
                    elif not placement_sources:
                        not_edible["status"] = "waiting"
                        not_edible["error"] = "等待氛围摆放图生成完成"
                    else:
                        existing_outputs = not_edible.get("outputs") or {}

                        def output_is_current(item: dict[str, Any]) -> bool:
                            output = existing_outputs.get(item["slot_id"]) or {}
                            relative_path = str(output.get("file_path") or "")
                            if (
                                output.get("source_version_id") != item["source_version_id"]
                                or output.get("watermark_version_id") != watermark_version["id"]
                                or not relative_path
                            ):
                                return False
                            try:
                                return self.storage.resolve_relative(project_dir, relative_path).is_file()
                            except ValueError:
                                return False

                        is_current = all(output_is_current(item) for item in placement_sources)
                        if not is_current:
                            watermark_source = self.storage.resolve_relative(
                                project_dir, watermark_version["file_path"]
                            )
                            not_edible.update(
                                self.postprocess_service.build_watermark_outputs(
                                    project_dir,
                                    watermark_source,
                                    placement_sources,
                                )
                            )
                        else:
                            not_edible["status"] = "ready"
                            not_edible["error"] = ""
                except Exception as exc:
                    not_edible["status"] = "failed"
                    not_edible["error"] = str(exc)

            collage = postprocess.setdefault("collage", {"enabled": False})
            if collage.get("enabled"):
                try:
                    template_task = task_by_slot.get("COL-TEMPLATE")
                    text_task = task_by_slot.get("COL-TEXT")
                    template_version = self._current_version(template_task or {})
                    text_version = self._current_version(text_task or {})
                    scene_sources: list[dict[str, Any]] = []
                    for slot_id in collage.get("scene_slots", []):
                        task = task_by_slot.get(str(slot_id))
                        version = self._current_version(task or {})
                        if not task or not version:
                            continue
                        scene_sources.append(
                            {
                                "slot_id": str(slot_id),
                                "task_name": task["task_name"],
                                "source_path": self.storage.resolve_relative(project_dir, version["file_path"]),
                                "source_version_id": version["id"],
                            }
                        )
                    if not template_version or not text_version or len(scene_sources) < 4:
                        special_failed = any(
                            task and task.get("status") == "failed"
                            for task in (template_task, text_task)
                        )
                        collage["status"] = "failed" if special_failed else "waiting"
                        collage["error"] = (
                            "场景模板或艺术文字 Agent 生成失败"
                            if special_failed
                            else "等待场景模板、艺术文字和至少4张场景图生成完成"
                        )
                    else:
                        source_versions = {
                            "COL-TEMPLATE": template_version["id"],
                            "COL-TEXT": text_version["id"],
                            **{
                                item["slot_id"]: item["source_version_id"]
                                for item in scene_sources
                            },
                        }
                        package_relative = str(collage.get("package_path") or "")
                        package_exists = bool(
                            package_relative
                            and self.storage.resolve_relative(project_dir, package_relative).is_file()
                        )
                        if collage.get("source_versions") != source_versions or not package_exists:
                            collage.update(
                                self.postprocess_service.build_collage_package(
                                    project_dir,
                                    project["product_name"],
                                    self.storage.resolve_relative(project_dir, template_version["file_path"]),
                                    self.storage.resolve_relative(project_dir, text_version["file_path"]),
                                    scene_sources,
                                    source_versions,
                                )
                            )
                        else:
                            collage["status"] = "ready"
                            collage["error"] = ""
                except Exception as exc:
                    collage["status"] = "failed"
                    collage["error"] = str(exc)

            self.repo.update_project(project_id, postprocess=postprocess)
            self._write_manifest(project_id)

    def approve(self, task_id: str, version_id: str) -> None:
        task = self.repo.get_task(task_id)
        if not task or not any(v["id"] == version_id for v in task.get("versions", [])):
            raise ValueError("版本不存在")
        self.repo.select_version(task_id, version_id)
        self.finalize_project_assets(task["project_id"])
        self._refresh_project_status(task["project_id"])
        self._write_manifest(task["project_id"])

    def favorite(self, task_id: str, version_id: str, prompt: str = "") -> dict[str, str]:
        project, task, project_dir = self._project_and_task(task_id)
        versions = task.get("versions", [])
        selected = next((version for version in versions if version["id"] == version_id), None)
        if not selected:
            raise ValueError("收藏版本不存在")
        source_image = self.storage.resolve_relative(
            project_dir,
            self.final_relative_path(project, task, selected),
        )
        if not source_image.exists():
            raise ValueError("收藏图片文件不存在")
        visible_prompt = str(prompt or task.get("current_prompt") or selected.get("prompt") or "").strip()
        if not visible_prompt:
            raise ValueError("收藏描述词不能为空")
        return self.storage.archive_favorite(
            source_image,
            project["product_name"],
            task["slot_id"],
            task["task_name"],
            int(selected["version_number"]),
            visible_prompt,
        )

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
