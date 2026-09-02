import json
import hmac
import mimetypes
import re
import secrets
import threading
import time
import traceback
import webbrowser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import analytics, company_suggestions, database as db, examination_schedule, splitter_tracking
from .config import BASE_DIR, settings
from .llm import LLMNotConfigured
from .lab_results import LabResultsUnavailable, lookup_lab_results
from . import bitrix_payments, yookassa
from .orchestrator import orchestrator
from .onboarding import normalize_examination_selection, public_onboarding
from .prompts import public_agents


STATIC_DIR = BASE_DIR / "static"
ALLOWED_STATIC = {
    "app.js", "agents.js", "styles.css", "styles.07ffaefb4795.css", "metrika.js",
    "rich-text.js", "rich-text.css", "rich-text.2bf1f5fab764.css", "dashboard.js", "dashboard.css",
    "manager.js", "manager.css", "icon-192.png", "icon-512.png",
    "icon-maskable-512.png", "apple-touch-icon.png", "favicon.svg",
}
SERVER_ERROR_LOG = settings.log_path
MANAGER_SESSION_COOKIE = "consilium_manager_session"


def admin_token_valid(authorization: str, expected: str | None = None) -> bool:
    expected = settings.admin_dashboard_token if expected is None else expected
    authorization = str(authorization or "")
    if not authorization.startswith("Bearer "):
        return False
    supplied = authorization.removeprefix("Bearer ").strip()
    return bool(expected and supplied and hmac.compare_digest(supplied, expected))


def bot_token_valid(authorization: str) -> bool:
    return admin_token_valid(authorization, settings.bot_integration_secret)


def _record_startup_event(message: str) -> None:
    line = f"[{db.utc_now()}] {message}\n"
    try:
        with SERVER_ERROR_LOG.open("a", encoding="utf-8") as log:
            log.write(line)
    except OSError:
        pass
    print(message, flush=True)


def _refresh_due_lab_result_notifications() -> None:
    """Turn due subscriptions into the existing bot delivery stream."""
    for subscription in db.claim_due_lab_result_subscriptions(3):
        try:
            result = lookup_lab_results(subscription["med_id"]).to_dict()
            documents = result.get("documents") if result.get("status") == "found" else []
            if documents:
                db.complete_lab_result_subscription_check(
                    subscription["id"], documents, settings.public_base_url,
                )
        except (ValueError, LabResultsUnavailable):
            # The reservation already delays the next attempt, so an unavailable
            # Google Sheet cannot create a tight retry loop in messenger bots.
            continue


def _record_server_error(prefix: str) -> None:
    details = f"\n[{db.utc_now()}] {prefix}\n{traceback.format_exc()}\n"
    try:
        with SERVER_ERROR_LOG.open("a", encoding="utf-8") as log:
            log.write(details)
    except OSError:
        pass
    print(details, flush=True)


class ConsiliumHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        _record_server_error(f"Ошибка запроса от {client_address[0]}:{client_address[1]}")


