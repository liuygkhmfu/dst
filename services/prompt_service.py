from __future__ import annotations

import base64
import json
import mimetypes
import re
import urllib.request
from pathlib import Path
from typing import Any

from config.runtime_config import RuntimeConfigStore
from config.task_definitions import TASK_DEFINITIONS


DEFAULT_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept": "application/json",
}
REFERENCE_VARIABLE_MARKER = "【参考图变量约束】"
REFERENCE_VARIABLE_END = "这些变量名称只用于指代输入图片，不得作为文字出现在最终画面中。"
PROMPT_VARIABLES = [
    {"name": "产品外观参考图", "kind": "动态图片变量", "source": "单品解析为 stt；系列品解析为 cpt"},
    {"name": "手托比例参考图", "kind": "图片变量", "source": "原工作流 stt；锁定手与产品的真实比例，单品同时锁定外观"},
    {"name": "系列外观参考图", "kind": "图片变量", "source": "原工作流 cpt；仅系列品使用并锚定具体产品外观"},
    {"name": "额外需求参考图", "kind": "图片变量", "source": "每条额外需求单独上传的参考图"},
    {"name": "stt", "kind": "兼容图片变量", "source": "等同手托比例参考图"},
    {"name": "cpt", "kind": "兼容图片变量", "source": "等同系列外观参考图"},
    {"name": "产品名", "kind": "文字变量", "source": "新建项目时填写的产品名称"},
    {"name": "产品文字信息", "kind": "文字变量", "source": "产品描述、材质和回弹类型"},
    {"name": "产品数量", "kind": "文字变量", "source": "新建项目时填写的产品数量"},
    {"name": "摆放展示要求", "kind": "文字变量", "source": "仅传给摆放图"},
    {"name": "自定义使用场景", "kind": "文字变量", "source": "仅传给图11"},
    {"name": "产品尺寸", "kind": "文字变量", "source": "用户填写的数值信息，用于尺寸图标注；不能替代 stt 比例依据"},
    {"name": "是否系列品", "kind": "文字变量", "source": "项目的单品/系列品选择"},
]


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def parse_prompt_response(value: Any) -> dict[str, str]:
    """Accept strict JSON first, then the original |#| / 【图N】 format."""
    if isinstance(value, dict):
        nested: dict[str, str] = {}
        for key, item in value.items():
            match = re.fullmatch(r"(?:图\s*)?0?(\d{1,2})", str(key).strip())
            if not match:
                continue
            candidate = item
            if isinstance(item, dict):
                candidate = item.get("prompt") or item.get("final_prompt") or item.get("text") or item.get("content")
            if isinstance(candidate, str) and candidate.strip():
                nested[match.group(1).zfill(2)] = candidate.strip()
        if nested:
            return nested
        if value and all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
            return {str(key).zfill(2): item.strip() for key, item in value.items() if item.strip()}
        value = value.get("text") or value.get("content") or value.get("result") or value
    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in value:
            if isinstance(item, dict) and item.get("id") and item.get("prompt"):
                result[str(item["id"]).zfill(2)] = str(item["prompt"]).strip()
        return result
    text = str(value or "").strip()
    try:
        parsed = json.loads(text.strip("` \n"))
        return parse_prompt_response(parsed)
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    result = {}
    for piece in text.split("|#|"):
        match = re.search(r"图\s*0?(\d{1,2})", piece)
        if match:
            prompt = re.sub(r"^.*?图\s*0?\d{1,2}[】\]:：\s-]*", "", piece, count=1).strip()
            if prompt:
                result[match.group(1).zfill(2)] = prompt
    return result


