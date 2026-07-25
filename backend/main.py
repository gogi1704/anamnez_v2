import json
import hmac
import mimetypes
import re
import secrets
import threading
import traceback
import webbrowser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import database as db
from .config import BASE_DIR, settings
from .llm import LLMNotConfigured
from .orchestrator import orchestrator
from .onboarding import TEST_CATALOG, public_onboarding
from .prompts import public_agents


STATIC_DIR = BASE_DIR / "static"
ALLOWED_STATIC = {"app.js", "agents.js", "styles.css"}
SERVER_ERROR_LOG = settings.log_path


def _record_startup_event(message: str) -> None:
    line = f"[{db.utc_now()}] {message}\n"
    try:
        with SERVER_ERROR_LOG.open("a", encoding="utf-8") as log:
            log.write(line)
    except OSError:
        pass
    print(message, flush=True)


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
        if path == "/auth/max":
            return self._consume_max_login(parse_qs(parsed.query).get("t", [""])[0])
        self._ensure_user_context()
        if path == "/":
            return self._send_file(BASE_DIR / "index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            name = path.removeprefix("/static/")
            if name not in ALLOWED_STATIC:
                return self._json(404, {"detail": "Файл не найден"})
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return self._send_file(STATIC_DIR / name, f"{mime}; charset=utf-8")
        if path == "/api/health":
            return self._json(200, {"status": "ok"})
        if path == "/api/ready":
            database_ready = False
            try:
                with db.connection() as conn:
                    database_ready = conn.execute("SELECT 1").fetchone()[0] == 1
            except Exception:
                database_ready = False
            max_auth_ready = bool(settings.bot_integration_secret) and (
                settings.app_env != "production" or settings.public_base_url.startswith("https://")
            )
            ready = database_ready and bool(settings.openai_api_key) and max_auth_ready
            return self._json(200 if ready else 503, {
                "status": "ready" if ready else "not_ready",
                "database": "ok" if database_ready else "error",
                "ai_configured": bool(settings.openai_api_key),
                "max_auth_configured": max_auth_ready,
            })
        if path == "/api/agents":
            return self._json(200, public_agents())
        if path == "/api/me":
            identity = db.current_external_identity()
            return self._json(200, {
                "chel_id": db.current_chel_id(),
                "authenticated": bool(identity),
                "provider": identity["provider"] if identity else None,
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
            return self._json(200, public_onboarding(db.get_onboarding(), db.get_profile()))
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
        if path.startswith("/api/conversations/"):
            conversation_id = path.removeprefix("/api/conversations/")
            item = db.get_conversation(conversation_id)
            if not item:
                return self._json(404, {"detail": "Диалог не найден"})
            return self._json(200, {**item, "messages": db.list_messages(conversation_id), "handoffs": db.list_handoffs(conversation_id)})
        return self._json(404, {"detail": "Маршрут не найден"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/auth/max/link":
            return self._create_max_auth_link()
        self._ensure_user_context()
        if path == "/api/conversations":
            return self._json(201, db.create_conversation())
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
            db.ensure_user(new_chel_id)
            db.set_current_chel_id(new_chel_id)
            self._identity_cookie_required = True
            return self._json(200, {
                "status": "reset", "chel_id": new_chel_id, "identity_preserved": False,
            })
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
        if path == "/api/onboarding/profile":
            try:
                profile = db.save_profile(self._validate_profile(self._read_json(), required=True))
                state = db.save_onboarding(status="exams", selected_tests=[], payment_status="none")
                return self._json(200, public_onboarding(state, profile))
            except (ValueError, TypeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/onboarding/appearance":
            try:
                payload = self._read_json()
                font_size = str(payload.get("font_size", "standard"))
                if font_size not in {"standard", "large", "extra"}:
                    raise ValueError("Выберите доступный размер текста")
                current = db.get_onboarding()
                next_status = "questionnaire" if current["status"] == "appearance" else current["status"]
                state = db.save_onboarding(status=next_status, font_size=font_size)
                return self._json(200, public_onboarding(state, db.get_profile()))
            except (ValueError, TypeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/onboarding/exams":
            try:
                payload = self._read_json()
                allowed = {item["id"] for item in TEST_CATALOG}
                selected = list(dict.fromkeys(str(item) for item in payload.get("selected_tests", [])))
                if any(item not in allowed for item in selected):
                    raise ValueError("Выбран неизвестный набор обследований")
                state = db.save_onboarding(
                    status="payment" if selected else "complete",
                    selected_tests=selected,
                    payment_status="pending" if selected else "skipped",
                )
                return self._json(200, public_onboarding(state, db.get_profile()))
            except (ValueError, TypeError) as exc:
                return self._json(422, {"detail": str(exc)})
        if path == "/api/onboarding/payment":
            state = db.get_onboarding()
            if state["status"] != "payment" or not state["selected_tests"]:
                return self._json(422, {"detail": "Сначала выберите обследования"})
            state = db.save_onboarding(status="complete", payment_status="demo_paid")
            return self._json(200, public_onboarding(state, db.get_profile()))
        if path == "/api/onboarding/intro-seen":
            state = db.get_onboarding()
            if state["status"] != "complete":
                return self._json(422, {"detail": "Сначала завершите анкету"})
            state = db.save_onboarding(status="complete", intro_seen=True)
            return self._json(200, public_onboarding(state, db.get_profile()))
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
        if path in {"/api/second-opinion", "/api/council"}:
            try:
                payload = self._read_json()
                conversation_id = str(payload.get("conversation_id", ""))
                result = orchestrator.second_opinion(conversation_id) if path.endswith("second-opinion") else orchestrator.council(conversation_id)
                return self._json(200, result)
            except (ValueError, LLMNotConfigured) as exc:
                return self._json(422 if isinstance(exc, ValueError) else 503, {"detail": str(exc)})
            except Exception as exc:
                return self._json(502, {"detail": f"Ошибка AI-провайдера: {exc}"})
        if path == "/api/human-preference":
            try:
                return self._set_human_preference(self._read_json())
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._json(400, {"detail": "Некорректный JSON"})
        if path != "/api/chat":
            return self._json(404, {"detail": "Маршрут не найден"})
        if db.get_onboarding()["status"] != "complete":
            return self._json(403, {"detail": "Сначала завершите короткую анкету"})
        try:
            payload = self._read_json(max_bytes=17_000_000)
            message = str(payload.get("message", "")).strip()
            attachments = self._validate_attachments(payload.get("attachments", []))
            if (not message and not attachments) or len(message) > 12_000:
                return self._json(422, {"detail": "Сообщение должно содержать от 1 до 12000 символов"})
            if not message:
                message = "Проанализируй прикреплённый файл и объясни, что в нём важно."
            result = orchestrator.process(payload.get("conversation_id"), message, attachments)
            return self._json(200, result.to_dict())
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

    def _consume_max_login(self, token: str) -> None:
        login = db.consume_login_token(token)
        if not login:
            return self._json(400, {
                "detail": "Ссылка недействительна или уже использована. Вернитесь в MAX и получите новую.",
            })
        db.set_current_chel_id(login["chel_id"])
        self.send_response(303)
        self._send_security_headers()
        self.send_header("Location", "/")
        self.send_header("Cache-Control", "no-store")
        self._send_session_cookie(login["session"])
        self.send_header(
            "Set-Cookie",
            "chel_id=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
            + ("; Secure" if settings.cookie_secure else ""),
        )
        self.end_headers()

    def _set_human_preference(self, payload: dict) -> None:
        conversation_id = str(payload.get("conversation_id", "")).strip()
        channel = str(payload.get("channel", "")).strip()
        if channel not in {"chat", "call"}:
            return self._json(422, {"detail": "Выберите чат или созвон"})
        conversation = db.get_conversation(conversation_id)
        if not conversation:
            return self._json(404, {"detail": "Диалог не найден"})
        if conversation.get("human_status") != "pending" or not conversation.get("human_ticket_id"):
            return self._json(409, {"detail": "Сначала запросите подключение человека"})

        phone = None
        if channel == "call":
            try:
                phone = self._normalize_russian_phone(payload.get("phone", ""))
            except ValueError as exc:
                return self._json(422, {"detail": str(exc)})
        db.set_human_channel(conversation_id, channel, phone)
        if channel == "chat":
            text = (
                "Хорошо, оставляю обращение в формате чата. Специалист получит историю диалога и сможет "
                "продолжить с этого места после подключения. Пока можете писать сюда — ИИ останется на связи."
            )
        else:
            masked_phone = f"+7 ••• •••-{phone[-4:-2]}-{phone[-2:]}"
            text = (
                f"Хорошо, запрос на созвон принят. Контактный номер {masked_phone} сохранён и будет передан специалисту "
                "вместе со сводкой. Сейчас это демонстрационный сценарий: звонок автоматически не создаётся. "
                "Пока можете продолжить диалог с ИИ."
            )
        message = db.add_message(
            conversation_id, "assistant", text, "manager",
            {"action": "human_preference", "human_channel": channel, "human_ticket_id": conversation["human_ticket_id"]},
        )
        return self._json(200, {
            "conversation_id": conversation_id,
            "ticket_id": conversation["human_ticket_id"],
            "channel": channel,
            "human_status": "pending",
            "masked_phone": f"+7 ••• •••-{phone[-4:-2]}-{phone[-2:]}" if phone else None,
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
    def _validate_profile(payload: dict, required: bool = False) -> dict:
        def optional_number(name: str, minimum: float, maximum: float):
            value = payload.get(name)
            if value in (None, ""):
                return None
            number = float(value)
            if not minimum <= number <= maximum:
                raise ValueError(f"Поле {name} должно быть от {minimum:g} до {maximum:g}")
            return number

        age_value = optional_number("age", 0, 120)
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
        if sex not in {"", "female", "male", "intersex", "other"}:
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

        result = {
            "preferred_name": " ".join(str(payload.get("preferred_name", "")).split())[:100],
            "age": age, "sex": sex,
            "height_cm": optional_number("height_cm", 30, 250),
            "weight_kg": optional_number("weight_kg", 1, 500),
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
            missing = [name for name in ("age", "sex", "height_cm", "weight_kg") if result[name] in (None, "")]
            if missing:
                raise ValueError("Заполните обязательные поля: возраст, пол, рост и вес")
            if smoking == "unknown" or alcohol == "unknown" or activity == "unknown":
                raise ValueError("Ответьте на вопросы о курении, алкоголе и активности")
        return result

    def _read_json(self, max_bytes: int = 32_000) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > max_bytes:
            raise ValueError("Слишком большой запрос")
        return json.loads(self.rfile.read(length).decode("utf-8"))

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
        self._ensure_user_context()
        path = urlparse(self.path).path
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
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            return self._json(404, {"detail": "Файл не найден"})
        body = path.read_bytes()
        self.send_response(200)
        self._send_security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_identity_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _ensure_user_context(self) -> None:
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
        db.ensure_user(candidate)
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

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args) -> None:
        message = format % args
        message = re.sub(r"(/auth/max\?t=)[^ ]+", r"\1[REDACTED]", message)
        print(f"{self.address_string()} — {message}")


def serve() -> None:
    _record_startup_event("Начало запуска")
    db.init_db()
    _record_startup_event("База данных готова")
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
        _record_startup_event("Остановка сервера")
        server.server_close()
