from __future__ import annotations

import json
import re
import tempfile
from dataclasses import replace
from pathlib import Path

from config.settings import get_settings
from config.runtime_config import RuntimeConfigStore
from database.db import Database
from database.repositories import Repository
from services.prompt_service import PromptService, parse_prompt_response
from services.storage_service import StorageService, make_mock_png, read_image_info, safe_name


def test_prompt_parser_accepts_json_and_legacy_format():
    assert parse_prompt_response('{"03":"prompt three", "04":"prompt four"}') == {"03": "prompt three", "04": "prompt four"}
    assert parse_prompt_response('{"03":{"prompt":"完整中文描述词"}}') == {"03": "完整中文描述词"}
    parsed = parse_prompt_response("|#|【图3】prompt three|#|【图04】prompt four")
    assert parsed["03"] == "prompt three"
    assert parsed["04"] == "prompt four"


def test_storage_rejects_unsafe_relative_paths(tmp_path: Path):
    storage = StorageService(tmp_path / "outputs")
    project = storage.create_project_dir("abc", "测试产品")
    assert safe_name('a:b?c') == 'a_b_c'
    try:
        storage.resolve_relative(project, "../outside.png")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe path accepted")


def test_mock_image_has_valid_png_signature():
    content = make_mock_png("task-03")
    assert read_image_info(content)["format"] == "png"
    assert len(content) > 100


def test_database_persists_project(tmp_path: Path):
    repo = Repository(Database(tmp_path / "app.db"))
    row = {"id":"p1","product_name":"demo","product_description":"","is_series":0,"product_count":1,"custom_scene":"","display_requirements":"","product_dimensions":"","input_product_path":"input/a.png","input_series_path":None,"output_dir":str(tmp_path),"status":"created","created_at":"now","updated_at":"now"}
    repo.create_project(row)
    assert repo.get_project("p1")["product_name"] == "demo"


def test_extra_reference_request_creates_independent_task(tmp_path: Path):
    from services.generation_service import GenerationService

    settings = get_settings(tmp_path)
    settings = replace(settings, data_dir=tmp_path / "data", output_root=tmp_path / "outputs", db_path=tmp_path / "data" / "app.db", mock_mode=True)
    repo = Repository(Database(settings.db_path))
    service = GenerationService(settings, repo, StorageService(settings.output_root))
    product = {"filename": "product.png", "content": make_mock_png("product"), "content_type": "image/png"}
    reference = {"filename": "style.png", "content": make_mock_png("style"), "content_type": "image/png"}
    project_id = service.create_project(
        {"product_name": "demo", "enabled_tasks": '["03"]', "extra_requests": '[{"upload_key":"extra_0","requirement":"warm desk"}]'},
        product,
        None,
        {"extra_0": [reference]},
    )
    service._run_initial(project_id)
    tasks = repo.get_tasks(project_id)
    assert [task["slot_id"] for task in tasks] == ["03", "XR-01"]
    assert all(len(task["versions"]) == 1 for task in tasks)
    assert len(repo.get_extra_requests(project_id)[0]["reference_paths"]) == 1
    extra_task = next(task for task in tasks if task["slot_id"] == "XR-01")
    assert extra_task["current_prompt"].startswith("【参考图变量约束】")
    extra_usage = json.loads(extra_task["versions"][0]["api_usage_json"])
    assert [item["label"] for item in extra_usage["reference_inputs"]] == ["手托比例参考图", "额外需求参考图1"]
    assert "输入参考图1＝【手托比例参考图】" in extra_task["versions"][0]["prompt"]
    assert "输入参考图2＝【额外需求参考图1】" in extra_task["versions"][0]["prompt"]


def test_series_uses_cpt_for_appearance_and_stt_only_for_scale(tmp_path: Path):
    from services.generation_service import GenerationService

    settings = get_settings(tmp_path)
    settings = replace(settings, data_dir=tmp_path / "data", output_root=tmp_path / "outputs", db_path=tmp_path / "data" / "app.db", mock_mode=True)
    repo = Repository(Database(settings.db_path))
    service = GenerationService(settings, repo, StorageService(settings.output_root))
    product = {"filename": "product.png", "content": make_mock_png("product"), "content_type": "image/png"}
    series = {"filename": "series.png", "content": make_mock_png("series"), "content_type": "image/png"}
    project_id = service.create_project({"product_name": "demo", "is_series": "1", "enabled_tasks": '["08"]'}, product, series, {})
    service._run_initial(project_id)
    task = repo.get_tasks(project_id)[0]
    usage = json.loads(task["versions"][0]["api_usage_json"])
    assert [item["label"] for item in usage["reference_inputs"]] == ["系列外观参考图", "手托比例参考图"]
    assert "【系列外观参考图】" in task["current_prompt"]
    assert "不得用手托比例参考图覆盖或改写系列外观参考图" in task["current_prompt"]


