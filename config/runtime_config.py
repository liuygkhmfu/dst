from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from config.task_definitions import TASK_DEFINITIONS


PROMPT_GROUPS: dict[str, dict[str, str]] = {
    "size": {
        "label": "功能图",
        "description": "选择一个完整功能图模板作为 GPT-5.5 的描述词生成要求；既可维护尺寸图，也可新增卖点图、结构图等其他功能性图片要求。",
        "mode": "selected_template_analysis",
    },
    "atmosphere": {
        "label": "摆放图",
        "description": "无人物的真实电商摆拍，以产品为绝对主体并按单图要求配置主题道具。",
    },
    "scene": {
        "label": "场景图",
        "description": "美国人物的真实使用场景，严格限制取景、身体细节和产品交互。",
    },
}


DEFAULT_GROUP_CONSTRAINTS: dict[str, str] = {
    "atmosphere": (
        "画面必须为真实相机实拍的电商摄影，禁止背景虚化、计算机生成感、渲染感、虚假光影和不真实合成质感。"
        "整体营造可爱氛围，不得出现人物、手部或任何人体特征。产品外观只以{{产品外观参考图}}为准，最终描述词不得"
        "自行描述、补充或创造产品外观，不得改变颜色、造型、图案、结构、比例、表面细节和材质，不得把产品变成"
        "真实食物。辅以与产品设计灵感同源的少量道具，道具数量和类型服从每张图自己的要求；背景简洁、不抢主体、"
        "不与产品撞色。产品占画面约90%以上，居中、完整、清晰展示。超清细节，画面比例固定为1:1。"
    ),
    "scene": (
        "画面必须为真实相机实拍摄影，禁止计算机生成感、渲染感、过度打光、虚假合成和明显生成痕迹。所有人物均设定为"
        "美国人，动作自然抓拍，不摆拍、不夸张。人物只拍肩部以下，最多出现局部下巴、嘴部或侧脸轮廓，不得出现"
        "完整面部。皮肤、手部、衣物和身体结构必须真实，禁止塑料皮肤、蜡像感和畸形肢体。产品外观严格以"
        "{{产品外观参考图}}为准，手与产品的真实大小比例严格以{{手托比例参考图}}为准；最终描述词不得自行描述产品视觉细节，不得改变颜色、造型、图案、结构、比例、"
        "表面细节和材质。手持、递送、按压等互动必须符合真实物理逻辑。每张场景图必须独立成图，产品始终清晰可见"
        "并作为视觉核心，画面比例固定为1:1。"
    ),
}


DEFAULT_FUNCTION_TEMPLATES: list[dict[str, str]] = [
    {
        "id": "size-01",
        "name": "模板1",
        "prompt": (
            "任务概述：分析{{产品外观参考图}}中这款产品的设计DNA，制作电商尺寸图描述词。"
            "描述词具体任务：在同一个完整背景下，左上角展示女性手托产品，右下角展示产品尺寸标注图；"
            "严禁两个区域泾渭分明或形成分割画面，必须自然融合在同一背景中。"
            "背景要求：整体色调从参考产品主色中提炼，点缀元素从产品主题元素、设计灵感和材质中提炼；"
            "风格简约可爱、高级克制，背景和点缀不能抢夺产品主体。"
            "左上角构图：根据{{手托比例参考图}}中的手托构图，将{{产品外观参考图}}中的同一产品放在精致女性手模手中；"
            "女性手指纤细优美，带精致美甲，直观展示产品真实大小。此区域仅展示产品与手部的比例关系，严禁出现任何尺寸信息、尺寸线或箭头。"
            "右下角构图：把{{产品外观参考图}}中的同一款捏捏玩具作为标注目标，使用正视图平放展示，底部保留自然阴影；"
            "添加清晰干净的尺寸线、虚线、箭头和英文尺寸标签，仅允许长、宽、高三根尺寸线，严禁增加第四根或重复标注。"
            "尺寸标注必须精准对应产品长边、宽边和高边的位置延伸，具有机械制图中轴测图尺寸线的空间对应感。"
            "尺寸文字要求：尺寸数值来自{{产品尺寸}}。用户提供明确尺寸数值时，必须同时显示cm和inch，且清楚区分主辅单位；"
            "用户上传的原始数值和原始尺寸单位使用较大、明亮的产品同色字体，自动换算的另一单位尺寸使用较小黑色字体。"
            "如果{{产品尺寸}}为空或未提供明确数值，绝对不要编造厘米或英寸数值，仅展示长、宽、高三根尺寸线。"
            "右上角加入可爱的PRODUCT SIZE图标，图标设计风格和字体必须契合参考产品；左下角加入从产品设计DNA提炼的拟真元素点缀。"
            "严格约束：必须严格保持{{产品外观参考图}}中的产品款式和全部细节不变，最终描述词严禁出现任何对产品外观的描述性语句，"
            "只能称其为参考图中的捏捏玩具，禁止人工智能自行创造、补充或猜测产品细节。"
            "产品大小比例必须锁定{{手托比例参考图}}中手与产品的真实大小比例。图片比例固定为1:1。"
        ),
    }
]
# 兼容旧代码和旧测试；持久化文件改用 function_templates。
DEFAULT_SIZE_TEMPLATES = DEFAULT_FUNCTION_TEMPLATES


