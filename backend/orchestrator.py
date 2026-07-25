import json
import re
import uuid

from . import database as db
from .config import settings
from .llm import LLMNotConfigured, LLMProviderError, LLMService, llm_service
from .schemas import AgentResult, ChatResponse, RouteDecision, normalize_context


HUMAN_REQUEST = re.compile(
    r"\b(?:позов\w*|подключ\w*|перевед\w*|соедин\w*|хочу поговорить с)\s+"
    r"(?:жив\w*\s+)?(?:чел\w*|оператор\w*|врач\w*|специалист\w*)\b",
    re.IGNORECASE,
)
HUMAN_CALL_CHOICE = re.compile(
    r"(?:^\s*(?:созвон|звонок|по\s+телефону)\s*[.!?]*\s*$|"
    r"\bвыбира\w*\s+(?:вариант\s+)?созвон\b|"
    r"\b(?:хочу|предпочитаю|давайте|нужен)\s+(?:на\s+)?созвон\b|"
    r"\b(?:созвонимся|позвоните\s+мне|по\s+телефону)\b)",
    re.IGNORECASE,
)
HUMAN_CALL_REJECTION = re.compile(
    r"\b(?:не\s+(?:хочу|нужен|выбираю)|без|отказываюсь\s+от)\s+созвон\w*\b",
    re.IGNORECASE,
)
CRITICAL_RISK = re.compile(
    r"(не могу дышать|задыхаюсь|потер\w* сознани|внезапн\w* парализ|"
    r"сильн\w* боль\w* в груди|неостанавливаем\w* кровотечени|"
    r"хочу покончить с собой|планирую суицид|не хочу жить|лучше бы меня не было)",
    re.IGNORECASE,
)
MEDICAL_AGENTS = {
    "therapist", "cardiologist", "neurologist", "dermatologist",
    "pediatrician", "psychologist",
}
ASSESSMENT_QUESTION_LIMIT = 5
QUESTIONS_PER_MESSAGE_LIMIT = 2


def _profile_for_ai() -> dict:
    return {
        key: value for key, value in db.get_profile().items()
        if key not in {"chel_id", "tube_number"}
    }