def test_series_project_requires_cpt_reference(tmp_path: Path):
    from services.generation_service import GenerationService

    settings = get_settings(tmp_path)
    settings = replace(settings, data_dir=tmp_path / "data", output_root=tmp_path / "outputs", db_path=tmp_path / "data" / "app.db", mock_mode=True)
    repo = Repository(Database(settings.db_path))
    service = GenerationService(settings, repo, StorageService(settings.output_root))
    product = {"filename": "stt.png", "content": make_mock_png("stt"), "content_type": "image/png"}
    try:
        service.create_project({"product_name": "demo", "is_series": "1"}, product, None, {})
    except ValueError as exc:
        assert "cpt 系列外观参考图" in str(exc)
    else:
        raise AssertionError("series project accepted without cpt")


def test_runtime_config_overrides_task_brief(tmp_path: Path):
    store = RuntimeConfigStore(tmp_path / "data")
    store.save(group_constraints={"atmosphere": "摆放图专属约束。"}, task_briefs={"03": "A custom placement direction."})
    definitions = store.task_definitions()
    task_03 = next(item for item in definitions if item["id"] == "03")
    task_04 = next(item for item in definitions if item["id"] == "04")
    assert store.constraint_for("atmosphere") == "摆放图专属约束。"
    assert store.constraint_for("scene") != "摆放图专属约束。"
    assert set(store.group_constraints()) == {"size", "atmosphere", "scene"}
    assert task_03["brief"] == "A custom placement direction."
    assert task_04["brief"] != "A custom placement direction."


def test_local_prompts_keep_three_logics_and_one_prompt_per_image(tmp_path: Path):
    settings = replace(get_settings(tmp_path), data_dir=tmp_path / "data")
    service = PromptService(settings, RuntimeConfigStore(settings.data_dir))
    project = {
        "product_description": "PU材质，慢回弹",
        "display_requirements": "一个产品放在浅色托盘上",
        "custom_scene": "窗边书桌上的日常使用场景",
        "product_dimensions": "6 x 5 x 3 cm",
    }
    prompts = service.build_local_prompts(project)
    assert set(prompts) == {"03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13"}
    assert len(set(prompts.values())) == 11
    assert all(len(value) >= 80 and "Realistic camera" not in value for value in prompts.values())
    assert all(value.startswith("【参考图变量约束】") for value in prompts.values())
    assert all("【手托比例参考图】" in value for value in prompts.values())
    assert all("【系列外观参考图】" not in value for value in prompts.values())
    assert all("{{" not in value and "}}" not in value for value in prompts.values())
    assert "一个产品放在浅色托盘上" in prompts["03"]
    assert "窗边书桌上的日常使用场景" in prompts["11"]
    assert "6 × 5 × 3 厘米" in prompts["13"]
    assert "不得出现人物、手部或任何人体特征" in prompts["03"]
    assert "所有人物均设定为美国人" in prompts["07"]
    assert "必须恰好使用长、宽、高三根" in prompts["13"]
    assert not any(re.search(r"[A-Za-z]", value) for value in prompts.values())
    definitions = service.task_definitions()
    assert {item["logic"] for item in definitions} == {"氛围摆放", "使用场景", "尺寸展示"}


