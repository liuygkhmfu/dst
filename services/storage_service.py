from __future__ import annotations

import base64
import io
import json
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path


ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def safe_name(value: str, fallback: str = "item") -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", str(value or "").strip())
    value = re.sub(r"\s+", "_", value).strip("._")
    return (value[:80] or fallback)


def extension_for(filename: str, content_type: str = "") -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in ALLOWED_EXTENSIONS:
        return ".jpg" if suffix == ".jpeg" else suffix
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(content_type.lower(), ".png")


class StorageService:
    def __init__(self, output_root: Path):
        self.output_root = output_root.resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)

    def create_project_dir(self, project_id: str, product_name: str) -> Path:
        path = (self.output_root / f"{safe_name(project_id)}_{safe_name(product_name, 'product')}").resolve()
        self._assert_inside(path, self.output_root)
        for child in ["input", "extra_requests", "tasks"]:
            (path / child).mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _assert_inside(path: Path, root: Path) -> None:
        path.resolve().relative_to(root.resolve())

    def relative(self, project_dir: Path, path: Path) -> str:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()

    def resolve_relative(self, project_dir: Path, relative_path: str) -> Path:
        if not relative_path or ".." in Path(relative_path).parts:
            raise ValueError("非法文件路径")
        path = (project_dir / relative_path).resolve()
        self._assert_inside(path, project_dir)
        return path

    def save_upload(self, project_dir: Path, folder: str, filename: str, content: bytes, content_type: str = "") -> Path:
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("上传文件超过 20MB 限制")
        ext = extension_for(filename, content_type)
        target_dir = (project_dir / folder).resolve()
        self._assert_inside(target_dir, project_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{safe_name(Path(filename).stem, 'upload')}_{uuid.uuid4().hex[:8]}{ext}"
        self.atomic_write(target, content)
        return target

    def task_dir(self, project_dir: Path, slot_id: str, task_name: str) -> Path:
        path = (project_dir / "tasks" / f"{safe_name(slot_id)}_{safe_name(task_name)}").resolve()
        self._assert_inside(path, project_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def version_path(self, project_dir: Path, slot_id: str, task_name: str, product_name: str, number: int, mode: str, ext: str = ".png") -> Path:
        folder = self.task_dir(project_dir, slot_id, task_name)
        return folder / f"{safe_name(slot_id)}_{safe_name(product_name)}_{safe_name(task_name)}_v{number:02d}_{safe_name(mode)}{ext}"

    @staticmethod
    def atomic_write(target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".writing_", dir=target.parent, delete=False) as temp:
            temp.write(content)
            temp.flush()
            temp_path = Path(temp.name)
        temp_path.replace(target)

    def write_manifest(self, project_dir: Path, manifest: dict) -> None:
        self.atomic_write(project_dir / "project_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))

    def export_final_zip(self, project_dir: Path, files: list[Path]) -> Path:
        target = project_dir / f"{safe_name(project_dir.name)}_final.zip"
        with tempfile.NamedTemporaryFile(prefix=".zip_", dir=project_dir, delete=False) as temp:
            temp_path = Path(temp.name)
        try:
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for file in files:
                    if file.exists():
                        archive.write(file, arcname=file.name)
            temp_path.replace(target)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return target


def read_image_info(content: bytes) -> dict[str, str | int]:
    """Lightweight validation/info extraction; Pillow is optional."""
    if not content:
        raise ValueError("图片文件为空")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return {"mime": "image/png", "format": "png"}
    if content.startswith(b"\xff\xd8\xff"):
        return {"mime": "image/jpeg", "format": "jpeg"}
    if content[:12].startswith(b"RIFF") and content[8:12] == b"WEBP":
        return {"mime": "image/webp", "format": "webp"}
    raise ValueError("文件不是受支持的 PNG/JPEG/WEBP 图片")


def make_mock_png(seed: str, size: int = 512) -> bytes:
    """Create a deterministic, dependency-free preview PNG for mock mode."""
    import struct
    import zlib

    digest = __import__("hashlib").sha256(seed.encode("utf-8")).digest()
    c1, c2, c3 = digest[0], digest[1], digest[2]
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            mix = (x * 255 // max(1, size - 1) + y * 255 // max(1, size - 1)) // 2
            row.extend(((c1 + mix) % 256, (c2 + mix // 2) % 256, (c3 + 255 - mix) % 256, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")

