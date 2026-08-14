import base64
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings


class YooKassaUnavailable(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.yookassa_shop_id and settings.yookassa_secret_key)


def _request(method: str, path: str, payload: dict | None = None, *, idempotence_key: str = "") -> dict:
    if not configured():
        raise YooKassaUnavailable("Онлайн-оплата пока не настроена")
    token = base64.b64encode(
        f"{settings.yookassa_shop_id}:{settings.yookassa_secret_key}".encode("utf-8")
    ).decode("ascii")
    headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{settings.yookassa_api_url}/{path.lstrip('/')}", data=body,
        headers=headers, method=method,
    )
    try:
        with urlopen(request, timeout=settings.yookassa_timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            details = json.loads(exc.read().decode("utf-8"))
            description = str(details.get("description") or details.get("code") or "")
        except Exception:
            description = ""
        raise YooKassaUnavailable(
            f"ЮKassa отклонила запрос{': ' + description[:200] if description else ''}"
        ) from exc
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise YooKassaUnavailable("ЮKassa временно недоступна. Попробуйте ещё раз") from exc
    if not isinstance(result, dict):
        raise YooKassaUnavailable("ЮKassa вернула некорректный ответ")
    return result


def create_payment(order: dict, return_url: str, receipt_email: str = "") -> dict:
    payload = {
        "amount": {"value": f"{int(order['amount_kopecks']) / 100:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": f"Дополнительные обследования, заказ {order['id'][-12:]}",
        "metadata": {"order_id": order["id"]},
    }
    if settings.yookassa_receipts_enabled:
        email = str(receipt_email or "").strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
            raise ValueError("Укажите корректную электронную почту для отправки чека")
        payload["receipt"] = {
            "customer": {"email": email},
            "items": [
                {
                    "description": str(item["name"])[:128],
                    "quantity": "1.00",
                    "amount": {"value": f"{int(item['price']):.2f}", "currency": "RUB"},
                    "vat_code": settings.yookassa_vat_code,
                    "payment_mode": settings.yookassa_payment_mode,
                    "payment_subject": "service",
                }
                for item in order["items"]
            ],
        }
    return _request(
        "POST", "payments", payload, idempotence_key=order["idempotence_key"],
    )


def get_payment(payment_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", str(payment_id or "")):
        raise ValueError("Некорректный идентификатор платежа")
    return _request("GET", f"payments/{payment_id}")
