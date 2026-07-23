from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


IMAGE_QUALITY_OPTIONS = ("auto", "low", "medium", "high")


def normalize_image_quality(value: str, fallback: str = "low") -> str:
    quality = str(value or "").strip().lower()
    return quality if quality in IMAGE_QUALITY_OPTIONS else fallback


def load_dotenv(path: Path) -> None:
    """Load a small .env subset without requiring python-dotenv at runtime."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    data_dir: Path
    output_root: Path
    db_path: Path
    image_api_base_url: str
    image_api_key: str
    image_model: str
    image_size: str
    image_quality: str
    image_output_format: str
    image_background: str
    image_moderation: str
    prompt_api_base_url: str
    prompt_api_key: str
    prompt_model: str
    max_upload_mb: int
    generation_concurrency: int
    api_timeout_seconds: int
    mock_mode: bool


def get_settings(root_dir: Path | None = None) -> Settings:
    root = (root_dir or Path(__file__).resolve().parents[1]).resolve()
    load_dotenv(root / ".env")
    data_dir = root / "data"
    return Settings(
        root_dir=root,
        data_dir=data_dir,
        output_root=Path(os.getenv("OUTPUT_ROOT", str(data_dir / "outputs"))).resolve(),
        db_path=Path(os.getenv("DB_PATH", str(data_dir / "app.db"))).resolve(),
        image_api_base_url=os.getenv("IMAGE_API_BASE_URL", "").strip(),
        image_api_key=os.getenv("IMAGE_API_KEY", "").strip(),
        image_model=os.getenv("IMAGE_MODEL", "gpt-image-2").strip(),
        image_size=os.getenv("IMAGE_SIZE", "1024x1024").strip(),
        image_quality=normalize_image_quality(os.getenv("IMAGE_QUALITY", "low")),
        image_output_format=os.getenv("IMAGE_OUTPUT_FORMAT", "png").strip(),
        image_background=os.getenv("IMAGE_BACKGROUND", "auto").strip(),
        image_moderation=os.getenv("IMAGE_MODERATION", "auto").strip(),
        prompt_api_base_url=os.getenv("PROMPT_API_BASE_URL", "").strip(),
        prompt_api_key=os.getenv("PROMPT_API_KEY", "").strip(),
        prompt_model=os.getenv("PROMPT_MODEL", "").strip(),
        max_upload_mb=max(1, int(os.getenv("MAX_UPLOAD_MB", "20"))),
        generation_concurrency=max(1, int(os.getenv("GENERATION_CONCURRENCY", "2"))),
        api_timeout_seconds=max(10, int(os.getenv("API_TIMEOUT_SECONDS", "300"))),
        mock_mode=os.getenv("MOCK_MODE", "").strip().lower() in {"1", "true", "yes", "on"},
    )
