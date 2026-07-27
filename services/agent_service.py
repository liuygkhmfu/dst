from __future__ import annotations

import json
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from database.repositories import now_iso


AGENT_IMAGE_SIZES: tuple[dict[str, str], ...] = (
    {"value": "1024x1024", "label": "1:1 正方形（1024×1024）"},
    {"value": "1536x1024", "label": "3:2 横图（1536×1024）"},
    {"value": "1024x1536", "label": "2:3 竖图（1024×1536）"},
)
AGENT_IMAGE_QUALITIES: tuple[dict[str, str], ...] = (
    {"value": "auto", "label": "自动"},
    {"value": "low", "label": "低"},
    {"value": "medium", "label": "中"},
    {"value": "high", "label": "高"},
)
AGENT_PROJECT_VARIABLES: tuple[dict[str, str], ...] = (
    {"name": "产品名", "label": "产品名称", "kind": "text", "source": "新建项目：产品名称"},
    {"name": "产品文字信息", "label": "产品描述、材质和回弹类型", "kind": "text", "source": "新建项目：产品描述、材质和回弹类型"},
    {"name": "产品描述", "label": "产品描述（兼容名称）", "kind": "text", "source": "等同产品文字信息"},
    {"name": "产品数量", "label": "产品数量", "kind": "number", "source": "新建项目：产品数量"},
    {"name": "摆放展示要求", "label": "展示图要求", "kind": "textarea", "source": "新建项目：展示图要求"},
    {"name": "自定义使用场景", "label": "自定义使用场景", "kind": "textarea", "source": "新建项目：自定义使用场景"},
    {"name": "产品尺寸", "label": "产品尺寸", "kind": "text", "source": "新建项目：产品尺寸"},
    {"name": "是否系列品", "label": "产品类型", "kind": "select", "source": "新建项目：单品/系列品"},
    {"name": "产品外观参考图", "label": "产品外观参考图", "kind": "image", "source": "单品映射 stt；系列品映射 cpt"},
    {"name": "手托比例参考图", "label": "stt 手托比例参考图", "kind": "image", "source": "新建项目：stt"},
    {"name": "系列外观参考图", "label": "cpt 系列外观参考图", "kind": "image", "source": "新建项目：cpt，仅系列品"},
    {"name": "stt", "label": "stt（兼容名称）", "kind": "image", "source": "等同手托比例参考图"},
    {"name": "cpt", "label": "cpt（兼容名称）", "kind": "image", "source": "等同系列外观参考图"},
    {"name": "Agent参考图", "label": "Agent 任务参考图", "kind": "image", "source": "优先使用 Agent 固定绑定图，也可在测试或套图任务中临时追加多张"},
)


