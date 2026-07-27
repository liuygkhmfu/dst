from __future__ import annotations

import base64
import json
import http.client
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .storage_service import make_mock_png


DEFAULT_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept": "application/json",
}


class ImageServiceError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, error_type: str = "api_error"):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


def _multipart(fields: dict[str, str], images: list[Path]) -> tuple[bytes, str]:
    boundary = "----CodexEcommerce" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend([f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(), str(value).encode("utf-8"), b"\r\n"])
    for path in images:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.extend([f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="image[]"; filename="{path.name}"\r\n'.encode(), f"Content-Type: {mime}\r\n\r\n".encode(), path.read_bytes(), b"\r\n"])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class ImageService:
    def __init__(self, settings):
        self.settings = settings

    def generate(
        self,
        prompt: str,
        image_paths: list[Path],
        *,
        seed: str = "",
        image_size: str | None = None,
        image_quality: str | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        selected_size = str(image_size or self.settings.image_size)
        selected_quality = str(image_quality or self.settings.image_quality)
        if self.settings.mock_mode or not self.settings.image_api_base_url or not self.settings.image_api_key:
            time.sleep(0.08)
            return make_mock_png(seed or prompt), {
                "mock": True,
                "image_count": len(image_paths),
                "size": selected_size,
                "quality": selected_quality,
            }
        base = self.settings.image_api_base_url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        fields = {
            "model": self.settings.image_model,
            "prompt": prompt,
            "size": selected_size,
            "quality": selected_quality,
            "output_format": self.settings.image_output_format,
            "background": self.settings.image_background,
            "moderation": self.settings.image_moderation,
        }
        body, content_type = _multipart(fields, image_paths)
        attempts = 0
        last_error: Exception | None = None
        for attempt in range(3):
            attempts += 1
            try:
                # 每次重试重新创建 Request，避免某些 Windows urllib/网关组合复用已消费的请求对象。
                request = urllib.request.Request(
                    base + "/images/edits",
                    data=body,
                    headers={**DEFAULT_API_HEADERS, "Authorization": f"Bearer {self.settings.image_api_key}", "Content-Type": content_type},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=self.settings.api_timeout_seconds) as response:
                    status = response.status
                    raw = response.read()
                parsed = json.loads(raw.decode("utf-8"))
                if status >= 400 or parsed.get("error"):
                    raise ImageServiceError(str(parsed.get("error") or "图片接口返回错误"), status)
                data = next((item for item in parsed.get("data", []) if item.get("b64_json") or item.get("url")), None)
                if not data:
                    raise ImageServiceError("图片接口未返回 data[].b64_json 或 data[].url", status, "invalid_response")
                if data.get("b64_json"):
                    content = base64.b64decode(data["b64_json"])
                else:
                    download = urllib.request.Request(data["url"], headers=DEFAULT_API_HEADERS, method="GET")
                    with urllib.request.urlopen(download, timeout=self.settings.api_timeout_seconds) as image_response:
                        content = image_response.read()
                return content, {"attempt_count": attempts, "status_code": status, "usage": parsed.get("usage"), "revised_prompt": data.get("revised_prompt")}
            except urllib.error.HTTPError as exc:
                status = exc.code
                text = exc.read().decode("utf-8", errors="replace")[:1000]
                last_error = ImageServiceError(f"HTTP {status}: {text}", status, "http_error")
                # 网关的 403/1010 可能是瞬时 WAF/并发拦截；认证、参数和路由错误无需重复等待。
                if status in {400, 401, 404, 405, 413, 422}:
                    break
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException, ConnectionError, OSError, ImageServiceError) as exc:
                last_error = exc
                if isinstance(exc, ImageServiceError) and exc.status_code and exc.status_code < 500 and exc.status_code != 429:
                    if exc.status_code not in {403, 408, 409}:
                        break
            if attempt < 2:
                time.sleep(min(8, 2 ** attempt))
        raise ImageServiceError(str(last_error or "图片生成失败"), getattr(last_error, "status_code", None), getattr(last_error, "error_type", "network_error"))