class ConversationOrchestrator:
    def __init__(self, llm: LLMService = llm_service) -> None:
        self.llm = llm

    def process(self, conversation_id: str | None, user_text: str, attachments: list[dict] | None = None) -> ChatResponse:
        conversation = db.get_conversation(conversation_id) if conversation_id else None
        if not conversation:
            conversation = db.create_conversation(self._title(user_text))
        conversation_id = conversation["id"]
        conversation["_memories"] = [{"category": item["category"], "content": item["content"]} for item in db.list_memories()[:20]]
        conversation["_profile"] = _profile_for_ai()
        conversation["_body_symptoms"] = db.list_body_symptoms(status="active", limit=20)
        previous_agent = conversation["active_agent"]
        attachment_meta = [{"name": item.get("name"), "type": item.get("type")} for item in (attachments or [])]
        user_message = db.add_message(conversation_id, "user", user_text, metadata={"attachments": attachment_meta})
        history = db.list_messages(conversation_id, settings.max_history_messages)
        previous_context = self._load_context(conversation.get("context_summary", ""))
        previous_question_count = self._assessment_question_count(
            history, previous_context.get("current_topic", "")
        )
        conversation["_consultation_progress"] = self._consultation_progress(previous_question_count)

        if (
            conversation.get("human_status") == "pending"
            and not conversation.get("human_channel")
            and self._wants_human_call(user_text)
        ):
            ticket_id = conversation.get("human_ticket_id")
            answer = (
                "Хорошо, выбираем созвон. На какой номер вам позвонить? "
                "Укажите российский номер в появившемся поле."
            )
            assistant_message = db.add_message(
                conversation_id, "assistant", answer, "manager",
                {
                    "action": "human_channel_prompt",
                    "human_channel_prompt": "call",
                    "human_ticket_id": ticket_id,
                },
            )
            db.update_conversation(
                conversation_id,
                active_agent="manager",
                context_summary=json.dumps(previous_context, ensure_ascii=False),
                status="waiting_human",
                human_status="pending",
                human_ticket_id=ticket_id,
                human_channel=None,
            )
            return ChatResponse(
                conversation_id=conversation_id,
                user_message=user_message,
                assistant_message=assistant_message,
                agent="manager",
                handoff_from=None,
                handoff_reason="Пользователь выбрал созвон для подготовленного обращения",
                action="human_channel_prompt",
                human_escalation=False,
                human_ticket_id=ticket_id,
                human_channel_prompt="call",
                context=previous_context,
                attachments=attachment_meta,
            )

        decision = self._normalize_route(
            self._decide(user_text, history, conversation, previous_context, attachments), previous_agent
        )
        context = decision.context
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
            human_ticket_id, answer = self._prepare_human_handoff(
                history, context, decision, conversation, human_status, human_ticket_id, human_channel
            )
            human_status = "pending"
            target = "manager"
        else:
            result = self.llm.answer(target, history, context, decision, conversation, attachments)
            answer = result.message
            urgency = result.urgency
            remaining_questions = max(0, ASSESSMENT_QUESTION_LIMIT - question_count_before)
            if target in MEDICAL_AGENTS:
                per_turn_limit = min(QUESTIONS_PER_MESSAGE_LIMIT, remaining_questions)
                answer = self._limit_questions(answer, per_turn_limit)
                missing_information = result.missing_information[:per_turn_limit]
                if result.next_action == "ask":
                    assessment_questions_this_turn = min(
                        per_turn_limit,
                        max(self._question_count(answer), len(missing_information)),
                    )
                if question_count_before >= ASSESSMENT_QUESTION_LIMIT and result.next_action != "emergency":
                    result.next_action = "human"
                    result.handoff_reason = (
                        "Собран максимально допустимый объём уточнений; "
                        "пора предложить чат со специалистом или созвон"
                    )
                    answer = ""
                    missing_information = []
                    assessment_questions_this_turn = 0
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
                    per_turn_limit = min(
                        QUESTIONS_PER_MESSAGE_LIMIT,
                        max(0, ASSESSMENT_QUESTION_LIMIT - question_count_before),
                    )
                    answer = self._limit_questions(answer, per_turn_limit)
                    missing_information = second_result.missing_information[:per_turn_limit]
                    if second_result.next_action == "ask":
                        assessment_questions_this_turn = min(
                            per_turn_limit,
                            max(self._question_count(answer), len(missing_information)),
                        )
                    if (
                        question_count_before >= ASSESSMENT_QUESTION_LIMIT
                        and second_result.next_action != "emergency"
                    ):
                        second_result.next_action = "human"
                        second_result.handoff_reason = (
                            "Собран максимально допустимый объём уточнений; "
                            "пора предложить чат со специалистом или созвон"
                        )
                        answer = ""
                        missing_information = []
                        assessment_questions_this_turn = 0
                if second_result.next_action == "human":
                    human_decision = RouteDecision("human", "manager", second_result.handoff_reason, context)
                    human_ticket_id, answer = self._prepare_human_handoff(
                        history, context, human_decision, conversation, human_status, human_ticket_id, human_channel
                    )
                    human_status = "pending"
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
                human_status = "pending"
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
            "assessment_questions_total": min(
                ASSESSMENT_QUESTION_LIMIT,
                question_count_before + assessment_questions_this_turn,
            ),
            "assessment_topic": context.get("current_topic", ""),
            "attachments": attachment_meta,
        }
        assistant_message = db.add_message(conversation_id, "assistant", answer, target, metadata)
        status = "waiting_human" if action == "human" else "active"
        db.update_conversation(
            conversation_id,
            active_agent=target,
            context_summary=json.dumps(context, ensure_ascii=False),
            status=status,
            human_status=human_status,
            human_ticket_id=human_ticket_id,
            human_channel=human_channel,
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

        decision = self.llm.route(history, context, conversation, attachments)
        # A model cannot repeat an old human request from history. New explicit
        # requests are handled by the deterministic check above.
        if decision.action == "human":
            decision.action = "respond" if decision.target_agent == "manager" else "continue"
            decision.reason = "Продолжение AI-диалога; прежнее обращение человеку не повторяется"
        return decision

    @staticmethod
    def _wants_human_call(text: str) -> bool:
        return bool(HUMAN_CALL_CHOICE.search(text)) and not HUMAN_CALL_REJECTION.search(text)

    def second_opinion(self, conversation_id: str) -> dict:
        conversation = db.get_conversation(conversation_id)
        if not conversation:
            raise ValueError("Диалог не найден")
        conversation["_memories"] = [{"category": item["category"], "content": item["content"]} for item in db.list_memories()[:20]]
        conversation["_profile"] = _profile_for_ai()
        conversation["_body_symptoms"] = db.list_body_symptoms(status="active", limit=20)
        context = self._load_context(conversation.get("context_summary", ""))
        history = db.list_messages(conversation_id, settings.max_history_messages)
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
        conversation["_body_symptoms"] = db.list_body_symptoms(status="active", limit=20)
        context = self._load_context(conversation.get("context_summary", ""))
        history = db.list_messages(conversation_id, settings.max_history_messages)
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
    def _question_count(text: str) -> int:
        return len(re.findall(r"\?", text or ""))

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
        return min(ASSESSMENT_QUESTION_LIMIT, total)

    @staticmethod
    def _consultation_progress(question_count: int) -> dict:
        asked = max(0, min(ASSESSMENT_QUESTION_LIMIT, int(question_count)))
        return {
            "questions_asked": asked,
            "question_limit": ASSESSMENT_QUESTION_LIMIT,
            "remaining_questions": ASSESSMENT_QUESTION_LIMIT - asked,
            "questions_per_message_limit": QUESTIONS_PER_MESSAGE_LIMIT,
            "instruction": (
                "Задай только 1–2 наиболее важных вопроса и дождись ответа. "
                "Когда данных достаточно или remaining_questions=0, верни next_action=human."
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
    ) -> tuple[str, str]:
        ticket_id = ticket_id or f"H-{uuid.uuid4().hex[:6].upper()}"
        if status == "pending":
            if channel == "chat":
                return ticket_id, (
                    f"Запрос {ticket_id} уже сохранён для чата со специалистом. "
                    "Пока человек не подключён, но вы можете продолжать писать мне — контекст не потеряется."
                )
            if channel == "call":
                return ticket_id, (
                    f"Для обращения {ticket_id} уже выбран созвон. В этой демоверсии звонок автоматически не создаётся, "
                    "а пока мы можем продолжить разговор здесь."
                )
            return ticket_id, (
                f"Обращение {ticket_id} уже подготовлено. Как вам удобнее продолжить со специалистом: "
                "в чате или созвоном? Пока выбираете, можете продолжать разговор со мной."
            )

        draft = ""
        try:
            manager_conversation = {**conversation, "active_agent": "manager"}
            draft = self.llm.answer("manager", history, context, decision, manager_conversation).message.strip()
            draft = self._limit_questions(draft, 0)
        except (LLMNotConfigured, LLMProviderError, ValueError):
            draft = "Понимаю — здесь лучше дать вам возможность поговорить с человеком. Я сохраню контекст этой переписки, чтобы не пришлось начинать сначала."

        choice = (
            f"Обращение {ticket_id} подготовлено, но человек пока не подключён. "
            "Как вам удобнее продолжить: в чате со специалистом или созвоном?"
        )
        if not draft:
            return ticket_id, choice
        return ticket_id, f"{draft}\n\n{choice}"

    @staticmethod
    def _title(text: str) -> str:
        compact = " ".join(text.split())
        return compact[:57] + ("…" if len(compact) > 57 else "")

    @staticmethod
    def _format_human_reminder(text: str) -> str:
        pattern = r"\s*(Обращение H-[A-ZА-Я0-9-]+ уже передано человеку; ожидайте ответа специалиста\.)"
        return re.sub(pattern, r"\n\n\n\1", text, flags=re.IGNORECASE).lstrip()


orchestrator = ConversationOrchestrator()