class AgentStore:
    """Persist reusable image-agent definitions and their fixed task references."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.path = self.data_dir / "agents.json"
        self.assets_root = (self.data_dir / "agent_assets").resolve()
        self.runs_root = (self.data_dir / "agent_runs").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.assets_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, list):
                return []
            return [self._clean_agent(item) for item in value if isinstance(item, dict)]
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def _save(self, agents: list[dict[str, Any]]) -> None:
        content = json.dumps(agents, ensure_ascii=False, indent=2).encode("utf-8")
        with tempfile.NamedTemporaryFile(prefix=".agents_", suffix=".json", dir=self.data_dir, delete=False) as temp:
            temp.write(content)
            temp.flush()
            temp_path = Path(temp.name)
        temp_path.replace(self.path)

    @staticmethod
    def detect_project_variables(prompt: Any) -> list[str]:
        used = {
            match.group(1).strip()
            for match in re.finditer(r"\{\{\s*([^{}]+?)\s*\}\}", str(prompt or ""))
        }
        return [item["name"] for item in AGENT_PROJECT_VARIABLES if item["name"] in used]

    @classmethod
    def _clean_agent(cls, item: dict[str, Any]) -> dict[str, Any]:
        size_values = {option["value"] for option in AGENT_IMAGE_SIZES}
        quality_values = {option["value"] for option in AGENT_IMAGE_QUALITIES}
        agent_id = str(item.get("id") or "").strip()
        expected_prefix = f"agent_assets/{agent_id}/"
        reference_paths = []
        for value in item.get("reference_paths") or []:
            normalized = str(value or "").replace("\\", "/").strip()
            if normalized.startswith(expected_prefix) and ".." not in Path(normalized).parts:
                reference_paths.append(normalized)
        return {
            "id": agent_id,
            "name": str(item.get("name") or "未命名 Agent").strip(),
            "prompt": str(item.get("prompt") or "").strip(),
            "image_size": (
                str(item.get("image_size") or "1024x1024").strip()
                if str(item.get("image_size") or "") in size_values
                else "1024x1024"
            ),
            "image_quality": (
                str(item.get("image_quality") or "medium").strip().lower()
                if str(item.get("image_quality") or "").strip().lower() in quality_values
                else "medium"
            ),
            "project_variables": cls.detect_project_variables(item.get("prompt", "")),
            "input_schema": [],
            "reference_paths": reference_paths[:8],
            "created_at": str(item.get("created_at") or now_iso()),
            "updated_at": str(item.get("updated_at") or now_iso()),
        }

    def list_agents(self) -> list[dict[str, Any]]:
        return sorted(self._load(), key=lambda item: item.get("updated_at", ""), reverse=True)

    def get(self, agent_id: str) -> dict[str, Any] | None:
        key = str(agent_id or "").strip()
        return next((item for item in self._load() if item["id"] == key), None)

    def _replace_reference_files(self, agent_id: str, uploads: list[dict[str, Any]]) -> list[str]:
        target_dir = (self.assets_root / agent_id).resolve()
        target_dir.relative_to(self.assets_root)
        stage_dir = Path(tempfile.mkdtemp(prefix=f".{agent_id}_", dir=self.assets_root)).resolve()
        try:
            staged: list[Path] = []
            for index, upload in enumerate(uploads[:8], 1):
                if not upload.get("content"):
                    continue
                suffix = Path(str(upload.get("filename") or "")).suffix.lower()
                if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                    suffix = ".png"
                if suffix == ".jpeg":
                    suffix = ".jpg"
                stem = re.sub(r"[^\w\u3400-\u9fff-]+", "_", Path(str(upload.get("filename") or "")).stem).strip("_")
                target = stage_dir / f"fixed_{index:02d}_{stem[:40] or 'reference'}{suffix}"
                target.write_bytes(upload["content"])
                staged.append(target)
            if target_dir.exists():
                shutil.rmtree(target_dir)
            stage_dir.replace(target_dir)
            return [
                path.relative_to(self.data_dir).as_posix()
                for path in sorted(target_dir.iterdir())
                if path.is_file()
            ]
        except Exception:
            if stage_dir.exists():
                shutil.rmtree(stage_dir)
            raise

    def save_agent(
        self,
        fields: dict[str, Any],
        reference_uploads: list[dict[str, Any]] | None = None,
        **_legacy_kwargs: Any,
    ) -> dict[str, Any]:
        agents = self._load()
        requested_id = str(fields.get("agent_id") or "").strip()
        existing = next((item for item in agents if item["id"] == requested_id), None) if requested_id else None
        agent_id = existing["id"] if existing else f"agent-{uuid.uuid4().hex[:10]}"
        name = str(fields.get("name") or "").strip()
        prompt = str(fields.get("prompt") or "").strip()
        image_size = str(fields.get("image_size") or "1024x1024").strip()
        image_quality = str(fields.get("image_quality") or "medium").strip().lower()
        if not name:
            raise ValueError("Agent 名称不能为空")
        if not prompt:
            raise ValueError("Agent 描述词不能为空")
        if image_size not in {item["value"] for item in AGENT_IMAGE_SIZES}:
            raise ValueError("Agent 图片比例不受支持")
        if image_quality not in {item["value"] for item in AGENT_IMAGE_QUALITIES}:
            raise ValueError("Agent 图片清晰度不受支持")
        uploads = [item for item in (reference_uploads or [])[:8] if item.get("content")]
        clear_references = str(fields.get("clear_fixed_references") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        reference_paths = list(existing.get("reference_paths") or []) if existing else []
        if uploads:
            reference_paths = self._replace_reference_files(agent_id, uploads)
        elif clear_references:
            target_dir = (self.assets_root / agent_id).resolve()
            target_dir.relative_to(self.assets_root)
            if target_dir.exists():
                shutil.rmtree(target_dir)
            reference_paths = []
        timestamp = now_iso()
        saved = {
            "id": agent_id,
            "name": name,
            "prompt": prompt,
            "image_size": image_size,
            "image_quality": image_quality,
            "project_variables": self.detect_project_variables(prompt),
            "input_schema": [],
            "reference_paths": reference_paths,
            "created_at": existing.get("created_at", timestamp) if existing else timestamp,
            "updated_at": timestamp,
        }
        agents = [saved if item["id"] == agent_id else item for item in agents]
        if not existing:
            agents.append(saved)
        self._save(agents)
        return saved

    def reference_files(self, agent: dict[str, Any] | str) -> list[Path]:
        item = self.get(agent) if isinstance(agent, str) else agent
        if not item:
            return []
        files: list[Path] = []
        for relative in item.get("reference_paths") or []:
            path = (self.data_dir / relative).resolve()
            try:
                path.relative_to(self.assets_root)
            except ValueError:
                continue
            if path.is_file():
                files.append(path)
        return files

    def resolve_reference_file(self, agent_id: str, relative_path: str) -> Path:
        agent = self.get(agent_id)
        normalized = str(relative_path or "").replace("\\", "/")
        if not agent or normalized not in set(agent.get("reference_paths") or []):
            raise ValueError("Agent 固定参考图不存在")
        path = (self.data_dir / normalized).resolve()
        path.relative_to(self.assets_root)
        if not path.is_file():
            raise ValueError("Agent 固定参考图文件不存在")
        return path

    def save_run_output(self, agent_id: str, content: bytes, suffix: str = ".png") -> str:
        if not self.get(agent_id):
            raise ValueError("Agent 不存在")
        safe_suffix = suffix if suffix in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
        run_dir = (self.runs_root / agent_id).resolve()
        run_dir.relative_to(self.runs_root)
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / f"run_{uuid.uuid4().hex[:12]}{safe_suffix}"
        with tempfile.NamedTemporaryFile(prefix=".writing_", dir=run_dir, delete=False) as temp:
            temp.write(content)
            temp.flush()
            temp_path = Path(temp.name)
        temp_path.replace(target)
        return target.relative_to(self.data_dir).as_posix()

    def resolve_run_file(self, agent_id: str, relative_path: str) -> Path:
        normalized = str(relative_path or "").replace("\\", "/")
        expected_prefix = f"agent_runs/{agent_id}/"
        if not self.get(agent_id) or not normalized.startswith(expected_prefix):
            raise ValueError("Agent 测试结果不存在")
        path = (self.data_dir / normalized).resolve()
        path.relative_to(self.runs_root)
        if not path.is_file():
            raise ValueError("Agent 测试结果文件不存在")
        return path

    def delete(self, agent_id: str) -> None:
        agents = self._load()
        target = next((item for item in agents if item["id"] == str(agent_id or "")), None)
        if not target:
            raise ValueError("Agent 不存在")
        self._save([item for item in agents if item["id"] != target["id"]])
        for root in (self.assets_root, self.runs_root):
            path = (root / target["id"]).resolve()
            path.relative_to(root)
            if path.exists():
                shutil.rmtree(path)
