"""Read laboratory-result document links from Google Sheets by tube number."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .config import settings


URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
MED_ID_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_-]{1,80}")
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, "LabResult"]] = {}


class LabResultsUnavailable(RuntimeError):
    """Google Sheets integration is unavailable or misconfigured."""


@dataclass(frozen=True)
class LabResult:
    med_id: str
    status: str
    urls: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        documents = lab_result_documents(self.urls)
        return {
            "med_id": self.med_id,
            "status": self.status,
            "urls": list(self.urls),
            "documents": documents,
        }


def _google_document_export_url(url: str) -> str:
    """Convert a shared Google editor/Drive URL into a model-readable file URL."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path
    if host == "docs.google.com":
        match = re.search(r"/(document|spreadsheets|presentation)/d/([^/]+)", path)
        if match:
            kind, document_id = match.groups()
            export_format = "pdf"
            if kind == "spreadsheets":
                return f"https://docs.google.com/spreadsheets/d/{document_id}/export?format={export_format}"
            return f"https://docs.google.com/{kind}/d/{document_id}/export/{export_format}"
    if host == "drive.google.com":
        match = re.search(r"/file/d/([^/]+)", path)
        file_id = match.group(1) if match else parse_qs(parsed.query).get("id", [""])[0]
        if file_id:
            return f"https://drive.google.com/uc?{urlencode({'export': 'download', 'id': file_id})}"
    return urlunparse(parsed)


def lab_result_documents(urls: tuple[str, ...] | list[str]) -> list[dict]:
    documents: list[dict] = []
    total = len(urls)
    for index, raw_url in enumerate(urls):
        url = str(raw_url)
        documents.append({
            "id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
            "index": index,
            "title": f"Результаты анализов{f' · документ {index + 1}' if total > 1 else ''}",
            "url": url,
            "analysis_url": _google_document_export_url(url),
        })
    return documents


def normalize_med_id(value: object) -> str:
    med_id = "".join(str(value or "").split())
    if med_id.endswith(".0") and med_id[:-2].isdigit():
        med_id = med_id[:-2]
    if not MED_ID_RE.fullmatch(med_id):
        raise ValueError("Проверьте номер пробирки: допустимы буквы, цифры, дефис и подчёркивание")
    return med_id


def extract_urls(value: object) -> tuple[str, ...]:
    urls: list[str] = []
    for match in URL_RE.findall(str(value or "")):
        url = match.rstrip(".,);]")
        if url not in urls:
            urls.append(url)
    return tuple(urls)


def _normalized_header(value: object) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _worksheet(client):
    spreadsheet = client.open(settings.after_tests_spreadsheet)
    names = [
        settings.after_tests_worksheet,
        "tetst_and_results",
        "tests_and_results",
    ]
    last_error: Exception | None = None
    for name in dict.fromkeys(item for item in names if item):
        try:
            return spreadsheet.worksheet(name)
        except Exception as exc:  # gspread raises WorksheetNotFound
            if exc.__class__.__name__ != "WorksheetNotFound":
                raise LabResultsUnavailable("Не удалось открыть лист результатов") from exc
            last_error = exc
    raise LabResultsUnavailable(
        "В after_tests_db не найден лист tetst_and_results или tests_and_results"
    ) from last_error


def _lookup_uncached(med_id: str) -> LabResult:
    if not settings.lab_results_enabled:
        raise LabResultsUnavailable("Получение результатов временно отключено")
    credentials_path = Path(settings.after_tests_google_credentials)
    if not credentials_path.is_file():
        raise LabResultsUnavailable("Не найден ключ доступа к after_tests_db")
    try:
        import gspread

        client = gspread.service_account(filename=str(credentials_path))
        http_client = getattr(client, "http_client", None)
        if hasattr(http_client, "set_timeout"):
            http_client.set_timeout(settings.google_sheets_timeout_seconds)
        values = _worksheet(client).get_all_values()
    except LabResultsUnavailable:
        raise
    except Exception as exc:
        raise LabResultsUnavailable("Не удалось подключиться к after_tests_db") from exc

    if not values:
        return LabResult(med_id, "not_found")
    headers = [_normalized_header(value) for value in values[0]]
    try:
        med_id_index = headers.index("med_id")
    except ValueError as exc:
        raise LabResultsUnavailable("В таблице результатов отсутствует колонка med_id") from exc
    result_candidates = ("results", "result", "result_url", "results_url", "ссылка", "ссылки")
    result_index = next((headers.index(name) for name in result_candidates if name in headers), None)
    if result_index is None:
        raise LabResultsUnavailable("В таблице результатов отсутствует колонка results")

    lookup_key = med_id.casefold()
    for row in values[1:]:
        if med_id_index >= len(row):
            continue
        try:
            row_med_id = normalize_med_id(row[med_id_index])
        except ValueError:
            continue
        if row_med_id.casefold() != lookup_key:
            continue
        urls = extract_urls(row[result_index] if result_index < len(row) else "")
        return LabResult(med_id, "found" if urls else "processing", urls)
    return LabResult(med_id, "not_found")


def lookup_lab_results(med_id_value: object) -> LabResult:
    med_id = normalize_med_id(med_id_value)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(med_id.casefold())
        if cached and cached[0] > now:
            return cached[1]
    result = _lookup_uncached(med_id)
    with _cache_lock:
        _cache[med_id.casefold()] = (
            now + max(0, settings.lab_results_cache_seconds),
            result,
        )
    return result
