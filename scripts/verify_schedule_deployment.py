"""Verify the deployed schedule sync and Bitrix payment-field support."""

import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import database as db  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.examination_schedule import (  # noqa: E402
    ExaminationScheduleUnavailable,
    configured as schedule_configured,
    sync_now,
)


def _json_get(url: str, timeout: float = 10) -> dict:
    with urlopen(url, timeout=timeout) as response:
        raw = response.read(64_001)
        if len(raw) > 64_000:
            raise ValueError("oversized response")
        payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid JSON response")
    return payload


def main() -> int:
    report = {
        "status": "error",
        "checks": {},
        "errors": [],
    }
    db.init_db()
    report["checks"]["schedule_configured"] = schedule_configured()
    if not schedule_configured():
        report["errors"].append("Синхронизация графика отключена или токен не настроен")
    else:
        try:
            report["sync"] = sync_now()
        except ExaminationScheduleUnavailable as exc:
            report["errors"].append(str(exc))

    with db.connection() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS rows_count, COUNT(DISTINCT inn) AS inns_count,
            MIN(examination_date) AS first_date, MAX(examination_date) AS last_date
            FROM enterprise_examination_schedule WHERE status='active'"""
        ).fetchone()
        sample = conn.execute(
            """SELECT inn FROM enterprise_examination_schedule
            WHERE status='active' ORDER BY examination_date, inn LIMIT 1"""
        ).fetchone()
    database_summary = dict(row)
    report["database"] = database_summary
    report["checks"]["active_schedule_rows"] = database_summary["rows_count"] > 0
    if not report["checks"]["active_schedule_rows"]:
        report["errors"].append("В базе нет активных записей графика")
    if sample:
        matched = db.find_upcoming_enterprise_examination(sample["inn"])
        report["checks"]["inn_to_brigade_match"] = bool(
            matched and matched.get("brigade") and matched.get("examination_date")
        )
    else:
        report["checks"]["inn_to_brigade_match"] = False
    if not report["checks"]["inn_to_brigade_match"]:
        report["errors"].append("Не работает сопоставление ИНН с бригадой")

    try:
        consilium = _json_get("http://127.0.0.1:8000/api/health")
        report["checks"]["consilium_health"] = consilium.get("status") == "ok"
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        report["checks"]["consilium_health"] = False
        report["errors"].append("Недоступен /api/health Консилиума")

    try:
        connector = _json_get(f"{settings.bitrix_connector_url}/health")
        report["checks"]["bitrix_connector_health"] = connector.get("status") == "ok"
        capabilities = connector.get("capabilities") if isinstance(connector, dict) else {}
        report["checks"]["bitrix_schedule_fields"] = bool(
            isinstance(capabilities, dict)
            and capabilities.get("consilium_payment_schedule_fields")
        )
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        report["checks"]["bitrix_connector_health"] = False
        report["checks"]["bitrix_schedule_fields"] = False
        report["errors"].append("Недоступен обновлённый Bitrix Connector")
    if not report["checks"].get("bitrix_schedule_fields"):
        report["errors"].append("Bitrix Connector не подтвердил поддержку полей бригады")

    report["status"] = "ok" if not report["errors"] else "error"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
