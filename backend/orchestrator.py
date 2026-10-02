import json
import hashlib
import re
import secrets

from . import database as db
from .config import settings
from .lab_results import LabResultsUnavailable, lab_result_documents, lookup_lab_results
from .llm import LLMNotConfigured, LLMProviderError, LLMService, llm_service
from .schemas import AgentResult, ChatResponse, RouteDecision, normalize_context


HUMAN_REQUEST = re.compile(
    r"\b(?:позов\w*|подключ\w*|перевед\w*|соедин\w*|хочу поговорить с)\s+"
    r"(?:жив\w*\s+)?(?:чел\w*|оператор\w*|врач\w*|специалист\w*)\b",
    re.IGNORECASE,
)
CRITICAL_RISK = re.compile(
    r"(не могу дышать|задыхаюсь|потер\w* сознани|внезапн\w* парализ|"
    r"сильн\w* боль\w* в груди|неостанавливаем\w* кровотечени|"
    r"хочу покончить с собой|планирую суицид|не хочу жить|лучше бы меня не было)",
    re.IGNORECASE,
)
LAB_RESULTS_REQUEST = re.compile(
    r"(?:получ\w*|покаж\w*|пришл\w*|найд\w*|откр\w*|хочу|где)\s+"
    r"(?:мои\s+)?результат\w*(?:\s+(?:анализ\w*|обследован\w*))?|"
    r"результат\w*\s+(?:моих\s+)?(?:анализ\w*|обследован\w*)|"
    r"(?:пришл\w*|отправ\w*|покаж\w*|вывед\w*)\s+(?:мои\s+)?"
    r"(?:анализ\w*|обследован\w*)(?:\s+(?:сюда|в\s+чат))?",
    re.IGNORECASE,
)
LAB_RESULTS_INTERPRETATION = re.compile(
    r"\b(?:расшифр\w*|объясн\w*|интерпрет\w*|прокоммент\w*)\b",
    re.IGNORECASE,
)
PROFILE_ANALYSIS_REQUEST = re.compile(
    r"(?:\b(?:проанализир\w*|разбер\w*|оцен\w*)\b.{0,80}"
    r"\b(?:анкет\w*|профил\w*\s+здоров\w*|медицинск\w*\s+данн\w*)\b|"
    r"\b(?:анкет\w*|профил\w*\s+здоров\w*|медицинск\w*\s+данн\w*)\b.{0,80}"
    r"\b(?:проанализир\w*|разбер\w*|оцен\w*)\b)",
    re.IGNORECASE,
)
FOOD_ENTRY_HINT = re.compile(
    r"\b(?:ел(?:а|и)?|съел(?:а|и)?|поел(?:а|и)?|выпил(?:а|и)?|завтрак\w*|"
    r"обед\w*|ужин\w*|перекус\w*|каша|суп|салат|мяс\w*|рыб\w*|куриц\w*|"
    r"яйц\w*|овощ\w*|фрукт\w*|хлеб\w*|кофе|чай|йогурт\w*|творог\w*)\b",
    re.IGNORECASE,
)
CHECKUP_PURCHASE_OBJECT = re.compile(
    r"\b(?:чек[- ]?ап\w*|обследован\w*|комплекс\w*\s+анализ\w*|набор\w*\s+анализ\w*)\b",
    re.IGNORECASE,
)
CHECKUP_PURCHASE_ACTION = re.compile(
    r"\b(?:купить|покупать|приобрест\w*|заказ\w*|оформ\w*|оплат\w*|беру|возьму)\b",
    re.IGNORECASE,
)
CHECKUP_PURCHASE_NEGATION = re.compile(
    r"\b(?:не\s+хочу|не\s+буду|не\s+готов\w*|пока\s+не|передумал\w*)\b.{0,35}"
    r"\b(?:купить|покупать|приобрест\w*|заказ\w*|оформ\w*|оплат\w*|брать)\b",
    re.IGNORECASE,
)


def _attachment_metadata(attachments: list[dict] | None) -> list[dict]:
    """Keep safe display metadata and a small persisted image preview."""
    result = []
    for item in attachments or []:
        metadata = {"name": item.get("name"), "type": item.get("type")}
        if item.get("thumbnail_url"):
            metadata["thumbnail_url"] = item["thumbnail_url"]
        result.append(metadata)
    return result
OTHER_PERSON_SUBJECT = re.compile(
    r"\b(?:реб[её]н\w*|сын\w*|доч\w*|мам\w*|пап\w*|муж\w*|жен\w*|"
    r"бабуш\w*|дедуш\w*|друг\w*|подруг\w*|пациент\w*)\b",
    re.IGNORECASE,
)
MEDICAL_AGENTS = {
    "therapist", "cardiologist", "neurologist", "dermatologist",
    "pediatrician", "psychologist",
}
QUESTIONS_PER_MESSAGE_LIMIT = 2


def _profile_for_ai() -> dict:
    return {
        key: value for key, value in db.get_profile().items()
        if key not in {"chel_id", "company_inn", "tube_number"}
    }


def _device_for_ai() -> dict:
    device = db.current_device()
    return {
        "device_type": device.get("device_type", "other"),
        "operating_system": device.get("operating_system", "Другое"),
        "browser": device.get("browser", "Другое"),
    }


def _messenger_access_for_ai() -> dict:
    linked = [item["provider"] for item in db.current_external_identities()]
    available = []
    if settings.telegram_bot_auth_url:
        available.append("telegram")
    if settings.max_bot_auth_url:
        available.append("max")
    return {
        "is_anonymous": not bool(linked),
        "linked_providers": linked,
        "available_providers": available,
    }


def _health_passport_for_ai() -> dict:
    return db.health_passport_ai_context()


def _checkup_catalog_for_ai() -> list[dict]:
    """Expose only current catalog fields needed for a grounded recommendation."""
    return [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "description": str(item.get("description") or ""),
            "includes": str(item.get("includes") or ""),
            "price_rub": int(item.get("price") or 0),
        }
        for item in db.list_examinations()
    ]


