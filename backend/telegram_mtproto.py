"""In-memory MTProto stream for Telegram media above the Bot API limit."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import mimetypes
import threading
import uuid

from .config import settings

try:  # Keep diagnostics/imports usable before the optional dependency is installed.
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
except ImportError:  # pragma: no cover - exercised only before deployment installs deps.
    TelegramClient = None
    MemorySession = None


class TelegramMTProtoUnavailable(RuntimeError):
    pass


class TelegramRangeNotSatisfiable(TelegramMTProtoUnavailable):
    def __init__(self, total_size: int):
        super().__init__("Запрошенный диапазон недоступен")
        self.total_size = int(total_size)


def configured() -> bool:
    return bool(
        settings.telegram_feed_enabled
        and settings.telegram_feed_bot_token
        and settings.telegram_feed_mtproto_api_id > 0
        and settings.telegram_feed_mtproto_api_hash
        and TelegramClient is not None
        and MemorySession is not None
    )


def _byte_range(value: str, total_size: int) -> tuple[int, int, int]:
    """Return (status, offset, length) for one HTTP byte range."""
    total = max(0, int(total_size))
    candidate = str(value or "").strip()
    if not candidate:
        return 200, 0, total
    if not candidate.lower().startswith("bytes=") or "," in candidate:
        raise TelegramRangeNotSatisfiable(total)
    spec = candidate[6:].strip()
    if "-" not in spec:
        raise TelegramRangeNotSatisfiable(total)
    start_text, end_text = (part.strip() for part in spec.split("-", 1))
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0 or total <= 0:
                raise ValueError
            length = min(suffix, total)
            return 206, total - length, length
        start = int(start_text)
        if start < 0 or start >= total:
            raise ValueError
        end = total - 1 if not end_text else min(int(end_text), total - 1)
        if end < start:
            raise ValueError
        return 206, start, end - start + 1
    except (TypeError, ValueError) as exc:
        raise TelegramRangeNotSatisfiable(total) from exc


@dataclass
class MTProtoStream:
    worker: "_MTProtoWorker"
    stream_id: str
    status: int
    total_size: int
    offset: int
    content_length: int
    content_type: str
    _closed: bool = False

    @property
    def content_range(self) -> str:
        if self.status != 206 or self.content_length <= 0:
            return ""
        end = self.offset + self.content_length - 1
        return f"bytes {self.offset}-{end}/{self.total_size}"

    def read(self) -> bytes:
        if self._closed:
            return b""
        return self.worker.read(self.stream_id)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.worker.close_stream(self.stream_id)


class _MTProtoWorker:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._run, name="telegram-feed-mtproto", daemon=True,
        )
        self.client = None
        self.streams: dict[str, dict] = {}
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=settings.telegram_feed_mtproto_timeout_seconds)
        except TelegramMTProtoUnavailable:
            raise
        except Exception as exc:
            future.cancel()
            raise TelegramMTProtoUnavailable("Telegram MTProto временно недоступен") from exc

    async def _client(self):
        if self.client is not None and self.client.is_connected():
            return self.client
        if not configured():
            raise TelegramMTProtoUnavailable("MTProto-поток для больших видео не настроен")
        self.client = TelegramClient(
            MemorySession(),
            settings.telegram_feed_mtproto_api_id,
            settings.telegram_feed_mtproto_api_hash,
        )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.sign_in(bot_token=settings.telegram_feed_bot_token)
        return self.client

    async def _open(self, media: dict, range_header: str) -> MTProtoStream:
        client = await self._client()
        channel_id = int(str(media.get("channel_id") or "0"))
        message_id = int(media.get("message_id") or 0)
        if not channel_id or not message_id:
            raise TelegramMTProtoUnavailable("У видео отсутствует адрес сообщения Telegram")
        # A public username can be resolved by a fresh in-memory session without
        # persisting Telegram's entity/access-hash cache on disk.
        target = settings.telegram_feed_channel_username or channel_id
        message = await client.get_messages(target, ids=message_id)
        if not message or not message.media:
            raise TelegramMTProtoUnavailable("Видео больше недоступно в Telegram")
        message_file = getattr(message, "file", None)
        total_size = int(getattr(message_file, "size", 0) or media.get("file_size") or 0)
        if total_size <= 0:
            raise TelegramMTProtoUnavailable("Telegram не сообщил размер видео")
        status, offset, length = _byte_range(range_header, total_size)
        content_type = str(
            getattr(message_file, "mime_type", "")
            or media.get("mime_type")
            or mimetypes.guess_type(str(media.get("file_name") or ""))[0]
            or "application/octet-stream"
        )
        iterator = client.iter_download(
            message.media,
            offset=offset,
            request_size=512 * 1024,
            chunk_size=512 * 1024,
        ).__aiter__()
        stream_id = uuid.uuid4().hex
        self.streams[stream_id] = {"iterator": iterator, "remaining": length}
        return MTProtoStream(
            worker=self, stream_id=stream_id, status=status,
            total_size=total_size, offset=offset, content_length=length,
            content_type=content_type,
        )

    def open(self, media: dict, range_header: str) -> MTProtoStream:
        return self._submit(self._open(media, range_header))

    async def _read(self, stream_id: str) -> bytes:
        state = self.streams.get(stream_id)
        if state is None or state["remaining"] <= 0:
            return b""
        try:
            chunk = await anext(state["iterator"])
        except StopAsyncIteration:
            self.streams.pop(stream_id, None)
            return b""
        remaining = int(state["remaining"])
        if len(chunk) >= remaining:
            self.streams.pop(stream_id, None)
            closer = getattr(state["iterator"], "aclose", None)
            if closer:
                await closer()
            return bytes(chunk[:remaining])
        state["remaining"] = remaining - len(chunk)
        return bytes(chunk)

    def read(self, stream_id: str) -> bytes:
        return self._submit(self._read(stream_id))

    async def _close(self, stream_id: str) -> None:
        state = self.streams.pop(stream_id, None)
        closer = getattr((state or {}).get("iterator"), "aclose", None)
        if closer:
            await closer()

    def close_stream(self, stream_id: str) -> None:
        try:
            self._submit(self._close(stream_id))
        except TelegramMTProtoUnavailable:
            pass


_worker: _MTProtoWorker | None = None
_worker_lock = threading.Lock()


def open_media(media: dict, range_header: str = "") -> MTProtoStream:
    global _worker
    if not configured():
        raise TelegramMTProtoUnavailable("MTProto-поток для больших видео не настроен")
    with _worker_lock:
        if _worker is None:
            _worker = _MTProtoWorker()
    return _worker.open(media, str(range_header or "")[:200])
