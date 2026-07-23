from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from config.task_definitions import TASK_DEFINITIONS


PROMPT_GROUPS: dict[str, dict[str, str]] = {
    "size": {
        "label": "尺寸图",
        "description": "手持比例、三根尺寸线、厘米/英寸标注与尺寸主题排版。",
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
    "size": (
        "制作一张同一完整背景内的尺寸展示图，画面比例固定为1:1。左上区域由精致女性手模自然手托产品，"
        "只用于展示产品真实比例，不在手持区域添加尺寸文字；右下区域展示同一产品正面视角并保留自然投影。"
        "必须恰好使用长、宽、高三根清晰尺寸线、虚线、箭头和标签，机械对齐且不遮挡产品。"
        "用户提供尺寸时，同时标注厘米和英寸：原始单位数值使用醒目的产品主题色，换算单位使用较小黑色文字；"
        "用户未提供尺寸时绝对不能编造数值，只保留三根尺寸线。右上区域添加与产品风格协调的产品尺寸主题图标，"
        "左下区域加入少量与产品主题相关的装饰。产品身份、外观、颜色、造型、图案、结构、比例、表面细节和材质"
        "完全以{{产品外观参考图}}为准，不得自行描述、补充、重设计或改变；手与产品的真实大小比例必须严格以"
        "{{手托比例参考图}}为准。"
    ),
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
        return {"group_constraints": copy.deepcopy(DEFAULT_GROUP_CONSTRAINTS), "task_briefs": {}}

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
                for group in PROMPT_GROUPS:
                    candidate = str(stored_groups.get(group) or "").strip()
                    if candidate:
                        defaults["group_constraints"][group] = candidate
            # 旧版只有一个通用约束。它不能安全映射到三种工作流，因此迁移时使用三类工作流默认值。
            defaults["task_briefs"] = value.get("task_briefs") if isinstance(value.get("task_briefs"), dict) else {}
            return defaults
        except (OSError, ValueError, json.JSONDecodeError):
            return defaults

    def save(
        self,
        group_constraints: dict[str, str] | None = None,
        task_briefs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        current = self.load()
        if group_constraints is not None:
            for raw_group, value in group_constraints.items():
                group = GROUP_ALIASES.get(str(raw_group), str(raw_group))
                if group not in PROMPT_GROUPS:
                    continue
                current["group_constraints"][group] = str(value).strip() or DEFAULT_GROUP_CONSTRAINTS[group]
        if task_briefs is not None:
            current["task_briefs"] = {
                str(key).zfill(2): str(value).strip()
                for key, value in task_briefs.items()
                if str(value).strip()
            }
        self.path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return current

    def group_constraints(self) -> dict[str, str]:
        return dict(self.load().get("group_constraints") or DEFAULT_GROUP_CONSTRAINTS)

    def constraint_for(self, prompt_group: str) -> str:
        group = GROUP_ALIASES.get(str(prompt_group), str(prompt_group))
        return self.group_constraints().get(group, DEFAULT_GROUP_CONSTRAINTS["atmosphere"])

    def task_definitions(self) -> list[dict[str, Any]]:
        data = self.load()
        overrides = data.get("task_briefs", {})
        result = copy.deepcopy(TASK_DEFINITIONS)
        for item in result:
            override = overrides.get(item["id"])
            if isinstance(override, str) and override.strip():
                item["brief"] = override.strip()
        return result