class ConversationOrchestrator:
    def __init__(self, llm: LLMService = llm_service) -> None:
        self.llm = llm

    @staticmethod
    def _retryable_user_message(
        conversation_id: str, user_text: str, attachment_meta: list[dict], retry: bool,
    ) -> dict | None:
        if not retry:
            return None
        recent = db.list_messages(conversation_id, 1)
        if not recent:
            return None
        message = recent[-1]
        metadata = message.get("metadata") or {}
        if (
            message.get("role") == "user"
            and str(message.get("content") or "") == user_text
            and list(metadata.get("attachments") or []) == attachment_meta
        ):
            return message
        return None

    def process(
        self, conversation_id: str | None, user_text: str,
        attachments: list[dict] | None = None, *, retry: bool = False,
    ) -> ChatResponse:
        conversation = db.get_conversation(conversation_id) if conversation_id else None
        if not conversation:
            conversation = db.create_conversation(self._title(user_text))
        conversation_id = conversation["id"]
        conversation["_memories"] = [{"category": item["category"], "content": item["content"]} for item in db.list_memories()[:20]]
        conversation["_profile"] = _profile_for_ai()
        conversation["_health_passport"] = _health_passport_for_ai()
        conversation["_checkup_catalog"] = _checkup_catalog_for_ai()
        conversation["_device"] = _device_for_ai()
        conversation["_messenger_access"] = _messenger_access_for_ai()
        conversation["_body_symptoms"] = db.list_body_symptoms(status="active", limit=20)
        if (
            conversation.get("dialog_type") == "weight_control"
            and not HUMAN_REQUEST.search(user_text)
            and not CRITICAL_RISK.search(user_text)
            and not self._wants_to_buy_checkup(
                user_text, self._recent_history(conversation_id),
            )
        ):
            return self._process_weight_control(conversation, user_text, attachments, retry=retry)
        previous_agent = conversation["active_agent"]
        attachment_meta = _attachment_metadata(attachments)
        user_message = self._retryable_user_message(
            conversation_id, user_text, attachment_meta, retry,
        ) or db.add_message(
            conversation_id, "user", user_text, metadata={"attachments": attachment_meta},
        )
        history = self._recent_history(conversation_id)
        previous_context = self._load_context(conversation.get("context_summary", ""))
        previous_question_count = self._assessment_question_count(
            history, previous_context.get("current_topic", "")
        )
        conversation["_consultation_progress"] = self._consultation_progress(previous_question_count)

        if self._wants_to_buy_checkup(user_text, history[:-1]):
            return self._process_checkup_purchase(
                conversation, user_message, previous_context, attachment_meta, user_text,
            )

        if self._wants_lab_interpretation(user_text, attachments):
            return self._interpret_lab_results_response(
                conversation, user_message, previous_context, attachment_meta, "all"
            )

        if self._wants_lab_results(user_text, attachments):
            return self._lab_results_response(
                conversation, user_message, previous_context, attachment_meta
            )

        decision = self._normalize_route(
            self._decide(user_text, history, conversation, previous_context, attachments), previous_agent
        )
        context = self._apply_topic_transition(previous_context, decision.context)
        decision.context = context
        previous_topic = " ".join(str(previous_context.get("current_topic", "")).lower().split())
        current_topic = " ".join(str(context.get("current_topic", "")).lower().split())
        genuinely_new_topic = bool(
            context.get("topic_relation") == "new"
            and previous_topic
            and current_topic
            and current_topic != previous_topic
        )
        question_count_before = (
            0 if genuinely_new_topic
            else self._assessment_question_count(history, context.get("current_topic", ""))
        )
        conversation["_consultation_progress"] = self._consultation_progress(question_count_before)
        target = decision.target_agent
        action = decision.action
        handoff_from = None
        handoff_reason = decision.reason
        human_ticket_id = conversation.get("human_ticket_id")
        human_status = conversation.get("human_status", "none")
        human_channel = conversation.get("human_channel")
        emergency = action == "emergency"
        urgency = "emergency" if emergency else "routine"
        missing_information: list[str] = []
        assessment_questions_this_turn = 0

        if target != previous_agent:
            db.add_handoff(conversation_id, previous_agent, target, decision.reason)
            handoff_from = previous_agent

        if action == "human":
            if human_status == "closed":
                human_ticket_id = None
                human_channel = None
            human_ticket_id, answer = self._prepare_human_handoff(
                history, context, decision, conversation, human_status, human_ticket_id, human_channel
            )
            target = "manager"
        else:
            result = self.llm.answer(target, history, context, decision, conversation, attachments)
            answer = result.message
            urgency = result.urgency
            if target in MEDICAL_AGENTS:
                per_turn_limit = QUESTIONS_PER_MESSAGE_LIMIT
                answer = self._limit_questions(answer, per_turn_limit)
                missing_information = result.missing_information[:per_turn_limit]
                if result.next_action == "ask":
                    assessment_questions_this_turn = min(
                        per_turn_limit,
                        max(self._question_count(answer), len(missing_information)),
                    )
            else:
                missing_information = result.missing_information

            # A specialist can perform one bounded handoff in the same user turn.
            if result.next_action == "handoff" and result.target_agent and result.target_agent != target:
                next_target = result.target_agent
                reason = result.handoff_reason or f"{target} запросил профиль {next_target}"
                db.add_handoff(conversation_id, target, next_target, reason)
                handoff_from = target
                handoff_reason = reason
                second_decision = RouteDecision("handoff", next_target, reason, context)
                second_result = self.llm.answer(next_target, history, context, second_decision, conversation, attachments)
                target = next_target
                answer = second_result.message
                action = "handoff"
                urgency = second_result.urgency
                missing_information = second_result.missing_information
                if target in MEDICAL_AGENTS:
                    per_turn_limit = QUESTIONS_PER_MESSAGE_LIMIT
                    answer = self._limit_questions(answer, per_turn_limit)
                    missing_information = second_result.missing_information[:per_turn_limit]
                    if second_result.next_action == "ask":
                        assessment_questions_this_turn = min(
                            per_turn_limit,
                            max(self._question_count(answer), len(missing_information)),
                        )
                if second_result.next_action == "human":
                    human_decision = RouteDecision("human", "manager", second_result.handoff_reason, context)
                    human_ticket_id, answer = self._prepare_human_handoff(
                        history, context, human_decision, conversation, human_status, human_ticket_id, human_channel
                    )
                    db.add_handoff(conversation_id, target, "manager", second_result.handoff_reason or "Агент запросил участие человека")
                    handoff_from = target
                    handoff_reason = second_result.handoff_reason or "Агент запросил участие человека"
                    target = "manager"
                    action = "human"
                elif second_result.next_action == "emergency" and target != "safety":
                    reason = second_result.handoff_reason or "Специалист обнаружил непосредственную угрозу"
                    db.add_handoff(conversation_id, target, "safety", reason)
                    safety_decision = RouteDecision("emergency", "safety", reason, context)
                    safety_result = self.llm.answer("safety", history, context, safety_decision, conversation, attachments)
                    handoff_from = target
                    handoff_reason = reason
                    target = "safety"
                    answer = safety_result.message
                    action = "emergency"
                    emergency = True
                else:
                    emergency = second_result.next_action == "emergency" or second_result.urgency == "emergency"
            elif result.next_action == "emergency" and target != "safety":
                reason = result.handoff_reason or "Профильный агент обнаружил непосредственную угрозу"
                db.add_handoff(conversation_id, target, "safety", reason)
                handoff_from = target
                handoff_reason = reason
                safety_decision = RouteDecision("emergency", "safety", reason, context)
                safety_result = self.llm.answer("safety", history, context, safety_decision, conversation, attachments)
                target = "safety"
                answer = safety_result.message
                action = "emergency"
                emergency = True
            elif result.next_action == "human":
                human_decision = RouteDecision("human", "manager", result.handoff_reason, context)
                human_ticket_id, answer = self._prepare_human_handoff(
                    history, context, human_decision, conversation, human_status, human_ticket_id, human_channel
                )
                handoff_from = target if target != "manager" else handoff_from
                handoff_reason = result.handoff_reason or "Агент запросил участие человека"
                if target != "manager":
                    db.add_handoff(conversation_id, target, "manager", handoff_reason)
                target = "manager"
                action = "human"
            elif result.next_action == "emergency":
                action = "emergency"
                emergency = True

        answer = self._format_human_reminder(answer)
        context_limit_warning = False
        if action not in {"human", "emergency"}:
            context_usage = db.conversation_context_usage(conversation_id)
            warning_message_threshold = max(4, settings.max_history_messages - 4)
            warning_char_threshold = max(1000, int(settings.max_history_chars * 0.85))
            context_limit_warning = bool(
                not context_usage["warning_shown"]
                and (
                    context_usage["message_count"] >= warning_message_threshold
                    or context_usage["content_chars"] >= warning_char_threshold
                )
            )
            if context_limit_warning:
                answer = (
                    f"{answer}\n\n"
                    "💡 **Диалог стал длинным.** Скоро часть ранних сообщений может "
                    f"перестать попадать в контекст: я учитываю до последних "
                    f"{settings.max_history_messages} сообщений. Если хотите обсудить "
                    "новую тему и сохранить её полный контекст, лучше начать новый диалог."
                )
        metadata = {
            "action": action,
            "handoff_from": handoff_from,
            "handoff_reason": handoff_reason,
            "human_escalation": action == "human",
            "emergency": emergency,
            "human_ticket_id": human_ticket_id if action == "human" else None,
            "human_channel": human_channel,
            "urgency": urgency,
            "missing_information": missing_information,
            "assessment_questions_asked": assessment_questions_this_turn,
            "assessment_questions_total": question_count_before + assessment_questions_this_turn,
            "assessment_topic": context.get("current_topic", ""),
            "attachments": attachment_meta,
            "context_limit_warning": context_limit_warning,
        }
        assistant_message = db.add_message(conversation_id, "assistant", answer, target, metadata)
        status = (
            "waiting_human"
            if not bool(conversation.get("ai_enabled", 1))
            or human_status in {"pending", "connected"}
            else "active"
        )
        db.update_conversation(
            conversation_id,
            active_agent=target,
            context_summary=json.dumps(context, ensure_ascii=False),
            status=status,
            human_status=human_status,
            human_ticket_id=human_ticket_id,
            human_channel=human_channel,
            ai_enabled=None,
        )

        return ChatResponse(
            conversation_id=conversation_id,
            user_message=user_message,
            assistant_message=assistant_message,
            agent=target,
            handoff_from=handoff_from,
            handoff_reason=handoff_reason,
            action=action,
            human_escalation=action == "human",
            emergency=emergency,
            human_ticket_id=human_ticket_id if action == "human" else None,
            human_channel=human_channel,
            context=context,
            urgency=urgency,
            missing_information=missing_information,
            attachments=attachment_meta,
            council_available=target in {"therapist", "cardiologist", "neurologist", "dermatologist", "pediatrician", "psychologist"} and not emergency,
        )

    def _process_weight_control(
        self, conversation: dict, user_text: str,
        attachments: list[dict] | None = None,
        *, retry: bool = False,
    ) -> ChatResponse:
        """Advance the dedicated structured weight-control dialogue."""
        conversation_id = str(conversation["id"])
        attachment_meta = _attachment_metadata(attachments)
        user_message = self._retryable_user_message(
            conversation_id, user_text, attachment_meta, retry,
        ) or db.add_message(
            conversation_id, "user", user_text,
            metadata={"attachments": attachment_meta, "specialized_dialog": "weight_control"},
        )
        history = self._recent_history(conversation_id)
        state = db.get_weight_control_state(conversation_id)
        if not state:
            raise ValueError("Анкета контроля веса не найдена")
        previous_stage = str(state.get("stage") or "intake")
        reminder_data = db.list_weight_control_reminders(conversation_id)
        state = {
            **state,
            "messenger_access": dict(conversation.get("_messenger_access") or {}),
            "active_reminders": [
                {
                    "type": item.get("reminder_type"),
                    "title": item.get("title"),
                    "time": item.get("time_local"),
                    "weekdays": item.get("weekdays", []),
                }
                for item in reminder_data.get("reminders", []) if item.get("enabled")
            ],
        }
        try:
            result = self.llm.weight_control_turn(
                history, conversation.get("_profile", {}), state, attachments,
            )
        except (LLMProviderError, LLMNotConfigured):
            image_received = any(
                str(item.get("type") or "").startswith("image/")
                for item in (attachments or [])
            )
            food_text_received = bool(user_text and FOOD_ENTRY_HINT.search(user_text))
            if str(state.get("stage") or "intake") not in {"analysis", "readiness"} or not (
                image_received or food_text_received
            ):
                raise
            fallback_analysis = dict(state.get("analysis") or {})
            fallback_analysis.update({
                "meal_event": "draft" if image_received else "confirmed",
                "meal_draft": (
                    "Фото приёма пищи — состав пока не распознан"
                    if image_received else user_text
                ),
                "awaiting_meal_confirmation": False,
            })
            result = {
                "stage": str(state.get("stage") or "analysis"),
                "risk_level": str(state.get("risk_level") or "routine"),
                "assessment": dict(state.get("assessment") or {}),
                "analysis": fallback_analysis,
                "missing_fields": [],
                "reminder_offer": "none",
                "message": (
                    "Фото сохранено в дневнике питания. Сейчас не удалось распознать "
                    "его состав. Напишите одним сообщением, что было на тарелке — я "
                    "добавлю описание к этой записи."
                    if image_received else
                    "Запись о приёме пищи сохранена в дневнике. Сейчас подробный "
                    "разбор временно недоступен, но сама запись не потерялась."
                ),
                "provider_fallback": True,
            }
        stage = str(result.get("stage") or "intake")
        risk_level = str(result.get("risk_level") or "routine")
        if stage not in {"intake", "analysis", "readiness"}:
            stage = "intake"
        if risk_level not in {"routine", "soon", "urgent", "emergency"}:
            risk_level = "routine"
        assessment = result.get("assessment")
        analysis = result.get("analysis")
        if not isinstance(assessment, dict):
            assessment = dict(state.get("assessment") or {})
        if not isinstance(analysis, dict):
            analysis = dict(state.get("analysis") or {})
        db.save_weight_control_state(
            conversation_id, stage=stage, assessment=assessment,
            analysis=analysis, risk_level=risk_level,
        )
        intake_completed = previous_stage == "intake" and stage in {"analysis", "readiness"}
        program = None
        show_program_intro = False
        linked_messengers = list(
            conversation.get("_messenger_access", {}).get("linked_providers") or []
        )
        daily_messenger_offer = False
        if stage in {"analysis", "readiness"} and risk_level != "emergency":
            program = db.ensure_weight_control_program(conversation_id)
            show_program_intro = not program.get("onboarding_shown")
            daily_messenger_offer = db.weight_messenger_prompt_offer_due(conversation_id)
        if show_program_intro:
            db.ensure_default_weight_meal_reminders(conversation_id)
            body_measurements = db.weight_control_body_measurements(conversation_id)
            show_body_prompt = not bool(body_measurements.get("history"))
            messenger_text = (
                "Мессенджер уже привязан — напоминания будут приходить туда и в этот чат."
                if linked_messengers else
                "Чтобы дневником было удобно пользоваться каждый день, привяжите Telegram "
                "или MAX. Туда будут приходить напоминания о завтраке, обеде и ужине."
            )
            body_prompt_text = (
                "### Зафиксируем отправную точку\n"
                "Укажите текущий вес и доступные объёмы тела. Это поможет видеть "
                "динамику, а не оценивать результат по одному дню. Если сейчас неудобно, "
                "я могу напомнить об этом завтра.\n\n"
                if show_body_prompt else
                "Ваши параметры тела уже сохранены — продолжим отслеживать их динамику.\n\n"
            )
            intro_message = (
                "## Анкета готова\n\n"
                "Следующие **14 дней** я буду помогать вам следить за питанием и "
                "постепенно двигаться к снижению веса без жёстких запретов. "
                "Теперь основная работа — **контроль питания по дневнику**. "
                f"{messenger_text}\n\n"
                "### Как вести дневник\n"
                "Пришлите в этот чат всё, что уже съели сегодня. Можно:\n"
                "- написать состав блюда и примерный объём;\n"
                "- отправить **фотографию еды** вместо текстового описания;\n"
                "- добавлять каждый приём пищи отдельным сообщением.\n\n"
                "Я разберу баланс блюда, отмечу сильные стороны и предложу небольшое "
                "улучшение с учётом вашей цели. Точную калорийность по одной фотографии "
                "я обещать не буду — оценка порции всегда приблизительная.\n\n"
                "### Возможности этого чата\n"
                "- **«Контроль веса»** в меню показывает все 14 дней программы, "
                "прогресс и подтверждённые приёмы пищи по дням;\n"
                "- **«Время питания»** позволяет изменить часы и дни напоминаний, "
                "временно отключить или снова включить их;\n"
                "- **«Параметры тела»** хранит замеры веса и объёмов по датам, "
                "показывает динамику и учитывает её в следующих разборах;\n"
                "- еду можно присылать **текстом или фотографией** — состав на фото "
                "сначала будет показан вам для проверки;\n"
                "- напоминания приходят в этот чат, а после привязки — ещё и в "
                "**Telegram или MAX**;\n"
                "- по окончании 14 дней при наличии не менее **20 подтверждённых "
                "записей** в «Контроле веса» появится кнопка **«Анализ питания»**. "
                "По нажатию я подготовлю заключение по питанию, "
                "привычкам и вашим наблюдениям с планом следующих шагов и отправлю "
                "его в этот чат.\n\n"
                "Я добавила примерное время питания: **09:00, 13:00 и 19:00**. "
                "Его можно изменить в разделе «Время питания и напоминания». "
                "Также раз в пять дней я напомню обновить параметры тела.\n\n"
                f"{body_prompt_text}"
                "### Начнём\n"
                "Пришлите описание или фото того, что вы съели сегодня."
            )
            if intake_completed:
                message = intro_message
            else:
                current_message = self._limit_questions(
                    str(result.get("message") or ""), QUESTIONS_PER_MESSAGE_LIMIT,
                )
                message = f"{intro_message}\n\n---\n\n{current_message}" if current_message else intro_message
        else:
            message = self._limit_questions(
                str(result.get("message") or "Продолжим уточнять данные."),
                QUESTIONS_PER_MESSAGE_LIMIT,
            )
            if daily_messenger_offer:
                message = (
                    f"{message}\n\n---\n\n"
                    "Привяжите Telegram или MAX, чтобы ежедневные напоминания о "
                    "питании приходили даже когда Консилиум закрыт."
                )
        action = "emergency" if risk_level == "emergency" else (
            "clarify" if stage == "intake" else "continue"
        )
        missing_information = [
            str(item)[:300] for item in (result.get("missing_fields") or [])[:12]
        ]
        reminder_offer = str(result.get("reminder_offer") or "none")
        if reminder_offer not in {"none", "medication", "sport", "warmup", "meal", "custom"}:
            reminder_offer = "none"
        metadata = {
            "action": action,
            "urgency": risk_level,
            "emergency": risk_level == "emergency",
            "missing_information": missing_information[:2],
            "weight_control_stage": stage,
            "weight_control_risk_reason": str(result.get("risk_reason") or "")[:500],
            "specialized_dialog": True,
            "attachments": attachment_meta,
        }
        if result.get("provider_fallback"):
            metadata["weight_entry_saved_without_analysis"] = True
        awaiting_meal_confirmation = bool(
            analysis.get("awaiting_meal_confirmation")
        )
        if show_program_intro:
            metadata["weight_program_intro"] = True
            if show_body_prompt:
                metadata["weight_body_prompt"] = True
                metadata["weight_body_remind_later"] = True
        if daily_messenger_offer:
            metadata["weight_messenger_link"] = True
            metadata["weight_messenger_dismiss"] = True
            metadata["weight_messenger_label"] = "Привязать Telegram или MAX"
        if awaiting_meal_confirmation and risk_level != "emergency":
            metadata.update({
                "action": "weight_meal_confirmation",
                "action_label": "Всё верно",
                "meal_draft": str(analysis.get("meal_draft") or "")[:2_000],
            })
        elif show_program_intro and not daily_messenger_offer:
            metadata.update({
                "action": "weight_reminder_setup",
                "reminder_type": "meal",
                "action_label": "Настроить время питания",
            })
        elif show_program_intro and daily_messenger_offer:
            metadata.update({
                "action": "weight_messenger_link",
                "action_label": "Привязать Telegram или MAX",
            })
        elif reminder_offer != "none":
            metadata.update({
                "action": "weight_reminder_setup",
                "reminder_type": reminder_offer,
                "action_label": "Настроить напоминание",
            })
        assistant_message = db.add_message(
            conversation_id, "assistant", message, "manager", metadata,
        )
        if show_program_intro:
            db.mark_weight_control_onboarding_shown(conversation_id)
        if daily_messenger_offer:
            db.mark_weight_messenger_prompt_shown(conversation_id)
        meal_event = str(analysis.get("meal_event") or "none")
        source_type = "photo" if any(
            str(item.get("type") or "").startswith("image/") for item in (attachments or [])
        ) else "text"
        meal_description = str(analysis.get("meal_draft") or "")
        # The diary must not depend solely on perfect model classification:
        # recognizable food text and every food photo are preserved deterministically.
        if meal_event not in {"draft", "confirmed"} and source_type == "photo":
            meal_event = "draft"
            meal_description = meal_description or "Фото приёма пищи — состав уточняется"
        elif meal_event not in {"draft", "confirmed"} and FOOD_ENTRY_HINT.search(user_text):
            meal_event = "confirmed"
            meal_description = meal_description or user_text
        db.record_weight_control_meal(
            conversation_id,
            user_message_id=int(user_message.get("id") or 0),
            assistant_message_id=int(assistant_message.get("id") or 0),
            source_type=source_type,
            meal_event=meal_event,
            description=meal_description,
            analysis=analysis,
        )
        known_facts = []
        for key in (
            "weight_change", "weight_gain_started", "goal", "eating_pattern",
            "hunger_pattern", "sleep", "stress", "activity", "previous_attempts",
        ):
            value = assessment.get(key)
            if value not in {None, ""}:
                known_facts.append(f"{key}: {value}")
        context = normalize_context({
            "current_topic": "контроль веса",
            "topic_relation": "followup",
            "user_goal": str(assessment.get("goal") or "разобраться с факторами веса"),
            "known_facts": known_facts[:20],
            "open_questions": missing_information[:20],
            "patient": {
                "age": assessment.get("age"),
                "sex": assessment.get("sex") or "",
                "weight_kg": assessment.get("current_weight_kg"),
                "pregnancy": conversation.get("_profile", {}).get("pregnancy", "unknown"),
                "conditions": assessment.get("conditions") or [],
                "medications": assessment.get("medications") or [],
                "allergies": conversation.get("_profile", {}).get("allergies") or [],
            },
        })
        db.update_conversation(
            conversation_id, active_agent="manager",
            context_summary=json.dumps(context, ensure_ascii=False),
            status="active", human_status=conversation.get("human_status", "none"),
            human_ticket_id=conversation.get("human_ticket_id"),
            human_channel=conversation.get("human_channel"), ai_enabled=None,
        )
        return ChatResponse(
            conversation_id=conversation_id,
            user_message=user_message,
            assistant_message=assistant_message,
            agent="manager",
            handoff_from=None,
            handoff_reason="Специализированный маршрут «Контроль питания»",
            action=action,
            emergency=risk_level == "emergency",
            context=context,
            urgency=risk_level,
            missing_information=missing_information,
            attachments=attachment_meta,
            council_available=stage in {"analysis", "readiness"} and risk_level != "emergency",
        )

    @staticmethod
    def _wants_lab_results(text: str, attachments: list[dict] | None = None) -> bool:
        return bool(
            not attachments
            and LAB_RESULTS_REQUEST.search(text)
            and not LAB_RESULTS_INTERPRETATION.search(text)
        )

    @staticmethod
    def _wants_to_buy_checkup(text: str, previous_history: list[dict] | None = None) -> bool:
        compact = " ".join(str(text or "").split())
        if not compact or CHECKUP_PURCHASE_NEGATION.search(compact):
            return False
        if not CHECKUP_PURCHASE_ACTION.search(compact):
            return False
        if CHECKUP_PURCHASE_OBJECT.search(compact):
            return True
        prior_assistant = next(
            (
                str(message.get("content") or "")
                for message in reversed(previous_history or [])
                if message.get("role") == "assistant"
            ),
            "",
        )
        return bool(
            CHECKUP_PURCHASE_OBJECT.search(prior_assistant)
            and (
                re.search(
                    r"\b(?:его|этот|эту|выбранн\w*|рекомендованн\w*)\b",
                    compact, re.IGNORECASE,
                )
                or re.fullmatch(
                    r"(?:да[,! ]*)?(?:хочу\s+|готов\w*\s+|давайте\s+)?"
                    r"(?:купить|покупать|приобрест\w*|заказ\w*|оформ\w*|оплат\w*|беру|возьму)"
                    r"[.! ]*",
                    compact, re.IGNORECASE,
                )
            )
        )

    @staticmethod
    def _mentioned_checkups(text: str, catalog: list[dict]) -> list[str]:
        normalized = " ".join(str(text or "").casefold().replace("ё", "е").split())
        matches = []
        for item in catalog:
            name = str(item.get("name") or "").strip()
            comparable = " ".join(name.casefold().replace("ё", "е").split())
            if comparable and comparable in normalized:
                matches.append(name)
        return matches[:3]

    def _process_checkup_purchase(
        self, conversation: dict, user_message: dict, previous_context: dict,
        attachment_meta: list[dict], user_text: str,
    ) -> ChatResponse:
        conversation_id = str(conversation["id"])
        previous_agent = str(conversation.get("active_agent") or "manager")
        ticket = f"H-{secrets.token_hex(3).upper()}"
        saved, created = db.confirm_human_chat(
            conversation_id, ticket, recipient_role="manager",
        )
        if not saved:
            raise ValueError("Диалог не найден")

        names = self._mentioned_checkups(
            user_text, list(conversation.get("_checkup_catalog") or []),
        )
        selected = f" «{'», «'.join(names)}»" if names else ""
        answer = (
            f"Поняла, вы хотите приобрести чекап{selected}. "
            "Важно: сдать чекап сейчас можно только во время медосмотра. "
            "Я передала заявку менеджеру и сохранила контекст разговора. "
            "Менеджер ответит вам в этом чате и поможет оформить покупку."
        )
        context = normalize_context(previous_context)
        context.update({
            "current_topic": "покупка чекапа",
            "topic_relation": "new",
            "user_goal": "приобрести чекап через чат",
            "open_questions": [],
        })
        fact = f"Пользователь хочет приобрести чекап{selected}"
        context["known_facts"] = (list(context.get("known_facts") or []) + [fact])[-20:]
        reason = "Пользователь подтвердил желание приобрести чекап"
        if created and previous_agent != "manager":
            db.add_handoff(conversation_id, previous_agent, "manager", reason)
        metadata = {
            "action": "checkup_purchase_handoff",
            "human_escalation": False,
            "human_ticket_id": saved.get("human_ticket_id"),
            "human_channel": "chat",
            "purchase_intent": True,
            "checkup_names": names,
            "checkup_purchase_cancel": True,
            "attachments": attachment_meta,
        }
        assistant_message = db.add_message(
            conversation_id, "assistant", answer, "manager", metadata,
        )
        db.update_conversation(
            conversation_id, active_agent="manager",
            context_summary=json.dumps(context, ensure_ascii=False),
            status="waiting_human", human_status="pending",
            human_ticket_id=saved.get("human_ticket_id"), human_channel="chat",
            ai_enabled=False,
        )
        if created:
            db.enqueue_manager_notifications(
                "new_request", conversation_id,
                message_id=int(user_message.get("id") or 0),
                message_text=user_text,
                request_kind="checkup_purchase",
            )
        return ChatResponse(
            conversation_id=conversation_id,
            user_message=user_message,
            assistant_message=assistant_message,
            agent="manager",
            handoff_from=previous_agent if previous_agent != "manager" else None,
            handoff_reason=reason,
            action="checkup_purchase",
            human_escalation=False,
            human_ticket_id=saved.get("human_ticket_id"),
            human_channel="chat",
            context=context,
            urgency="routine",
            attachments=attachment_meta,
            council_available=False,
        )

    @staticmethod
    def _wants_lab_interpretation(text: str, attachments: list[dict] | None = None) -> bool:
        return bool(
            not attachments
            and LAB_RESULTS_INTERPRETATION.search(text)
            and re.search(
                r"\b(?:анализ\w*|результат\w*|обследован\w*)\b",
                text,
                re.IGNORECASE,
            )
        )

    def _lab_results_response(
        self,
        conversation: dict,
        user_message: dict,
        context: dict,
        attachment_meta: list[dict],
    ) -> ChatResponse:
        profile = db.get_profile()
        tube_number = str(profile.get("tube_number", "")).strip()
        urls: list[str] = []
        documents: list[dict] = []
        if not tube_number:
            action = "lab_results_prompt"
            answer = (
                "Чтобы найти результаты, нужен номер пробирки с наклейки. "
                "Введите его в появившемся окне — я сохраню номер в анкете и сразу выполню поиск."
            )
        else:
            try:
                result = lookup_lab_results(tube_number)
                urls = list(result.urls)
                documents = lab_result_documents(result.urls)
                if result.status == "found":
                    action = "lab_results_found"
                    links = "\n".join(urls)
                    answer = (
                        "Нашла результаты по сохранённому номеру пробирки. "
                        "Откройте документ по ссылке:\n" + links
                    )
                elif result.status == "processing":
                    action = "lab_results_processing"
                    answer = (
                        "Номер пробирки найден, но ссылка на документ пока не добавлена. "
                        "Вероятно, результаты ещё обрабатываются. Попробуйте проверить позже."
                    )
                else:
                    action = "lab_results_not_found"
                    answer = (
                        "По сохранённому номеру пробирки результаты пока не найдены. "
                        "Проверьте номер в «Моей анкете» или попробуйте позже."
                    )
            except (ValueError, LabResultsUnavailable):
                action = "lab_results_unavailable"
                answer = (
                    "Сейчас не удалось обратиться к базе результатов. "
                    "Номер пробирки сохранён — попробуйте ещё раз немного позже."
                )

        metadata = {
            "action": action,
            "lab_result_urls": urls,
            "lab_result_documents": documents,
            "attachments": attachment_meta,
        }
        assistant_message = db.add_message(
            conversation["id"], "assistant", answer, "manager", metadata
        )
        human_status = conversation.get("human_status", "none")
        db.update_conversation(
            conversation["id"],
            active_agent="manager",
            context_summary=json.dumps(context, ensure_ascii=False),
            status="waiting_human" if human_status == "pending" else "active",
            human_status=human_status,
            human_ticket_id=conversation.get("human_ticket_id"),
            human_channel=conversation.get("human_channel"),
        )
        return ChatResponse(
            conversation_id=conversation["id"],
            user_message=user_message,
            assistant_message=assistant_message,
            agent="manager",
            handoff_from=None,
            handoff_reason="Запрос результатов анализов обработан интерфейсом сервиса",
            action=action,
            context=context,
            attachments=attachment_meta,
        )

    def interpret_lab_results(
        self,
        conversation_id: str | None,
        document_id: str = "all",
    ) -> ChatResponse:
        conversation = db.get_conversation(conversation_id) if conversation_id else None
        if not conversation:
            conversation = db.create_conversation("Расшифровка результатов анализов")
        context = self._load_context(conversation.get("context_summary", ""))
        selected_label = "все результаты" if document_id == "all" else "отдельный документ"
        user_message = db.add_message(
            conversation["id"],
            "user",
            f"Расшифруй {selected_label} и сопоставь с моей анкетой.",
            metadata={
                "action": "lab_interpretation_request",
                "document_id": document_id,
            },
        )
        return self._interpret_lab_results_response(
            conversation, user_message, context, [], document_id
        )

    def _interpret_lab_results_response(
        self,
        conversation: dict,
        user_message: dict,
        context: dict,
        attachment_meta: list[dict],
        document_id: str,
    ) -> ChatResponse:
        profile = db.get_profile()
        tube_number = str(profile.get("tube_number", "")).strip()
        if not tube_number:
            return self._lab_results_response(
                conversation, user_message, context, attachment_meta
            )
        try:
            result = lookup_lab_results(tube_number)
        except (ValueError, LabResultsUnavailable):
            return self._lab_results_response(
                conversation, user_message, context, attachment_meta
            )
        if result.status != "found" or not result.urls:
            return self._lab_results_response(
                conversation, user_message, context, attachment_meta
            )

        documents = lab_result_documents(result.urls)
        if document_id == "all":
            selected = documents
            scope_label = "Все найденные документы как единый набор"
        else:
            selected = [item for item in documents if item["id"] == document_id]
            if not selected:
                raise ValueError("Выбранный документ результатов не найден")
            scope_label = selected[0]["title"]

        source_urls = [item["url"] for item in selected]
        scope_source = "\n".join(source_urls)
        scope_prefix = "all" if document_id == "all" else "document"
        scope_key = (
            f"{scope_prefix}:"
            f"{hashlib.sha256(scope_source.encode('utf-8')).hexdigest()}"
        )
        profile_hash = db.profile_fingerprint(profile)
        cached = db.get_lab_interpretation(
            result.med_id, scope_key, profile_hash
        )
        if cached:
            interpretation = cached["interpretation"]
            from_cache = True
        else:
            interpretation = self.llm.interpret_lab_results(
                profile, selected, scope_label=scope_label
            )
            db.save_lab_interpretation(
                result.med_id,
                scope_key,
                source_urls,
                profile_hash,
                interpretation,
            )
            from_cache = False

        metadata = {
            "action": "lab_interpretation",
            "lab_result_documents": selected,
            "lab_result_urls": source_urls,
            "document_id": document_id,
            "interpretation_cached": from_cache,
            "attachments": attachment_meta,
            "urgency": "routine",
        }
        assistant_message = db.add_message(
            conversation["id"],
            "assistant",
            interpretation,
            "therapist",
            metadata,
        )
        updated_context = normalize_context(context)
        updated_context.update({
            "current_topic": "расшифровка результатов анализов",
            "topic_relation": "followup" if context.get("current_topic") else "new",
            "user_goal": "понять результаты анализов с учётом медицинской анкеты",
        })
        human_status = conversation.get("human_status", "none")
        db.update_conversation(
            conversation["id"],
            active_agent="therapist",
            context_summary=json.dumps(updated_context, ensure_ascii=False),
            status="waiting_human" if human_status == "pending" else "active",
            human_status=human_status,
            human_ticket_id=conversation.get("human_ticket_id"),
            human_channel=conversation.get("human_channel"),
        )
        previous_agent = conversation.get("active_agent")
        return ChatResponse(
            conversation_id=conversation["id"],
            user_message=user_message,
            assistant_message=assistant_message,
            agent="therapist",
            handoff_from=previous_agent if previous_agent != "therapist" else None,
            handoff_reason="Терапевт сопоставил лабораторные результаты с анкетой",
            action="lab_interpretation",
            context=updated_context,
            urgency="routine",
            attachments=attachment_meta,
            council_available=True,
        )

    def _decide(self, text: str, history: list[dict], conversation: dict, context: dict, attachments: list[dict] | None = None) -> RouteDecision:
        if HUMAN_REQUEST.search(text):
            return RouteDecision("human", "manager", "Пользователь явно запросил человека", context)
        if CRITICAL_RISK.search(text):
            updated = normalize_context(context)
            updated["known_facts"] = (updated["known_facts"] + [text])[-20:]
            return RouteDecision(
                "emergency", "safety",
                "Обнаружена формулировка возможной непосредственной угрозы", updated,
            )
        if (
            not attachments
            and PROFILE_ANALYSIS_REQUEST.search(text)
            and not OTHER_PERSON_SUBJECT.search(text)
        ):
            updated = normalize_context(context)
            previous_topic = self._normalize_topic(updated.get("current_topic", ""))
            topic = "анализ медицинской анкеты"
            updated.update({
                "current_topic": topic,
                "topic_relation": "followup" if previous_topic == topic else "new",
                "user_goal": "получить персональный разбор заполненной анкеты",
                "open_questions": [],
            })
            target = "therapist"
            action = "continue" if conversation.get("active_agent") == target else "handoff"
            return RouteDecision(
                action, target,
                "Пользователь запросил медицинский анализ своей анкеты", updated,
            )

        decision = self.llm.route(history, context, conversation, attachments)
        # A model cannot repeat an old human request from history. New explicit
        # requests are handled by the deterministic check above.
        if decision.action == "human":
            decision.action = "respond" if decision.target_agent == "manager" else "continue"
            decision.reason = "Продолжение AI-диалога; прежнее обращение человеку не повторяется"
        return decision

    def second_opinion(self, conversation_id: str) -> dict:
        conversation = db.get_conversation(conversation_id)
        if not conversation:
            raise ValueError("Диалог не найден")
        conversation["_memories"] = [{"category": item["category"], "content": item["content"]} for item in db.list_memories()[:20]]
        conversation["_profile"] = _profile_for_ai()
        conversation["_health_passport"] = _health_passport_for_ai()
        conversation["_checkup_catalog"] = _checkup_catalog_for_ai()
        conversation["_device"] = _device_for_ai()
        conversation["_messenger_access"] = _messenger_access_for_ai()
        conversation["_body_symptoms"] = db.list_body_symptoms(status="active", limit=20)
        context = self._load_context(conversation.get("context_summary", ""))
        history = self._recent_history(conversation_id)
        primary = conversation.get("active_agent", "therapist")
        alternate = self._council_agents(primary)[0]
        decision = RouteDecision("handoff", alternate, f"Независимое второе мнение после {primary}", context)
        result = self.llm.answer(alternate, history, context, decision, conversation)
        message = db.add_message(
            conversation_id, "assistant", result.message, alternate,
            {"action": "second_opinion", "primary_agent": primary, "urgency": result.urgency},
        )
        return {"agent": alternate, "message": message, "urgency": result.urgency}

    def council(self, conversation_id: str) -> dict:
        conversation = db.get_conversation(conversation_id)
        if not conversation:
            raise ValueError("Диалог не найден")
        conversation["_memories"] = [{"category": item["category"], "content": item["content"]} for item in db.list_memories()[:20]]
        conversation["_profile"] = _profile_for_ai()
        conversation["_health_passport"] = _health_passport_for_ai()
        conversation["_checkup_catalog"] = _checkup_catalog_for_ai()
        conversation["_device"] = _device_for_ai()
        conversation["_messenger_access"] = _messenger_access_for_ai()
        conversation["_body_symptoms"] = db.list_body_symptoms(status="active", limit=20)
        context = self._load_context(conversation.get("context_summary", ""))
        history = self._recent_history(conversation_id)
        primary = conversation.get("active_agent", "therapist")
        agents = [primary] + [agent for agent in self._council_agents(primary) if agent != primary]
        agents = [agent for agent in agents if agent not in {"manager", "safety", "general"}][:3]
        if len(agents) < 2:
            agents = ["therapist", "neurologist"]
        opinions = []
        for agent in agents:
            focus = self._council_focus(agent)
            decision = RouteDecision("continue" if agent == primary else "handoff", agent, focus, context)
            if hasattr(self.llm, "council_opinion"):
                result = self.llm.council_opinion(agent, history, context, conversation, focus, opinions)
            else:
                result = self.llm.answer(agent, history, context, decision, conversation)
            if any(self._opinions_too_similar(result.message, previous["message"]) for previous in opinions):
                result.message = (
                    f"Мой фокус: {focus}\n\n"
                    "Новых независимых выводов сверх уже названных нет. Оценку в моей области "
                    "могут изменить дополнительные профильные симптомы или результаты очного осмотра."
                )
            opinions.append({"agent": agent, "focus": focus, "message": result.message, "urgency": result.urgency})
        synthesis = self.llm.synthesize_council(history, context, opinions, conversation)
        message = db.add_message(
            conversation_id, "assistant", synthesis, "manager",
            {"action": "council", "agents": agents, "opinions": opinions},
        )
        return {"agents": agents, "opinions": opinions, "message": message}

    @staticmethod
    def _council_agents(primary: str) -> list[str]:
        mapping = {
            "therapist": ["neurologist", "cardiologist"],
            "cardiologist": ["therapist", "neurologist"],
            "neurologist": ["therapist", "cardiologist"],
            "dermatologist": ["therapist", "pediatrician"],
            "pediatrician": ["therapist", "neurologist"],
            "psychologist": ["therapist", "neurologist"],
        }
        return mapping.get(primary, ["therapist", "neurologist"])

    @staticmethod
    def _council_focus(agent: str) -> str:
        focuses = {
            "therapist": "Собрать общую клиническую картину, сопутствующие факторы и определить рациональный очный маршрут",
            "cardiologist": "Оценить только сердечно-сосудистые риски, давление, пульс, нагрузочные симптомы и срочность",
            "neurologist": "Оценить только неврологические признаки, начало и динамику симптомов и профильные красные флаги",
            "dermatologist": "Оценить морфологию, динамику и внешние воздействия, не делая выводов без осмотра или изображения",
            "pediatrician": "Проверить возрастные особенности, питьё, активность, дыхание и более низкий порог очной оценки",
            "psychologist": "Оценить влияние стресса и эмоционального состояния, не объясняя ими соматические симптомы без оснований",
        }
        return focuses.get(agent, "Добавить только уникальную профильную оценку и безопасный следующий шаг")

    @staticmethod
    def _opinions_too_similar(left: str, right: str) -> bool:
        ignored = {"который", "нужно", "может", "также", "если", "чтобы", "вашей", "вашего", "следует"}
        left_words = {word for word in re.findall(r"[a-zа-яё]{4,}", left.lower()) if word not in ignored}
        right_words = {word for word in re.findall(r"[a-zа-яё]{4,}", right.lower()) if word not in ignored}
        if not left_words or not right_words:
            return left.strip().lower() == right.strip().lower()
        overlap = len(left_words & right_words) / min(len(left_words), len(right_words))
        return overlap >= 0.78

    @staticmethod
    def _normalize_route(decision: RouteDecision, active_agent: str) -> RouteDecision:
        if decision.action in {"respond", "clarify", "human"}:
            decision.target_agent = "manager"
        elif decision.action == "emergency":
            decision.target_agent = "safety"
        elif decision.action == "continue":
            decision.target_agent = active_agent
        elif decision.action == "handoff" and decision.target_agent == active_agent:
            decision.action = "continue"
            decision.reason = "Текущий специалист продолжает ту же тему"
        return decision

    @staticmethod
    def _normalize_topic(value: object) -> str:
        return " ".join(str(value or "").casefold().split())

    @classmethod
    def _apply_topic_transition(cls, previous: dict, current: dict) -> dict:
        """Не позволяет незавершённым вопросам старой темы управлять новой."""
        previous = normalize_context(previous)
        current = normalize_context(current)
        old_topic = cls._normalize_topic(previous.get("current_topic", ""))
        new_topic = cls._normalize_topic(current.get("current_topic", ""))
        is_new = bool(
            current.get("topic_relation") == "new"
            and old_topic and new_topic and old_topic != new_topic
        )
        if not is_new:
            return current

        topic_history = list(previous.get("topic_history", []))
        if previous.get("current_topic"):
            topic_history.append(str(previous["current_topic"]))
        deduplicated: list[str] = []
        for topic in topic_history:
            if cls._normalize_topic(topic) not in {
                cls._normalize_topic(item) for item in deduplicated
            }:
                deduplicated.append(topic)
        current["topic_history"] = deduplicated[-8:]

        old_facts = {
            cls._normalize_topic(item) for item in previous.get("known_facts", [])
        }
        current["known_facts"] = [
            item for item in current.get("known_facts", [])
            if cls._normalize_topic(item) not in old_facts
        ]
        for key in ("answered_questions", "open_questions", "red_flags_checked"):
            if current.get(key) == previous.get(key):
                current[key] = []
        return current

    @staticmethod
    def _question_count(text: str) -> int:
        return len(re.findall(r"\?", text or ""))

    @staticmethod
    def _bound_history(history: list[dict], max_chars: int) -> list[dict]:
        """Keep the newest complete turns within a predictable text budget.

        Questionnaire data and the accumulated structured summary are supplied to
        the model separately, so older transcript text can be dropped safely.  The
        newest message is always retained; an exceptionally long one is truncated
        from the beginning so that the user's latest wording remains available.
        """
        budget = max(1000, int(max_chars or 0))
        selected: list[dict] = []
        used = 0
        for message in reversed(history):
            content = str(message.get("content", ""))
            cost = len(content)
            if selected and used + cost > budget:
                break
            item = message
            if not selected and cost > budget:
                item = {**message, "content": content[-budget:]}
                cost = budget
            selected.append(item)
            used += cost
        return list(reversed(selected))

    def _recent_history(self, conversation_id: str) -> list[dict]:
        history = db.list_messages(conversation_id, settings.max_history_messages)
        return self._bound_history(history, settings.max_history_chars)

    @classmethod
    def _limit_questions(cls, text: str, limit: int) -> str:
        """Оставляет в ответе не больше limit явных вопросов, сохраняя пояснения."""
        if cls._question_count(text) <= limit:
            return text.strip()
        kept: list[str] = []
        used = 0
        for part in re.split(r"(?<=[.!?])\s+|\n+", text.strip()):
            part = part.strip()
            if not part:
                continue
            question_marks = cls._question_count(part)
            if not question_marks:
                kept.append(part)
                continue
            if used >= limit:
                continue
            allowed = limit - used
            if question_marks > allowed:
                end = 0
                for _ in range(allowed):
                    end = part.find("?", end) + 1
                part = part[:end].strip()
                question_marks = allowed
            kept.append(part)
            used += question_marks
        return "\n".join(kept).strip()

    @staticmethod
    def _assessment_question_count(history: list[dict], topic: str = "") -> int:
        total = 0
        normalized_topic = " ".join((topic or "").lower().split())
        for message in history:
            if message.get("role") != "assistant" or message.get("agent_id") not in MEDICAL_AGENTS:
                continue
            metadata = message.get("metadata") or {}
            message_topic = " ".join(str(metadata.get("assessment_topic", "")).lower().split())
            if normalized_topic and message_topic and message_topic != normalized_topic:
                continue
            stored = metadata.get("assessment_questions_asked")
            if stored is None:
                stored = min(QUESTIONS_PER_MESSAGE_LIMIT, len(re.findall(r"\?", message.get("content", ""))))
            try:
                total += max(0, min(QUESTIONS_PER_MESSAGE_LIMIT, int(stored)))
            except (TypeError, ValueError):
                continue
        return total

    @staticmethod
    def _consultation_progress(question_count: int) -> dict:
        asked = max(0, int(question_count))
        return {
            "questions_asked": asked,
            "questions_per_message_limit": QUESTIONS_PER_MESSAGE_LIMIT,
            "unlimited_dialogue": True,
            "instruction": (
                "Общение не ограничено числом реплик. Если уточнение необходимо, "
                "задай только 1–2 наиболее важных вопроса и дождись ответа. "
                "Не предлагай человека автоматически из-за количества вопросов."
            ),
        }

    @staticmethod
    def _load_context(raw: str) -> dict:
        if not raw:
            return normalize_context(None)
        try:
            return normalize_context(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            context = normalize_context(None)
            context["known_facts"] = [raw[:700]]
            return context

    def _prepare_human_handoff(
        self, history: list[dict], context: dict, decision: RouteDecision,
        conversation: dict, status: str, ticket_id: str | None, channel: str | None,
    ) -> tuple[None, str]:
        draft = ""
        try:
            manager_conversation = {**conversation, "active_agent": "manager"}
            draft = self.llm.answer(
                "manager", history, context, decision, manager_conversation
            ).message.strip()
            draft = self._limit_questions(draft, 0)
        except (LLMNotConfigured, LLMProviderError, ValueError):
            draft = (
                "Понимаю — здесь может быть полезен разговор с медицинским "
                "специалистом. Контекст этой переписки уже подготовлен."
            )

        choice = (
            "Если хотите, подключу медицинского специалиста к этому чату. "
            "Обращение будет создано только после подтверждения в появившемся окне. "
            "Если специалист сейчас не нужен, оставайтесь в этом диалоге — ИИ продолжит отвечать."
        )
        if not draft:
            return None, choice
        return None, f"{draft}\n\n{choice}"

    @staticmethod
    def _title(text: str) -> str:
        compact = " ".join(text.split())
        return compact[:57] + ("…" if len(compact) > 57 else "")

    @staticmethod
    def _format_human_reminder(text: str) -> str:
        pattern = r"\s*(Обращение H-[A-ZА-Я0-9-]+ уже передано человеку; ожидайте ответа специалиста\.)"
        return re.sub(pattern, r"\n\n\n\1", text, flags=re.IGNORECASE).lstrip()


orchestrator = ConversationOrchestrator()
