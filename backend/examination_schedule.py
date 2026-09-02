"""Read-only synchronization of enterprise examination dates and brigades."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from calendar import monthrange
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

from . import database as db
from .config import settings


class ExaminationScheduleUnavailable(RuntimeError):
    pass


_token_lock = threading.Lock()
_runtime_access_token = ""
_runtime_refresh_token = ""
_tokens_loaded = False
_MONTHS_RU = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4,
    "май": 5, "июнь": 6, "июль": 7, "август": 8,
    "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}


def configured() -> bool:
    return bool(
        settings.examination_schedule_enabled
        and settings.examination_schedule_access_token
    )


def _add_months(value: date, months: int) -> date:
    zero_based = value.month - 1 + max(0, int(months))
    year = value.year + zero_based // 12
    month = zero_based % 12 + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _unwrap_list(payload, *keys: str) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in (*keys, "data", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _unwrap_list(value, *keys)
            if nested:
                return nested
    return []


def _tokens() -> tuple[str, str]:
    global _runtime_access_token, _runtime_refresh_token, _tokens_loaded
    with _token_lock:
        if not _tokens_loaded:
            path = settings.examination_schedule_token_cache_path
            try:
                if path.exists() and path.stat().st_size <= 16_000:
                    cached = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(cached, dict):
                        _runtime_access_token = str(cached.get("access_token") or "")[:8_000]
                        _runtime_refresh_token = str(cached.get("refresh_token") or "")[:8_000]
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            _tokens_loaded = True
        return (
            unquote(_runtime_access_token or settings.examination_schedule_access_token),
            unquote(_runtime_refresh_token or settings.examination_schedule_refresh_token),
        )


def _save_tokens(access_token: str, refresh_token: str) -> None:
    path = settings.examination_schedule_token_cache_path
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({
            "access_token": access_token,
            "refresh_token": refresh_token,
        }), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
    except OSError:
        # The refreshed token remains usable in memory. A read-only filesystem
        # must not make the synchronization itself fail.
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _decode_response(response) -> object:
    raw = response.read(settings.examination_schedule_max_response_bytes + 1)
    if len(raw) > settings.examination_schedule_max_response_bytes:
        raise ExaminationScheduleUnavailable("Ответ графика слишком большой")
    return json.loads(raw.decode("utf-8"))


def _refresh_tokens() -> str:
    global _runtime_access_token, _runtime_refresh_token
    access_token, refresh_token = _tokens()
    if not refresh_token:
        raise ExaminationScheduleUnavailable("Токен графика истёк, refresh-токен не настроен")
    request = Request(
        f"{settings.examination_schedule_api_url}/v1/refresh",
        data=json.dumps({"refreshToken": refresh_token}).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {refresh_token}",
            "Cookie": f"at={quote(access_token, safe='')}; rt={quote(refresh_token, safe='')}",
            "User-Agent": "ConsiliumScheduleSync/1.0",
        },
    )
    try:
        with urlopen(request, timeout=settings.examination_schedule_timeout_seconds) as response:
            result = _decode_response(response)
            headers = response.headers.get_all("Set-Cookie") or []
    except HTTPError as exc:
        raise ExaminationScheduleUnavailable(
            f"Не удалось обновить токен графика (HTTP {exc.code})"
        ) from exc
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExaminationScheduleUnavailable("Не удалось обновить токен графика") from exc
    data = result.get("data", result) if isinstance(result, dict) else {}
    new_access = _plain(
        data.get("access_token") or data.get("accessToken") or data.get("at") or data.get("token")
    ) if isinstance(data, dict) else ""
    new_refresh = _plain(
        data.get("refresh_token") or data.get("refreshToken") or data.get("rt")
    ) if isinstance(data, dict) else ""
    for header in headers:
        for part in str(header).split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == "at":
                new_access = value
            elif separator and name == "rt":
                new_refresh = value
    if not new_access:
        raise ExaminationScheduleUnavailable("Сервис графика не вернул новый access-токен")
    with _token_lock:
        _runtime_access_token = new_access
        _runtime_refresh_token = new_refresh or refresh_token
        current_refresh = _runtime_refresh_token
    _save_tokens(new_access, current_refresh)
    return new_access


def _request(path: str, *, retry_auth: bool = True) -> object:
    token, refresh_token = _tokens()
    request = Request(
        f"{settings.examination_schedule_api_url}/{path.lstrip('/')}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            # The current frontend uses these token names. Sending both the
            # standard Bearer header and cookies keeps the read-only client
            # compatible while the service is maintained by another team.
            "Cookie": (
                f"at={quote(token, safe='')}; rt={quote(refresh_token, safe='')}"
            ),
            "User-Agent": "ConsiliumScheduleSync/1.0",
        },
    )
    try:
        with urlopen(request, timeout=settings.examination_schedule_timeout_seconds) as response:
            return _decode_response(response)
    except HTTPError as exc:
        if exc.code in {401, 403} and retry_auth and refresh_token:
            _refresh_tokens()
            return _request(path, retry_auth=False)
        raise ExaminationScheduleUnavailable(
            "Сервис графика отклонил запрос авторизации"
            if exc.code in {401, 403} else f"Сервис графика вернул HTTP {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExaminationScheduleUnavailable("Сервис графика временно недоступен") from exc


def _plain(value) -> str:
    if isinstance(value, dict):
        for key in ("name", "title", "label", "value", "text"):
            if value.get(key) not in (None, ""):
                return _plain(value[key])
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(filter(None, (_plain(item) for item in value)))
    return str(value or "").strip()


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", str(value or "").casefold())


def _field(row: dict, *aliases: str):
    wanted = {_normalized_key(alias) for alias in aliases}
    for key, value in row.items():
        if _normalized_key(key) in wanted:
            return value
    nested = row.get("attributes") or row.get("data")
    if isinstance(nested, dict):
        return _field(nested, *aliases)
    return None


def _cell_values(row: dict) -> list:
    for key in ("cells", "values", "columns", "data"):
        value = row.get(key)
        if isinstance(value, list):
            return [_plain(item) for item in value]
    return []


def _parse_date(value: object) -> date | None:
    text = _plain(value).strip()
    if not text:
        return None
    for pattern, day_first in (
        (r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$", False),
        (r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$", True),
    ):
        match = re.match(pattern, text)
        if not match:
            continue
        parts = [int(item) for item in match.groups()]
        try:
            return date(parts[2], parts[1], parts[0]) if day_first else date(*parts)
        except ValueError:
            return None
    return None


def _brigade_map(payload: object) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in _unwrap_list(payload, "brigades"):
        if not isinstance(item, dict):
            continue
        identifier = _plain(_field(item, "id", "brigade_id", "value"))
        name = _plain(_field(item, "name", "title", "label"))
        if identifier and name:
            result[identifier] = name[:200]
    return result


def _sheet_month(sheet: dict) -> tuple[int, int] | None:
    label = _plain(_field(sheet, "name", "title", "label")).casefold()
    year_match = re.search(r"\b(20\d{2})\b", label)
    if not year_match:
        return None
    for month_name, month in _MONTHS_RU.items():
        if month_name in label:
            return int(year_match.group(1)), month
    numeric = re.search(r"(?:^|\D)(0?[1-9]|1[0-2])[./-](20\d{2})(?:\D|$)", label)
    return (int(numeric.group(2)), int(numeric.group(1))) if numeric else None


def _months_in_window(start: date, end: date) -> set[tuple[int, int]]:
    months: set[tuple[int, int]] = set()
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cursor <= last:
        months.add((cursor.year, cursor.month))
        cursor = date(cursor.year + (1 if cursor.month == 12 else 0), cursor.month % 12 + 1, 1)
    return months


def _parse_row(
    raw_row: object, sheet_id: str, brigades: dict[str, str],
    date_from: date, date_to: date,
) -> dict | None:
    if not isinstance(raw_row, dict):
        return None
    cells = _cell_values(raw_row)
    raw_brigade = _field(raw_row, "brigade", "brigade_name", "бригада")
    raw_date = _field(raw_row, "date", "examination_date", "inspection_date", "дата")
    raw_organization = _field(
        raw_row, "organization", "organization_name", "company", "company_name", "организация",
    )
    raw_inn = _field(raw_row, "inn", "company_inn", "organization_inn", "инн")
    if cells:
        raw_brigade = raw_brigade if raw_brigade not in (None, "") else (cells[1] if len(cells) > 1 else "")
        raw_date = raw_date if raw_date not in (None, "") else (cells[2] if len(cells) > 2 else "")
        raw_organization = raw_organization if raw_organization not in (None, "") else (cells[8] if len(cells) > 8 else "")
        raw_inn = raw_inn if raw_inn not in (None, "") else (cells[9] if len(cells) > 9 else "")

    examination_date = _parse_date(raw_date)
    inn = re.sub(r"\D", "", _plain(raw_inn))
    organization = _plain(raw_organization)
    brigade_id = _plain(_field(raw_row, "brigade_id", "brigadeId"))
    brigade = _plain(raw_brigade) or brigades.get(brigade_id, "")
    if brigade in brigades:
        brigade = brigades[brigade]
    if (
        examination_date is None or not (date_from <= examination_date <= date_to)
        or len(inn) not in {10, 12} or not organization or not brigade
    ):
        return None

    note = " ".join(filter(None, (
        _plain(_field(raw_row, "note", "notes", "comment", "description", "примечание")),
        _plain(_field(raw_row, "status")),
    ))).casefold()
    status = "canceled" if re.search(r"отмен|не состо", note) else "active"
    source_id = _plain(_field(raw_row, "id", "row_id", "uuid"))
    if not source_id:
        canonical = json.dumps(raw_row, ensure_ascii=False, sort_keys=True, default=str)
        source_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return {
        "source_sheet_id": sheet_id,
        "source_row_id": source_id[:200],
        "inn": inn,
        "organization_name": organization[:300],
        "examination_date": examination_date.isoformat(),
        "brigade": brigade[:200],
        "status": status,
        "source_updated_at": _plain(_field(raw_row, "updated_at", "updatedAt"))[:80],
    }


def sync_now(today: date | None = None) -> dict:
    """Fetch and atomically upsert the current two-month examination window."""
    if not configured():
        return {"status": "disabled", "rows": 0, "sheets": 0}
    start = today or date.today()
    end = _add_months(start, settings.examination_schedule_horizon_months)
    sheets = _unwrap_list(_request("v1/sheets"), "sheets")
    if len(sheets) > settings.examination_schedule_max_sheets:
        raise ExaminationScheduleUnavailable("Сервис графика вернул слишком много таблиц")
    try:
        brigades = _brigade_map(_request("v1/dictionaries/brigades"))
    except ExaminationScheduleUnavailable:
        # Some installations expose the brigade name directly in every row.
        brigades = {}
    target_months = _months_in_window(start, end)
    dated_sheets = [sheet for sheet in sheets if isinstance(sheet, dict) and _sheet_month(sheet)]
    if dated_sheets:
        sheets = [sheet for sheet in dated_sheets if _sheet_month(sheet) in target_months]
    parsed_rows: list[dict] = []
    fetched_sheet_ids: list[str] = []
    for sheet in sheets:
        if not isinstance(sheet, dict):
            continue
        sheet_id = _plain(_field(sheet, "id", "sheet_id", "value"))
        if not sheet_id or len(sheet_id) > 100:
            continue
        rows = _unwrap_list(_request(f"v1/sheets/{sheet_id}/rows"), "rows")
        fetched_sheet_ids.append(sheet_id)
        for raw_row in rows:
            parsed = _parse_row(raw_row, sheet_id, brigades, start, end)
            if parsed:
                parsed_rows.append(parsed)
    db.replace_enterprise_examination_schedule(parsed_rows, fetched_sheet_ids)
    # Persist even a still-valid pair from the environment. Previously the
    # cache appeared only after the first token refresh, so a successful first
    # sync could leave nothing to copy to another host.
    _save_tokens(*_tokens())
    return {
        "status": "ok", "rows": len(parsed_rows), "sheets": len(fetched_sheet_ids),
        "date_from": start.isoformat(), "date_to": end.isoformat(),
    }


def start_background_sync(log) -> threading.Event:
    """Start one daemon worker and return its shutdown event."""
    stop = threading.Event()
    if not configured():
        return stop

    def worker() -> None:
        if stop.wait(max(0, settings.examination_schedule_initial_delay_seconds)):
            return
        while not stop.is_set():
            try:
                result = sync_now()
                log(f"График осмотров обновлён: {result['rows']} записей")
            except ExaminationScheduleUnavailable as exc:
                log(f"График осмотров не обновлён: {exc}")
            except Exception:
                # Never terminate the application because an external read-only
                # integration returned an unexpected schema.
                log("График осмотров не обновлён: неожиданный формат данных")
            stop.wait(max(300, settings.examination_schedule_sync_interval_seconds))

    threading.Thread(target=worker, name="examination-schedule-sync", daemon=True).start()
    return stop