def test_prompt_model_is_called_separately_for_three_workflow_groups(tmp_path: Path, monkeypatch):
    settings = replace(
        get_settings(tmp_path),
        data_dir=tmp_path / "data",
        prompt_api_base_url="https://example.test",
        prompt_api_key="test-key",
        prompt_model="gpt-5.5",
    )
    service = PromptService(settings, RuntimeConfigStore(settings.data_dir))
    image_path = tmp_path / "product.png"
    image_path.write_bytes(make_mock_png("product"))
    calls: list[list[str]] = []

    def fake_request(project, image_path, definitions, ids):
        calls.append(list(ids))
        return {
            task_id: f"这是图{task_id}的独立中文描述词，严格执行本类型专属全局约束，保持产品外观参考图中的外观、颜色、结构、比例和材质完全不变。画面构图、主体、环境、光线、动作和摄影要求均完整明确，可直接用于图片生成，并确保每张图片互不合并、互不省略。"
            for task_id in ids
        }

    monkeypatch.setattr(service, "_request_prompts", fake_request)
    prompts = service.generate_prompts({}, image_path)
    assert set(prompts) == {"03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13"}
    assert calls == [["03", "04", "05", "06"], ["07", "08", "09", "10", "11", "12"], ["13"]]


def test_each_workflow_task_receives_only_its_relevant_user_inputs(tmp_path: Path):
    settings = replace(get_settings(tmp_path), data_dir=tmp_path / "data")
    service = PromptService(settings, RuntimeConfigStore(settings.data_dir))
    definitions = {item["id"]: item for item in service.task_definitions()}
    project = {
        "product_description": "PU材质",
        "product_count": 12,
        "display_requirements": "十二个产品放在浅盘里",
        "custom_scene": "户外野餐场景",
        "product_dimensions": "6 x 5 x 3 cm",
    }
    atmosphere = service._task_user_inputs(project, definitions["03"])
    regular_scene = service._task_user_inputs(project, definitions["07"])
    custom_scene = service._task_user_inputs(project, definitions["11"])
    size = service._task_user_inputs(project, definitions["13"])
    assert "摆放展示要求" in atmosphere and "自定义使用场景" not in atmosphere
    assert "摆放展示要求" not in regular_scene and "自定义使用场景" not in regular_scene
    assert custom_scene["自定义使用场景"] == "户外野餐场景"
    assert set(size) == {"产品文字信息", "产品外观参考图变量", "手托比例参考图变量", "产品尺寸"}


def test_workflow_placeholders_render_actual_values_and_keep_unknowns_visible(tmp_path: Path):
    settings = replace(get_settings(tmp_path), data_dir=tmp_path / "data")
    service = PromptService(settings, RuntimeConfigStore(settings.data_dir))
    project = {
        "product_name": "桃子捏捏",
        "product_description": "PU材质",
        "product_count": 12,
        "display_requirements": "随机放在浅色圆盘上",
        "custom_scene": "户外野餐",
        "product_dimensions": "6 x 5 x 3 cm",
        "is_series": 1,
        "input_series_path": "input/series.png",
    }
    template = "{{产品名}}；{{产品数量}}个；{{摆放展示要求}}；{{产品尺寸}}；{{stt}}；{{cpt}}；{{未知变量}}"
    rendered = service.render_template(template, project)
    assert rendered == "桃子捏捏；12个；随机放在浅色圆盘上；6 x 5 x 3 cm；【手托比例参考图】；【系列外观参考图】；{{未知变量}}"
    raw_definitions = service.task_definitions()
    assert "{{摆放展示要求}}" in next(item for item in raw_definitions if item["id"] == "03")["brief"]
    assert "{{自定义使用场景}}" in next(item for item in raw_definitions if item["id"] == "11")["brief"]
    assert "{{产品尺寸}}" in next(item for item in raw_definitions if item["id"] == "13")["brief"]


def test_analysis_reference_order_matches_single_and_series_rules(tmp_path: Path):
    settings = replace(get_settings(tmp_path), data_dir=tmp_path / "data")
    service = PromptService(settings, RuntimeConfigStore(settings.data_dir))
    stt = tmp_path / "stt.png"
    cpt = tmp_path / "cpt.png"
    stt.write_bytes(make_mock_png("stt"))
    cpt.write_bytes(make_mock_png("cpt"))

    single = service._analysis_image_inputs({"is_series": 0}, stt)
    series = service._analysis_image_inputs({"is_series": 1}, stt, cpt)

    assert [item["label"] for item in single] == ["手托比例参考图"]
    assert "外观与手托比例" in single[0]["purpose"]
    assert [item["label"] for item in series] == ["系列外观参考图", "手托比例参考图"]
    assert "只锚定" in series[1]["purpose"]