class PromptService:
    def __init__(self, settings, config_store: RuntimeConfigStore | None = None):
        self.settings = settings
        self.config_store = config_store or RuntimeConfigStore(settings.data_dir)

    def task_definitions(self) -> list[dict[str, Any]]:
        return self.config_store.task_definitions()

    def group_constraints(self) -> dict[str, str]:
        return self.config_store.group_constraints()

    def constraints_for(self, prompt_group: str) -> str:
        return self.config_store.constraint_for(prompt_group)

    @staticmethod
    def template_values(project: dict[str, Any]) -> dict[str, str]:
        is_series = bool(project.get("is_series"))
        scale_reference = "【手托比例参考图】"
        series_appearance = "【系列外观参考图】" if project.get("input_series_path") else "缺少系列外观参考图"
        appearance_reference = series_appearance if is_series else scale_reference
        return {
            "产品外观参考图": appearance_reference,
            "手托比例参考图": scale_reference,
            "系列外观参考图": series_appearance,
            "额外需求参考图": "【额外需求参考图】",
            "stt": scale_reference,
            "cpt": series_appearance,
            "产品名": str(project.get("product_name") or "未命名产品"),
            "产品文字信息": str(project.get("product_description") or "未补充产品文字信息"),
            "产品描述": str(project.get("product_description") or "未补充产品文字信息"),
            "产品数量": str(project.get("product_count") or 1),
            "摆放展示要求": str(project.get("display_requirements") or "干净、可爱、适合电商展示的摆放方式"),
            "自定义使用场景": str(project.get("custom_scene") or "自然真实的日常使用场景"),
            "产品尺寸": str(project.get("product_dimensions") or "未提供尺寸数值，不得编造数字"),
            "是否系列品": "是" if project.get("is_series") else "否",
        }

    @classmethod
    def render_template(cls, template: str, project: dict[str, Any]) -> str:
        values = cls.template_values(project)

        def replace(match: re.Match[str]) -> str:
            name = match.group(1).strip()
            return values.get(name, match.group(0))

        return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", replace, str(template or ""))

    @staticmethod
    def _logic_name(item: dict[str, Any]) -> str:
        return str(item.get("logic") or {"placement": "氛围摆放", "usage_scene": "使用场景", "scene": "使用场景", "size": "尺寸展示"}.get(item.get("prompt_group"), "电商图片"))

    @staticmethod
    def _usable_prompt(value: Any) -> bool:
        text = str(value or "").strip()
        if len(text) < 80 or not re.search(r"[\u3400-\u9fff]", text):
            return False
        if re.search(r"realistic camera|product description context|this squishy toy|only edit the first input image", text, re.I):
            return False
        return True

    @staticmethod
    def _normalize_chinese_prompt(value: Any) -> str:
        text = str(value or "")
        replacements = (
            (r"(?i)(?<![A-Za-z])4K(?![A-Za-z])", "超清"),
            (r"(?i)(?<![A-Za-z])CG(?![A-Za-z])", "计算机生成"),
            (r"(?i)(?<![A-Za-z])PU(?![A-Za-z])", "聚氨酯"),
            (r"(?i)(?<![A-Za-z])PVC(?![A-Za-z])", "聚氯乙烯"),
            (r"(?i)(?<![A-Za-z])EVA(?![A-Za-z])", "乙烯醋酸乙烯酯"),
            (r"(?i)(?<![A-Za-z])TPR(?![A-Za-z])", "热塑性橡胶"),
            (r"(?i)(?<![A-Za-z])ABS(?![A-Za-z])", "丙烯腈丁二烯苯乙烯"),
            (r"(?i)(?<![A-Za-z])cm(?![A-Za-z])", "厘米"),
            (r"(?i)(?<![A-Za-z])(?:inches|inch|in)(?![A-Za-z])", "英寸"),
            (r"(?<=\d)\s*[xX]\s*(?=\d)", " × "),
        )
        for pattern, replacement in replacements:
            text = re.sub(pattern, replacement, text)
        # 网关偶尔仍会夹带英文摄影术语；最终交给生图模型的描述词严格保持全中文。
        text = re.sub(r"[A-Za-z]+", "", text)
        return re.sub(r"[ \t]{2,}", " ", text)

    @classmethod
    def _clean_prompt(cls, value: Any) -> str:
        text = str(value or "").strip().strip("`").strip()
        text = re.sub(r"^(?:提示词|完整描述词|最终描述词)\s*[:：]\s*", "", text)
        text = re.sub(r"^【?图\s*\d{1,2}】?\s*[:：\-]?\s*", "", text)
        return cls._normalize_chinese_prompt(text).strip()

    @staticmethod
    def _task_user_inputs(project: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        """Expose only the user inputs relevant to this one workflow task."""
        inputs: dict[str, Any] = {
            "产品文字信息": project.get("product_description") or "未补充，仅以图片变量锁定产品",
            "产品外观参考图变量": PromptService.template_values(project)["产品外观参考图"],
            "手托比例参考图变量": PromptService.template_values(project)["手托比例参考图"],
        }
        group = str(item.get("prompt_group") or "")
        if group == "atmosphere":
            inputs["产品数量"] = project.get("product_count") or 1
            inputs["摆放展示要求"] = project.get("display_requirements") or "干净、可爱、适合电商展示"
        elif group == "scene":
            inputs["产品尺寸"] = project.get("product_dimensions") or "未提供尺寸数值，真实比例仍以手托比例参考图为准"
            if item.get("id") == "11":
                inputs["自定义使用场景"] = project.get("custom_scene") or "自然真实的日常使用场景"
        elif group == "size":
            inputs["产品尺寸"] = project.get("product_dimensions") or "未提供，不得编造数值，只保留三根尺寸线"
        return inputs

    @staticmethod
    def _reference_variable_contract(project: dict[str, Any], item: dict[str, Any]) -> str:
        if project.get("is_series"):
            contract = (
                f"{REFERENCE_VARIABLE_MARKER}本项目是系列品：把实际上传的系列产品外观图片定义为【系列外观参考图】，"
                "产品的款式、颜色、造型、图案、结构、表面细节和材质只能由它锚定；"
                "把实际上传的手托产品图片定义为【手托比例参考图】，只用于锁定手与产品的真实大小比例，"
                "不得用手托比例参考图覆盖或改写系列外观参考图中的产品外观。"
            )
        else:
            contract = (
                f"{REFERENCE_VARIABLE_MARKER}本项目是单品：把实际上传的手托产品图片定义为【手托比例参考图】，"
                "该图片同时是产品外观与手托比例的唯一依据，锁定款式、颜色、造型、图案、结构、比例、表面细节和材质。"
            )
        return contract + REFERENCE_VARIABLE_END

    @classmethod
    def _with_reference_variable_contract(cls, prompt: str, project: dict[str, Any], item: dict[str, Any]) -> str:
        text = str(prompt or "").strip()
        if text.startswith(REFERENCE_VARIABLE_MARKER):
            end = text.find(REFERENCE_VARIABLE_END)
            if end >= 0:
                text = text[end + len(REFERENCE_VARIABLE_END):].lstrip()
        return cls._reference_variable_contract(project, item) + text

    def build_local_prompts(self, project: dict[str, Any], enabled_ids: list[str] | None = None) -> dict[str, str]:
        definitions = self.task_definitions()
        enabled = set(enabled_ids or [item["id"] for item in definitions])
        product = project.get("product_description") or "用户未补充产品文字信息"
        display = project.get("display_requirements") or "干净、可爱、适合电商展示的摆放方式"
        custom = project.get("custom_scene") or "自然真实的日常使用场景"
        dimensions = project.get("product_dimensions") or "未提供尺寸数值，只保留清晰尺寸线，不得编造数字"
        prompts: dict[str, str] = {}
        for item in definitions:
            if item["id"] not in enabled:
                continue
            slot = item["id"]
            logic = self._logic_name(item)
            template_values = self.template_values(project)
            appearance_reference = template_values["产品外观参考图"]
            scale_reference = template_values["手托比例参考图"]
            group_constraints = self.render_template(self.constraints_for(item["prompt_group"]), project)
            rendered_brief = self.render_template(item["brief"], project)
            if item["prompt_group"] == "atmosphere":
                variant = {
                    "03": f"以用户摆放要求为第一优先：{display}。产品作为唯一视觉主体，居中完整展示，画面中不出现人体。",
                    "04": "加入少量、最多两件、与产品主题同源的道具，背景简洁并与产品形成协调对比，不能抢主体。",
                    "05": "加入三件以内与产品设计灵感相符的道具和层次关系，画面丰富但保持留白、秩序和电商主图清晰度。",
                    "06": "加入与产品主题呼应的食物或生活道具营造拟真氛围；道具可以是真实食物，但产品本身绝不能被改造成真实食物。",
                }.get(slot, item["brief"])
            elif item["prompt_group"] == "scene":
                variant = {
                    "07": "室内第三人称抓拍视角，妈妈弯腰把产品递给美国小学阶段孩子，孩子伸手准备接过并以肢体表现惊喜，不出现礼盒，完整面部不入镜。",
                    "08": "室内温馨自然的亲子送礼互动，产品是送礼核心物品，动作符合真实物理逻辑；系列品外观严格由系列外观参考图锚定，手托比例参考图只控制真实大小比例。",
                    "09": "真实美国职场办公环境，对面工位第三人称视角，女性坐在电脑前自然握持并轻微按压产品，表现放松解压；只拍肩部以下或局部下半脸，不出现完整面部。",
                    "10": "生日蛋糕旁，一个美国孩子惊喜地拿着产品，另外两个孩子从左右两侧围拢观察，其中一人准备伸手触碰；突出兴趣、互动和惊喜，完整面部不入镜。",
                    "11": f"将用户自定义场景扩写为真实可执行的电商摄影画面，用户场景为：{custom}。",
                    "12": "美国小学教室侧中后排第三人称视角，产品与书本文具放在讲台，老师在旁边指向产品且只拍肩部以下；前排学生以背影或侧后方轮廓举手回答，体现教学互动。",
                }.get(slot, item["brief"])
            else:
                variant = f"在同一张完整画面中完成尺寸展示：左上角用精致女性手托产品体现真实比例，右下角展示同一产品并添加长、宽、高三根对应的尺寸线、虚线和箭头。用户提供的尺寸为：{dimensions}。"
            prompt = (
                f"这是单独生成的第{slot}张{logic}电商图片，不能与其他任务合并，也不能省略本张图片的画面要求。"
                f"{group_constraints} "
                f"本张图片的具体构图和动作要求：{variant} "
                f"本任务内置要求：{rendered_brief} "
                f"产品文字信息仅作为材质和使用语境参考：{product}。产品外观、颜色、造型、图案、结构、表面细节和材质表现必须完全以{appearance_reference}为准；手与产品的真实大小比例必须以{scale_reference}为准。提示词不要自行描写或创造产品外观。"
                "画面比例固定为1:1，产品必须清晰可见且是视觉主体；不得出现与要求无关的文字、标志、畸形手部、虚假光影或明显生成痕迹。"
            )
            prompts[slot] = self._with_reference_variable_contract(
                self._normalize_chinese_prompt(prompt), project, item
            )
        return prompts

    def build_extra_prompt(self, project: dict[str, Any], requirement: str) -> str:
        product = project.get("product_description") or "参考图中的产品"
        base_contract = self._reference_variable_contract(project, {}).replace(REFERENCE_VARIABLE_END, "")
        appearance_reference = self.template_values(project)["产品外观参考图"]
        prompt = (
            f"{base_contract}把本条需求上传的其他图片依次定义为【额外需求参考图】；"
            f"【额外需求参考图】只用于本条需求的构图、动作、风格或氛围，发生冲突时必须以{appearance_reference}为外观最高优先级。"
            f"{REFERENCE_VARIABLE_END}这是单独生成的一张额外参考图需求图片，不能与其他任务合并。"
            "画面采用真实相机摄影，比例固定为1:1，产品必须作为视觉主体；"
            f"本次具体要求是：{requirement.strip()}。产品文字信息仅作为使用语境参考：{product}。"
            "必须保持产品外观设计、颜色、造型、结构、表面细节和大小比例完全不变，不得复制额外参考图中的无关人物、文字、标志或背景。"
        )
        return self._normalize_chinese_prompt(prompt)

    @staticmethod
    def _analysis_image_inputs(project: dict[str, Any], stt_path: Path, cpt_path: Path | None = None) -> list[dict[str, Any]]:
        if project.get("is_series"):
            if not cpt_path or not cpt_path.exists():
                raise ValueError("系列品缺少 cpt 系列外观参考图")
            return [
                {"path": cpt_path, "label": "系列外观参考图", "purpose": "锚定具体产品外观"},
                {"path": stt_path, "label": "手托比例参考图", "purpose": "只锚定手与产品的真实大小比例"},
            ]
        return [
            {"path": stt_path, "label": "手托比例参考图", "purpose": "单品同时锚定产品外观与手托比例"},
        ]

    def _request_prompts(self, project: dict[str, Any], image_inputs: list[dict[str, Any]], definitions: list[dict[str, Any]], ids: list[str]) -> dict[str, str]:
        base = self.settings.prompt_api_base_url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        items = [item for item in definitions if item["id"] in ids]
        groups = {str(item["prompt_group"]) for item in items}
        if len(groups) != 1:
            raise ValueError("每次描述词分析只能处理一种工作流类型")
        prompt_group = str(items[0]["prompt_group"]) if items else "atmosphere"
        instruction = {
            "分析参考图输入顺序": [
                {"输入序号": index, "变量": item["label"], "用途": item["purpose"]}
                for index, item in enumerate(image_inputs, 1)
            ],
            "任务": [
                {
                    "编号": item["id"],
                    "名称": item["name"],
                    "逻辑": self._logic_name(item),
                    "内置描述词模板": item["brief"],
                    "渲染后的内置要求": self.render_template(item["brief"], project),
                    "参考图变量约束": self._reference_variable_contract(project, item),
                    "仅供本图使用的用户输入": self._task_user_inputs(project, item),
                }
                for item in items
            ],
            "当前图片类型": self._logic_name(items[0]) if items else "电商图片",
            "本类型专属全局规则模板": self.constraints_for(prompt_group),
            "渲染后的本类型专属全局规则": self.render_template(self.constraints_for(prompt_group), project),
            "输出要求": "必须为任务列表中的每一个编号分别生成一条独立、完整、可直接用于图片生成的中文描述词。绝对不能合并、跳过、复用或省略任何一张图。只返回 JSON 对象，键必须是两位任务编号，值必须是全中文描述词；单位写作厘米和英寸，不要使用英文字母，不要输出 Markdown、解释、备注或编号标签。每张图只能使用该任务自己的用户输入，严禁把其他图片类型或其他编号的用户输入混入本图。单品的外观和比例都以手托比例参考图为准；系列品的外观只能由系列外观参考图锚定，手托比例参考图只控制大小比例，不得覆盖外观。",
        }
        content_parts = [{"type": "text", "text": json.dumps(instruction, ensure_ascii=False)}]
        content_parts.extend(
            {"type": "image_url", "image_url": {"url": _data_url(item["path"])}}
            for item in image_inputs
        )
        body = json.dumps({"model": self.settings.prompt_model, "messages": [{"role": "user", "content": content_parts}]}, ensure_ascii=False).encode()
        req = urllib.request.Request(base + "/chat/completions", data=body, headers={**DEFAULT_API_HEADERS, "Authorization": f"Bearer {self.settings.prompt_api_key}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.settings.api_timeout_seconds) as response:
            raw = json.loads(response.read().decode("utf-8"))
        content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        return parse_prompt_response(content)

    def generate_prompts(
        self,
        project: dict[str, Any],
        image_path: Path | None,
        enabled_ids: list[str] | None = None,
        cpt_path: Path | None = None,
    ) -> dict[str, str]:
        if not (self.settings.prompt_api_base_url and self.settings.prompt_api_key and self.settings.prompt_model and image_path and image_path.exists()):
            return self.build_local_prompts(project, enabled_ids)
        try:
            definitions = self.task_definitions()
            ids = enabled_ids or [item["id"] for item in definitions]
            local = self.build_local_prompts(project, enabled_ids)
            image_inputs = self._analysis_image_inputs(project, image_path, cpt_path)
            accepted: dict[str, str] = {}
            grouped_ids: dict[str, list[str]] = {}
            for item in definitions:
                if item["id"] in ids:
                    grouped_ids.setdefault(str(item["prompt_group"]), []).append(item["id"])
            # 三种工作流分别调用分析模型，确保每批只继承本类型的全局约束。
            for group_task_ids in grouped_ids.values():
                try:
                    prompts = self._request_prompts(project, image_inputs, definitions, group_task_ids)
                except Exception:
                    prompts = {}
                for key, value in prompts.items():
                    candidate = self._clean_prompt(value)
                    if key in group_task_ids and key in local and self._usable_prompt(candidate):
                        accepted[key] = candidate
                missing = [task_id for task_id in group_task_ids if task_id not in accepted]
                # 若分组返回漏掉任何一个槽位，逐张补问一次，仍然沿用该图所属类型的专属约束。
                for task_id in missing:
                    try:
                        one = self._request_prompts(project, image_inputs, definitions, [task_id])
                        candidate = self._clean_prompt(one.get(task_id, ""))
                        if self._usable_prompt(candidate):
                            accepted[task_id] = candidate
                    except Exception:
                        continue
            local.update({key: val for key, val in accepted.items() if key in local})
            definition_map = {item["id"]: item for item in definitions}
            return {
                task_id: self._with_reference_variable_contract(local[task_id], project, definition_map[task_id])
                for task_id in ids
            }
        except Exception:
            # 提示词接口失败时仍返回逐张完整中文模板，不能因为分析接口暂时失败而丢任务或拼出半截提示词。
            return self.build_local_prompts(project, enabled_ids)
