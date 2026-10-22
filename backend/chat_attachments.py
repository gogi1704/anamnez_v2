"""Persistent, access-controlled storage for files attached to chat messages."""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import settings


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,80}")
MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
OPTIMIZED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_SIDE = 1920
THUMBNAIL_SIDE = 480
MAX_IMAGE_PIXELS = 32_000_000


def storage_dir() -> Path:
    """Keep attachments beside the main database so the existing data volume persists them."""
    return settings.database_path.parent / "chat_attachments"


def _safe_name(value: object) -> str:
    name = Path(str(value or "file").replace("\\", "/")).name.strip()
    return (name or "file")[:160]


def _webp_name(value: object) -> str:
    name = _safe_name(value)
    return f"{(Path(name).stem or 'image')[:155]}.webp"


def _save_webp(image: Image.Image, *, max_side: int, quality: int) -> bytes:
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    if image.mode not in {"RGB", "RGBA"}:
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    output = BytesIO()
    image.save(output, format="WEBP", quality=quality, method=6)
    return output.getvalue()


def _optimize_image(body: bytes) -> tuple[bytes, bytes]:
    """Return a bounded WebP image and a small WebP thumbnail."""
    try:
        with Image.open(BytesIO(body)) as opened:
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("Изображение имеет слишком большое разрешение")
            normalized = ImageOps.exif_transpose(opened)
            normalized.load()
            full = _save_webp(normalized.copy(), max_side=MAX_IMAGE_SIDE, quality=82)
            thumbnail = _save_webp(
                normalized.copy(), max_side=THUMBNAIL_SIDE, quality=70,
            )
            return full, thumbnail
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Не удалось прочитать изображение") from exc


def store(item: dict, owner_chel_id: str) -> dict:
    """Persist one validated data URL and return metadata safe for message JSON."""
    mime = str(item.get("type") or "")
    extension = MIME_EXTENSIONS.get(mime)
    if not extension:
        raise ValueError("Неподдерживаемый тип вложения")
    data_url = str(item.get("data_url") or "")
    prefix = f"data:{mime};base64,"
    if not data_url.startswith(prefix):
        raise ValueError("Некорректные данные вложения")
    try:
        body = base64.b64decode(data_url[len(prefix):], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Некорректные данные вложения") from exc
    if not body or len(body) > 4 * 1024 * 1024:
        raise ValueError("Размер одного файла должен быть от 1 байта до 4 МБ")

    original_size = len(body)
    thumbnail_body = b""
    stored_mime = mime
    stored_name = _safe_name(item.get("name"))
    if mime in OPTIMIZED_IMAGE_TYPES:
        body, thumbnail_body = _optimize_image(body)
        extension = ".webp"
        stored_mime = "image/webp"
        stored_name = _webp_name(item.get("name"))
    elif mime == "image/gif":
        # Preserve animation for opening/downloading, but serve a compact first
        # frame in the message list instead of loading the whole GIF there.
        _, thumbnail_body = _optimize_image(body)

    root = storage_dir()
    root.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    file_path = root / f"{token}{extension}"
    metadata_path = root / f"{token}.json"
    file_path.write_bytes(body)
    thumbnail_path = root / f"{token}.thumb.webp"
    if thumbnail_body:
        thumbnail_path.write_bytes(thumbnail_body)
    record = {
        "token": token,
        "owner_chel_id": str(owner_chel_id),
        "name": stored_name,
        "type": stored_mime,
        "size": len(body),
        "original_size": original_size,
        "file": file_path.name,
        **({"thumbnail_file": thumbnail_path.name} if thumbnail_body else {}),
    }
    metadata_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return {
        "name": record["name"],
        "type": stored_mime,
        "size": len(body),
        "url": f"/api/chat-attachments/{token}",
        "download_url": f"/api/chat-attachments/{token}?download=1",
        **({
            "thumbnail_url": f"/api/chat-attachments/{token}?thumbnail=1",
        } if thumbnail_body else {}),
    }


def public_metadata(item: dict) -> dict:
    """Strip model-only fields while retaining links needed by both chat UIs."""
    keys = ("name", "type", "size", "url", "download_url", "thumbnail_url")
    return {key: item[key] for key in keys if item.get(key) not in (None, "")}


def load(token: str, *, thumbnail: bool = False) -> tuple[dict, bytes] | None:
    """Load an attachment by its opaque token without accepting path fragments."""
    if not TOKEN_PATTERN.fullmatch(str(token or "")):
        return None
    root = storage_dir()
    metadata_path = root / f"{token}.json"
    try:
        record = json.loads(metadata_path.read_text(encoding="utf-8"))
        key = "thumbnail_file" if thumbnail else "file"
        file_name = str(record.get(key) or "")
        if Path(file_name).name != file_name or not file_name.startswith(f"{token}."):
            return None
        body = (root / file_name).read_bytes()
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if thumbnail:
        record = {**record, "type": "image/webp", "name": _webp_name(record.get("name"))}
    return record, body


def delete_public_attachments(items: list[dict], owner_chel_id: str) -> int:
    """Delete files referenced by message metadata, but only for their owner."""
    root = storage_dir()
    deleted = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        match = re.fullmatch(
            r"/api/chat-attachments/([A-Za-z0-9_-]{32,80})",
            str(item.get("url") or "").split("?", 1)[0],
        )
        if not match:
            continue
        token = match.group(1)
        metadata_path = root / f"{token}.json"
        try:
            record = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if str(record.get("owner_chel_id") or "") != str(owner_chel_id or ""):
            continue
        file_name = str(record.get("file") or "")
        if Path(file_name).name != file_name or not file_name.startswith(f"{token}."):
            continue
        try:
            (root / file_name).unlink(missing_ok=True)
            thumbnail_file = str(record.get("thumbnail_file") or "")
            if (
                thumbnail_file
                and Path(thumbnail_file).name == thumbnail_file
                and thumbnail_file.startswith(f"{token}.")
            ):
                (root / thumbnail_file).unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            deleted += 1
        except OSError:
            continue
    return deleted
