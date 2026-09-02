"""Forward provider-verified payments to the internal Bitrix Connector."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import company_suggestions
from . import database as db
from .config import settings


class BitrixConnectorUnavailable(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.bitrix_connector_url and settings.bitrix_payment_secret)


def _organization_name(company_inn: str) -> str:
    if not company_inn or not company_suggestions.configured():
        return ""
    try:
        suggestions = company_suggestions.suggest_companies(
            company_inn, client_key="payment-notification",
        )
    except (
        ValueError,
        company_suggestions.CompanySuggestionsUnavailable,
        company_suggestions.CompanySuggestionsRateLimited,
    ):
        return ""
    exact = next((item for item in suggestions if item.get("inn") == company_inn), None)
    return str((exact or {}).get("name", ""))[:300]


def build_payload(order: dict, payment: dict, profile: dict) -> dict:
    payment_method = (
        payment.get("payment_method")
        if isinstance(payment.get("payment_method"), dict) else {}
    )
    company_inn = str(profile.get("company_inn") or "")[:12]
    schedule = db.find_upcoming_enterprise_examination(company_inn) or {}
    return {
        "order_id": str(order["id"]),
        "provider_payment_id": str(payment["id"]),
        "status": "succeeded",
        "amount_kopecks": int(order["amount_kopecks"]),
        "currency": str(order.get("currency") or "RUB"),
        "client_name": str(profile.get("preferred_name") or "")[:100],
        "company_inn": company_inn,
        "organization_name": str(
            schedule.get("organization_name") or _organization_name(company_inn)
        )[:300],
        "brigade": str(schedule.get("brigade") or "")[:200],
        "examination_date": str(schedule.get("examination_date") or "")[:10],
        "paid_at": str(payment.get("captured_at") or payment.get("created_at") or "")[:80],
        "provider_created_at": str(payment.get("created_at") or "")[:80],
        "provider_description": str(payment.get("description") or "")[:300],
        "payment_method": str(
            payment_method.get("title") or payment_method.get("type") or ""
        )[:100],
        "test": bool(payment.get("test")),
        "items": [
            {
                "name": str(item.get("name") or "")[:128],
                "amount_kopecks": int(item.get("price") or 0) * 100,
            }
            for item in order.get("items") or []
        ],
    }


def notify_verified_payment(order: dict, payment: dict, profile: dict) -> str:
    """Queue one notification; connector deduplicates retries by local order ID."""
    if not configured():
        return "disabled"
    body = json.dumps(
        build_payload(order, payment, profile), ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        f"{settings.bitrix_connector_url}/bitrix/payments/consilium",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Consilium-Payment-Secret": settings.bitrix_payment_secret,
        },
    )
    try:
        with urlopen(request, timeout=settings.bitrix_connector_timeout_seconds) as response:
            raw = response.read(64_001)
            if len(raw) > 64_000:
                raise BitrixConnectorUnavailable("Bitrix Connector returned an oversized response")
            result = json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        raise BitrixConnectorUnavailable(
            f"Bitrix Connector rejected the notification (HTTP {exc.code})"
        ) from exc
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BitrixConnectorUnavailable("Bitrix Connector is temporarily unavailable") from exc
    status = str(result.get("status", "")) if isinstance(result, dict) else ""
    if status not in {"queued", "duplicate"}:
        raise BitrixConnectorUnavailable("Bitrix Connector returned an invalid response")
    return status
