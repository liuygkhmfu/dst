from __future__ import annotations

import json
import shutil
import tempfile
import uuid
import zipfile
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image

from .storage_service import StorageService, safe_name


class PostprocessService:
    """Local, non-destructive image composition for generated project assets."""

    def __init__(self, storage: StorageService):
        self.storage = storage

    @staticmethod
    def _is_background(pixel: tuple[int, ...], threshold: int = 246) -> bool:
        red, green, blue = pixel[:3]
        alpha = pixel[3] if len(pixel) > 3 else 255
        return alpha == 0 or (red >= threshold and green >= threshold and blue >= threshold)

    def prepare_transparent_asset(self, source: Path, target: Path) -> Path:
        """
        Remove only near-white pixels connected to the canvas edge.

        Internal whites (eyes, teeth, lettering and dashed outlines) are retained,
        unlike a global white-to-alpha replacement.
        """
        with Image.open(source) as opened:
            image = opened.convert("RGBA")
        width, height = image.size
        pixels = image.load()
        visited = bytearray(width * height)
        queue: deque[tuple[int, int]] = deque()

        def enqueue(x: int, y: int) -> None:
            index = y * width + x
            if not visited[index] and self._is_background(pixels[x, y]):
                visited[index] = 1
                queue.append((x, y))

        for x in range(width):
            enqueue(x, 0)
            enqueue(x, height - 1)
        for y in range(height):
            enqueue(0, y)
            enqueue(width - 1, y)

        while queue:
            x, y = queue.popleft()
            red, green, blue, _ = pixels[x, y]
            pixels[x, y] = (red, green, blue, 0)
            if x:
                enqueue(x - 1, y)
            if x + 1 < width:
                enqueue(x + 1, y)
            if y:
                enqueue(x, y - 1)
            if y + 1 < height:
                enqueue(x, y + 1)

        alpha_box = image.getchannel("A").getbbox()
        if alpha_box:
            image = image.crop(alpha_box)
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, format="PNG", optimize=True)
        return target

    def overlay_bottom_right(
        self,
        base_source: Path,
        overlay_source: Path,
        target: Path,
        width_ratio: float = 0.28,
        margin_ratio: float = 0.025,
    ) -> Path:
        with Image.open(base_source) as opened:
            base = opened.convert("RGBA")
        with Image.open(overlay_source) as opened:
            overlay = opened.convert("RGBA")
        desired_width = max(1, round(base.width * width_ratio))
        scale = desired_width / max(1, overlay.width)
        desired_height = max(1, round(overlay.height * scale))
        max_height = round(base.height * 0.42)
        if desired_height > max_height:
            scale = max_height / max(1, overlay.height)
            desired_width = max(1, round(overlay.width * scale))
            desired_height = max(1, round(overlay.height * scale))
        overlay = overlay.resize((desired_width, desired_height), Image.Resampling.LANCZOS)
        margin = max(8, round(min(base.size) * margin_ratio))
        position = (max(0, base.width - desired_width - margin), max(0, base.height - desired_height - margin))
        base.alpha_composite(overlay, position)
        target.parent.mkdir(parents=True, exist_ok=True)
        base.save(target, format="PNG", optimize=True)
        return target

    def build_watermark_outputs(
        self,
        project_dir: Path,
        watermark_source: Path,
        placement_sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex[:8]
        target_dir = project_dir / "postprocess" / "not_edible" / f"run_{run_id}"
        transparent = self.prepare_transparent_asset(
            watermark_source,
            target_dir / "禁止食用水印_透明.png",
        )
        outputs: dict[str, dict[str, str]] = {}
        for item in placement_sources:
            slot_id = str(item["slot_id"])
            target = target_dir / f"{safe_name(slot_id)}_{safe_name(item['task_name'])}_禁止食用水印版.png"
            self.overlay_bottom_right(item["source_path"], transparent, target)
            outputs[slot_id] = {
                "file_path": self.storage.relative(project_dir, target),
                "source_version_id": str(item["source_version_id"]),
                "watermark_version_id": str(item["watermark_version_id"]),
            }
        return {
            "status": "ready",
            "transparent_asset_path": self.storage.relative(project_dir, transparent),
            "outputs": outputs,
            "error": "",
        }

    def build_collage_package(
        self,
        project_dir: Path,
        product_name: str,
        template_source: Path,
        text_source: Path,
        scene_sources: list[dict[str, Any]],
        source_versions: dict[str, str],
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex[:8]
        package_name = f"{safe_name(product_name, 'product')}_场景拼图素材_{run_id}"
        package_dir = project_dir / "postprocess" / "collage" / package_name
        template_dir = package_dir / "01_场景拼图模板"
        scene_dir = package_dir / "02_场景图素材"
        text_dir = package_dir / "03_艺术文字素材"
        template_dir.mkdir(parents=True, exist_ok=True)
        scene_dir.mkdir(parents=True, exist_ok=True)
        text_dir.mkdir(parents=True, exist_ok=True)

        template_original = template_dir / f"场景拼图模板_原图{template_source.suffix.lower()}"
        text_original = text_dir / f"艺术文字_原图{text_source.suffix.lower()}"
        shutil.copy2(template_source, template_original)
        shutil.copy2(text_source, text_original)
        self.prepare_transparent_asset(template_source, template_dir / "场景拼图模板_透明参考.png")
        self.prepare_transparent_asset(text_source, text_dir / "艺术文字_透明可替换.png")

        scene_manifest: list[dict[str, str]] = []
        for index, item in enumerate(scene_sources, 1):
            source = item["source_path"]
            target = scene_dir / f"{index:02d}_{safe_name(item['slot_id'])}_{safe_name(item['task_name'])}{source.suffix.lower()}"
            shutil.copy2(source, target)
            scene_manifest.append(
                {
                    "slot_id": str(item["slot_id"]),
                    "task_name": str(item["task_name"]),
                    "filename": target.name,
                    "source_version_id": str(item["source_version_id"]),
                }
            )

        manifest = {
            "说明": "Photoshop人工拼图素材包：模板透明参考 + 至少4张场景图 + 可替换透明艺术文字。",
            "产品名称": product_name,
            "源版本": source_versions,
            "场景图": scene_manifest,
        }
        manifest_path = package_dir / "素材清单.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        archive = project_dir / "postprocess" / "collage" / f"{package_name}.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".collage_", dir=archive.parent, delete=False) as temp:
            temp_path = Path(temp.name)
        try:
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for file in package_dir.rglob("*"):
                    if file.is_file():
                        zip_file.write(file, arcname=f"{package_name}/{file.relative_to(package_dir).as_posix()}")
            temp_path.replace(archive)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        return {
            "status": "ready",
            "package_path": self.storage.relative(project_dir, archive),
            "package_dir": self.storage.relative(project_dir, package_dir),
            "source_versions": source_versions,
            "scene_count": len(scene_sources),
            "error": "",
        }