GROUP_ALIASES = {
    "placement": "atmosphere",
    "usage_scene": "scene",
}


class RuntimeConfigStore:
    """Persist user-editable prompt templates separately from Python source code."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.path = self.data_dir / "runtime_config.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _default_payload() -> dict[str, Any]:
        return {
            "group_constraints": copy.deepcopy(DEFAULT_GROUP_CONSTRAINTS),
            "task_briefs": {},
            "function_templates": copy.deepcopy(DEFAULT_FUNCTION_TEMPLATES),
        }

    @staticmethod
    def _clean_function_templates(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return copy.deepcopy(DEFAULT_FUNCTION_TEMPLATES)
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, item in enumerate(value, 1):
            if not isinstance(item, dict):
                continue
            template_id = str(item.get("id") or f"function-{index:02d}").strip()
            name = str(item.get("name") or f"模板{index}").strip()
            prompt = str(item.get("prompt") or "").strip()
            if not template_id or template_id in seen or not prompt:
                continue
            seen.add(template_id)
            result.append({"id": template_id, "name": name, "prompt": prompt})
        return result or copy.deepcopy(DEFAULT_FUNCTION_TEMPLATES)

    def load(self) -> dict[str, Any]:
        defaults = self._default_payload()
        if not self.path.exists():
            return defaults
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("runtime config must be an object")
            stored_groups = value.get("group_constraints")
            if isinstance(stored_groups, dict):
                for group in DEFAULT_GROUP_CONSTRAINTS:
                    candidate = str(stored_groups.get(group) or "").strip()
                    if candidate:
                        defaults["group_constraints"][group] = candidate
            # 旧版只有一个通用约束。它不能安全映射到三种工作流，因此迁移时使用三类工作流默认值。
            defaults["task_briefs"] = value.get("task_briefs") if isinstance(value.get("task_briefs"), dict) else {}
            stored_templates = value.get("function_templates")
            if stored_templates is None:
                stored_templates = value.get("size_templates")
            defaults["function_templates"] = self._clean_function_templates(stored_templates)
            return defaults
        except (OSError, ValueError, json.JSONDecodeError):
            return defaults

    def save(
        self,
        group_constraints: dict[str, str] | None = None,
        task_briefs: dict[str, str] | None = None,
        function_templates: list[dict[str, Any]] | None = None,
        size_templates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        current = self.load()
        if group_constraints is not None:
            for raw_group, value in group_constraints.items():
                group = GROUP_ALIASES.get(str(raw_group), str(raw_group))
                if group not in DEFAULT_GROUP_CONSTRAINTS:
                    continue
                current["group_constraints"][group] = str(value).strip() or DEFAULT_GROUP_CONSTRAINTS[group]
        if task_briefs is not None:
            current["task_briefs"] = {
                str(key).zfill(2): str(value).strip()
                for key, value in task_briefs.items()
                if str(value).strip()
            }
        templates = function_templates if function_templates is not None else size_templates
        if templates is not None:
            current["function_templates"] = self._clean_function_templates(templates)
        temp_path = self.path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)
        return current

    def group_constraints(self) -> dict[str, str]:
        return dict(self.load().get("group_constraints") or DEFAULT_GROUP_CONSTRAINTS)

    def constraint_for(self, prompt_group: str) -> str:
        group = GROUP_ALIASES.get(str(prompt_group), str(prompt_group))
        return self.group_constraints().get(group, DEFAULT_GROUP_CONSTRAINTS["atmosphere"])

    def function_templates(self) -> list[dict[str, str]]:
        return copy.deepcopy(self.load().get("function_templates") or DEFAULT_FUNCTION_TEMPLATES)

    def function_template(self, template_id: str | None) -> dict[str, str]:
        templates = self.function_templates()
        return next((item for item in templates if item["id"] == str(template_id or "")), templates[0])

    def size_templates(self) -> list[dict[str, str]]:
        return self.function_templates()

    def size_template(self, template_id: str | None) -> dict[str, str]:
        return self.function_template(template_id)

    def task_definitions(self) -> list[dict[str, Any]]:
        data = self.load()
        overrides = data.get("task_briefs", {})
        result = copy.deepcopy(TASK_DEFINITIONS)
        for item in result:
            if item.get("prompt_group") == "size":
                continue
            override = overrides.get(item["id"])
            if isinstance(override, str) and override.strip():
                item["brief"] = override.strip()
        return result