class ConsiliumHandler(BaseHTTPRequestHandler):
    server_version = "Consilium/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/auth/max", "/auth/messenger"}:
            return self._consume_messenger_login(parse_qs(parsed.query).get("t", [""])[0])
        if path == "/api/health":
            return self._json(200, {"status": "ok"})
        if path == "/api/public-config":
            counter_id = settings.yandex_metrika_counter_id
            return self._json(200, {
                "yandex_metrika_counter_id": counter_id if counter_id.isdigit() else "",
                "online_payments_enabled": bool(
                    settings.online_payments_enabled and yookassa.configured()
                ),
                "payment_receipt_email_required": bool(settings.yookassa_receipts_enabled),
                "company_suggestions_enabled": company_suggestions.configured(),
            })
        if path == "/api/bot/manager-notifications":
            if not bot_token_valid(self.headers.get("Authorization", "")):
                return self._json(401, {"detail": "Неверные данные интеграции"})
            query = parse_qs(parsed.query)
            try:
                provider = query.get("provider", [""])[0]
                limit = max(1, min(50, int(query.get("limit", ["20"])[0])))
                _refresh_due_lab_result_notifications()
                user_notifications = db.claim_user_result_notifications(
                    provider, min(5, limit),
                )
                manager_notifications = db.claim_manager_notifications(
                    provider, max(1, limit - len(user_notifications)),
                ) if len(user_notifications) < limit else []
                return self._json(200, {
                    "notifications": user_notifications + manager_notifications,
                })
            except (ValueError, TypeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/favicon.ico":
            return self._send_file(STATIC_DIR / "favicon.svg", "image/svg+xml; charset=utf-8")
        if path == "/api/ready":
            database_ready = False
            try:
                with db.connection() as conn:
                    database_ready = conn.execute("SELECT 1").fetchone()[0] == 1
            except Exception:
                database_ready = False
            messenger_auth_ready = bool(settings.bot_integration_secret) and (
                settings.app_env != "production" or settings.public_base_url.startswith("https://")
            )
            ready = database_ready and bool(settings.openai_api_key) and messenger_auth_ready
            return self._json(200 if ready else 503, {
                "status": "ready" if ready else "not_ready",
                "database": "ok" if database_ready else "error",
                "ai_configured": bool(settings.openai_api_key),
                "messenger_auth_configured": messenger_auth_ready,
                "max_auth_configured": messenger_auth_ready,
            })
        if path in {"/dashboard", "/admin"}:
            return self._send_file(BASE_DIR / "dashboard.html", "text/html; charset=utf-8")
        if path == "/manager":
            return self._send_file(BASE_DIR / "manager.html", "text/html; charset=utf-8")
        if path == "/manifest.webmanifest":
            return self._send_file(
                BASE_DIR / "manifest.webmanifest",
                "application/manifest+json; charset=utf-8",
            )
        if path == "/service-worker.js":
            return self._send_file(
                BASE_DIR / "service-worker.js",
                "application/javascript; charset=utf-8",
            )
        if path.startswith("/static/"):
            name = path.removeprefix("/static/")
            if name not in ALLOWED_STATIC:
                return self._json(404, {"detail": "Файл не найден"})
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            immutable = bool(re.search(r"\.[0-9a-f]{12}\.(?:css|js)$", name))
            return self._send_file(
                STATIC_DIR / name,
                f"{mime}; charset=utf-8",
                cache_control=(
                    "public, max-age=31536000, immutable" if immutable else "no-store"
                ),
            )
        if path in {"/api/admin/dashboard", "/api/admin/table", "/api/admin/ai-costs", "/api/admin/analytics", "/api/admin/metric2"}:
            if not self._admin_authorized():
                return
            if path == "/api/admin/dashboard":
                return self._json(200, db.admin_dashboard())
            query = parse_qs(parsed.query)
            if path == "/api/admin/metric2":
                try:
                    report = analytics.metric2_report(
                        query.get("period", ["30"])[0],
                        query.get("device", [""])[0],
                        query.get("method", [""])[0],
                        query.get("source", [""])[0],
                        query.get("date_from", [""])[0],
                        query.get("date_to", [""])[0],
                        query.get("flow", ["standard"])[0],
                    )
                    report["examinations"] = db.list_examinations()
                    return self._json(200, report)
                except (ValueError, TypeError) as exc:
                    return self._json(422, {"detail": str(exc)})
            if path == "/api/admin/analytics":
                try:
                    period = query.get("period", ["30"])[0]
                    report = analytics.admin_report(
                        period,
                        query.get("device", [""])[0],
                        query.get("method", [""])[0],
                        query.get("source", [""])[0],
                        int(query.get("recent_page", ["1"])[0]),
                        int(query.get("recent_limit", ["25"])[0]),
                        query.get("date_from", [""])[0],
                        query.get("date_to", [""])[0],
                    )
                    report["manager_attribution"] = db.admin_manager_attribution(
                        period,
                        query.get("date_from", [""])[0],
                        query.get("date_to", [""])[0],
                    )
                    return self._json(200, report)
                except (ValueError, TypeError) as exc:
                    return self._json(422, {"detail": str(exc)})
            if path == "/api/admin/ai-costs":
                try:
                    return self._json(200, db.admin_ai_costs(
                        query.get("period", ["30"])[0],
                        int(query.get("limit", ["100"])[0]),
                    ))
                except (ValueError, TypeError) as exc:
                    return self._json(422, {"detail": str(exc)})
            try:
                return self._json(200, db.admin_table(
                    query.get("name", [""])[0],
                    query.get("query", [""])[0],
                    int(query.get("limit", ["25"])[0]),
                    int(query.get("offset", ["0"])[0]),
                    query.get("created_from", [""])[0],
                    query.get("created_to", [""])[0],
                    query.get("sort", [""])[0],
                    query.get("order", ["desc"])[0],
                ))
            except (ValueError, TypeError):
                return self._json(422, {"detail": "Некорректные параметры таблицы"})
        if path == "/api/admin/managers":
            if not self._admin_authorized():
                return
            return self._json(200, db.admin_list_staff())
        if path == "/api/admin/examinations":
            if not self._admin_authorized():
                return
            return self._json(200, db.list_examinations())
        if path.startswith("/api/manager/"):
            manager = self._manager_authorized()
            if not manager:
                return
            if path == "/api/manager/me":
                return self._json(200, manager)
            if path == "/api/manager/conversations":
                query = parse_qs(parsed.query)
                try:
                    return self._json(200, db.manager_list_conversations(
                        query.get("query", [""])[0],
                        query.get("queue", ["open"])[0],
                        int(query.get("limit", ["100"])[0]),
                        query.get("include_related", ["0"])[0] == "1",
                    ))
                except (ValueError, TypeError) as exc:
                    return self._json(422, {"detail": str(exc)})
            conversation_id = path.removeprefix("/api/manager/conversations/").strip("/")
            if conversation_id and "/" not in conversation_id:
                detail = db.manager_conversation_detail(conversation_id)
                if not detail:
                    return self._json(404, {"detail": "Диалог не найден"})
                return self._json(200, detail)
            return self._json(404, {"detail": "Маршрут панели менеджера не найден"})
        if path in {"/", "/result", "/result/"}:
            splitter_tracking.notify_async(
                splitter_tracking.tracking_from_query(parse_qs(parsed.query)),
                splitter_tracking.SERVER_STAGE,
            )
        self._ensure_user_context()
        if path in {"/", "/result", "/result/"}:
            db.record_device_access(self.headers.get("User-Agent", ""))
            return self._send_file(
                BASE_DIR / "index.html",
                "text/html; charset=utf-8",
                allow_metrika_frame=True,
            )
        if path == "/api/agents":
            return self._json(200, public_agents())
        if path == "/api/me":
            identities = db.current_external_identities()
            return self._json(200, {
                "chel_id": db.current_chel_id(),
                "authenticated": bool(identities),
                "provider": identities[0]["provider"] if identities else None,
                "providers": [identity["provider"] for identity in identities],
                "result_entry": db.current_user_has_result_entry(),
                "messengers": {
                    "telegram": {"configured": bool(settings.telegram_bot_auth_url)},
                    "max": {"configured": bool(settings.max_bot_auth_url)},
                },
            })
        if path == "/api/memories":
            return self._json(200, db.list_memories())
        if path == "/api/profile":
            return self._json(200, db.get_profile())
        if path == "/api/body-symptoms":
            return self._json(200, db.list_body_symptoms())
        if path == "/api/health-history":
            return self._json(200, db.list_health_history())
        if path == "/api/onboarding":
            return self._json(200, public_onboarding(
                db.get_onboarding(), db.get_profile(), db.list_examinations(),
            ))
        if path == "/api/purchases":
            return self._json(200, {"purchases": db.list_payment_orders()})
        if path.startswith("/api/payments/"):
            order_id = path.removeprefix("/api/payments/").strip("/")
            order = db.payment_order_private(order_id)
            if not order:
                return self._json(404, {"detail": "Заказ не найден"})
            try:
                if order.get("provider_payment_id") and order.get("status") not in {"succeeded", "canceled"}:
                    verified = yookassa.get_payment(order["provider_payment_id"])
                    order = db.apply_yookassa_status(order_id, verified)
                else:
                    order = db.public_payment_order(order_id)
                return self._json(200, {"order": order})
            except (ValueError, yookassa.YooKassaUnavailable) as exc:
                return self._json(502, {"detail": str(exc)})
        if path.startswith("/api/handoff-preview/"):
            conversation_id = path.removeprefix("/api/handoff-preview/")
            item = db.get_conversation(conversation_id)
            if not item:
                return self._json(404, {"detail": "Диалог не найден"})
            try:
                context = json.loads(item.get("context_summary") or "{}")
            except json.JSONDecodeError:
                context = {}
            return self._json(200, {
                "ticket_id": item.get("human_ticket_id"),
                "goal": context.get("user_goal", ""),
                "topic": context.get("current_topic", ""),
                "facts": context.get("known_facts", []),
                "open_questions": context.get("open_questions", []),
                "active_agent": item.get("active_agent"),
            })
        if path == "/api/conversations":
            return self._json(200, db.list_conversations())
        if path == "/api/conversations/unread":
            return self._json(200, {"unread_counts": db.conversation_unread_counts()})
        if path.startswith("/api/conversations/") and path.endswith("/updates"):
            conversation_id = path.removeprefix("/api/conversations/").removesuffix("/updates").strip("/")
            item = db.get_conversation(conversation_id)
            if not item:
                return self._json(404, {"detail": "Диалог не найден"})
            query = parse_qs(parsed.query)
            try:
                after_id = int(query.get("after_id", ["0"])[0])
            except (ValueError, TypeError):
                return self._json(422, {"detail": "Некорректный идентификатор сообщения"})
            return self._json(200, {
                "conversation_id": conversation_id,
                "ai_enabled": bool(item.get("ai_enabled", 1)),
                "human_status": item.get("human_status", "none"),
                "human_ticket_id": item.get("human_ticket_id"),
                "human_channel": item.get("human_channel"),
                "messages": db.list_messages_after(conversation_id, after_id),
                "unread_counts": db.conversation_unread_counts(),
            })
        if path.startswith("/api/conversations/"):
            conversation_id = path.removeprefix("/api/conversations/")
            item = db.get_conversation(conversation_id)
            if not item:
                return self._json(404, {"detail": "Диалог не найден"})
            return self._json(200, {**item, "messages": db.list_messages(conversation_id), "handoffs": db.list_handoffs(conversation_id)})
        return self._json(404, {"detail": "Маршрут не найден"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/splitter/event":
            try:
                payload = self._read_json(max_bytes=4_000)
                tracking = splitter_tracking.tracking_from_payload(payload)
                event = str(payload.get("event", ""))
                if not tracking or event not in splitter_tracking.CLIENT_STAGES:
                    raise ValueError("Некорректное событие доставки")
                result = splitter_tracking.notify(tracking, event)
                if result == "failed":
                    return self._json(503, {"status": "retry"})
                return self._json(202, {"status": result})
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/payments/yookassa/webhook":
            try:
                payload = self._read_json(max_bytes=128_000)
                event = str(payload.get("event", ""))
                notification = payload.get("object")
                if event not in {"payment.succeeded", "payment.canceled", "payment.waiting_for_capture"}:
                    return self._json(200, {"status": "ignored"})
                if not isinstance(notification, dict):
                    raise ValueError("Некорректное уведомление ЮKassa")
                provider_id = str(notification.get("id", ""))
                order = db.payment_order_by_provider_id(provider_id)
                if not order:
                    return self._json(200, {"status": "unknown_payment"})
                # The webhook body is never trusted on its own: re-read the payment
                # from YooKassa with server credentials before changing local state.
                verified = yookassa.get_payment(provider_id)
                updated = db.apply_yookassa_status(order["id"], verified)
                if updated["status"] == "succeeded" and updated.get("paid"):
                    analytics.record_server_event(
                        order["chel_id"], "payment_completed",
                        {"provider": "yookassa", "result": "succeeded"},
                    )
                    bitrix_payments.notify_verified_payment(
                        order, verified, db.payment_customer_profile(order["chel_id"]),
                    )
                return self._json(200, {"status": updated["status"]})
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(400, {"detail": str(exc)})
            except yookassa.YooKassaUnavailable as exc:
                return self._json(503, {"detail": str(exc)})
            except bitrix_payments.BitrixConnectorUnavailable as exc:
                # YooKassa will retry the webhook. The connector deduplicates by
                # local order ID if an earlier attempt was already accepted.
                return self._json(503, {"detail": str(exc)})
        if path == "/api/bot/manager-bind":
            if not bot_token_valid(self.headers.get("Authorization", "")):
                return self._json(401, {"detail": "Неверные данные интеграции"})
            try:
                payload = self._read_json()
                binding = db.bind_staff_messenger(
                    str(payload.get("token", "")), str(payload.get("provider", "")),
                    payload.get("provider_user_id", ""), payload.get("chat_id", ""),
                )
                return self._json(200, {
                    **binding, "manager_url": f"{settings.public_base_url}/manager",
                })
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path.startswith("/api/bot/manager-notifications/") and path.endswith("/ack"):
            if not bot_token_valid(self.headers.get("Authorization", "")):
                return self._json(401, {"detail": "Неверные данные интеграции"})
            try:
                notification_id = int(
                    path.removeprefix("/api/bot/manager-notifications/").removesuffix("/ack").strip("/")
                )
                payload = self._read_json()
                if not isinstance(payload.get("success"), bool):
                    raise ValueError("success должен быть true или false")
                if notification_id < 0:
                    acknowledged = db.acknowledge_user_result_notification(
                        abs(notification_id), str(payload.get("lease_token", "")),
                        payload["success"], str(payload.get("error", "")),
                    )
                else:
                    acknowledged = db.acknowledge_manager_notification(
                        notification_id, str(payload.get("lease_token", "")),
                        payload["success"], str(payload.get("error", "")),
                    )
                if not acknowledged:
                    return self._json(409, {"detail": "Уведомление уже обработано или аренда истекла"})
                return self._json(200, {"status": "acknowledged"})
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/manager/login":
            try:
                payload = self._read_json()
                authenticated = db.authenticate_staff(
                    str(payload.get("login", "")), str(payload.get("password", "")),
                )
                if not authenticated:
                    return self._json(401, {"detail": "Неверный логин или пароль"})
                self._manager_session_to_set = authenticated["token"]
                return self._json(200, authenticated["user"])
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                return self._json(401, {"detail": "Неверный логин или пароль"})
        if path == "/api/manager/logout":
            if self.headers.get("X-Consilium-Manager") != "1":
                return self._json(403, {"detail": "Запрос менеджера не подтверждён"})
            token = self._manager_cookie()
            db.revoke_staff_session(token)
            self._clear_manager_session = True
            return self._json(200, {"status": "logged_out"})
        if path == "/api/admin/managers":
            if not self._admin_authorized():
                return
            try:
                payload = self._read_json()
                for key in ("notify_new_requests", "notify_new_messages"):
                    if key in payload and not isinstance(payload[key], bool):
                        raise ValueError(f"{key} должен быть true или false")
                return self._json(201, db.admin_create_staff(
                    str(payload.get("display_name", "")),
                    str(payload.get("login", "")),
                    str(payload.get("password", "")),
                    telegram_id=payload.get("telegram_id", ""),
                    max_id=payload.get("max_id", ""),
                    notify_new_requests=payload.get("notify_new_requests", True),
                    notify_new_messages=payload.get("notify_new_messages", True),
                ))
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/admin/users/delete-data":
            if not self._admin_authorized():
                return
            if self.headers.get("X-Consilium-Action") != "delete-user-data":
                return self._json(403, {"detail": "Подтвердите полное удаление данных пользователя"})
            try:
                payload = self._read_json()
                chel_id = str(payload.get("chel_id", "")).strip()
                if str(payload.get("confirmation", "")).strip() != chel_id:
                    raise ValueError("Для подтверждения повторите chel_id без изменений")
                main_result = db.admin_delete_user_data(chel_id)
                analytics_result = analytics.delete_user_data(chel_id)
                deleted = main_result["deleted"] + sum(analytics_result.values())
                if not deleted:
                    return self._json(404, {"detail": "Пользователь с таким chel_id не найден"})
                return self._json(200, {
                    **main_result,
                    "analytics": analytics_result,
                    "deleted": deleted,
                    "status": "deleted",
                })
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/admin/examinations":
            if not self._admin_authorized():
                return
            try:
                payload = self._read_json()
                return self._json(201, db.admin_create_examination(
                    payload.get("name", ""),
                    payload.get("description", ""),
                    payload.get("includes", ""),
                    payload.get("price"),
                    payload.get("competitor_price"),
                    payload.get("price_without_discount"),
                    payload.get("competitor_label"),
                    payload.get("retail_price_label"),
                    payload.get("discount_price_label"),
                    payload.get("show_competitor_price"),
                    payload.get("show_retail_price"),
                    payload.get("show_discount_price"),
                ))
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path.startswith("/api/admin/examinations/"):
            if not self._admin_authorized():
                return
            try:
                examination_id = path.removeprefix("/api/admin/examinations/").strip("/")
                payload = self._read_json()
                item = db.admin_update_examination(
                    examination_id,
                    payload.get("name", ""),
                    payload.get("description", ""),
                    payload.get("includes", ""),
                    payload.get("price"),
                    payload.get("competitor_price"),
                    payload.get("price_without_discount"),
                    payload.get("competitor_label"),
                    payload.get("retail_price_label"),
                    payload.get("discount_price_label"),
                    payload.get("show_competitor_price"),
                    payload.get("show_retail_price"),
                    payload.get("show_discount_price"),
                )
                if not item:
                    return self._json(404, {"detail": "Обследование не найдено"})
                return self._json(200, item)
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path.startswith("/api/admin/managers/") and path.endswith("/messenger-link"):
            if not self._admin_authorized():
                return
            try:
                staff_id = int(
                    path.removeprefix("/api/admin/managers/").removesuffix("/messenger-link").strip("/")
                )
                payload = self._read_json()
                binding = db.create_staff_messenger_token(staff_id, payload.get("provider", ""))
                template = (
                    settings.telegram_bot_auth_url
                    if binding["provider"] == "telegram" else settings.max_bot_auth_url
                )
                if not template or "{token}" not in template:
                    raise ValueError(f"Ссылка бота {binding['provider'].upper()} не настроена")
                return self._json(201, {
                    **binding,
                    "bot_url": template.replace("{token}", quote(binding["token"])),
                })
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path.startswith("/api/admin/managers/"):
            if not self._admin_authorized():
                return
            try:
                staff_id = int(path.removeprefix("/api/admin/managers/").strip("/"))
                payload = self._read_json()
                if "is_active" in payload and not isinstance(payload["is_active"], bool):
                    raise ValueError("is_active должен быть true или false")
                for key in ("notify_new_requests", "notify_new_messages"):
                    if key in payload and not isinstance(payload[key], bool):
                        raise ValueError(f"{key} должен быть true или false")
                item = db.admin_update_staff(
                    staff_id,
                    display_name=payload.get("display_name"),
                    password=payload.get("password"),
                    is_active=payload.get("is_active"),
                    telegram_id=payload.get("telegram_id"),
                    max_id=payload.get("max_id"),
                    notify_new_requests=payload.get("notify_new_requests"),
                    notify_new_messages=payload.get("notify_new_messages"),
                )
                if not item:
                    return self._json(404, {"detail": "Менеджер не найден"})
                return self._json(200, item)
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/auth/max/link":
            return self._create_max_auth_link()
        if path == "/api/auth/messenger/link":
            return self._create_messenger_auth_link()
        if path.startswith("/api/manager/conversations/"):
            manager = self._manager_authorized()
            if not manager:
                return
            suffix = path.removeprefix("/api/manager/conversations/")
            if suffix.endswith("/close"):
                conversation_id = suffix.removesuffix("/close").strip("/")
                conversation = db.manager_close_conversation(
                    conversation_id, manager["display_name"],
                )
                if not conversation:
                    return self._json(404, {"detail": "Диалог не найден"})
                return self._json(200, {"conversation": conversation})
            if suffix.endswith("/reply"):
                conversation_id = suffix.removesuffix("/reply").strip("/")
                try:
                    payload = self._read_json()
                    message = db.manager_add_reply(
                        conversation_id,
                        str(payload.get("message", "")),
                        manager["display_name"],
                    )
                    return self._json(201, {
                        "message": message,
                        "conversation": db.manager_conversation_detail(conversation_id)["conversation"],
                    })
                except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    return self._json(422, {"detail": str(exc)})
            if suffix.endswith("/ai-mode"):
                conversation_id = suffix.removesuffix("/ai-mode").strip("/")
                try:
                    payload = self._read_json()
                    if not isinstance(payload.get("enabled"), bool):
                        raise ValueError("Передайте enabled=true или enabled=false")
                    conversation = db.manager_set_ai_enabled(
                        conversation_id,
                        payload["enabled"],
                        manager["display_name"],
                    )
                    if not conversation:
                        return self._json(404, {"detail": "Диалог не найден"})
                    return self._json(200, {"conversation": conversation})
                except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    return self._json(422, {"detail": str(exc)})
            return self._json(404, {"detail": "Маршрут панели менеджера не найден"})
        self._ensure_user_context()
        if path == "/api/company-suggestions":
            try:
                payload = self._read_json(max_bytes=1_000)
                query = str(payload.get("query", "")).strip()
                if not query.isdigit() or not 4 <= len(query) <= 12:
                    raise ValueError("Введите от 4 до 12 цифр ИНН")
                return self._json(200, {
                    "suggestions": company_suggestions.suggest_companies(
                        query, client_key=self.client_address[0],
                    ),
                })
            except company_suggestions.CompanySuggestionsRateLimited as exc:
                return self._json(429, {"detail": str(exc)})
            except company_suggestions.CompanySuggestionsUnavailable as exc:
                return self._json(503, {"detail": str(exc)})
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/payments/yookassa/create":
            order = None
            try:
                payload = self._read_json()
                if not settings.online_payments_enabled:
                    raise yookassa.YooKassaUnavailable(
                        "Онлайн-оплата временно недоступна"
                    )
                if not yookassa.configured():
                    raise yookassa.YooKassaUnavailable("Онлайн-оплата пока не настроена")
                state = db.get_onboarding()
                if state["status"] != "payment" or not state["selected_tests"]:
                    raise ValueError("Сначала выберите дополнительные обследования")
                order = db.create_payment_order()
                private_order = db.payment_order_private(order["id"])
                if private_order.get("provider_payment_id"):
                    # A user may return to the payment screen after the provider
                    # link has expired or the payment has already completed. Read
                    # the current status before reusing any saved redirect URL.
                    verified = yookassa.get_payment(private_order["provider_payment_id"])
                    refreshed = db.apply_yookassa_status(order["id"], verified)
                    if refreshed["status"] != "canceled":
                        return self._json(200, {"order": refreshed})
                    order = db.create_payment_order()
                    private_order = db.payment_order_private(order["id"])
                return_to_chat = payload.get("return_to_chat") is True
                payment_source = str(payload.get("payment_source", "onboarding")).strip()
                if payment_source not in {"onboarding", "purchases"}:
                    payment_source = "onboarding"
                return_url = (
                    f"{settings.public_base_url}/?payment_return={quote(order['id'])}"
                    f"{'&return_to_chat=1' if return_to_chat else ''}"
                    f"&payment_source={quote(payment_source)}"
                )
                payment = yookassa.create_payment(
                    private_order, return_url, str(payload.get("receipt_email", "")),
                )
                result = db.attach_yookassa_payment(order["id"], payment)
                if result["status"] in {"succeeded", "canceled", "waiting_for_capture"}:
                    result = db.apply_yookassa_status(order["id"], payment)
                self._track_analytics("payment_created", {
                    "provider": "yookassa", "amount_kopecks": private_order["amount_kopecks"],
                    "selected_count": len(private_order["items"]),
                })
                return self._json(201, {"order": result})
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                if order:
                    db.mark_payment_creation_failed(order["id"], str(exc))
                return self._json(422, {"detail": str(exc)})
            except yookassa.YooKassaUnavailable as exc:
                # Keep the same idempotence key after an uncertain network/API
                # result. A retry cannot accidentally create a second charge.
                return self._json(503, {"detail": str(exc)})
        if path.startswith("/api/payments/") and path.endswith("/abandon"):
            order_id = path.removeprefix("/api/payments/").removesuffix("/abandon").strip("/")
            try:
                order = db.payment_order_private(order_id)
                if not order:
                    return self._json(404, {"detail": "Заказ не найден"})
                if order.get("provider_payment_id"):
                    verified = yookassa.get_payment(order["provider_payment_id"])
                    refreshed = db.apply_yookassa_status(order_id, verified)
                    if refreshed["status"] == "waiting_for_capture":
                        canceled = yookassa.cancel_payment(
                            order["provider_payment_id"], f"{order_id}-cancel",
                        )
                        refreshed = db.apply_yookassa_status(order_id, canceled)
                    elif refreshed["status"] == "pending":
                        refreshed = db.mark_payment_abandoned(order_id)
                else:
                    refreshed = db.mark_payment_abandoned(order_id)
                if refreshed["status"] == "abandoned":
                    self._track_analytics("payment_abandoned", {"provider": "yookassa"})
                return self._json(200, {"order": refreshed})
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(422, {"detail": str(exc)})
            except yookassa.YooKassaUnavailable as exc:
                return self._json(503, {"detail": str(exc)})
        if path == "/api/analytics/events":
            try:
                payload = self._read_json(max_bytes=64_000)
                result = analytics.record_events(
                    db.current_chel_id(), payload.get("events", []),
                    user_agent=self.headers.get("User-Agent", ""),
                )
                return self._json(202, result)
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
                return self._json(202, {"accepted": 0, "duplicates": 0})
        if path == "/api/register-choice":
            try:
                payload = self._read_json()
                method = str(payload.get("method", "")).strip().lower()
                if method != "anonymous":
                    raise ValueError("На этом экране доступен только анонимный вход")
                user = db.mark_current_user_registered(method)
                self._track_analytics("registration_completed", {"method": method})
                return self._json(200, {
                    "status": "registered",
                    "chel_id": user["chel_id"],
                    "registration_method": user["registration_method"],
                    "registered_at": user["registered_at"],
                })
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/auth/messenger/start":
            return self._start_messenger_auth()
        if path == "/api/conversations":
            conversation = db.create_conversation()
            self._track_analytics("conversation_created")
            return self._json(201, conversation)
        if path == "/api/reset-user":
            if self.headers.get("X-Consilium-Action") != "reset-user":
                return self._json(403, {"detail": "Подтвердите полный сброс данных"})
            linked_identity = db.current_external_identity()
            db.reset_current_user(preserve_identity=bool(linked_identity))
            if linked_identity:
                return self._json(200, {
                    "status": "reset",
                    "chel_id": db.current_chel_id(),
                    "identity_preserved": True,
                })
            new_chel_id = f"chel_{secrets.token_hex(16)}"
            db.ensure_user(new_chel_id, pending=True)
            db.set_current_chel_id(new_chel_id)
            self._identity_cookie_required = True
            return self._json(200, {
                "status": "reset", "chel_id": new_chel_id, "identity_preserved": False,
            })
        if path == "/api/delete-my-data":
            if self.headers.get("X-Consilium-Action") != "delete-my-data":
                return self._json(403, {"detail": "Подтвердите полное удаление данных"})
            try:
                payload = self._read_json()
                if str(payload.get("confirmation", "")).strip() != "delete-my-data":
                    raise ValueError("Подтверждение удаления не получено")
                chel_id = db.current_chel_id()
                main_result = db.admin_delete_user_data(chel_id)
                analytics_result = analytics.delete_user_data(chel_id)
                new_chel_id = f"chel_{secrets.token_hex(16)}"
                db.ensure_user(new_chel_id, pending=True)
                db.set_current_chel_id(new_chel_id)
                self._identity_cookie_required = True
                self._clear_user_session = True
                return self._json(200, {
                    "status": "deleted",
                    "chel_id": new_chel_id,
                    "identity_preserved": False,
                    "deleted": main_result["deleted"] + sum(analytics_result.values()),
                })
            except ValueError as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/result-entry/start":
            try:
                first_result_entry = not db.current_user_has_result_entry()
                user = db.mark_current_user_result_entry()
                self._track_analytics("result_entry_started", {
                    "first_entry": first_result_entry,
                })
                if first_result_entry and user["registration_method"] == "result":
                    self._track_analytics("registration_completed", {"method": "result"})
                return self._json(200, {
                    "status": "registered",
                    "chel_id": user["chel_id"],
                    "registration_method": user["registration_method"],
                    "result_entry_at": user["result_entry_at"],
                })
            except ValueError as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/memories":
            try:
                payload = self._read_json()
                return self._json(201, db.add_memory(str(payload.get("content", "")), str(payload.get("category", "preference"))))
            except ValueError as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/profile":
            try:
                return self._json(200, db.save_profile(self._validate_profile(self._read_json())))
            except (ValueError, TypeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/lab-results/interpret":
            missing_profile = self._interpretation_profile_missing(db.get_profile())
            if missing_profile:
                return self._json(422, {
                    "code": "interpretation_profile_required",
                    "detail": "Перед расшифровкой заполните пол, возраст, рост и вес",
                    "missing_fields": missing_profile,
                })
            try:
                payload = self._read_json()
                self._track_analytics("lab_interpretation_started", {
                    "document_count": 1 if str(payload.get("document_id", "all")) != "all" else 0,
                })
                result = orchestrator.interpret_lab_results(
                    payload.get("conversation_id"),
                    str(payload.get("document_id", "all")),
                )
                result_payload = result.to_dict()
                self._track_analytics("lab_interpretation_completed", {
                    "cached": bool(result_payload.get("cached")),
                })
                return self._json(200, result_payload)
            except LLMNotConfigured as exc:
                return self._json(503, {"detail": str(exc)})
            except ValueError as exc:
                return self._json(422, {"detail": str(exc)})
            except Exception as exc:
                return self._json(502, {
                    "detail": f"Не удалось расшифровать результаты: {exc}",
                })
        if path == "/api/lab-results":
            self._track_analytics("lab_results_requested")
            tube_number = str(db.get_profile().get("tube_number", "")).strip()
            if not tube_number:
                return self._json(422, {
                    "status": "tube_required",
                    "detail": "Сначала введите номер пробирки",
                })
            try:
                lab_result = lookup_lab_results(tube_number).to_dict()
                self._track_analytics("lab_results_found", {
                    "document_count": len(lab_result.get("documents", [])),
                })
                return self._json(200, lab_result)
            except ValueError as exc:
                self._track_analytics("lab_results_not_found", {"error_code": "invalid_tube"})
                return self._json(422, {"status": "invalid_tube", "detail": str(exc)})
            except LabResultsUnavailable:
                self._track_analytics("lab_results_not_found", {"error_code": "unavailable"})
                return self._json(503, {
                    "status": "unavailable",
                    "detail": "Сервис результатов временно недоступен. Попробуйте позже.",
                })
        if path == "/api/lab-results/notification":
            try:
                tube_number = str(db.get_profile().get("tube_number", "")).strip()
                subscription = db.create_lab_result_subscription(tube_number)
                self._track_analytics("lab_results_notification_requested", {
                    "provider": "+".join(subscription.get("providers", [])),
                })
                return self._json(201, {"subscription": subscription})
            except ValueError as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/body-symptoms":
            try:
                return self._json(201, db.add_body_symptom(self._validate_body_symptom(self._read_json())))
            except (ValueError, TypeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/body-symptoms/status":
            try:
                payload = self._read_json()
                symptom_id = int(payload.get("id"))
                item = db.set_body_symptom_status(symptom_id, str(payload.get("status", "")))
                if not item:
                    return self._json(404, {"detail": "Отметка симптома не найдена"})
                return self._json(200, item)
            except (ValueError, TypeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/onboarding/not-medical-exam":
            state = db.save_onboarding(
                status="complete",
                selected_tests=[],
                payment_status="not_medical_exam",
                intro_seen=False,
            )
            self._track_analytics("not_medical_exam_selected")
            self._track_analytics("onboarding_completed", {"result": "not_medical_exam"})
            return self._json(200, public_onboarding(
                state, db.get_profile(), db.list_examinations(),
            ))
        if path == "/api/onboarding/profile":
            try:
                profile = db.save_profile(self._validate_profile(self._read_json(), required=True))
                state = db.save_onboarding(status="exams", selected_tests=[], payment_status="none")
                self._track_analytics("questionnaire_completed")
                return self._json(200, public_onboarding(
                    state, profile, db.list_examinations(),
                ))
            except (ValueError, TypeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/onboarding/appearance":
            try:
                payload = self._read_json()
                font_size = str(payload.get("font_size", "extra"))
                if font_size not in {"standard", "large", "extra"}:
                    raise ValueError("Выберите доступный размер текста")
                current = db.get_onboarding()
                next_status = "questionnaire" if current["status"] == "appearance" else current["status"]
                state = db.save_onboarding(status=next_status, font_size=font_size)
                self._track_analytics("appearance_completed", {"font_size": font_size})
                return self._json(200, public_onboarding(
                    state, db.get_profile(), db.list_examinations(),
                ))
            except (ValueError, TypeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/onboarding/exams":
            try:
                payload = self._read_json()
                allowed = {item["id"] for item in db.list_examinations()}
                selected = normalize_examination_selection(payload.get("selected_tests", []))
                if any(item not in allowed for item in selected):
                    raise ValueError("Выбран неизвестный набор обследований")
                state = db.save_onboarding(
                    status="payment" if selected else "complete",
                    selected_tests=selected,
                    payment_status="pending" if selected else "skipped",
                )
                priced_onboarding = public_onboarding(
                    state, db.get_profile(), db.list_examinations(),
                )
                examination_items = priced_onboarding["tests"]
                total_price = sum(
                    int(item.get("effective_price", item.get("price", 0)))
                    for item in examination_items if item["id"] in selected
                )
                selection_id = f"selection-{secrets.token_hex(12)}"
                self._track_analytics("examinations_selection_completed", {
                    "selected_count": len(selected), "total_price": total_price,
                    "selection_id": selection_id,
                })
                selected_set = set(selected)
                for item in examination_items:
                    if item["id"] in selected_set:
                        self._track_analytics("examination_selection_confirmed", {
                            "selection_id": selection_id,
                            "exam_id": item["id"],
                            "exam_name": item.get("name") or item["id"],
                        })
                if not selected:
                    self._track_analytics("examinations_skipped")
                    self._track_analytics("onboarding_completed", {"result": "examinations_skipped"})
                return self._json(200, public_onboarding(
                    state, db.get_profile(), db.list_examinations(),
                ))
            except (ValueError, TypeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/onboarding/payment":
            try:
                payload = self._read_json()
                payment_method = str(payload.get("method", "")).strip().lower()
                if payment_method != "at_exam":
                    raise ValueError("Онлайн-оплата временно недоступна")
                state = db.get_onboarding()
                if state["status"] != "payment" or not state["selected_tests"]:
                    raise ValueError("Сначала выберите обследования")
                state = db.save_onboarding(status="complete", payment_status="pay_at_exam")
                self._track_analytics("payment_method_selected", {
                    "method": "at_exam", "selected_count": len(state.get("selected_tests", [])),
                })
                self._track_analytics("onboarding_completed", {"result": "examinations_selected"})
                return self._json(200, public_onboarding(
                    state, db.get_profile(), db.list_examinations(),
                ))
            except (ValueError, TypeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/onboarding/intro-seen":
            state = db.get_onboarding()
            if state["status"] != "complete":
                return self._json(422, {"detail": "Сначала завершите анкету"})
            state = db.save_onboarding(status="complete", intro_seen=True)
            return self._json(200, public_onboarding(
                state, db.get_profile(), db.list_examinations(),
            ))
        if path == "/api/context":
            try:
                payload = self._read_json()
                conversation_id = str(payload.get("conversation_id", ""))
                if not db.get_conversation(conversation_id):
                    return self._json(404, {"detail": "Диалог не найден"})
                from .schemas import normalize_context
                context = normalize_context(payload.get("context"))
                db.update_context(conversation_id, context)
                return self._json(200, {"conversation_id": conversation_id, "context": context})
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._json(400, {"detail": "Некорректный JSON"})
        if path == "/api/human-preference":
            try:
                return self._set_human_preference(self._read_json())
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._json(400, {"detail": "Некорректный JSON"})
        if path.startswith("/api/conversations/") and path.endswith("/read"):
            conversation_id = path.removeprefix("/api/conversations/").removesuffix("/read").strip("/")
            result = db.mark_conversation_read(conversation_id)
            if not result:
                return self._json(404, {"detail": "Диалог не найден"})
            return self._json(200, {**result, "unread_counts": db.conversation_unread_counts()})
        if path != "/api/chat":
            return self._json(404, {"detail": "Маршрут не найден"})
        try:
            chat_started = time.perf_counter()
            payload = self._read_json(max_bytes=17_000_000)
            message = str(payload.get("message", "")).strip()
            attachments = self._validate_attachments(payload.get("attachments", []))
            if (not message and not attachments) or len(message) > 12_000:
                return self._json(422, {"detail": "Сообщение должно содержать от 1 до 12000 символов"})
            if not message:
                message = "Проанализируй прикреплённый файл и объясни, что в нём важно."
            conversation_id = str(payload.get("conversation_id") or "")
            conversation = db.get_conversation(conversation_id) if conversation_id else None
            is_first_message = not conversation or not db.list_messages(conversation_id, 1)
            self._track_analytics("message_sent")
            if is_first_message:
                self._track_analytics("first_message_sent")
            if conversation and not bool(conversation.get("ai_enabled", 1)):
                user_message, updated = db.add_user_message_waiting_for_manager(
                    conversation_id, message, attachments,
                )
                db.enqueue_manager_notifications(
                    "new_message", conversation_id,
                    message_id=user_message["id"], message_text=message,
                )
                return self._json(202, {
                    "conversation_id": conversation_id,
                    "user_message": user_message,
                    "assistant_message": None,
                    "agent": "manager",
                    "action": "waiting_human",
                    "queued_for_human": True,
                    "ai_enabled": False,
                    "human_status": updated.get("human_status", "connected"),
                    "human_ticket_id": updated.get("human_ticket_id"),
                    "context": json.loads(updated.get("context_summary") or "{}"),
                    "attachments": [
                        {"name": item.get("name"), "type": item.get("type")}
                        for item in attachments
                    ],
                })
            result = orchestrator.process(payload.get("conversation_id"), message, attachments)
            response = result.to_dict()
            saved = db.get_conversation(result.conversation_id) or {}
            response["ai_enabled"] = bool(saved.get("ai_enabled", 1))
            response["human_status"] = saved.get("human_status", "none")
            self._track_analytics("ai_response_completed", {
                "agent": response.get("agent", ""),
                "route_action": response.get("action", ""),
                "response_ms": int((time.perf_counter() - chat_started) * 1000),
            })
            if response.get("action") == "human":
                self._track_analytics("human_offer_shown")
            return self._json(200, response)
        except LLMNotConfigured as exc:
            return self._json(503, {"detail": str(exc)})
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(400, {"detail": "Некорректный JSON"})
        except ValueError as exc:
            return self._json(422, {"detail": str(exc)})
        except Exception as exc:
            return self._json(502, {"detail": f"Ошибка AI-провайдера: {exc}"})

    def _create_max_auth_link(self) -> None:
        configured_secret = settings.bot_integration_secret
        authorization = self.headers.get("Authorization", "")
        supplied_secret = authorization.removeprefix("Bearer ").strip()
        if not configured_secret:
            return self._json(503, {"detail": "Интеграция с MAX не настроена"})
        if not supplied_secret or not hmac.compare_digest(supplied_secret, configured_secret):
            return self._json(401, {"detail": "Неверные данные интеграции"})
        try:
            payload = self._read_json()
            max_user_id = int(payload.get("max_user_id"))
            legacy_chel_id = int(payload.get("chel_id"))
            login = db.create_max_login(max_user_id, legacy_chel_id)
            return self._json(201, {
                "auth_url": f"{settings.public_base_url}/auth/max?t={login['token']}",
                "expires_at": login["expires_at"],
            })
        except (ValueError, TypeError) as exc:
            return self._json(422, {"detail": str(exc)})
        except PermissionError as exc:
            return self._json(403, {"detail": str(exc)})

    def _create_messenger_auth_link(self) -> None:
        configured_secret = settings.bot_integration_secret
        authorization = self.headers.get("Authorization", "")
        supplied_secret = authorization.removeprefix("Bearer ").strip()
        if not configured_secret:
            return self._json(503, {"detail": "Интеграция с мессенджерами не настроена"})
        if not supplied_secret or not hmac.compare_digest(supplied_secret, configured_secret):
            return self._json(401, {"detail": "Неверные данные интеграции"})
        try:
            payload = self._read_json()
            login = db.create_messenger_login(
                provider=str(payload.get("provider", "")),
                provider_user_id=payload.get("provider_user_id", ""),
                intent_token=str(payload.get("intent_token", "")),
                legacy_chel_id=payload.get("legacy_chel_id"),
                from_manager=str(payload.get("from_manager", "")),
            )
            return self._json(201, {
                "auth_url": f"{settings.public_base_url}/auth/messenger?t={login['token']}",
                "expires_at": login["expires_at"],
                "chel_id": login["chel_id"],
            })
        except (ValueError, TypeError) as exc:
            return self._json(422, {"detail": str(exc)})
        except PermissionError as exc:
            return self._json(403, {"detail": str(exc)})

    def _start_messenger_auth(self) -> None:
        try:
            payload = self._read_json()
            provider = str(payload.get("provider", "")).strip().lower()
            templates = {
                "telegram": settings.telegram_bot_auth_url,
                "max": settings.max_bot_auth_url,
            }
            if provider not in templates:
                raise ValueError("Выберите Telegram или MAX")
            template = templates[provider]
            if not template:
                return self._json(503, {
                    "detail": f"Бот {provider.upper() if provider == 'max' else 'Telegram'} пока не подключён",
                    "provider": provider,
                    "configured": False,
                })
            if "{token}" not in template or urlparse(template).scheme not in {"http", "https"}:
                return self._json(503, {
                    "detail": f"Ссылка на бота {provider} настроена некорректно",
                    "provider": provider,
                    "configured": False,
                })
            db.mark_current_user_registered(provider)
            intent = db.create_auth_intent(provider)
            bot_url = template.replace("{token}", quote(intent["token"], safe=""))
            return self._json(201, {
                "provider": provider,
                "bot_url": bot_url,
                "expires_at": intent["expires_at"],
                "configured": True,
            })
        except (ValueError, TypeError) as exc:
            return self._json(422, {"detail": str(exc)})

    def _consume_messenger_login(self, token: str) -> None:
        login = db.consume_login_token(token)
        if not login:
            owner_chel_id = db.get_consumed_login_owner(token)
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except CookieError:
                cookie = SimpleCookie()
            session_cookie = cookie.get(settings.session_cookie_name)
            session_chel_id = db.get_session_chel_id(
                session_cookie.value if session_cookie else ""
            )
            if owner_chel_id and session_chel_id == owner_chel_id:
                db.set_current_chel_id(owner_chel_id)
                self.send_response(303)
                self._send_security_headers()
                self.send_header("Location", "/?auth=messenger_login")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if owner_chel_id:
                self.send_response(303)
                self._send_security_headers()
                self.send_header("Location", "/?auth=messenger_required")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            return self._json(400, {
                "detail": "Ссылка недействительна. Вернитесь в мессенджер и получите новую.",
            })
        db.set_current_chel_id(login["chel_id"])
        identity = db.current_external_identity()
        db.mark_current_user_registered(identity["provider"] if identity else "max")
        self._track_analytics("registration_completed", {
            "method": identity["provider"] if identity else "max",
        })
        self.send_response(303)
        self._send_security_headers()
        self.send_header("Location", "/?auth=messenger_login")
        self.send_header("Cache-Control", "no-store")
        self._send_session_cookie(login["session"])
        self.send_header(
            "Set-Cookie",
            "chel_id=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
            + (
                "; Secure"
                if settings.cookie_secure
                or self.headers.get("X-Forwarded-Proto", "").lower() == "https"
                else ""
            ),
        )
        self.end_headers()

    def _set_human_preference(self, payload: dict) -> None:
        conversation_id = str(payload.get("conversation_id", "")).strip()
        channel = str(payload.get("channel", "")).strip()
        if channel != "chat":
            return self._json(422, {"detail": "Доступен чат с медицинским специалистом"})
        conversation = db.get_conversation(conversation_id)
        if not conversation:
            return self._json(404, {"detail": "Диалог не найден"})
        missing_profile = ConsiliumHandler._interpretation_profile_missing(db.get_profile())
        if missing_profile:
            return self._json(422, {
                "code": "consultation_profile_required",
                "detail": "Перед консультацией заполните пол, возраст, рост и вес",
                "missing_fields": missing_profile,
            })

        confirmed, created = db.confirm_human_chat(
            conversation_id, f"H-{secrets.token_hex(3).upper()}"
        )
        if not confirmed:
            return self._json(404, {"detail": "Диалог не найден"})
        ticket_id = confirmed["human_ticket_id"]
        already_requested = not created
        self._track_analytics("human_channel_selected", {"channel": "chat"})
        if not already_requested:
            self._track_analytics("human_requested")

        text = (
            "Обращение передано медицинскому специалисту. ИИ в этом диалоге приостановлен. "
            "Пишите сюда — специалист увидит сообщения и ответит в этой переписке. "
            "Пока ожидаете ответа, для общения с ИИ можно открыть новый диалог."
        )
        message = db.add_message(
            conversation_id, "assistant", text, "manager",
            {
                "action": "human_preference",
                "human_channel": "chat",
                "human_ticket_id": ticket_id,
            },
        )
        if not already_requested:
            db.enqueue_manager_notifications(
                "new_request", conversation_id, message_id=message["id"],
            )
        return self._json(200, {
            "conversation_id": conversation_id,
            "ticket_id": ticket_id,
            "channel": "chat",
            "human_status": "pending",
            "ai_enabled": False,
            "assistant_message": message,
        })

    @staticmethod
    def _normalize_russian_phone(value) -> str:
        digits = re.sub(r"\D", "", str(value))
        if len(digits) == 10:
            digits = "7" + digits
        elif len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if not re.fullmatch(r"7[3489]\d{9}", digits):
            raise ValueError("Введите российский номер в формате +7 999 123-45-67")
        return f"+{digits}"

    @staticmethod
    def _validate_attachments(items) -> list[dict]:
        if not isinstance(items, list) or len(items) > 3:
            raise ValueError("Можно прикрепить не более трёх файлов")
        allowed = {"application/pdf", "text/plain", "text/csv", "image/jpeg", "image/png", "image/webp", "image/gif"}
        result = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Некорректное вложение")
            mime = str(item.get("type", ""))
            data_url = str(item.get("data_url", ""))
            if mime not in allowed or not data_url.startswith(f"data:{mime}"):
                raise ValueError("Поддерживаются изображения, PDF, TXT и CSV")
            if len(data_url) > 5_500_000:
                raise ValueError("Размер одного файла не должен превышать 4 МБ")
            result.append({
                "name": str(item.get("name", "file"))[:160], "type": mime,
                "data_url": data_url, "text": str(item.get("text", ""))[:12000],
            })
        return result

    @staticmethod
    def _interpretation_profile_missing(profile: dict) -> list[str]:
        required = ("sex", "age", "height_cm", "weight_kg")
        return [name for name in required if profile.get(name) in (None, "")]

    @staticmethod
    def _validate_profile(payload: dict, required: bool = False) -> dict:
        def optional_number(name: str, minimum: float, maximum: float):
            value = payload.get(name)
            if value in (None, ""):
                return None
            number = float(value)
            if not minimum <= number <= maximum:
                raise ValueError(f"Поле {name} должно быть от {minimum:g} до {maximum:g}")
            return number

        age_value = optional_number("age", 18, 99)
        if age_value is not None and not age_value.is_integer():
            raise ValueError("Возраст должен быть указан целым числом")
        age = int(age_value) if age_value is not None else None
        sex = str(payload.get("sex", ""))
        pregnancy = str(payload.get("pregnancy", "not_applicable"))
        smoking = str(payload.get("smoking", "unknown"))
        alcohol = str(payload.get("alcohol", "unknown"))
        activity = str(payload.get("activity", "unknown"))
        blood_pressure = str(payload.get("blood_pressure", "unknown"))
        blood_sugar = str(payload.get("blood_sugar", "unknown"))
        dark_in_eyes = str(payload.get("dark_in_eyes", "unknown"))
        joint_pain = str(payload.get("joint_pain", "unknown"))
        fatigue = str(payload.get("fatigue", "unknown"))
        if sex not in {"", "female", "male"}:
            raise ValueError("Некорректное значение пола")
        if pregnancy not in {"yes", "no", "possible", "unknown", "not_applicable"}:
            raise ValueError("Некорректное значение беременности")
        if smoking not in {"never", "former", "current", "unknown"}:
            raise ValueError("Некорректный статус курения")
        choices = {
            "alcohol": (alcohol, {"never", "rarely", "weekly", "often", "unknown"}),
            "activity": (activity, {"low", "moderate", "high", "unknown"}),
            "blood_pressure": (blood_pressure, {"normal", "high", "low", "unstable", "unknown"}),
            "blood_sugar": (blood_sugar, {"normal", "high", "unknown"}),
            "dark_in_eyes": (dark_in_eyes, {"yes", "no", "unknown"}),
            "joint_pain": (joint_pain, {"yes", "no", "unknown"}),
            "fatigue": (fatigue, {"yes", "no", "unknown"}),
        }
        for name, (value, allowed) in choices.items():
            if value not in allowed:
                raise ValueError(f"Некорректное значение поля {name}")

        def lines(name: str) -> list[str]:
            value = payload.get(name, [])
            if isinstance(value, str):
                value = value.splitlines()
            if not isinstance(value, list):
                raise ValueError(f"Поле {name} должно быть списком")
            return [" ".join(str(item).split())[:200] for item in value if str(item).strip()][:20]

        company_inn = "".join(str(payload.get("company_inn", "")).split())
        if company_inn and company_inn != db.TEST_COMPANY_INN and (
            not company_inn.isdigit() or len(company_inn) not in {10, 12}
        ):
            raise ValueError("ИНН должен состоять из 10 или 12 цифр")

        result = {
            "preferred_name": " ".join(str(payload.get("preferred_name", "")).split())[:100],
            "company_inn": company_inn, "age": age, "sex": sex,
            "height_cm": optional_number("height_cm", 50, 250),
            "weight_kg": optional_number("weight_kg", 40, 250),
            "pregnancy": pregnancy, "smoking": smoking,
            "alcohol": alcohol, "activity": activity, "blood_pressure": blood_pressure,
            "blood_sugar": blood_sugar, "dark_in_eyes": dark_in_eyes,
            "joint_pain": joint_pain, "fatigue": fatigue,
            "conditions": lines("conditions"), "medications": lines("medications"),
            "allergies": lines("allergies"),
            "tube_number": " ".join(str(payload.get("tube_number", "")).split())[:80],
            "notes": str(payload.get("notes", "")).strip()[:1000],
        }
        if required:
            missing = [name for name in ("company_inn", "age", "sex", "height_cm", "weight_kg") if result[name] in (None, "")]
            if missing:
                raise ValueError("Заполните обязательные поля: ИНН предприятия, возраст, пол, рост и вес")
            if smoking == "unknown" or alcohol == "unknown" or activity == "unknown":
                raise ValueError("Ответьте на вопросы о курении, алкоголе и активности")
        return result

    def _read_json(self, max_bytes: int = 32_000) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > max_bytes:
            raise ValueError("Слишком большой запрос")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _track_analytics(self, event_name: str, properties: dict | None = None) -> None:
        analytics.record_server_event(
            db.current_chel_id(), event_name, properties,
            user_agent=self.headers.get("User-Agent", ""),
            session_id=self.headers.get("X-Analytics-Session", ""),
        )

    def _admin_authorized(self) -> bool:
        if not settings.admin_dashboard_token:
            self._json(503, {
                "detail": "Панель отключена: задайте ADMIN_DASHBOARD_TOKEN",
            })
            return False
        if not admin_token_valid(self.headers.get("Authorization", "")):
            self._json(401, {"detail": "Неверный токен администратора"})
            return False
        return True

    def _manager_cookie(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        value = cookie.get(MANAGER_SESSION_COOKIE)
        return value.value if value else ""

    def _manager_authorized(self) -> dict | None:
        manager = db.get_staff_session(self._manager_cookie())
        if not manager:
            self._json(401, {"detail": "Войдите под учётной записью менеджера"})
            return None
        if self.command != "GET" and self.headers.get("X-Consilium-Manager") != "1":
            self._json(403, {"detail": "Запрос менеджера не подтверждён"})
            return None
        return manager

    @staticmethod
    def _validate_body_symptom(payload: dict) -> dict:
        regions = {
            "Голова", "Шея", "Грудь", "Живот", "Таз",
            "Левое плечо", "Правое плечо", "Левая рука", "Правая рука",
            "Левая кисть", "Правая кисть", "Левое бедро", "Правое бедро",
            "Левая нога", "Правая нога", "Левая стопа", "Правая стопа",
            "Верх спины", "Поясница",
        }
        symptom_types = {
            "Боль", "Жжение", "Онемение", "Покалывание", "Зуд",
            "Отёк", "Слабость", "Сыпь", "Другое",
        }
        patterns = {"constant", "episodes", "movement", "touch", "unknown"}
        durations = {"", "minutes", "hours", "days", "weeks", "months"}
        region = " ".join(str(payload.get("region", "")).split())
        symptom_type = " ".join(str(payload.get("symptom_type", "")).split())
        if region not in regions:
            raise ValueError("Выберите область на карте тела")
        if symptom_type not in symptom_types:
            raise ValueError("Выберите характер симптома")
        if symptom_type == "Другое":
            custom_symptom = " ".join(str(payload.get("custom_symptom", "")).split())[:100]
            if not custom_symptom:
                raise ValueError("Опишите симптом своими словами")
            symptom_type = custom_symptom
        intensity = int(payload.get("intensity", 0))
        if intensity < 1 or intensity > 10:
            raise ValueError("Интенсивность должна быть от 1 до 10")
        view = str(payload.get("view", "front"))
        pattern = str(payload.get("pattern", "constant"))
        duration = str(payload.get("duration", ""))
        if view not in {"front", "back"} or pattern not in patterns or duration not in durations:
            raise ValueError("Некорректные параметры симптома")
        started_at = str(payload.get("started_at", "")).strip()[:40]
        notes = str(payload.get("notes", "")).strip()[:1000]
        return {
            "region": region, "view": view, "symptom_type": symptom_type,
            "intensity": intensity, "started_at": started_at,
            "duration": duration, "pattern": pattern, "notes": notes,
        }

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/admin/managers/"):
            if not self._admin_authorized():
                return
            try:
                staff_id = int(path.removeprefix("/api/admin/managers/").strip("/"))
            except ValueError:
                return self._json(400, {"detail": "Некорректный идентификатор менеджера"})
            if db.admin_delete_staff(staff_id):
                return self._json(200, {"deleted": staff_id})
            return self._json(404, {"detail": "Менеджер не найден"})
        if path.startswith("/api/admin/examinations/"):
            if not self._admin_authorized():
                return
            examination_id = path.removeprefix("/api/admin/examinations/").strip("/")
            if db.admin_delete_examination(examination_id):
                return self._json(200, {"deleted": examination_id})
            return self._json(404, {"detail": "Обследование не найдено"})
        self._ensure_user_context()
        if path.startswith("/api/purchases/"):
            order_id = path.removeprefix("/api/purchases/").strip("/")
            try:
                hidden = db.hide_payment_order(order_id)
                self._track_analytics("purchase_attempt_removed", {
                    "provider": "yookassa", "status": hidden.get("status", ""),
                })
                return self._json(200, {"deleted": order_id})
            except ValueError as exc:
                message = str(exc)
                status = 404 if message == "Заказ не найден" else 409
                return self._json(status, {"detail": message})
        if path.startswith("/api/memories/"):
            try:
                memory_id = int(path.removeprefix("/api/memories/"))
            except ValueError:
                return self._json(400, {"detail": "Некорректный идентификатор"})
            if db.delete_memory(memory_id):
                return self._json(200, {"deleted": memory_id})
            return self._json(404, {"detail": "Запись памяти не найдена"})
        if path.startswith("/api/body-symptoms/"):
            try:
                symptom_id = int(path.removeprefix("/api/body-symptoms/"))
            except ValueError:
                return self._json(400, {"detail": "Некорректный идентификатор"})
            if db.delete_body_symptom(symptom_id):
                return self._json(200, {"deleted": symptom_id})
            return self._json(404, {"detail": "Отметка симптома не найдена"})
        return self._json(404, {"detail": "Маршрут не найден"})

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_identity_cookie()
        self._send_cleared_user_session_cookie()
        self._send_manager_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _send_file(
        self, path: Path, content_type: str, *, allow_metrika_frame: bool = False,
        cache_control: str = "no-store",
    ) -> None:
        if not path.exists():
            return self._json(404, {"detail": "Файл не найден"})
        body = path.read_bytes()
        self.send_response(200)
        self._send_security_headers(allow_metrika_frame=allow_metrika_frame)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self._send_identity_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _ensure_user_context(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        from_manager = (
            query.get("from_manager", [""])[0]
            or query.get("splitter_source", [""])[0]
        )
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            cookie = SimpleCookie()
        session_cookie = cookie.get(settings.session_cookie_name)
        session_chel_id = db.get_session_chel_id(session_cookie.value if session_cookie else "")
        if session_chel_id:
            self._identity_cookie_required = False
            self._authenticated_session = True
            db.set_current_chel_id(session_chel_id)
            return
        self._authenticated_session = False
        candidate = cookie.get("chel_id").value if cookie.get("chel_id") else ""
        if not re.fullmatch(r"chel_[A-Za-z0-9_-]{8,64}", candidate) or not db.user_exists(candidate):
            candidate = f"chel_{secrets.token_hex(16)}"
            self._identity_cookie_required = True
        else:
            self._identity_cookie_required = False
        db.ensure_user(candidate, pending=True, from_manager=from_manager)
        db.set_current_chel_id(candidate)

    def _send_session_cookie(self, session_value: str) -> None:
        parts = [
            f"{settings.session_cookie_name}={session_value}",
            "Path=/",
            f"Max-Age={settings.session_ttl_days * 86400}",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if settings.cookie_secure or self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _send_identity_cookie(self) -> None:
        if not getattr(self, "_identity_cookie_required", False):
            return
        parts = [
            f"chel_id={db.current_chel_id()}",
            "Path=/",
            "Max-Age=31536000",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if settings.cookie_secure or self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _send_cleared_user_session_cookie(self) -> None:
        if not getattr(self, "_clear_user_session", False):
            return
        parts = [
            f"{settings.session_cookie_name}=",
            "Path=/",
            "Max-Age=0",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if settings.cookie_secure or self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _send_manager_cookie(self) -> None:
        token = getattr(self, "_manager_session_to_set", "")
        clear = getattr(self, "_clear_manager_session", False)
        if not token and not clear:
            return
        parts = [
            f"{MANAGER_SESSION_COOKIE}={token}",
            "Path=/",
            f"Max-Age={0 if clear else 43200}",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if settings.cookie_secure or self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _send_security_headers(self, *, allow_metrika_frame: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        if not allow_metrika_frame:
            self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        metrika_sources = (
            " https://mc.yandex.ru https://mc.yandex.com https://mc.webvisor.com "
            "https://mc.webvisor.org https://yastatic.net wss://mc.yandex.ru "
            "wss://mc.yandex.com wss://mc.webvisor.com wss://mc.webvisor.org"
            if settings.yandex_metrika_counter_id.isdigit() else ""
        )
        metrika_frame_ancestors = (
            "'self' https://metrika.yandex.ru https://metrika.yandex.by "
            "https://metrika.yandex.com https://metrika.yandex.com.tr "
            "https://metrika.yandex.kz https://metrika.yandex.uz "
            "https://metrica.yandex https://metrica.yandex.ru "
            "https://metrica.yandex.by https://metrica.yandex.com "
            "https://metrica.yandex.com.tr https://metrica.yandex.kz "
            "https://analytics.yandex.ru https://analytics.yandex.by "
            "https://analytics.yandex.com https://analytics.yandex.com.tr "
            "https://analytics.yandex.kz https://metr.yandex.ru "
            "https://metr.yandex.by https://metr.yandex.com "
            "https://metr.yandex.com.tr https://metr.yandex.kz "
            "https://metrika.ya.ru https://metrica.ya.ru "
            "https://webvisor.com https://*.webvisor.com"
            if allow_metrika_frame and settings.yandex_metrika_counter_id.isdigit()
            else "'none'"
        )
        metrika_frames = (
            " child-src 'self' blob: https://mc.yandex.ru https://mc.yandex.com "
            "https://mc.webvisor.com https://mc.webvisor.org; "
            "frame-src blob: https://mc.yandex.ru https://mc.yandex.com "
            "https://mc.webvisor.com https://mc.webvisor.org;"
            if settings.yandex_metrika_counter_id.isdigit() else ""
        )
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'self'; script-src 'self'{metrika_sources}; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            f"img-src 'self' data: blob:{metrika_sources}; connect-src 'self'{metrika_sources};"
            f"{metrika_frames} worker-src 'self' blob:; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; "
            f"frame-ancestors {metrika_frame_ancestors}",
        )

    def log_message(self, format: str, *args) -> None:
        message = format % args
        message = re.sub(r"(/auth/(?:max|messenger)\?t=)[^ ]+", r"\1[REDACTED]", message)
        print(f"{self.address_string()} — {message}")


def serve() -> None:
    _record_startup_event("Начало запуска")
    db.init_db()
    analytics.init_db()
    analytics.cleanup_old_events()
    _record_startup_event("База данных готова")
    schedule_stop = examination_schedule.start_background_sync(_record_startup_event)
    server = ConsiliumHTTPServer((settings.host, settings.port), ConsiliumHandler)
    _record_startup_event(f"Порт {settings.port} открыт")
    browser_host = "127.0.0.1" if settings.host in {"0.0.0.0", "::", ""} else settings.host
    browser_url = f"http://{browser_host}:{settings.port}"
    print(f"Консилиум запущен: {browser_url}")
    if settings.host in {"0.0.0.0", "::", ""}:
        print("Для телефона используйте IP-адрес этого компьютера в той же Wi-Fi сети.")
    if settings.auto_open_browser:
        opener = threading.Timer(0.6, webbrowser.open, args=(browser_url,))
        opener.daemon = True
        opener.start()
    print("Нажмите Ctrl+C для остановки")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        schedule_stop.set()
        _record_startup_event("Остановка сервера")
        server.server_close()
