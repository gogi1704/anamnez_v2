import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_temp_dir = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = str(Path(_temp_dir.name) / "test.db")
os.environ["ANALYTICS_DATABASE_PATH"] = str(Path(_temp_dir.name) / "analytics.db")

from backend import analytics
from backend import database as db  # noqa: E402
from backend import yookassa  # noqa: E402
from backend.ai_costs import usage_record  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.lab_results import (  # noqa: E402
    LabResult, extract_urls, lab_result_documents, normalize_med_id,
)
from backend.llm import LLMService  # noqa: E402
from backend.main import ConsiliumHandler, admin_token_valid  # noqa: E402
from backend.orchestrator import ConversationOrchestrator  # noqa: E402
from backend.onboarding import (  # noqa: E402
    EXAMINATION_UPGRADE_PAIRS, TEST_CATALOG, normalize_examination_selection,
    public_onboarding, recommend_test_ids,
)
from backend.prompts import ORCHESTRATOR_PROMPT, PROFILES  # noqa: E402
from backend.schemas import AgentResult, RouteDecision, normalize_context  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.route_calls = []
        self.answer_calls = []
        self.interpret_calls = []

    def route(self, history, context, conversation, attachments=None):
        self.route_calls.append({"history": history, "context": context, "conversation": dict(conversation)})
        active = conversation["active_agent"]
        target = "neurologist" if active == "manager" else active
        action = "handoff" if active != target else "continue"
        updated = normalize_context(context)
        updated.update({"current_topic": "головная боль", "topic_relation": "followup", "user_goal": "понять срочность"})
        updated["known_facts"] = ["Головная боль два дня"]
        return RouteDecision(action, target, "Тестовая контекстная маршрутизация", updated)

    def answer(self, agent_id, history, context, route_decision, conversation, attachments=None):
        self.answer_calls.append({"agent_id": agent_id, "route": route_decision, "context": context})
        return AgentResult(
            message=f"Ответ агента {agent_id}; сообщений в контексте: {len(history)}",
            next_action="respond", target_agent=None, handoff_reason="",
            urgency="routine", missing_information=[],
        )

    def synthesize_council(self, history, context, opinions, conversation):
        return "Общий вывод консилиума"

    def interpret_lab_results(self, profile, documents, *, scope_label):
        self.interpret_calls.append({
            "profile": dict(profile),
            "documents": [dict(item) for item in documents],
            "scope_label": scope_label,
        })
        return (
            "Общий вывод: результаты сопоставлены с анкетой.\n\n"
            "Отклонения: тестовая расшифровка."
        )


class StickyHumanLLM(FakeLLM):
    """Имитирует модель, которая цепляется за старый запрос человека."""

    def route(self, history, context, conversation, attachments=None):
        self.route_calls.append({"history": history, "context": context, "conversation": dict(conversation)})
        return RouteDecision("human", "manager", "В истории есть старая просьба", normalize_context(context))


class SpecialistHandoffLLM(FakeLLM):
    def route(self, history, context, conversation, attachments=None):
        return RouteDecision("handoff", "therapist", "Нужна первичная оценка", normalize_context(context))

    def answer(self, agent_id, history, context, route_decision, conversation, attachments=None):
        self.answer_calls.append({"agent_id": agent_id, "route": route_decision})
        if agent_id == "therapist":
            return AgentResult(
                "Подключаю невролога.", "handoff", "neurologist",
                "Появились очаговые неврологические симптомы", "urgent", [],
            )
        return AgentResult("Ответ невролога", "respond", None, "", "urgent", [])


class QuestioningMedicalLLM(FakeLLM):
    def route(self, history, context, conversation, attachments=None):
        self.route_calls.append({"history": history, "context": context, "conversation": dict(conversation)})
        updated = normalize_context(context)
        updated.update({
            "current_topic": "кашель",
            "topic_relation": "new" if not context.get("current_topic") else "followup",
            "user_goal": "выяснить причину жалобы и срочность",
        })
        active = conversation["active_agent"]
        return RouteDecision(
            "handoff" if active == "manager" else "continue",
            "therapist" if active == "manager" else active,
            "Нужна последовательная оценка жалобы",
            updated,
        )

    def answer(self, agent_id, history, context, route_decision, conversation, attachments=None):
        self.answer_calls.append({"agent_id": agent_id, "conversation": dict(conversation)})
        if agent_id == "manager":
            return AgentResult(
                "Я собрал основные сведения и передам их специалисту без повторного опроса.",
                "respond", None, "", "routine", [],
            )
        return AgentResult(
            "Понял вас. Когда начался кашель? Есть ли температура? Появилась ли одышка?",
            "ask", None, "", "routine",
            ["Когда начался кашель?", "Есть ли температура?", "Появилась ли одышка?"],
        )


class EarlyHumanMedicalLLM(QuestioningMedicalLLM):
    def answer(self, agent_id, history, context, route_decision, conversation, attachments=None):
        if agent_id == "manager":
            return super().answer(agent_id, history, context, route_decision, conversation, attachments)
        medical_answers = [
            item for item in self.answer_calls if item["agent_id"] == "therapist"
        ]
        self.answer_calls.append({"agent_id": agent_id, "conversation": dict(conversation)})
        if medical_answers:
            return AgentResult(
                "Картина стала понятнее: жалоба не выглядит экстренной, но её стоит обсудить со специалистом.",
                "human", None, "Собраны необходимые сведения", "soon", [],
            )
        return AgentResult(
            "Уточню два момента. Когда началась жалоба? Становится ли хуже?",
            "ask", None, "", "routine",
            ["Когда началась жалоба?", "Становится ли хуже?"],
        )


class CouncilFocusLLM(FakeLLM):
    def __init__(self):
        super().__init__()
        self.council_calls = []

    def council_opinion(self, agent_id, history, context, conversation, focus, previous_opinions):
        self.council_calls.append({"agent": agent_id, "focus": focus, "previous": len(previous_opinions)})
        return AgentResult(
            f"Мой фокус: {focus}\nУникальная оценка агента {agent_id}.",
            "respond", None, "", "routine", [],
        )


class OrchestratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def test_dialogue_is_persisted_and_context_reused(self):
        fake = FakeLLM()
        service = ConversationOrchestrator(fake)
        first = service.process(None, "У меня второй день болит голова")
        second = service.process(first.conversation_id, "Сила примерно 7 из 10")

        saved = db.get_conversation(first.conversation_id)
        context = json.loads(saved["context_summary"])
        self.assertEqual(first.agent, "neurologist")
        self.assertEqual(second.agent, "neurologist")
        self.assertEqual(second.action, "continue")
        self.assertEqual(saved["active_agent"], "neurologist")
        self.assertEqual(context["current_topic"], "головная боль")
        self.assertEqual(len(db.list_messages(first.conversation_id)), 4)
        self.assertEqual(len(db.list_handoffs(first.conversation_id)), 1)
        self.assertEqual(fake.route_calls[1]["conversation"]["active_agent"], "neurologist")
        self.assertGreaterEqual(len(fake.route_calls[1]["history"]), 3)

    def test_human_offer_keeps_ai_active_until_user_confirms(self):
        service = ConversationOrchestrator(FakeLLM())
        with patch.object(db, "enqueue_manager_notifications") as notify:
            result = service.process(None, "Позовите живого оператора")

        self.assertTrue(result.human_escalation)
        self.assertIsNone(result.human_ticket_id)
        self.assertIn("только после подтверждения", result.assistant_message["content"])
        saved = db.get_conversation(result.conversation_id)
        self.assertEqual(saved["status"], "active")
        self.assertEqual(saved["human_status"], "none")
        self.assertIsNone(saved["human_ticket_id"])
        self.assertTrue(saved["ai_enabled"])
        notify.assert_not_called()

    def test_repeated_human_offer_still_does_not_create_request(self):
        service = ConversationOrchestrator(FakeLLM())
        first = service.process(None, "Позови человека")
        repeated = service.process(first.conversation_id, "Подключи оператора")

        self.assertTrue(first.human_escalation)
        self.assertTrue(repeated.human_escalation)
        self.assertIsNone(first.human_ticket_id)
        self.assertIsNone(repeated.human_ticket_id)
        saved = db.get_conversation(first.conversation_id)
        self.assertTrue(saved["ai_enabled"])
        self.assertEqual(saved["human_status"], "none")

    def test_confirming_specialist_chat_creates_request_pauses_ai_and_notifies_once(self):
        offered = ConversationOrchestrator(FakeLLM()).process(None, "Позови человека")
        analytics_events = []
        handler = SimpleNamespace(
            _json=lambda status, payload: (status, payload),
            _track_analytics=lambda event, metadata=None: analytics_events.append((event, metadata)),
        )

        complete_profile = {"sex": "female", "age": 40, "height_cm": 168, "weight_kg": 65}
        with patch.object(db, "get_profile", return_value=complete_profile), \
                patch.object(db, "enqueue_manager_notifications") as notify:
            status, response = ConsiliumHandler._set_human_preference(
                handler,
                {"conversation_id": offered.conversation_id, "channel": "chat"},
            )
            repeated_status, repeated_response = ConsiliumHandler._set_human_preference(
                handler,
                {"conversation_id": offered.conversation_id, "channel": "chat"},
            )

        self.assertEqual(status, 200)
        self.assertEqual(repeated_status, 200)
        self.assertRegex(response["ticket_id"], r"^H-[A-F0-9]{6}$")
        self.assertEqual(repeated_response["ticket_id"], response["ticket_id"])
        saved = db.get_conversation(offered.conversation_id)
        self.assertEqual(saved["status"], "waiting_human")
        self.assertEqual(saved["human_status"], "pending")
        self.assertEqual(saved["human_channel"], "chat")
        self.assertFalse(saved["ai_enabled"])
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[:2], ("new_request", offered.conversation_id))
        self.assertEqual(
            [event for event, _ in analytics_events].count("human_requested"), 1,
        )

    def test_specialist_confirmation_requires_minimum_profile_without_disabling_ai(self):
        offered = ConversationOrchestrator(FakeLLM()).process(None, "Позови человека")
        handler = SimpleNamespace(
            _json=lambda status, payload: (status, payload),
            _track_analytics=lambda event, metadata=None: None,
        )

        with patch.object(db, "get_profile", return_value={"sex": "male"}):
            status, response = ConsiliumHandler._set_human_preference(
                handler,
                {"conversation_id": offered.conversation_id, "channel": "chat"},
            )

        self.assertEqual(status, 422)
        self.assertEqual(response["code"], "consultation_profile_required")
        self.assertEqual(response["missing_fields"], ["age", "height_cm", "weight_kg"])
        saved = db.get_conversation(offered.conversation_id)
        self.assertTrue(saved["ai_enabled"])
        self.assertEqual(saved["human_status"], "none")

    def test_specialist_confirmation_endpoint_rejects_call_channel(self):
        offered = ConversationOrchestrator(FakeLLM()).process(None, "Позови человека")
        handler = SimpleNamespace(
            _json=lambda status, payload: (status, payload),
            _track_analytics=lambda event, metadata=None: None,
        )
        status, response = ConsiliumHandler._set_human_preference(
            handler,
            {"conversation_id": offered.conversation_id, "channel": "call"},
        )

        self.assertEqual(status, 422)
        self.assertIn("чат с медицинским специалистом", response["detail"])
        saved = db.get_conversation(offered.conversation_id)
        self.assertTrue(saved["ai_enabled"])
        self.assertEqual(saved["human_status"], "none")
        self.assertIsNone(saved["human_ticket_id"])
    def test_human_modal_offers_specialist_chat_or_ai_without_call(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("Чат с медицинским специалистом", index)
        self.assertIn("Не звать медицинского специалиста", index)
        self.assertIn("Остаться в текущем чате с ИИ", index)
        self.assertNotIn('id="humanCallButton"', index)
        self.assertNotIn('id="callPhoneStep"', index)
        self.assertNotIn('id="ticketNumber"', index)
        decline_flow = script.split("function declineHumanSpecialist()", 1)[1].split(
            "function setHumanChoiceDisabled", 1,
        )[0]
        confirm_flow = script.split("async function chooseHumanSpecialistChat()", 1)[1].split(
            "function closeFunctionMenu", 1,
        )[0]
        self.assertIn("updateChatMode(true, 'none', null)", decline_flow)
        self.assertNotIn("/api/human-preference", decline_flow)
        self.assertIn("/api/human-preference", confirm_flow)
        self.assertIn("channel:'chat'", confirm_flow)
    def test_critical_phrase_bypasses_ai_router(self):
        fake = FakeLLM()
        result = ConversationOrchestrator(fake).process(None, "У меня сильная боль в груди, задыхаюсь")
        self.assertTrue(result.emergency)
        self.assertEqual(result.agent, "safety")
        self.assertEqual(fake.route_calls, [])

    def test_old_human_request_cannot_trigger_again(self):
        service = ConversationOrchestrator(StickyHumanLLM())
        escalated = service.process(None, "Позови человека")
        continued = service.process(escalated.conversation_id, "Ты тут?")
        self.assertTrue(escalated.human_escalation)
        self.assertFalse(continued.human_escalation)
        self.assertEqual(continued.action, "respond")
        self.assertIsNone(continued.human_ticket_id)

    def test_specialist_can_handoff_in_same_turn(self):
        fake = SpecialistHandoffLLM()
        result = ConversationOrchestrator(fake).process(None, "Мне нужна консультация")
        self.assertEqual(result.agent, "neurologist")
        self.assertEqual(result.handoff_from, "therapist")
        self.assertEqual(result.assistant_message["content"], "Ответ невролога")
        self.assertEqual([call["agent_id"] for call in fake.answer_calls], ["therapist", "neurologist"])

    def test_medical_agent_asks_no_more_than_two_questions_per_message(self):
        service = ConversationOrchestrator(QuestioningMedicalLLM())
        result = service.process(None, "У меня кашель")

        self.assertEqual(result.assistant_message["content"].count("?"), 2)
        self.assertEqual(len(result.missing_information), 2)
        self.assertEqual(result.assistant_message["metadata"]["assessment_questions_asked"], 2)
        self.assertEqual(result.assistant_message["metadata"]["assessment_questions_total"], 2)

    def test_medical_dialogue_does_not_force_human_after_five_questions(self):
        service = ConversationOrchestrator(QuestioningMedicalLLM())
        first = service.process(None, "У меня кашель")
        second = service.process(first.conversation_id, "Начался вчера, температура 37,5")
        third = service.process(first.conversation_id, "Одышки нет")
        fourth = service.process(first.conversation_id, "Становится немного лучше")

        self.assertEqual(first.assistant_message["metadata"]["assessment_questions_asked"], 2)
        self.assertEqual(second.assistant_message["metadata"]["assessment_questions_asked"], 2)
        self.assertEqual(third.assistant_message["metadata"]["assessment_questions_asked"], 2)
        self.assertEqual(third.assistant_message["metadata"]["assessment_questions_total"], 6)
        self.assertFalse(fourth.human_escalation)
        self.assertEqual(fourth.agent, "therapist")
        self.assertEqual(fourth.assistant_message["content"].count("?"), 2)
        self.assertEqual(fourth.assistant_message["metadata"]["assessment_questions_total"], 8)

    def test_medical_agent_can_offer_human_when_context_makes_it_useful(self):
        service = ConversationOrchestrator(EarlyHumanMedicalLLM())
        first = service.process(None, "У меня второй день болит горло")
        second = service.process(first.conversation_id, "Температуры нет, становится легче")

        self.assertEqual(first.assistant_message["metadata"]["assessment_questions_asked"], 2)
        self.assertTrue(second.human_escalation)
        self.assertIn("подтверждения", second.assistant_message["content"])

    def test_history_context_is_bounded_by_messages_and_characters(self):
        history = [
            {"id": index, "role": "user", "content": str(index) * 700, "metadata": {}}
            for index in range(1, 8)
        ]
        bounded = ConversationOrchestrator._bound_history(history, 1600)

        self.assertEqual([item["id"] for item in bounded], [6, 7])
        self.assertLessEqual(sum(len(item["content"]) for item in bounded), 1600)
        self.assertEqual(bounded[-1]["content"], history[-1]["content"])

        oversized = ConversationOrchestrator._bound_history([
            {"id": 1, "role": "user", "content": "начало" + "я" * 2500, "metadata": {}},
        ], 1000)
        self.assertEqual(len(oversized), 1)
        self.assertEqual(len(oversized[0]["content"]), 1000)
        self.assertTrue(oversized[0]["content"].endswith("я" * 100))

    def test_long_dialogue_warns_once_before_context_window_is_full(self):
        conversation = db.create_conversation("Длинный диалог")
        for index in range(settings.max_history_messages - 5):
            db.add_message(
                conversation["id"],
                "user" if index % 2 == 0 else "assistant",
                f"Сообщение {index + 1}",
                "therapist" if index % 2 else None,
            )

        service = ConversationOrchestrator(FakeLLM())
        warned = service.process(conversation["id"], "Продолжим обсуждение")
        repeated = service.process(conversation["id"], "Ещё один вопрос")

        self.assertTrue(warned.assistant_message["metadata"]["context_limit_warning"])
        self.assertIn("Диалог стал длинным", warned.assistant_message["content"])
        self.assertIn("последних 30 сообщений", warned.assistant_message["content"])
        self.assertFalse(repeated.assistant_message["metadata"]["context_limit_warning"])
        self.assertNotIn("Диалог стал длинным", repeated.assistant_message["content"])

    def test_human_reminder_has_two_blank_lines(self):
        text = "Да, я здесь. Обращение H-939398 уже передано человеку; ожидайте ответа специалиста."
        formatted = ConversationOrchestrator._format_human_reminder(text)
        self.assertEqual(
            formatted,
            "Да, я здесь.\n\n\nОбращение H-939398 уже передано человеку; ожидайте ответа специалиста.",
        )

    def test_context_is_returned_for_interactive_summary(self):
        result = ConversationOrchestrator(FakeLLM()).process(None, "Два дня болит голова")
        self.assertEqual(result.context["current_topic"], "головная боль")
        self.assertEqual(result.urgency, "routine")
        self.assertTrue(result.council_available)

    def test_council_is_saved_as_structured_message(self):
        service = ConversationOrchestrator(FakeLLM())
        first = service.process(None, "Два дня болит голова")
        council = service.council(first.conversation_id)
        self.assertEqual(council["message"]["content"], "Общий вывод консилиума")
        self.assertGreaterEqual(len(council["opinions"]), 2)
        self.assertEqual(council["message"]["metadata"]["action"], "council")

    def test_council_assigns_distinct_focus_and_shares_previous_opinions(self):
        fake = CouncilFocusLLM()
        service = ConversationOrchestrator(fake)
        first = service.process(None, "Два дня болит голова")
        council = service.council(first.conversation_id)
        focuses = [opinion["focus"] for opinion in council["opinions"]]
        self.assertEqual(len(focuses), len(set(focuses)))
        self.assertEqual([call["previous"] for call in fake.council_calls], list(range(len(fake.council_calls))))
        self.assertTrue(all("Мой фокус:" in opinion["message"] for opinion in council["opinions"]))

    def test_nearly_identical_council_opinions_are_replaced(self):
        repeated = "Нужно оценить симптомы и обратиться к врачу для очного осмотра и дальнейшей оценки"
        self.assertTrue(ConversationOrchestrator._opinions_too_similar(repeated, repeated + " состояния"))

    def test_multimodal_input_contains_image_and_pdf(self):
        payload = LLMService.multimodal_input("контекст", [
            {"name": "photo.png", "type": "image/png", "data_url": "data:image/png;base64,AAAA"},
            {"name": "result.pdf", "type": "application/pdf", "data_url": "data:application/pdf;base64,BBBB"},
        ])
        content = payload[0]["content"]
        self.assertEqual([item["type"] for item in content], ["input_text", "input_image", "input_file"])
        self.assertEqual(content[2]["filename"], "result.pdf")

    def test_profile_is_persisted_and_passed_to_agents(self):
        profile = db.save_profile({
            "preferred_name": "Анна", "company_inn": "7707083893", "age": 34, "sex": "female", "height_cm": 168,
            "weight_kg": 62, "pregnancy": "no", "conditions": ["Астма"],
            "medications": ["Назначенный ингалятор"], "allergies": ["Пенициллин"],
            "smoking": "never", "tube_number": "LAB-2026-0042", "notes": "",
        })
        self.assertEqual(profile["age"], 34)
        self.assertEqual(profile["allergies"], ["Пенициллин"])
        self.assertEqual(profile["tube_number"], "LAB-2026-0042")
        self.assertEqual(profile["company_inn"], "7707083893")

        fake = FakeLLM()
        ConversationOrchestrator(fake).process(None, "У меня болит голова")
        runtime = json.loads(LLMService.runtime_context([], normalize_context(None), fake.route_calls[-1]["conversation"]))
        self.assertEqual(runtime["user_profile"]["weight_kg"], 62.0)
        self.assertNotIn("company_inn", runtime["user_profile"])

    def test_company_inn_is_first_question_and_non_medical_route_opens_chat(self):
        project_root = Path(__file__).resolve().parents[1]
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        main_source = (project_root / "backend" / "main.py").read_text(encoding="utf-8")
        questionnaire = script.split("const onboardingQuestions = [", 1)[1].split(
            "];", 1,
        )[0]
        self.assertLess(questionnaire.index("key:'company_inn'"), questionnaire.index("key:'preferred_name'"))
        self.assertIn("Сообщите ИНН вашего предприятия", questionnaire)
        self.assertIn("Я не на мед-осмотр", script)
        self.assertIn("/api/onboarding/not-medical-exam", script)
        self.assertIn('payment_status="not_medical_exam"', main_source)
        self.assertIn("await openMainApp()", script)
        self.assertIn("payment_status === 'not_medical_exam'", script)
        self.assertIn("openInstallApp();", script)

        valid = ConsiliumHandler._validate_profile({"company_inn": "7707083893"})
        self.assertEqual(valid["company_inn"], "7707083893")
        test_valid = ConsiliumHandler._validate_profile({"company_inn": "123123"})
        self.assertEqual(test_valid["company_inn"], "123123")
        self.assertIn("value !== '123123'", script)
        for invalid_inn in ("123", "123122", "123124", "1231234", "123456789"):
            with self.subTest(company_inn=invalid_inn), self.assertRaises(ValueError):
                ConsiliumHandler._validate_profile({"company_inn": invalid_inn})

    def test_test_inn_user_is_excluded_from_every_statistics_surface(self):
        analytics.init_db()
        regular_id = "chel_stats_regular_inn"
        test_id = "chel_stats_test_inn"
        manager_marker = "manager_test_inn"
        dashboard_before = db.admin_dashboard(days=7)["summary"]
        costs_before = db.admin_ai_costs("all")["all_time"]["requests"]
        analytics_before = analytics.admin_report(period="all")["summary"]
        try:
            for chel_id, company_inn in (
                (regular_id, "7707083893"),
                (test_id, db.TEST_COMPANY_INN),
            ):
                db.ensure_user(chel_id, pending=True, from_manager=manager_marker)
                db.set_current_chel_id(chel_id)
                db.mark_current_user_registered("anonymous")
                if chel_id == test_id:
                    preexisting = analytics.record_events(chel_id, [{
                        "event_id": "web-test-inn-before-profile",
                        "session_id": "ses-test-inn-before-profile",
                        "event_name": "welcome_viewed",
                    }])
                    self.assertEqual(preexisting["accepted"], 1)
                db.save_profile({"company_inn": company_inn})
                if chel_id == test_id:
                    with analytics.connection() as analytics_conn:
                        remaining = analytics_conn.execute(
                            "SELECT COUNT(*) FROM analytics_events WHERE chel_id = ?",
                            (test_id,),
                        ).fetchone()[0]
                    self.assertEqual(remaining, 0)
                db.save_onboarding(status="payment", selected_tests=["lipids"])
                db.record_device_access(
                    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126.0 Mobile"
                )
                conversation = db.create_conversation("Проверка статистики")
                db.add_message(conversation["id"], "user", "Тестовое сообщение")
                usage_id = db.record_ai_usage({
                    "chel_id": chel_id, "operation": "routing", "model": "test-model",
                    "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                    "pricing_known": True, "total_cost_usd": 0.001,
                })
                accepted = analytics.record_events(chel_id, [{
                    "event_id": f"web-{chel_id}-registration",
                    "session_id": f"ses-{chel_id}-registration",
                    "event_name": "registration_completed",
                    "properties": {"method": "anonymous"},
                }])
                if chel_id == test_id:
                    self.assertEqual(usage_id, 0)
                    self.assertEqual(accepted["accepted"], 0)
                else:
                    self.assertGreater(usage_id, 0)
                    self.assertEqual(accepted["accepted"], 1)

            dashboard_after = db.admin_dashboard(days=7)["summary"]
            self.assertEqual(
                dashboard_after["users_total"], dashboard_before["users_total"] + 1,
            )
            self.assertEqual(
                dashboard_after["conversations_total"],
                dashboard_before["conversations_total"] + 1,
            )
            self.assertEqual(db.admin_table("users", test_id)["total"], 0)
            self.assertEqual(db.admin_table("conversations", test_id)["total"], 0)
            self.assertEqual(db.admin_table("devices", test_id)["total"], 0)

            manager_report = db.admin_manager_attribution("all")
            manager_row = next(
                item for item in manager_report["managers"]
                if item["from_manager"] == manager_marker
            )
            self.assertEqual(manager_row["users"], 1)
            self.assertEqual(manager_row["users_with_examinations"], 1)

            costs_after = db.admin_ai_costs("all")["all_time"]["requests"]
            self.assertEqual(costs_after, costs_before + 1)
            analytics_after = analytics.admin_report(period="all")["summary"]
            self.assertEqual(
                analytics_after["users"], analytics_before["users"] + 1,
            )
            self.assertEqual(
                analytics_after["visitors"], analytics_before["visitors"] + 1,
            )
        finally:
            for chel_id in (regular_id, test_id):
                analytics.delete_user_data(chel_id)
                db.set_current_chel_id(chel_id)
                db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_headache_suggestion_does_not_invent_duration(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")

        self.assertIn(
            'data-prompt="У меня болит голова">Болит голова</button>',
            index,
        )
        self.assertNotIn(
            'data-prompt="У меня второй день болит голова"',
            index,
        )

    def test_profile_measurement_ranges_match_questionnaire_and_server(self):
        project_root = Path(__file__).resolve().parents[1]
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        index = (project_root / "index.html").read_text(encoding="utf-8")
        main_source = (project_root / "backend" / "main.py").read_text(encoding="utf-8")
        questionnaire = script.split("const onboardingQuestions = [", 1)[1].split(
            "];", 1,
        )[0]

        self.assertIn("key:'age'", questionnaire)
        self.assertIn("min:18, max:99, step:'1'", questionnaire)
        self.assertIn("min:50, max:250, step:'0.1'", questionnaire)
        self.assertIn("min:40, max:250, step:'0.1'", questionnaire)
        self.assertNotIn("bmi-preview", script)
        self.assertIn('id="profileAge" type="number" min="18" max="99" step="1"', index)
        self.assertIn('id="profileHeight" type="number" min="50" max="250"', index)
        self.assertIn('id="profileWeight" type="number" min="40" max="250"', index)

        minimums = ConsiliumHandler._validate_profile({"age": 18, "height_cm": 50, "weight_kg": 40})
        maximums = ConsiliumHandler._validate_profile({"age": 99, "height_cm": 250, "weight_kg": 250})
        self.assertEqual((minimums["age"], minimums["height_cm"], minimums["weight_kg"]), (18, 50, 40))
        self.assertEqual((maximums["age"], maximums["height_cm"], maximums["weight_kg"]), (99, 250, 250))
        for payload in (
            {"age": 17}, {"age": 100}, {"age": 25.5},
            {"height_cm": 49.9}, {"height_cm": 250.1},
            {"weight_kg": 39.9}, {"weight_kg": 250.1},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                ConsiliumHandler._validate_profile(payload)

    def test_profile_analysis_request_routes_to_therapist_with_derived_summary(self):
        chel_id = "chel_profile_analysis"
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            db.save_profile({
                "age": 40, "sex": "male", "height_cm": 180, "weight_kg": 81,
                "conditions": ["Гипертония"], "smoking": "former",
            })
            fake = FakeLLM()
            response = ConversationOrchestrator(fake).process(
                None, "Проанализируй мою медицинскую анкету"
            )
            self.assertEqual(response.agent, "therapist")
            self.assertEqual(fake.route_calls, [])

            runtime = json.loads(LLMService.runtime_context(
                [], response.context,
                {"active_agent": "therapist", "_profile": {
                    "age": 40, "sex": "male", "height_cm": 180,
                    "weight_kg": 81, "conditions": ["Гипертония"],
                }},
            ))
            self.assertEqual(runtime["profile_analysis"]["derived_indicators"]["bmi"], 25.0)
            self.assertEqual(
                runtime["profile_analysis"]["available_fields"]["conditions"],
                ["Гипертония"],
            )
            self.assertIn("аллергии", runtime["profile_analysis"]["missing_fields"])
            self.assertIn("Не переписывай все ответы подряд", PROFILES["therapist"].prompt)
        finally:
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_runtime_context_marks_questions_that_must_not_be_repeated(self):
        context = normalize_context({
            "current_topic": "кашель",
            "answered_questions": ["Когда начался кашель? — три дня назад"],
            "open_questions": ["Есть ли одышка?"],
        })
        history = [
            {
                "role": "assistant", "agent_id": "therapist",
                "content": "Когда начался кашель? Есть ли температура?",
                "metadata": {
                    "missing_information": [
                        "Когда начался кашель?", "Есть ли температура?",
                    ],
                },
            },
            {"role": "user", "content": "Три дня назад, температуры нет."},
        ]
        runtime = json.loads(LLMService.runtime_context(
            history, context, {"active_agent": "therapist", "_profile": {}},
        ))
        continuity = runtime["dialogue_continuity"]
        self.assertEqual(
            continuity["questions_already_asked"],
            ["Когда начался кашель?", "Есть ли температура?"],
        )
        self.assertIn("три дня назад", continuity["questions_already_answered"][0])
        self.assertEqual(continuity["questions_still_open"], ["Есть ли одышка?"])

    def test_other_person_profile_request_uses_normal_router(self):
        fake = FakeLLM()
        ConversationOrchestrator(fake).process(
            None, "Проанализируй медицинскую анкету моего ребёнка"
        )
        self.assertEqual(len(fake.route_calls), 1)

    def test_new_topic_drops_stale_questions_and_keeps_topic_history(self):
        previous = normalize_context({
            "current_topic": "головная боль",
            "known_facts": ["Боль второй день"],
            "answered_questions": ["Когда началась боль?"],
            "open_questions": ["Есть ли тошнота?"],
            "red_flags_checked": ["Потери сознания нет"],
        })
        current = normalize_context(previous)
        current.update({
            "current_topic": "сыпь на руке",
            "topic_relation": "new",
            "user_goal": "понять причину сыпи",
            "known_facts": ["Боль второй день", "Сыпь появилась сегодня"],
        })
        transitioned = ConversationOrchestrator._apply_topic_transition(previous, current)
        self.assertEqual(transitioned["topic_history"], ["головная боль"])
        self.assertEqual(transitioned["known_facts"], ["Сыпь появилась сегодня"])
        self.assertEqual(transitioned["answered_questions"], [])
        self.assertEqual(transitioned["open_questions"], [])
        self.assertEqual(transitioned["red_flags_checked"], [])

    def test_manager_explains_tube_number_results_flow(self):
        manager_prompt = PROFILES["manager"].prompt
        self.assertIn("«Результаты анализов» находятся в меню функций", manager_prompt)
        self.assertIn("не просит номер", manager_prompt)
        self.assertIn("повторно", manager_prompt)
        self.assertIn("ищет документ в after_tests_db по med_id", manager_prompt)
        self.assertIn("относятся к manager и интерфейсу", ORCHESTRATOR_PROMPT)
        self.assertIn("current_device", manager_prompt)
        self.assertIn("На экран Домой", manager_prompt)
        self.assertIn("Установить приложение", manager_prompt)
        self.assertIn("ярлыке, рабочем столе", ORCHESTRATOR_PROMPT)

    def test_agents_can_explain_messenger_linking_with_current_status(self):
        manager_prompt = PROFILES["manager"].prompt
        self.assertIn("«Привязать мессенджер» находится в меню функций", manager_prompt)
        self.assertIn("Telegram ID, MAX ID, код или пароль", manager_prompt)
        self.assertIn("сохраняется тот же профиль", manager_prompt)
        self.assertIn("messenger_access", manager_prompt)
        self.assertIn("Вопросы о привязке, входе или восстановлении", ORCHESTRATOR_PROMPT)
        runtime = json.loads(LLMService.runtime_context([], {}, {
            "_messenger_access": {
                "is_anonymous": False,
                "linked_providers": ["telegram"],
                "available_providers": ["telegram", "max"],
            },
        }))
        self.assertEqual(runtime["messenger_access"]["linked_providers"], ["telegram"])
        self.assertEqual(runtime["messenger_access"]["available_providers"], ["telegram", "max"])

    def test_user_language_and_supported_topics_are_clear(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        agents_script = (project_root / "static" / "agents.js").read_text(encoding="utf-8")
        manager_script = (project_root / "static" / "manager.js").read_text(encoding="utf-8")
        public_ui = index + script

        self.assertIn("Я Ольга, ваш медицинский помощник", script)
        self.assertIn("Ольга", index)
        self.assertIn("Медицинский помощник", index)
        self.assertIn("name: 'Ольга'", agents_script)
        self.assertIn("role: 'Медицинский помощник'", agents_script)
        self.assertIn("state.active = 'manager'", script)
        self.assertIn("$('#handoffBanner').classList.add('hidden')", script)
        self.assertIn("Ольга · Медицинский помощник", manager_script)
        for retired_name in ("Мария", "Ирина", "Дмитрий", "Анна", "Сергей", "Елена", "Максим"):
            self.assertNotIn(retired_name, agents_script)
        for retired_role in ("ИИ-менеджер", "Терапевт", "Кардиолог", "Невролог", "Дерматолог", "Педиатр", "Психолог"):
            self.assertNotIn(retired_role, agents_script)
        self.assertIn("Задавайте вопросы о здоровье, питании, спорте", script)
        self.assertNotIn("AI-оркестратор", public_ui)
        self.assertNotIn("Команда агентов", public_ui)
        self.assertNotIn("План проекта", public_ui)
        self.assertIn("Медицинский помощник", public_ui)
        self.assertNotIn('id="stateQuestions"', index)
        self.assertNotIn("Осталось вопросов", script)
        self.assertNotIn("Вопросы собраны", script)

        manager_prompt = PROFILES["manager"].prompt
        lifestyle_prompt = PROFILES["general"].prompt
        self.assertIn("медицине, симптомах, профилактике", manager_prompt)
        self.assertIn("спорте, физических нагрузках", manager_prompt)
        self.assertIn("работе самого сервиса", manager_prompt)
        self.assertIn("не решай постороннюю задачу", manager_prompt)
        self.assertIn("спорт, нагрузки, восстановление, сон, питание", ORCHESTRATOR_PROMPT)
        self.assertIn("respond + manager", ORCHESTRATOR_PROMPT)
        self.assertIn("Не выполняй задачи про программирование", lifestyle_prompt)

    def test_chat_attachment_button_uses_paperclip_icon(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        button = index.split('class="attach-button"', 1)[1].split('</button>', 1)[0]
        self.assertIn('id="attachButton"', button)
        self.assertIn('aria-label="Прикрепить файл"', button)
        self.assertIn('<svg viewBox="0 0 24 24"', button)
        self.assertNotIn('＋', button)
        self.assertIn('.attach-button svg', styles)

    def test_lab_result_helpers_normalize_med_id_and_extract_document_links(self):
        self.assertEqual(normalize_med_id(" 12345.0 "), "12345")
        self.assertEqual(normalize_med_id("LAB-2026_42"), "LAB-2026_42")
        self.assertEqual(
            extract_urls("https://example.test/a.pdf и https://docs.google.com/document/d/abc"),
            ("https://example.test/a.pdf", "https://docs.google.com/document/d/abc"),
        )
        with self.assertRaises(ValueError):
            normalize_med_id("../небезопасно")
        documents = lab_result_documents([
            "https://docs.google.com/document/d/document-123/edit",
            "https://drive.google.com/file/d/file-456/view",
        ])
        self.assertEqual(len(documents), 2)
        self.assertEqual(
            documents[0]["analysis_url"],
            "https://docs.google.com/document/d/document-123/export/pdf",
        )
        self.assertIn("export=download", documents[1]["analysis_url"])
        self.assertNotEqual(documents[0]["id"], documents[1]["id"])

    def test_text_results_request_asks_for_tube_without_calling_ai(self):
        chel_id = "chel_lab_without_tube"
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            fake = FakeLLM()
            response = ConversationOrchestrator(fake).process(
                None, "Как получить результаты анализов?"
            )
            self.assertEqual(response.action, "lab_results_prompt")
            self.assertIn("нужен номер пробирки", response.assistant_message["content"])
            self.assertEqual(fake.route_calls, [])
            self.assertEqual(fake.answer_calls, [])
        finally:
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_text_results_request_returns_saved_document_without_calling_ai(self):
        chel_id = "chel_lab_with_tube"
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            db.save_profile({"tube_number": "MED-42", "sex": "female"})
            fake = FakeLLM()
            with patch(
                "backend.orchestrator.lookup_lab_results",
                return_value=LabResult("MED-42", "found", ("https://example.test/result.pdf",)),
            ):
                response = ConversationOrchestrator(fake).process(
                    None, "Покажи мои результаты анализов"
                )
            self.assertEqual(response.action, "lab_results_found")
            self.assertIn("https://example.test/result.pdf", response.assistant_message["content"])
            self.assertEqual(
                response.assistant_message["metadata"]["lab_result_urls"],
                ["https://example.test/result.pdf"],
            )
            self.assertEqual(
                response.assistant_message["metadata"]["lab_result_documents"][0]["url"],
                "https://example.test/result.pdf",
            )
            self.assertEqual(fake.route_calls, [])
            self.assertEqual(fake.answer_calls, [])
        finally:
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_send_analyses_to_chat_phrase_returns_document_cards(self):
        chel_id = "chel_lab_send_to_chat"
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            db.save_profile({"tube_number": "MED-CHAT", "sex": "male"})
            fake = FakeLLM()
            with patch(
                "backend.orchestrator.lookup_lab_results",
                return_value=LabResult(
                    "MED-CHAT",
                    "found",
                    ("https://example.test/one.pdf", "https://example.test/two.pdf"),
                ),
            ):
                response = ConversationOrchestrator(fake).process(
                    None, "Пришли мои анализы в чат"
                )
            documents = response.assistant_message["metadata"]["lab_result_documents"]
            self.assertEqual(response.action, "lab_results_found")
            self.assertEqual(len(documents), 2)
            self.assertEqual(documents[1]["title"], "Результаты анализов · документ 2")
            self.assertEqual(fake.route_calls, [])
        finally:
            db.set_current_chel_id(chel_id)
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_lab_interpretation_uses_profile_and_persistent_cache(self):
        chel_id = "chel_lab_interpret_cache"
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            db.save_profile({
                "tube_number": "MED-CACHE",
                "sex": "female",
                "age": 42,
                "conditions": ["Гипертония"],
            })
            fake = FakeLLM()
            service = ConversationOrchestrator(fake)
            lab_result = LabResult(
                "MED-CACHE",
                "found",
                ("https://example.test/result-a.pdf", "https://example.test/result-b.pdf"),
            )
            with patch("backend.orchestrator.lookup_lab_results", return_value=lab_result):
                first = service.process(None, "Расшифруй мои анализы")
                second = service.interpret_lab_results(first.conversation_id, "all")

            self.assertEqual(first.action, "lab_interpretation")
            self.assertFalse(first.assistant_message["metadata"]["interpretation_cached"])
            self.assertTrue(second.assistant_message["metadata"]["interpretation_cached"])
            self.assertEqual(len(fake.interpret_calls), 1)
            self.assertEqual(fake.interpret_calls[0]["profile"]["age"], 42)
            self.assertEqual(len(fake.interpret_calls[0]["documents"]), 2)

            document_id = lab_result_documents(lab_result.urls)[0]["id"]
            with patch("backend.orchestrator.lookup_lab_results", return_value=lab_result):
                single = service.interpret_lab_results(first.conversation_id, document_id)
            self.assertEqual(len(fake.interpret_calls), 2)
            self.assertEqual(
                single.assistant_message["metadata"]["lab_result_urls"],
                ["https://example.test/result-a.pdf"],
            )

            db.save_profile({
                **db.get_profile(),
                "tube_number": "MED-CACHE",
                "age": 43,
            })
            with patch("backend.orchestrator.lookup_lab_results", return_value=lab_result):
                refreshed = service.interpret_lab_results(first.conversation_id, "all")
            self.assertFalse(refreshed.assistant_message["metadata"]["interpretation_cached"])
            self.assertEqual(len(fake.interpret_calls), 3)
        finally:
            db.set_current_chel_id(chel_id)
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_lab_interpretation_request_uses_file_urls_and_disables_provider_storage(self):
        service = LLMService()
        captured = {}

        def fake_request(payload):
            captured.update(payload)
            return {
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Готовая расшифровка"}],
                }],
            }

        with patch.object(service, "_request", side_effect=fake_request):
            answer = service.interpret_lab_results(
                {"age": 35, "sex": "male", "tube_number": "SECRET"},
                [{
                    "url": "https://example.test/view",
                    "analysis_url": "https://example.test/result.pdf",
                    "title": "Документ",
                }],
                scope_label="Один документ",
            )
        self.assertEqual(answer, "Готовая расшифровка")
        self.assertFalse(captured["store"])
        content = captured["input"][0]["content"]
        self.assertEqual(content[1]["type"], "input_file")
        self.assertEqual(content[1]["file_url"], "https://example.test/result.pdf")
        self.assertNotIn("filename", content[1])
        self.assertNotIn("file_id", content[1])
        self.assertNotIn("file_data", content[1])
        self.assertNotIn("SECRET", json.dumps(content, ensure_ascii=False))

    def test_ai_usage_cost_separates_cached_tokens_and_does_not_double_reasoning(self):
        record = usage_record({
            "model": "gpt-5.6-sol",
            "usage": {
                "input_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 200},
                "output_tokens": 100,
                "output_tokens_details": {"reasoning_tokens": 40},
                "total_tokens": 1100,
            },
        }, {
            "model": "gpt-5.6-sol",
            "text": {"format": {"name": "agent_result"}},
        }, "chel_cost_test")
        self.assertEqual(record["operation"], "agent_response")
        self.assertEqual(record["cached_input_tokens"], 200)
        self.assertEqual(record["reasoning_tokens"], 40)
        # 800 * $5/M + 200 * $0.50/M + 100 * $30/M.
        self.assertAlmostEqual(record["total_cost_usd"], 0.0071, places=9)

    def test_successful_openai_request_is_recorded_in_admin_costs(self):
        service = LLMService()
        response_payload = {
            "model": "gpt-5.6-luna",
            "usage": {
                "input_tokens": 500,
                "input_tokens_details": {"cached_tokens": 100},
                "output_tokens": 50,
                "output_tokens_details": {"reasoning_tokens": 20},
                "total_tokens": 550,
            },
            "output": [],
        }

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(response_payload).encode("utf-8")

        before = db.admin_ai_costs("all")["all_time"]["requests"]
        with (
            patch("backend.llm.settings", SimpleNamespace(openai_api_key="test-key")),
            patch("backend.llm.urllib.request.urlopen", return_value=FakeResponse()),
        ):
            service._request({
                "model": "gpt-5.6-luna",
                "text": {"format": {"name": "route_decision"}},
            })
        costs = db.admin_ai_costs("all")
        self.assertEqual(costs["all_time"]["requests"], before + 1)
        self.assertTrue(any(item["model"] == "gpt-5.6-luna" for item in costs["by_model"]))
        self.assertTrue(any(item["operation"] == "routing" for item in costs["by_operation"]))
        self.assertTrue(costs["pricing"])

    def test_gender_choices_are_limited_to_female_and_male(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        profile_select = index.split('id="profileSex"', 1)[1].split("</select>", 1)[0]
        self.assertIn('value="female"', profile_select)
        self.assertIn('value="male"', profile_select)
        self.assertNotIn('value="intersex"', profile_select)
        self.assertNotIn('value="other"', profile_select)
        sex_question = script.split("{ key:'sex'", 1)[1].split("},", 1)[0]
        self.assertNotIn("intersex", sex_question)
        self.assertNotIn("'other'", sex_question)

    def test_lab_documents_have_individual_and_combined_interpretation_controls(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("расшифровать по одному или все вместе", index)
        self.assertIn('data-lab-interpret="${escapeAttr(document.id)}"', script)
        self.assertIn('data-lab-interpret="all"', script)
        self.assertIn("/api/lab-results/interpret", script)
        self.assertIn(".lab-document-card", styles)
        self.assertIn(".lab-interpret-all", styles)
        self.assertIn("function labInterpretationMarkup", script)
        self.assertIn("lab-report-section", script)
        self.assertIn(".message-row.lab-interpretation", styles)

    def test_lab_interpretation_requests_minimum_profile_before_ai(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        backend_source = (project_root / "backend" / "main.py").read_text(encoding="utf-8")

        self.assertIn('id="interpretationProfileModal"', index)
        self.assertIn('id="interpretationProfileSex"', index)
        self.assertIn('id="interpretationProfileAge"', index)
        self.assertIn('id="interpretationProfileHeight"', index)
        self.assertIn('id="interpretationProfileWeight"', index)
        self.assertIn("function interpretationProfileComplete", script)
        self.assertIn("openInterpretationProfileModal('consultation');", script)
        self.assertIn("await openLabResults();", script)
        self.assertIn("await chooseHumanSpecialistChat();", script)
        self.assertNotIn("Сначала завершите короткую анкету", backend_source)
        self.assertIn(".interpretation-profile-modal", styles)
        self.assertEqual(
            ConsiliumHandler._interpretation_profile_missing({
                "sex": "female", "age": 40, "height_cm": 168, "weight_kg": 65,
            }),
            [],
        )
        self.assertEqual(
            ConsiliumHandler._interpretation_profile_missing({
                "preferred_name": "Полная анкета", "company_inn": "1234567890",
                "sex": "male", "age": 35, "height_cm": 195, "weight_kg": 109,
                "smoking": "current", "alcohol": "weekly", "activity": "medium",
            }),
            [],
        )
        self.assertEqual(
            ConsiliumHandler._interpretation_profile_missing({"sex": "male"}),
            ["age", "height_cm", "weight_kg"],
        )

    def test_regular_ai_chat_has_no_questionnaire_or_minimum_profile_gate(self):
        project_root = Path(__file__).resolve().parents[1]
        backend_source = (project_root / "backend" / "main.py").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")

        chat_route = backend_source.split('if path != "/api/chat":', 1)[1].split(
            "    def _create_max_auth_link", 1,
        )[0]
        process_message = script.split("async function processMessage(text)", 1)[1].split(
            "function addSystemError", 1,
        )[0]
        self.assertNotIn("get_onboarding", chat_route)
        self.assertNotIn("_interpretation_profile_missing", chat_route)
        self.assertNotIn("interpretationProfileComplete", process_message)
        self.assertEqual(script.count("if (!interpretationProfileComplete())"), 2)

    def test_ai_markdown_uses_shared_safe_rich_text_renderer(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        manager_html = (project_root / "manager.html").read_text(encoding="utf-8")
        app_script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        manager_script = (project_root / "static" / "manager.js").read_text(encoding="utf-8")
        rich_script = (project_root / "static" / "rich-text.js").read_text(encoding="utf-8")
        rich_styles = (project_root / "static" / "rich-text.css").read_text(encoding="utf-8")

        self.assertIn('/static/rich-text.js?v=20260829-interpret-profile-v1', index)
        self.assertIn('/static/rich-text.js?v=20260816-rich-text', manager_html)
        self.assertIn('window.ConsiliumRichText.render(value)', app_script)
        self.assertIn('window.ConsiliumRichText.render(value)', manager_script)
        self.assertIn('function renderMarkdown(value)', rich_script)
        self.assertIn('escapeHtml(code.join', rich_script)
        self.assertIn('rich-table-scroll', rich_script)
        self.assertIn('.rich-table-scroll', rich_styles)
        self.assertIn('overflow-x:auto', rich_styles)

    def test_layout_prevents_desktop_shell_and_focus_from_scrolling_outside_frame(self):
        project_root = Path(__file__).resolve().parents[1]
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        app_script = (project_root / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("grid-template-rows:minmax(0,1fr)", styles)
        self.assertIn(".app-shell > * { min-width: 0; min-height:0; }", styles)
        self.assertIn(".history-section { min-height:0; max-height:none; flex:1 1 auto;", styles)
        self.assertIn("grid-template-columns:28px minmax(0,1fr) minmax(32px,max-content)", styles)
        self.assertIn("min-width:32px; width:auto; height:24px; padding:0 6px", styles)
        self.assertIn("input.focus({ preventScroll: true })", app_script)

    def test_mobile_keyboard_dialog_history_and_back_navigation_are_stable(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("viewport-fit=cover", index)
        self.assertIn("interactive-widget=resizes-content", index)
        self.assertIn('input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="file"])', styles)
        self.assertIn("font-size:16px !important", styles)
        self.assertIn('id="mobileDialogsButton"', index)
        self.assertNotIn('id="sidebarDialogsTab"', index)
        self.assertNotIn('id="sidebarTeamTab"', index)
        self.assertNotIn('id="agentList"', index)
        self.assertIn('id="mobileConversationCount"', index)
        self.assertIn("--app-top: 0px", styles)
        self.assertIn("top:var(--app-top,0px)", styles)
        self.assertIn(".sidebar .conversation-list", styles)
        self.assertIn("window.visualViewport?.addEventListener('scroll'", script)
        self.assertIn("document.documentElement.style.setProperty('--app-top'", script)
        self.assertIn("function closeTopUiLayer()", script)
        self.assertIn("window.addEventListener('popstate'", script)
        self.assertIn("Закрыть Консилиум?", script)
        self.assertIn("history.pushState", script)
        self.assertIn("items.map(item =>", script)
        self.assertIn("function conversationSummary(item)", script)
        self.assertNotIn("items.slice(0, 8)", script)
        escape_handler = script.split("document.addEventListener('keydown', event => {", 1)[1].split("});", 1)[0]
        self.assertIn("closeTopUiLayer();", escape_handler)

    def test_user_ui_hides_agent_roster_and_advanced_council_actions(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        main_source = (project_root / "backend" / "main.py").read_text(encoding="utf-8")
        prompts = (project_root / "backend" / "prompts.py").read_text(encoding="utf-8")

        self.assertNotIn('id="agentList"', index)
        self.assertNotIn('id="secondOpinionButton"', index)
        self.assertNotIn('id="councilButton"', index)
        self.assertNotIn('id="councilModal"', index)
        self.assertNotIn("Нужен API-ключ", index + script)
        self.assertNotIn("Укажите OPENAI_API_KEY", index + script)
        self.assertNotIn('/api/second-opinion', main_source)
        self.assertNotIn('/api/council', main_source)
        self.assertNotIn("«Второе мнение»", prompts)
        self.assertIn('id="capabilityExaminations"', index)
        self.assertIn("Выбор и оплата дополнительных обследований", index)
        self.assertIn("openExaminationsFromCapabilities", script)
        self.assertIn("state.returnToChatAfterExaminations = true", script)
        self.assertIn("function renderCurrentExamSelectionSummary()", script)
        self.assertIn("У вас выбраны обследования", script)
        self.assertIn('data-onboarding-action="edit-current-exams"', script)
        self.assertIn('data-onboarding-action="close-current-exams"', script)
        self.assertIn("if (returnDirectlyToChat) return openMainApp({ skipIntro:true })", script)
        self.assertIn("editProfileFromChatExamFlow", script)

    def test_admin_dashboard_token_is_required_and_compared_exactly(self):
        expected = "dashboard-secret-" + ("x" * 32)
        self.assertTrue(admin_token_valid(f"Bearer {expected}", expected))
        self.assertFalse(admin_token_valid(f"Bearer {expected}x", expected))
        self.assertFalse(admin_token_valid(expected, expected))
        self.assertFalse(admin_token_valid("Bearer anything", ""))

    def test_admin_dashboard_aggregates_without_medical_content(self):
        data = db.admin_dashboard(days=7, limit=20)
        self.assertEqual(len(data["activity"]), 7)
        self.assertFalse(data["privacy"]["message_content_included"])
        self.assertFalse(data["privacy"]["medical_profile_content_included"])
        self.assertIn("users_total", data["summary"])
        self.assertIn("human_pending", data["summary"])
        self.assertIn("tracked_devices", data["summary"])
        self.assertIn("devices", data)
        self.assertEqual(
            set(data["tables"]),
            {"users", "conversations", "human_requests"},
        )
        user_fields = set(data["tables"]["users"][0]) if data["tables"]["users"] else set()
        conversation_fields = (
            set(data["tables"]["conversations"][0])
            if data["tables"]["conversations"] else set()
        )
        for forbidden in {"content", "conditions", "medications", "allergies", "notes"}:
            self.assertNotIn(forbidden, user_fields)
            self.assertNotIn(forbidden, conversation_fields)

    def test_pending_link_and_first_screen_view_are_not_counted_as_users(self):
        pending_browser_id = "chel_pending_browser_choice"
        db.ensure_user(pending_browser_id, pending=True)
        messenger_login = db.create_messenger_login("max", "pending-link-user", legacy_chel_id=88001)
        try:
            self.assertEqual(
                db.admin_table("users", "pending_browser_choice", limit=10)["total"],
                0,
            )
            self.assertEqual(
                db.admin_table("users", messenger_login["chel_id"], limit=10)["total"],
                0,
            )

            db.set_current_chel_id(pending_browser_id)
            registered = db.mark_current_user_registered("anonymous")
            self.assertEqual(registered["registration_method"], "anonymous")
            self.assertTrue(registered["registered_at"])
            self.assertEqual(
                db.admin_table("users", "pending_browser_choice", limit=10)["total"],
                1,
            )

            db.set_current_chel_id(messenger_login["chel_id"])
            db.mark_current_user_registered("max")
            self.assertEqual(
                db.admin_table("users", messenger_login["chel_id"], limit=10)["total"],
                1,
            )
        finally:
            for chel_id in (pending_browser_id, messenger_login["chel_id"]):
                db.set_current_chel_id(chel_id)
                db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

        project_root = Path(__file__).resolve().parents[1]
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        main_source = (project_root / "backend" / "main.py").read_text(encoding="utf-8")
        self.assertIn("/api/register-choice", script)
        self.assertIn('db.mark_current_user_registered(provider)', main_source)
        self.assertIn('db.ensure_user(candidate, pending=True, from_manager=from_manager)', main_source)

    def test_manager_attribution_is_immutable_and_counts_examination_selection(self):
        users = (
            ("chel_attr_manager1_a", "manager_1", True),
            ("chel_attr_manager1_b", "manager_1", False),
            ("chel_attr_manager2_a", "manager_2", True),
        )
        try:
            for chel_id, manager, selected in users:
                db.ensure_user(chel_id, pending=True, from_manager=manager)
                db.set_current_chel_id(chel_id)
                db.mark_current_user_registered("anonymous")
                if selected:
                    db.save_onboarding(status="payment", selected_tests=["lipids"])

            db.ensure_user(
                "chel_attr_manager1_a", pending=True, from_manager="another_manager",
            )
            with db.connection() as conn:
                stored = conn.execute(
                    "SELECT from_manager FROM users WHERE chel_id = ?",
                    ("chel_attr_manager1_a",),
                ).fetchone()[0]
            self.assertEqual(stored, "manager_1")

            report = db.admin_manager_attribution("all")
            managers = {item["from_manager"]: item for item in report["managers"]}
            self.assertEqual(managers["manager_1"]["users"], 2)
            self.assertEqual(managers["manager_1"]["manager_name"], "Красильникова")
            self.assertEqual(managers["manager_1"]["users_with_examinations"], 1)
            self.assertEqual(managers["manager_1"]["examination_conversion"], 50.0)
            self.assertEqual(managers["manager_2"]["users"], 1)
            self.assertEqual(managers["manager_2"]["manager_name"], "Аминева")
            self.assertEqual(managers["manager_2"]["users_with_examinations"], 1)
            self.assertEqual(db.from_manager_name("manager_10"), "Черниговцев")
            self.assertEqual(db.from_manager_name("custom_source"), "custom_source")

            table = db.admin_table("users", "manager_1", limit=10)
            self.assertEqual(table["total"], 2)
            self.assertTrue(all(row["from_manager"] == "manager_1" for row in table["rows"]))
        finally:
            for chel_id, _, _ in users:
                db.set_current_chel_id(chel_id)
                db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_admin_table_searches_full_allowlisted_views(self):
        chel_id = "chel_dashboard_search_1234"
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            conversation = db.create_conversation("Скрытый медицинский заголовок")
            db.add_message(
                conversation["id"], "user",
                "Этот медицинский текст не должен попасть в дашборд",
            )
            users = db.admin_table("users","dashboard_search",limit=10)
            conversations = db.admin_table(
                "conversations",conversation["id"][:12],limit=10,
            )
            self.assertEqual(users["total"],1)
            self.assertEqual(users["rows"][0]["chel_id"],chel_id)
            self.assertEqual(conversations["total"],1)
            self.assertNotIn("content",conversations["rows"][0])
            self.assertNotIn("title",conversations["rows"][0])
            self.assertEqual(db.admin_table("users","%")["total"],0)
            with self.assertRaises(ValueError):
                db.admin_table("messages","")
        finally:
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_admin_users_table_filters_registration_dates_and_returns_counts(self):
        january_id = "chel_date_filter_january"
        february_id = "chel_date_filter_february"
        db.ensure_user(january_id)
        db.ensure_user(february_id)
        try:
            db.set_current_chel_id(january_id)
            january_conversation = db.create_conversation("Январский диалог")
            db.add_message(january_conversation["id"], "user", "Одно сообщение")
            db.set_current_chel_id(february_id)
            february_conversation = db.create_conversation("Февральский диалог")
            for index in range(3):
                db.add_message(february_conversation["id"], "user", f"Сообщение {index}")
            with db.connection() as conn:
                conn.execute(
                    "UPDATE users SET created_at = ?, registered_at = ? WHERE chel_id = ?",
                    ("2099-01-15T10:00:00+00:00", "2099-01-15T10:00:00+00:00", january_id),
                )
                conn.execute(
                    "UPDATE users SET created_at = ?, registered_at = ? WHERE chel_id = ?",
                    ("2099-02-20T10:00:00+00:00", "2099-02-20T10:00:00+00:00", february_id),
                )
                conn.commit()

            result = db.admin_table(
                "users", "date_filter", limit=10,
                created_from="2099-02-01", created_to="2099-02-28",
            )
            self.assertGreaterEqual(result["overall_total"], 2)
            self.assertEqual(result["period_total"], 1)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["rows"][0]["chel_id"], february_id)
            self.assertEqual(result["created_from"], "2099-02-01")
            self.assertEqual(result["created_to"], "2099-02-28")

            created_ascending = db.admin_table(
                "users", "date_filter", limit=10, sort="created_at", order="asc",
            )
            created_descending = db.admin_table(
                "users", "date_filter", limit=10, sort="created_at", order="desc",
            )
            self.assertEqual(created_ascending["rows"][0]["chel_id"], january_id)
            self.assertEqual(created_descending["rows"][0]["chel_id"], february_id)
            self.assertEqual(created_ascending["sort"], "created_at")
            self.assertEqual(created_ascending["order"], "asc")
            messages_ascending = db.admin_table(
                "users", "date_filter", limit=10, sort="messages", order="asc",
            )
            messages_descending = db.admin_table(
                "users", "date_filter", limit=10, sort="messages", order="desc",
            )
            self.assertEqual(messages_ascending["rows"][0]["chel_id"], january_id)
            self.assertEqual(messages_descending["rows"][0]["chel_id"], february_id)
            with self.assertRaises(ValueError):
                db.admin_table("users", sort="messages", order="sideways")

            no_search_match = db.admin_table(
                "users", "january", limit=10,
                created_from="2099-02-01", created_to="2099-02-28",
            )
            self.assertEqual(no_search_match["period_total"], 1)
            self.assertEqual(no_search_match["total"], 0)
            with self.assertRaises(ValueError):
                db.admin_table(
                    "users", created_from="2099-03-01", created_to="2099-02-01",
                )
        finally:
            with db.connection() as conn:
                conn.execute(
                    "DELETE FROM users WHERE chel_id IN (?, ?)",
                    (january_id, february_id),
                )
                conn.commit()
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_device_statistics_classify_and_aggregate_page_openings(self):
        chel_id = "chel_device_stats_1234"
        android_ua = (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "Chrome/126.0.0.0 Mobile Safari/537.36"
        )
        iphone_ua = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
            "AppleWebKit/605.1.15 Version/17.5 Mobile/15E148 Safari/604.1"
        )
        windows_ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36 Edg/126.0"
        )
        self.assertEqual(db.classify_user_agent(iphone_ua)["device_type"], "ios")
        self.assertEqual(db.classify_user_agent(windows_ua)["browser"], "Edge")
        self.assertEqual(db.classify_user_agent("Googlebot/2.1")["device_type"], "bot")
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            db.record_device_access(android_ua)
            db.record_device_access(android_ua)
            db.record_device_access(windows_ua)
            devices = db.admin_table("devices", chel_id, limit=10)
            self.assertEqual(devices["total"], 2)
            android = next(
                item for item in devices["rows"] if item["device_type"] == "android"
            )
            self.assertEqual(android["operating_system"], "Android")
            self.assertEqual(android["browser"], "Chrome")
            self.assertEqual(android["visit_count"], 2)
            self.assertEqual(db.current_device()["operating_system"], "Windows")
            dashboard = db.admin_dashboard(days=7)
            self.assertTrue(any(
                item["operating_system"] == "Android"
                for item in dashboard["operating_systems"]
            ))
            self.assertTrue(any(
                item["browser"] == "Edge" for item in dashboard["browsers"]
            ))
            self.assertIsNone(db.record_device_access("Googlebot/2.1"))
        finally:
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_dashboard_files_exist_without_user_menu_entry(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        dashboard = (project_root / "dashboard.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "dashboard.js").read_text(encoding="utf-8")
        styles = (project_root / "static" / "dashboard.css").read_text(encoding="utf-8")
        self.assertNotIn('id="menuDashboardButton"', index)
        self.assertNotIn('id="menuMemoryButton"', index)
        self.assertNotIn('id="mobileTeamButton"', index)
        self.assertIn('id="menuProfileButton"', index)
        self.assertIn('id="profileMemoryTitle"', index)
        self.assertIn('id="memoryList"', index)
        self.assertIn("Мои данные", index)
        self.assertIn('id="summaryGrid"', dashboard)
        self.assertIn('id="usersTable"', dashboard)
        self.assertIn('id="usersSearch"', dashboard)
        self.assertIn('id="usersTotalCount"', dashboard)
        self.assertIn('id="usersPeriodCount"', dashboard)
        self.assertIn('id="usersPeriod"', dashboard)
        self.assertIn('id="usersDateFrom"', dashboard)
        self.assertIn('id="usersDateTo"', dashboard)
        self.assertIn('id="conversationsNext"', dashboard)
        self.assertIn('id="deviceDistribution"', dashboard)
        self.assertIn('id="osDistribution"', dashboard)
        self.assertIn('id="browserDistribution"', dashboard)
        self.assertIn('id="devicesTable"', dashboard)
        self.assertIn("renderDevices", script)
        self.assertIn("created_from", script)
        self.assertIn("Новых за период", script)
        self.assertIn('id="managerCreateForm"', dashboard)
        self.assertIn('id="examinationsTab"', dashboard)
        self.assertIn('id="costsTab"', dashboard)
        self.assertIn('id="favoritesTab"', dashboard)
        self.assertIn('id="favoritesAdminView"', dashboard)
        self.assertIn('id="favoritesGrid"', dashboard)
        self.assertIn('id="metric2Tab"', dashboard)
        self.assertIn('id="metric2AdminView"', dashboard)
        self.assertIn('id="metric2Flow"', dashboard)
        self.assertIn('id="metric2StandardFlow"', dashboard)
        self.assertIn('id="metric2ResultFlow"', dashboard)
        self.assertIn('id="metric2Modal"', dashboard)
        self.assertIn('id="analyticsDateFrom" type="date"', dashboard)
        self.assertIn('id="analyticsDateTo" type="date"', dashboard)
        self.assertIn('id="paymentAnalyticsSummary"', dashboard)
        self.assertIn('id="paymentStatusDistribution"', dashboard)
        self.assertIn('id="paymentRecentTable"', dashboard)
        self.assertIn('id="metric2DateFrom" type="date"', dashboard)
        self.assertIn('id="metric2DateTo" type="date"', dashboard)
        self.assertIn("/api/admin/metric2", script)
        self.assertIn("function appendDateRange", script)
        self.assertIn("params.set('date_from', dateFrom)", script)
        self.assertIn("function metric2PreviewMarkup", script)
        self.assertIn("function openMetric2Screen", script)
        self.assertIn("flow:metric2ActiveFlow", script)
        self.assertIn("row.dataset.metric2Screen = item.screen_id", script)
        self.assertIn("screenLink.dataset.metric2Screen", script)
        self.assertIn("metric2-route-note", script)
        self.assertIn("item.explanation", script)
        self.assertIn(".metric2-screen-row", styles)
        self.assertIn(".metric2-flow-tabs", styles)
        self.assertIn(".metric2-modal-layout", styles)
        self.assertIn(".metric2-transition-row:hover", styles)
        self.assertIn('id="costSummaryGrid"', dashboard)
        self.assertIn('id="costModelsTable"', dashboard)
        self.assertIn('id="examinationForm"', dashboard)
        self.assertIn('id="examinationName"', dashboard)
        self.assertIn('id="examinationDescription"', dashboard)
        self.assertIn('id="examinationPrice"', dashboard)
        self.assertIn('id="examinationList"', dashboard)
        self.assertIn('id="staffList"', dashboard)
        self.assertIn('id="staffPassword" type="text" minlength="6"', dashboard)
        self.assertIn('id="staffTelegramId"', dashboard)
        self.assertIn('id="staffMaxId"', dashboard)
        self.assertIn('id="staffNotifyRequests"', dashboard)
        self.assertIn('id="userDataCleanupForm"', dashboard)
        self.assertIn('id="userDataCleanupId"', dashboard)

        self.assertNotIn('id="deleteMyDataButton"', dashboard)
        self.assertIn("/api/admin/users/delete-data", script)
        self.assertNotIn("/api/admin/my-user-id", script)
        self.assertIn("X-Consilium-Action':'delete-user-data'", script)
        self.assertNotIn('data-staff-action="cleanup-user"', script)
        self.assertIn("ID менеджера:", script)
        self.assertIn('data-staff-action="telegram-link"', script)
        self.assertIn('data-staff-action="max-link"', script)
        self.assertIn('class="staff-card-content"', script)
        self.assertIn('class="staff-channel-row"', script)
        self.assertIn('class="staff-card-actions"', script)
        self.assertIn(".staff-card-content { display:grid", styles)
        self.assertIn("@media (max-width:520px)", styles)
        self.assertIn('class="panel funnel-panel"', dashboard)
        self.assertIn(".funnel-panel .panel-heading { align-items:stretch; flex-direction:column; }", styles)
        self.assertIn(".funnel-row .funnel-label { grid-column:1/-1; width:100%; }", styles)
        self.assertIn(".funnel-mode-tabs { display:grid;", styles)
        self.assertIn("sessionStorage", script)
        self.assertIn("Authorization:`Bearer ${token || ''}`", script)
        self.assertIn("/api/admin/table", script)
        self.assertIn('data-table-sort="created_at"', dashboard)
        self.assertIn('data-table-sort="messages"', dashboard)
        self.assertIn("function updateTableSortIndicators(state)", script)
        self.assertIn("sort:state.sort,order:state.order", script)
        self.assertIn(".table-sort", styles)
        self.assertIn("/api/admin/managers", script)
        self.assertIn("/api/admin/examinations", script)
        self.assertIn("/api/admin/ai-costs", script)
        self.assertIn("renderCostSummary", script)
        self.assertIn("function renderPaymentAnalytics", script)
        self.assertIn("FAVORITES_KEY", script)
        self.assertIn("function decorateFavoriteSources()", script)
        self.assertIn("function renderFavorites()", script)
        self.assertIn("loadFavoriteSources", script)
        self.assertIn("data-favorite-toggle", script)
        self.assertIn(".dashboard.show-favorites", styles)
        self.assertIn(".favorites-grid", styles)
        self.assertIn("let dashboardLoading = false", script)
        self.assertIn("if (dashboardLoading) return", script)
        self.assertIn("response.status === 429 && method === 'GET'", script)
        self.assertIn("retryAttempt < 3", script)
        self.assertIn("Promise.all(Object.keys(tableStates).map(key => loadTable(key)))", script)
        self.assertIn("loadAdminViewData(activeAdminView)", script)
        self.assertIn("adminGetRequests", script)
        self.assertIn("admin-panel-loader", script)
        self.assertIn(".admin-panel-loader", styles)
        self.assertIn('id="adminTopProgress"', dashboard)
        self.assertIn("function setTopProgress", script)
        self.assertIn(".admin-top-progress", styles)
        self.assertNotIn("setInterval(() => loadDashboard(),60000)", script)
        self.assertNotIn("автообновление раз в минуту", script)
        self.assertIn("следующее обновление только по кнопке", script)
        self.assertIn("const loadedAdminViews = new Set()", script)
        self.assertIn("if (!force && loadedAdminViews.has(view)) return", script)
        self.assertIn("loadAdminViewData(activeAdminView, {force:true})", script)
        self.assertIn("if (sessionStorage.getItem(TOKEN_KEY))", script)
        self.assertLess(
            script.index("sessionStorage.setItem(TOKEN_KEY,token);", script.index("async function loadDashboard")),
            script.index("loadAdminViewData(activeAdminView, {force:true})", script.index("async function loadDashboard")),
        )
        self.assertIn("saveExamination", script)
        self.assertIn("manageExamination", script)
        self.assertIn('data-staff-action="delete"', script)
        self.assertIn("method:'DELETE'", script)
        self.assertIn("@media (max-width:700px)", styles)

    def test_metric2_preview_copy_and_controls_match_real_onboarding(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        app = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        script = (project_root / "static" / "dashboard.js").read_text(encoding="utf-8")
        analytics_source = (project_root / "backend" / "analytics.py").read_text(encoding="utf-8")
        main_source = (project_root / "backend" / "main.py").read_text(encoding="utf-8")

        questions = app.split("const onboardingQuestions = [", 1)[1].split("];", 1)[0]
        for field in ("title", "lead", "placeholder"):
            for value in re.findall(rf"{field}:'((?:\\'|[^'])*)'", questions):
                self.assertIn(value, script, f"Метрика 2.0 не повторяет {field}: {value}")
        for label in re.findall(r"\['[^']+','([^']+)'\]", questions):
            self.assertIn(label, script, f"Нет варианта ответа в превью: {label}")

        for text in (
            "Продолжить с Telegram", "Продолжить с MAX", "Подтверждение через бота",
            "Войти анонимно", "Понимаю, продолжить", "Какой размер текста вам удобен?",
            "Я не на мед-осмотр", "Посмотреть описания чек-апов",
            "Да, выбрать анализы", "Нет, не сейчас", "Изменить ответы анкеты",
            "Выбрать обследования", "Всё равно отказаться", "Ничего не выбирать",
            "Оплатить онлайн", "Оплатить на медосмотре", "Понятно",
            "Привязать мессенджер", "Установить приложение", "Установлю позже",
            "Перейти в Консилиум",
        ):
            self.assertIn(text, index + app, f"Кнопка отсутствует в приложении: {text}")
            self.assertIn(text, script, f"Кнопка отсутствует в Метрике 2.0: {text}")

        self.assertIn("trackOnboardingAction('select_option'", app)
        self.assertIn('"id": "select_option"', analytics_source)
        self.assertIn('"label": "Выбор варианта ответа"', analytics_source)
        self.assertIn("report[\"examinations\"] = db.list_examinations()", main_source)
        self.assertNotIn("Краткое пояснение к вопросу", script)
        self.assertNotIn("Вариант ответа", script)

        screens = {item["id"]: item for item in analytics._metric2_screen_definitions()}
        inn_actions = {item["id"] for item in screens["question_company_inn"]["actions"]}
        notes_actions = {item["id"] for item in screens["question_notes"]["actions"]}
        self.assertEqual(inn_actions, {"answer", "not_medical_exam"})
        self.assertIn("skip", notes_actions)
        self.assertIn("screen.id === 'question_company_inn'", script)
        self.assertIn("action.id !== 'skip'", script)
        skipped_completion = screens["completion_skipped"]
        self.assertEqual(skipped_completion["parent_id"], "exam_objection")
        self.assertTrue(skipped_completion["branch"])
        self.assertTrue(skipped_completion["display_as_main"])
        self.assertIn("screen.branch && !screen.display_as_main", script)
        self.assertEqual(
            {item["id"] for item in skipped_completion["actions"]},
            {"install", "continue", "link_messenger"},
        )
        self.assertIn("payment_processing", screens)
        self.assertIn("payment_success", screens)
        self.assertIn("payment_result", screens)
        self.assertEqual(
            {item["id"] for item in screens["payment_result"]["actions"]},
            {"retry", "purchases", "back"},
        )
        self.assertIn(screens["payment"]["actions"][0]["target"], {"payment_processing", "payment_unavailable"})

    def test_manager_messenger_api_and_deep_link_are_present(self):
        project_root = Path(__file__).resolve().parents[1]
        main_source = (project_root / "backend" / "main.py").read_text(encoding="utf-8")
        manager_script = (project_root / "static" / "manager.js").read_text(encoding="utf-8")
        self.assertIn('/api/bot/manager-bind', main_source)
        self.assertIn('/api/bot/manager-notifications', main_source)
        self.assertIn('/messenger-link', main_source)
        self.assertIn("get('conversation')", manager_script)

    def test_admin_can_create_authenticate_and_disable_manager(self):
        with self.assertRaises(ValueError):
            db.admin_create_staff("Короткий пароль", "short.pass", "12345")
        created = db.admin_create_staff("Ольга Иванова", "olga.test", "123456")
        self.assertEqual(created["login"], "olga.test")
        self.assertNotIn("password_hash", created)
        self.assertIsNone(db.authenticate_staff("olga.test", "wrong-password"))

        authenticated = db.authenticate_staff("OLGA.TEST", "123456")
        self.assertEqual(authenticated["user"]["display_name"], "Ольга Иванова")
        self.assertEqual(
            db.get_staff_session(authenticated["token"])["login"], "olga.test",
        )

        updated = db.admin_update_staff(
            created["id"], display_name="Ольга", password="654321",
        )
        self.assertEqual(updated["display_name"], "Ольга")
        self.assertIsNone(db.get_staff_session(authenticated["token"]))
        next_login = db.authenticate_staff("olga.test", "654321")
        self.assertIsNotNone(next_login)

        disabled = db.admin_update_staff(created["id"], is_active=False)
        self.assertFalse(disabled["is_active"])
        self.assertIsNone(db.get_staff_session(next_login["token"]))
        self.assertIsNone(db.authenticate_staff("olga.test", "654321"))
        self.assertTrue(db.admin_delete_staff(created["id"]))
        self.assertFalse(db.admin_delete_staff(created["id"]))
        self.assertFalse(any(
            item["id"] == created["id"] for item in db.admin_list_staff()
        ))

    def test_manager_messenger_binding_and_notification_outbox(self):
        manager = db.admin_create_staff(
            "Ирина Уведомления", "irina.notify", "123456",
            notify_new_requests=True, notify_new_messages=True,
        )
        binding_token = db.create_staff_messenger_token(manager["id"], "telegram")
        binding = db.bind_staff_messenger(
            binding_token["token"], "telegram", "112233", "112233",
        )
        self.assertEqual(binding["staff_id"], manager["id"])
        with self.assertRaises(ValueError):
            db.bind_staff_messenger(
                binding_token["token"], "telegram", "112233", "112233",
            )

        chel_id = f"chel_notify_{manager['id']}"
        db.ensure_user(chel_id)
        db.set_current_chel_id(chel_id)
        try:
            conversation = db.create_conversation("Уведомления")
            db.update_conversation(
                conversation["id"], active_agent="manager", context_summary="{}",
                status="waiting_human", human_status="pending",
                human_ticket_id="H-NOTIFY", human_channel="chat",
            )
            self.assertEqual(
                db.enqueue_manager_notifications("new_request", conversation["id"]), 1,
            )
            self.assertEqual(
                db.enqueue_manager_notifications("new_request", conversation["id"]), 0,
            )
            notifications = db.claim_manager_notifications("telegram")
            notification = next(item for item in notifications if item["conversation_id"] == conversation["id"])
            self.assertEqual(notification["recipient_id"], "112233")
            self.assertIn("manager?conversation=", notification["payload"]["manager_url"])
            self.assertTrue(db.acknowledge_manager_notification(
                notification["id"], notification["lease_token"], True,
            ))
            self.assertEqual(
                db.enqueue_manager_notifications(
                    "new_message", conversation["id"], message_id=77,
                    message_text="Проверка отложенной повторной доставки",
                ),
                1,
            )
            retry_item = next(
                item for item in db.claim_manager_notifications("telegram")
                if item["conversation_id"] == conversation["id"]
            )
            self.assertTrue(db.acknowledge_manager_notification(
                retry_item["id"], retry_item["lease_token"], False, "bot not started",
            ))
            immediate = db.claim_manager_notifications("telegram")
            self.assertFalse(any(item["id"] == retry_item["id"] for item in immediate))
        finally:
            db.set_current_chel_id("chel_test_default")
            db.admin_delete_staff(manager["id"])

    def test_admin_can_create_update_and_delete_examinations(self):
        with self.assertRaises(ValueError):
            db.admin_create_examination("Т", "Коротко", "", -1)
        created = db.admin_create_examination(
            "Тестовый комплекс",
            "Описание тестового комплекса для проверки каталога.",
            "Анализ A, анализ B",
            2750,
        )
        self.assertTrue(created["id"].startswith("exam_"))
        self.assertEqual(created["price"], 2750)
        self.assertTrue(any(
            item["id"] == created["id"] for item in db.list_examinations()
        ))

        updated = db.admin_update_examination(
            created["id"],
            "Обновлённый комплекс",
            "Новое подробное описание обследования.",
            "Анализ C",
            3100,
        )
        self.assertEqual(updated["name"], "Обновлённый комплекс")
        self.assertEqual(updated["price"], 3100)
        payload = public_onboarding(
            {"status": "exams", "selected_tests": []},
            {},
            db.list_examinations(),
        )
        self.assertTrue(any(
            item["id"] == created["id"] for item in payload["tests"]
        ))

        self.assertTrue(db.admin_delete_examination(created["id"]))
        self.assertFalse(db.admin_delete_examination(created["id"]))
        self.assertFalse(any(
            item["id"] == created["id"] for item in db.list_examinations()
        ))

    def test_manager_can_pause_ai_read_context_and_reply_in_same_chat(self):
        chel_id = "chel_manager_flow_1234"
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            db.save_profile({
                "preferred_name": "Тестовый пользователь", "age": 38,
                "sex": "male", "conditions": ["Гипертония"],
                "medications": ["Назначенный препарат"], "allergies": ["Пыльца"],
                "tube_number": "MED-777",
            })
            db.save_onboarding(status="complete", selected_tests=["lipids"])
            db.add_body_symptom({
                "region": "Грудь", "symptom_type": "Боль", "intensity": 4,
                "notes": "После нагрузки",
            })
            related_conversation = db.create_conversation("Предыдущий диалог")
            db.add_message(related_conversation["id"], "user", "Старый вопрос")
            conversation = db.create_conversation("Нужна консультация")
            db.add_message(conversation["id"], "user", "Хочу поговорить с менеджером")
            db.update_conversation(
                conversation["id"], active_agent="manager", context_summary="{}",
                status="waiting_human", human_status="pending",
                human_ticket_id="H-MGR001", human_channel="chat",
            )

            paused = db.manager_set_ai_enabled(
                conversation["id"], False, "Ольга",
            )
            self.assertFalse(paused["ai_enabled"])
            queued, updated = db.add_user_message_waiting_for_manager(
                conversation["id"], "Жду ответа человека",
            )
            self.assertTrue(queued["metadata"]["awaiting_manager"])
            self.assertFalse(updated["ai_enabled"])

            reply = db.manager_add_reply(
                conversation["id"], "Здравствуйте, я подключилась к диалогу.", "Ольга",
            )
            self.assertEqual(reply["metadata"]["sender_type"], "human_manager")
            self.assertEqual(reply["metadata"]["manager_name"], "Ольга")
            detail = db.manager_conversation_detail(conversation["id"])
            self.assertEqual(detail["profile"]["preferred_name"], "Тестовый пользователь")
            self.assertEqual(detail["profile"]["tube_number"], "MED-777")
            self.assertEqual(detail["symptoms"][0]["region"], "Грудь")
            self.assertEqual(detail["conversation"]["human_status"], "connected")
            self.assertFalse(detail["conversation"]["ai_enabled"])
            expected_exam_name = next(
                item["name"] for item in db.list_examinations() if item["id"] == "lipids"
            )
            self.assertEqual(
                detail["onboarding"]["selected_test_names"], [expected_exam_name],
            )
            self.assertTrue(any(
                item["action"] == "ai_mode" for item in detail["manager_actions"]
            ))

            updates = db.list_messages_after(conversation["id"], queued["id"])
            self.assertEqual(updates[-1]["content"], "Здравствуйте, я подключилась к диалогу.")
            self.assertEqual(updates[-1]["metadata"]["sender_type"], "human_manager")

            queue = db.manager_list_conversations("Тестовый", "ai_off")
            self.assertEqual(queue[0]["id"], conversation["id"])
            self.assertGreaterEqual(queue[0]["unanswered_user_messages"], 0)
            self.assertEqual(
                db.manager_list_conversations("тестовый", "ai_off")[0]["id"],
                conversation["id"],
            )
            self.assertEqual(
                db.manager_list_conversations("ТЕСТОВЫЙ", "ai_off")[0]["id"],
                conversation["id"],
            )
            grouped_queue = db.manager_list_conversations(
                "", "open", include_related=True,
            )
            self.assertEqual(
                {item["id"] for item in grouped_queue},
                {conversation["id"], related_conversation["id"]},
            )
            grouped_search = db.manager_list_conversations(
                related_conversation["id"], "all", include_related=True,
            )
            self.assertEqual(
                {item["id"] for item in grouped_search},
                {conversation["id"], related_conversation["id"]},
            )

            closed = db.manager_close_conversation(conversation["id"], "Ольга")
            self.assertEqual(closed["human_status"], "closed")
            self.assertTrue(closed["ai_enabled"])
            self.assertNotIn(
                conversation["id"],
                [item["id"] for item in db.manager_list_conversations("", "open")],
            )
            self.assertIn(
                conversation["id"],
                [item["id"] for item in db.manager_list_conversations("", "all")],
            )
            closed_detail = db.manager_conversation_detail(conversation["id"])
            self.assertTrue(any(
                item["action"] == "close" for item in closed_detail["manager_actions"]
            ))
        finally:
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_manager_panel_and_user_mode_indicator_exist(self):
        project_root = Path(__file__).resolve().parents[1]
        manager = (project_root / "manager.html").read_text(encoding="utf-8")
        manager_script = (project_root / "static" / "manager.js").read_text(encoding="utf-8")
        app = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        index = (project_root / "index.html").read_text(encoding="utf-8")
        dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('id="requestList"', manager)
        self.assertIn('id="managerMessages"', manager)
        self.assertIn('id="profileDetails"', manager)
        self.assertIn('id="aiEnabled"', manager)
        self.assertIn('id="closeRequestButton"', manager)
        self.assertIn('id="managerLogin"', manager)
        self.assertIn('id="managerPassword"', manager)
        self.assertIn("/api/manager/login", manager_script)
        self.assertIn("/api/manager/me", manager_script)
        self.assertNotIn("ADMIN_DASHBOARD_TOKEN", manager_script)
        self.assertIn("/api/manager/conversations/", manager_script)
        self.assertIn("include_related:'1'", manager_script)
        self.assertIn("data-person-id", manager_script)
        self.assertIn("data-person-toggle", manager_script)
        self.assertIn("collapsedPeople: new Set()", manager_script)
        self.assertIn("state.collapsedPeople.add(chelId)", manager_script)
        self.assertIn("chel_id, диалог или обращение", manager)
        self.assertIn("/close", manager_script)
        self.assertIn("playManagerSignal('request')", manager_script)
        self.assertIn("playManagerSignal('message')", manager_script)
        self.assertIn("previous && !item.ai_enabled", manager_script)
        self.assertIn("sender_type === 'human_manager'", manager_script)
        self.assertIn('id="chatModeBanner"', index)
        self.assertIn('id="chatModeNewDialog"', index)
        self.assertIn('id="chatModeToggle"', index)
        self.assertIn('aria-controls="chatModeDetails"', index)
        self.assertIn('.chat-mode-banner.expanded .chat-mode-details', styles)
        self.assertIn("$('#chatModeBanner').addEventListener('click'", app)
        self.assertIn("$('#chatModeBanner').addEventListener('keydown'", app)
        self.assertIn("if (event.target.closest('#chatModeNewDialog')) return", app)
        self.assertIn('.chat-mode-banner.expandable { cursor:pointer', styles)
        self.assertIn("вы можете продолжить общение с ИИ в новом диалоге", app)
        self.assertIn("$('#chatModeNewDialog').addEventListener('click', newConversation)", app)
        self.assertNotIn("Я правильно понял?", index)
        self.assertNotIn('id="insightDock"', index)
        self.assertIn("Проверьте сведения обращения", index)
        self.assertIn('id="editHandoffContext"', app)
        self.assertIn("returnToHumanAfterContextEdit", app)
        self.assertIn("/updates?after_id=", app)
        self.assertIn("Сообщение ожидает ответа медицинского специалиста", app)
        self.assertIn("playUserMessageSound()", app)
        self.assertIn('id="mobileHeaderDialogsButton"', index)
        self.assertIn('id="mobileDialogsUnread"', index)
        self.assertIn("applyUnreadCounts", app)
        self.assertIn("markConversationRead", app)
        self.assertIn("/api/conversations/unread", app)
        self.assertIn(".mobile-header-dialogs", styles)
        self.assertIn(".conversation-row.unread", styles)
        self.assertIn("index.html dashboard.html manager.html", dockerfile)

    def test_conversation_unread_counts_are_persisted_and_scoped(self):
        db.ensure_user("chel_unread_test")
        db.set_current_chel_id("chel_unread_test")
        try:
            conversation = db.create_conversation("Проверка непрочитанных")
            self.assertEqual(db.conversation_unread_counts(), {})

            db.add_message(conversation["id"], "assistant", "Первый ответ", "manager")
            db.add_message(conversation["id"], "user", "Моё сообщение")
            self.assertEqual(db.conversation_unread_counts(), {conversation["id"]: 1})

            marked = db.mark_conversation_read(conversation["id"])
            self.assertGreater(marked["last_read_message_id"], 0)
            self.assertEqual(db.conversation_unread_counts(), {})

            db.add_message(conversation["id"], "assistant", "Новый ответ", "manager")
            db.add_message(conversation["id"], "assistant", "Ещё один ответ", "manager")
            self.assertEqual(db.conversation_unread_counts(), {conversation["id"]: 2})

            db.ensure_user("chel_unread_other")
            db.set_current_chel_id("chel_unread_other")
            self.assertIsNone(db.mark_conversation_read(conversation["id"]))
            self.assertEqual(db.conversation_unread_counts(), {})
        finally:
            db.set_current_chel_id("chel_unread_test")
            db.reset_current_user()
            db.set_current_chel_id("chel_unread_other")
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_chel_id_separates_profiles_conversations_memories_and_symptoms(self):
        first_id = "chel_test_first"
        second_id = "chel_test_second"
        db.ensure_user(first_id)
        db.ensure_user(second_id)
        try:
            db.set_current_chel_id(first_id)
            db.save_profile({"preferred_name": "Первый", "tube_number": "TUBE-1"})
            first_conversation = db.create_conversation("Диалог первого")
            db.add_memory("Любит короткие ответы")
            first_symptom = db.add_body_symptom({
                "region": "Голова", "symptom_type": "Боль", "intensity": 3,
            })

            db.set_current_chel_id(second_id)
            self.assertEqual(db.get_profile()["preferred_name"], "")
            self.assertEqual(db.list_conversations(), [])
            self.assertEqual(db.list_memories(), [])
            self.assertEqual(db.list_body_symptoms(), [])
            self.assertIsNone(db.get_conversation(first_conversation["id"]))
            self.assertFalse(db.delete_body_symptom(first_symptom["id"]))
            db.save_profile({"preferred_name": "Второй", "tube_number": "TUBE-2"})

            db.set_current_chel_id(first_id)
            self.assertEqual(db.get_profile()["preferred_name"], "Первый")
            self.assertEqual(db.get_profile()["tube_number"], "TUBE-1")
            self.assertEqual(len(db.list_conversations()), 1)
            self.assertEqual(len(db.list_memories()), 1)
            self.assertEqual(len(db.list_body_symptoms()), 1)
        finally:
            db.set_current_chel_id("chel_test_default")

    def test_reset_current_user_removes_all_owned_data_only(self):
        reset_id = "chel_reset_target"
        other_id = "chel_reset_other"
        db.ensure_user(reset_id)
        db.ensure_user(other_id)
        try:
            db.set_current_chel_id(reset_id)
            conversation = db.create_conversation("Перед сбросом")
            db.add_message(conversation["id"], "user", "Секретный текст")
            db.add_handoff(conversation["id"], "manager", "therapist", "Проверка")
            db.add_memory("Сохранённый факт")
            db.save_profile({"preferred_name": "Удалить", "tube_number": "TUBE-RESET"})
            db.save_onboarding(status="complete", selected_tests=["lipids"])
            db.add_body_symptom({
                "region": "Голова", "symptom_type": "Боль", "intensity": 4,
            })

            db.set_current_chel_id(other_id)
            other_conversation = db.create_conversation("Не удалять")
            db.save_profile({"preferred_name": "Оставить"})

            db.set_current_chel_id(reset_id)
            db.reset_current_user()

            self.assertFalse(db.user_exists(reset_id))
            self.assertTrue(db.user_exists(other_id))
            with db.connection() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE chel_id = ?", (reset_id,)
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation["id"],)
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM handoffs WHERE conversation_id = ?", (conversation["id"],)
                ).fetchone()[0], 0)
                for table in (
                    "memories", "body_symptoms", "lab_interpretations",
                    "user_profile", "onboarding_state",
                ):
                    self.assertEqual(conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE chel_id = ?", (reset_id,)
                    ).fetchone()[0], 0)

            db.set_current_chel_id(other_id)
            self.assertIsNotNone(db.get_conversation(other_conversation["id"]))
            self.assertEqual(db.get_profile()["preferred_name"], "Оставить")
        finally:
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_admin_delete_user_data_removes_all_records_and_keeps_staff_account(self):
        chel_id = "chel_admin_delete_target"
        other_id = "chel_admin_delete_other"
        manager = db.admin_create_staff(
            "Менеджер Тест", "manager.cleanup", "123456", telegram_id="7788990011",
        )
        db.ensure_user(chel_id)
        db.ensure_user(other_id)
        try:
            db.set_current_chel_id(chel_id)
            conversation = db.create_conversation("Удаляемый диалог")
            db.add_message(conversation["id"], "user", "Удаляемое сообщение")
            db.add_handoff(conversation["id"], "manager", "therapist", "Удаляемая передача")
            db.manager_set_ai_enabled(conversation["id"], False, "Менеджер Тест")
            db.update_conversation(
                conversation["id"], active_agent="manager", context_summary="{}",
                status="waiting_human", human_status="pending",
                human_ticket_id="H-DELETE", human_channel="chat",
            )
            db.enqueue_manager_notifications("new_request", conversation["id"])
            db.add_memory("Удаляемая память")
            db.save_profile({"preferred_name": "Удалить", "tube_number": "DELETE-1"})
            db.save_onboarding(status="complete", selected_tests=["lipids"])
            db.add_body_symptom({
                "region": "Голова", "symptom_type": "Боль", "intensity": 4,
            })
            db.record_device_access("Mozilla/5.0 (iPhone) Safari")
            with db.connection() as conn:
                now = db.utc_now()
                conn.execute(
                    """INSERT INTO external_identities
                    (provider, provider_user_id, chel_id, access_status, created_at, last_login_at)
                    VALUES ('telegram', '7788990011', ?, 'active', ?, ?)""",
                    (chel_id, now, now),
                )
                conn.execute(
                    "INSERT INTO login_tokens (token_hash, chel_id, expires_at, created_at) VALUES ('delete-login', ?, ?, ?)",
                    (chel_id, now, now),
                )
                conn.execute(
                    "INSERT INTO auth_intents (token_hash, chel_id, provider, expires_at, created_at) VALUES ('delete-intent', ?, 'telegram', ?, ?)",
                    (chel_id, now, now),
                )
                conn.execute(
                    "INSERT INTO user_sessions (session_hash, chel_id, created_at, last_seen_at, expires_at) VALUES ('delete-session', ?, ?, ?, ?)",
                    (chel_id, now, now, now),
                )
                conn.execute(
                    "INSERT INTO ai_usage (chel_id, model, created_at) VALUES (?, 'test-model', ?)",
                    (chel_id, now),
                )
                conn.commit()

            listed = next(item for item in db.admin_list_staff() if item["id"] == manager["id"])
            self.assertEqual(listed["user_chel_ids"], [chel_id])

            result = db.admin_delete_user_data(chel_id)
            self.assertTrue(result["user_found"])
            self.assertGreater(result["deleted"], 0)
            self.assertFalse(db.user_exists(chel_id))
            self.assertTrue(db.user_exists(other_id))
            self.assertTrue(any(item["id"] == manager["id"] for item in db.admin_list_staff()))
            listed = next(item for item in db.admin_list_staff() if item["id"] == manager["id"])
            self.assertEqual(listed["user_chel_ids"], [])

            with db.connection() as conn:
                for table in (
                    "conversations", "memories", "body_symptoms", "lab_interpretations",
                    "user_profile", "onboarding_state", "user_device_stats", "ai_usage",
                    "login_tokens", "auth_intents", "user_sessions", "external_identities",
                    "users",
                ):
                    self.assertEqual(conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE chel_id = ?", (chel_id,),
                    ).fetchone()[0], 0, table)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation["id"],),
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM handoffs WHERE conversation_id = ?", (conversation["id"],),
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM manager_actions WHERE conversation_id = ?", (conversation["id"],),
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM manager_notification_outbox WHERE conversation_id = ?", (conversation["id"],),
                ).fetchone()[0], 0)

            with self.assertRaises(ValueError):
                db.admin_delete_user_data("chel_test_default")
        finally:
            db.set_current_chel_id("chel_test_default")
            db.admin_delete_staff(manager["id"])
            with db.connection() as conn:
                conn.execute("DELETE FROM users WHERE chel_id = ?", (other_id,))
                conn.commit()

    def test_function_menu_has_requested_order_and_only_two_items_after_separator(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        ordered_ids = (
            'id="newChatButton"',
            'id="menuProfileButton"',
            'id="menuLabResultsButton"',
            'id="mobileDialogsButton"',
            'id="humanButton"',
        )
        positions = [index.index(item) for item in ordered_ids]
        self.assertEqual(positions, sorted(positions))
        separator = index.index('class="function-menu-separator"')
        install = index.index('id="menuInstallAppButton"')
        font_size = index.index('id="menuFontSizeButton"')
        menu_end = index.index('</div>', font_size)
        self.assertLess(separator, install)
        self.assertLess(install, font_size)
        self.assertNotIn('<button', index[font_size:menu_end])
        self.assertIn("Установить приложение", index)
        self.assertNotIn('id="resetUserButton"', index)
        self.assertNotIn("$('#resetUserButton')", script)

    def test_capabilities_offer_confirmed_full_user_data_deletion(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        main_source = (project_root / "backend" / "main.py").read_text(encoding="utf-8")

        capabilities = index.split('id="capabilitiesModal"', 1)[1].split(
            'id="installAppModal"', 1,
        )[0]
        self.assertIn('id="capabilityDeleteData"', capabilities)
        self.assertGreater(
            capabilities.index('id="capabilityDeleteData"'),
            capabilities.index('capability-info-grid'),
        )
        self.assertIn('id="deleteMyDataModal"', index)
        self.assertIn("Это действие нельзя отменить", index)
        self.assertIn("привязка Telegram или MAX и доступ к текущему профилю", index)
        self.assertIn("потребуется зарегистрироваться и заполнить анкету заново", index)
        self.assertIn("/api/delete-my-data", script)
        self.assertIn("'X-Consilium-Action':'delete-my-data'", script)
        self.assertIn("localStorage.clear()", script)
        self.assertIn("sessionStorage.clear()", script)
        self.assertIn("caches.keys()", script)
        self.assertIn('path == "/api/delete-my-data"', main_source)
        self.assertIn("db.admin_delete_user_data(chel_id)", main_source)
        self.assertIn("analytics.delete_user_data(chel_id)", main_source)
        self.assertIn("self._clear_user_session = True", main_source)
        self.assertIn(".capability-danger-zone", styles)
        self.assertIn(".delete-my-data-modal", styles)

    def test_exam_offer_explains_choices_and_confirms_skipping(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        questionnaire = script.split("const onboardingQuestions = [", 1)[1].split(
            "];", 1,
        )[0]
        self.assertIn("key:'notes'", questionnaire)
        self.assertIn("Есть ли у вас жалобы?", questionnaire)
        self.assertIn("Если жалоб нет, этот вопрос можно пропустить", questionnaire)
        self.assertIn("maxlength:1000", questionnaire)
        self.assertIn("placeholder:'Например: две недели болит голова по вечерам, принимаю ибупрофен', optional:true", questionnaire)
        self.assertIn("if (question.optional && empty) return 'Пропустить'", script)
        self.assertIn("Жалобы и дополнительные сведения", index)
        self.assertIn("Давайте честно: здоровых людей не бывает", script)
        self.assertIn("Можно пригласить родственника или друга", script)
        self.assertIn("Хотели бы вы сдать дополнительные анализы", script)
        self.assertIn('data-onboarding-action="open-exam-catalog-info"', script)
        self.assertIn("function renderExamCatalogInfo()", script)
        self.assertIn("Кому подходит", script)
        self.assertIn("Для чего", script)
        self.assertIn("Что входит", script)
        self.assertIn("action:'catalog_info'", script)
        self.assertIn("Да, выбрать анализы", script)
        self.assertIn("← Изменить ответы анкеты", script)
        self.assertIn('data-onboarding-action="review-exam-skip"', script)
        self.assertIn("Татьяна Витальевна", script)
        self.assertIn("бесплатную консультацию", script)
        self.assertIn("Один визит вместо отдельной поездки", script)
        self.assertIn("Не придётся записываться отдельно", script)
        self.assertIn("Всё равно отказаться", script)
        self.assertIn('data-onboarding-action="confirm-skip-exams"', script)
        self.assertIn("function renderExamSkipCompletion()", script)
        self.assertIn("completion_skipped_viewed", script)
        self.assertIn("Спасибо! Ваши ответы сохранены.", script)
        self.assertIn("В Консилиуме вы сможете:", script)
        self.assertIn('data-onboarding-action="install-after-skip"', script)
        self.assertIn('data-onboarding-action="continue-after-skip"', script)
        self.assertIn('data-onboarding-action="link-messenger-after-skip"', script)
        self.assertIn("state.onboarding.payment_status === 'skipped'", script)
        self.assertIn("if (state.returnToChatAfterExaminations) return openMainApp({ skipIntro:true });", script)
        self.assertIn("renderExamSkipCompletion();", script)
        self.assertNotIn("Мы можем показать наборы из проекта медосмотров", script)
        self.assertIn(".exam-skip { width:100%; min-height:44px;", styles)
        self.assertIn(".exam-info-card", styles)
        self.assertIn("border:1px solid #b9c7c0", styles)

    def test_pregnancy_question_is_not_in_initial_questionnaire(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        questionnaire = script.split("const onboardingQuestions = [", 1)[1].split(
            "];", 1,
        )[0]
        self.assertNotIn("key:'pregnancy'", questionnaire)
        self.assertNotIn("Есть ли беременность?", questionnaire)
        self.assertNotIn('id="profilePregnancy"', index)
        self.assertNotIn("Беременность<select", index)
        self.assertIn("pregnancy:'not_applicable'", script)

    def test_user_app_is_installable_pwa_without_caching_api_data(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        manager_html = (project_root / "manager.html").read_text(encoding="utf-8")
        dashboard_html = (project_root / "dashboard.html").read_text(encoding="utf-8")
        main_source = (project_root / "backend" / "main.py").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        worker = (project_root / "service-worker.js").read_text(encoding="utf-8")
        manifest = json.loads(
            (project_root / "manifest.webmanifest").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(
            {icon["sizes"] for icon in manifest["icons"]},
            {"192x192", "512x512"},
        )
        self.assertIn('rel="manifest"', index)
        self.assertIn('rel="apple-touch-icon"', index)
        self.assertIn('/static/favicon.svg', index)
        self.assertIn('/static/favicon.svg', manager_html)
        self.assertIn('/static/favicon.svg', dashboard_html)
        self.assertNotIn('rel="icon" href="data:,"', dashboard_html)
        self.assertIn('STATIC_DIR / "favicon.svg"', main_source)
        self.assertIn('id="menuInstallAppButton"', index)
        self.assertIn('id="installAppModal"', index)
        self.assertIn("beforeinstallprompt", script)
        self.assertIn("navigator.serviceWorker.register('/service-worker.js')", script)
        self.assertIn("registration.update()", script)
        self.assertIn("controllerchange", script)
        self.assertIn("url.pathname.startsWith('/api/')", worker)
        self.assertIn("url.pathname.startsWith('/auth/')", worker)
        self.assertIn("consilium-shell-v84", worker)
        self.assertIn("fetch(request)", worker)
        self.assertIn("/static/styles.07ffaefb4795.css", index)
        self.assertIn("/static/rich-text.2bf1f5fab764.css", index)
        self.assertTrue((project_root / "static" / "styles.07ffaefb4795.css").is_file())
        self.assertTrue((project_root / "static" / "rich-text.2bf1f5fab764.css").is_file())
        self.assertIn("/static/app.js?v=20260829-chat-profile-v2", index)
        self.assertIn("/static/metrika.js?v=20260829-interpret-profile-v1", index)
        self.assertIn('id="welcomeScreen"', index)
        self.assertIn('id="welcomeNextButton"', index)
        self.assertIn("WELCOME_SEEN_KEY", script)
        self.assertIn("showWelcome(() => showAuthGate())", script)
        self.assertIn("entryParams.get('welcome') === '1'", script)
        self.assertIn("function renderExamCompletion()", script)
        self.assertIn('data-onboarding-action="install-after-exams"', script)
        self.assertIn('data-onboarding-action="later-after-exams"', script)
        self.assertIn("['demo_paid','paid_online','pay_at_exam'].includes(state.onboarding.payment_status)", script)
        self.assertIn("Оплатить онлайн", script)
        self.assertIn("Оплатить на медосмотре", script)
        self.assertIn("/api/payments/yookassa/create", script)
        self.assertIn("/api/purchases", script)
        self.assertIn("data-purchase-action=\"delete\"", script)
        self.assertIn("data-purchase-action=\"continue\"", script)
        self.assertIn("method:'DELETE'", script)
        self.assertIn('id="menuPurchasesButton"', index)
        self.assertIn('id="purchasesModal"', index)
        self.assertIn("PAYMENT_PENDING_ORDER_KEY", script)
        self.assertIn("/abandon", script)
        self.assertIn("if (!state.publicConfig.online_payments_enabled)", script)
        self.assertIn("trackEvent('payment_completed'", script)
        self.assertIn("for (let attempt = 0; attempt < 8; attempt += 1)", script)
        self.assertIn("Онлайн-оплата временно недоступна", script)
        self.assertIn("handlePaymentReturn", script)
        self.assertIn('data-onboarding-action="close-payment-review"', script)
        self.assertIn("source:'purchases'", script)
        self.assertIn("pendingOrder.source || params.get('payment_source')", script)
        self.assertIn('data-onboarding-action="leave-payment-result">Назад</button>', script)
        self.assertIn("async function restorePaymentReviewState()", script)
        self.assertIn("async function returnToOnlinePayment()", script)
        self.assertIn("await startOnlinePayment()", script)
        self.assertIn("source === 'purchases'", script)
        self.assertIn("else if (returnToChat)", script)
        self.assertIn("function renderPaymentSuccess", script)
        self.assertIn("Где потом найти оплату", script)
        self.assertIn("Открыть мои покупки", script)
        prompts = (project_root / "backend" / "prompts.py").read_text(encoding="utf-8")
        self.assertIn("«Мои покупки» находятся в меню функций справа", prompts)
        self.assertIn("данные карты, CVC/CVV", prompts)
        self.assertIn("Когда он появится", script)
        self.assertLess(
            index.index('id="menuInstallAppButton"'),
            index.index('id="menuFontSizeButton"'),
        )

    def test_yandex_metrika_tracks_safe_visits_clickmap_and_goals(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        metrika = (project_root / "static" / "metrika.js").read_text(encoding="utf-8")
        app = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        main = (project_root / "backend" / "main.py").read_text(encoding="utf-8")
        config = (project_root / "backend" / "config.py").read_text(encoding="utf-8")

        self.assertIn('src="/static/metrika.js?v=20260829-interpret-profile-v1"', index)
        self.assertIn('YANDEX_METRIKA_COUNTER_ID', config)
        self.assertIn('path == "/api/public-config"', main)
        self.assertIn('"metrika.js"', main)
        self.assertIn("https://mc.yandex.ru", main)
        self.assertNotIn("consilium_yandex_metrika_consent_v1", metrika)
        self.assertNotIn("metrika-consent", metrika)
        self.assertIn("webvisor: true", metrika)
        self.assertIn("ym-disable-keys", metrika)
        self.assertIn("ym-disable-submit", metrika)
        self.assertIn("ym-hide-content", metrika)
        self.assertIn("ym-show-content", metrika)
        self.assertNotIn("'#onboarding'", metrika)
        self.assertNotIn("'#appShell'", metrika)
        self.assertIn("root.matches(selector)", metrika)
        self.assertIn("'#messages'", metrika)
        self.assertIn("'#profileModal'", metrika)
        self.assertIn("'#profileModal button'", metrika)
        self.assertIn("trackLinks: true", metrika)
        self.assertIn("clickmap: true", metrika)
        self.assertIn("defer: true", metrika)
        self.assertIn("sendTitle: false", metrika)
        self.assertIn("'hit', `${location.origin}${location.pathname}`", metrika)
        self.assertIn("counterReady", metrika)
        self.assertIn("allow_metrika_frame=True", main)
        self.assertIn("https://metrika.yandex.ru", main)
        self.assertIn("https://metrika.yandex.com", main)
        self.assertIn("https://metrica.yandex.com.tr", main)
        self.assertIn("https://analytics.yandex.by", main)
        self.assertIn("https://webvisor.com", main)
        self.assertIn("https://mc.webvisor.com", main)
        self.assertIn("worker-src 'self' blob:", main)
        self.assertIn("child-src 'self' blob:", main)
        self.assertIn("wss://mc.webvisor.com", main)
        self.assertIn("if not allow_metrika_frame:", main)
        self.assertIn('self.send_header("X-Frame-Options", "DENY")', main)
        self.assertIn("public, max-age=31536000, immutable", main)
        self.assertNotIn("UserID", metrika)
        self.assertNotIn("userParams", metrika)
        self.assertNotIn("chel_id", metrika)
        self.assertIn("window.consiliumMetrikaGoal?.(goal)", app)

    def test_persisted_replies_are_deduplicated_by_server_message_id(self):
        project_root = Path(__file__).resolve().parents[1]
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        add_message = script.split("function addMessage(", 1)[1].split(
            "function addCouncilResult(", 1,
        )[0]
        human_flow = script.split("async function chooseHumanSpecialistChat(", 1)[1].split(
            "async function requestSecondOpinion(", 1,
        )[0]

        self.assertIn("const messageId = Number(metadata._message_id || 0);", add_message)
        self.assertIn('messages.querySelector(`[data-message-id="${messageId}"]`)', add_message)
        self.assertIn("wrapper.dataset.messageId = String(messageId);", add_message)
        self.assertIn("_message_id:result.assistant_message.id", human_flow)

    def test_production_port_is_isolated_from_existing_server_projects(self):
        project_root = Path(__file__).resolve().parents[1]
        production_env = (project_root / ".env.production.example").read_text(encoding="utf-8")
        docker_env = (project_root / ".env.docker.example").read_text(encoding="utf-8")
        compose = (project_root / "docker-compose.yml").read_text(encoding="utf-8")
        nginx = (project_root / "deploy" / "nginx-consilium.conf").read_text(encoding="utf-8")
        server_guide = (project_root / "README.md").read_text(encoding="utf-8")
        self.assertIn("PORT=8002", production_env)
        self.assertNotIn("PORT=8000", production_env)
        self.assertIn("HOST=0.0.0.0", docker_env)
        self.assertIn("CONSILIUM_HOST_PORT=8002", docker_env)
        self.assertIn("127.0.0.1:${CONSILIUM_HOST_PORT:-8002}:8000", compose)
        self.assertIn("external: true", compose)
        self.assertNotIn("0.0.0.0:${CONSILIUM_HOST_PORT", compose)
        self.assertEqual(nginx.count("proxy_pass http://127.0.0.1:8002;"), 5)
        self.assertNotIn("proxy_pass http://127.0.0.1:8000;", nginx)
        self.assertIn("zone=consilium_ai:10m rate=12r/m", nginx)
        self.assertIn("zone=consilium_login:10m rate=6r/m", nginx)
        self.assertIn("location ^~ /api/admin/", nginx)
        admin_location = nginx.split("location ^~ /api/admin/", 1)[1].split("}", 1)[0]
        self.assertNotIn("limit_req", admin_location)
        self.assertNotIn("chat|council|second-opinion", nginx)
        self.assertIn("anketa_bot_max", server_guide)
        self.assertIn("bitrix_connector", server_guide)
        deployment_check = (
            project_root / "scripts" / "deployment_bundle_check.py"
        ).read_text(encoding="utf-8")
        self.assertIn("consilium-telegram-bot", deployment_check)
        self.assertIn("consilium-max-bot", deployment_check)
        self.assertIn("--check-env", deployment_check)
        main_source = (project_root / "backend" / "main.py").read_text(encoding="utf-8")
        self.assertLess(
            main_source.index('if path == "/api/health":'),
            main_source.index("self._ensure_user_context()"),
        )

    def test_max_login_is_one_time_persistent_and_survives_user_reset(self):
        max_user_id = 987654321
        legacy_chel_id = 4321
        login = db.create_max_login(max_user_id, legacy_chel_id)
        self.assertEqual(login["chel_id"], "chel_max_000000004321")
        expires_at = datetime.fromisoformat(login["expires_at"])
        remaining = expires_at - datetime.now(timezone.utc)
        self.assertGreater(remaining, timedelta(days=6, hours=23))
        self.assertLessEqual(remaining, timedelta(days=7))

        session = db.consume_login_token(login["token"])
        self.assertIsNotNone(session)
        self.assertEqual(session["chel_id"], login["chel_id"])
        self.assertIsNone(db.consume_login_token(login["token"]))
        self.assertEqual(db.get_session_chel_id(session["session"]), login["chel_id"])

        second_login = db.create_max_login(max_user_id, legacy_chel_id)
        self.assertEqual(second_login["chel_id"], login["chel_id"])

        try:
            db.set_current_chel_id(login["chel_id"])
            db.save_profile({"preferred_name": "MAX пользователь"})
            db.create_conversation("Сохранённый разговор")
            identity = db.current_external_identity()
            self.assertEqual(identity["provider_user_id"], str(max_user_id))

            db.reset_current_user(preserve_identity=True)
            self.assertTrue(db.user_exists(login["chel_id"]))
            self.assertEqual(db.get_profile()["preferred_name"], "")
            self.assertEqual(db.list_conversations(), [])
            self.assertIsNotNone(db.current_external_identity())
            self.assertEqual(db.get_session_chel_id(session["session"]), login["chel_id"])
        finally:
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_max_identity_cannot_be_rebound_to_another_user(self):
        first = db.create_max_login(910000001, 55001)
        self.assertEqual(first["chel_id"], "chel_max_000000055001")
        with self.assertRaisesRegex(ValueError, "уже привязан"):
            db.create_max_login(910000002, 55001)

    def test_blocked_max_identity_loses_existing_session(self):
        login = db.create_max_login(920000001, 56001)
        session = db.consume_login_token(login["token"])
        self.assertEqual(db.get_session_chel_id(session["session"]), login["chel_id"])
        with db.connection() as conn:
            conn.execute(
                "UPDATE external_identities SET access_status = 'blocked' WHERE chel_id = ?",
                (login["chel_id"],),
            )
            conn.commit()
        self.assertIsNone(db.get_session_chel_id(session["session"]))
        with self.assertRaises(PermissionError):
            db.create_max_login(920000001, 56001)

    def test_messenger_identity_keeps_anonymous_profile_and_restores_it(self):
        anonymous_chel_id = "chel_auth_test_profile"
        try:
            db.ensure_user(anonymous_chel_id)
            db.set_current_chel_id(anonymous_chel_id)
            db.save_profile({"preferred_name": "Сохранённая анкета"})
            intent = db.create_auth_intent("telegram")
            login = db.create_messenger_login(
                "telegram", "tg-user-1001", intent_token=intent["token"],
            )
            self.assertEqual(login["chel_id"], anonymous_chel_id)
            self.assertEqual(db.get_profile()["preferred_name"], "Сохранённая анкета")

            other_browser = "chel_auth_test_other_browser"
            db.ensure_user(other_browser)
            db.set_current_chel_id(other_browser)
            second_intent = db.create_auth_intent("telegram")
            restored = db.create_messenger_login(
                "telegram", "tg-user-1001", intent_token=second_intent["token"],
            )
            self.assertEqual(restored["chel_id"], anonymous_chel_id)
            session = db.consume_login_token(restored["token"])
            self.assertEqual(db.get_session_chel_id(session["session"]), anonymous_chel_id)
        finally:
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_used_messenger_link_remains_bound_to_its_owner(self):
        login = db.create_messenger_login("telegram", "tg-repeat-link-owner")
        session = db.consume_login_token(login["token"])
        self.assertEqual(
            db.get_consumed_login_owner(login["token"]),
            login["chel_id"],
        )
        self.assertEqual(
            db.get_session_chel_id(session["session"]),
            login["chel_id"],
        )
        self.assertIsNone(db.consume_login_token(login["token"]))

        # Creating another link must not delete the ownership marker.
        db.create_messenger_login("telegram", "tg-repeat-link-cleanup")
        self.assertEqual(
            db.get_consumed_login_owner(login["token"]),
            login["chel_id"],
        )

    def test_reused_link_only_accepts_matching_browser_session(self):
        class FakeHandler:
            def __init__(self, cookie):
                self.headers = {"Cookie": cookie}
                self.status = None
                self.response_headers = {}

            def send_response(self, status):
                self.status = status

            def _send_security_headers(self):
                pass

            def send_header(self, name, value):
                self.response_headers[name] = value

            def end_headers(self):
                pass

        with (
            patch.object(db, "consume_login_token", return_value=None),
            patch.object(db, "get_consumed_login_owner", return_value="chel_owner"),
            patch.object(db, "get_session_chel_id", return_value="chel_owner"),
            patch.object(db, "set_current_chel_id") as set_current,
        ):
            handler = FakeHandler("consilium_session=matching-session")
            ConsiliumHandler._consume_messenger_login(handler, "used-token")
            self.assertEqual(handler.status, 303)
            self.assertEqual(
                handler.response_headers["Location"],
                "/?auth=messenger_login",
            )
            set_current.assert_called_once_with("chel_owner")

        with (
            patch.object(db, "consume_login_token", return_value=None),
            patch.object(db, "get_consumed_login_owner", return_value="chel_owner"),
            patch.object(db, "get_session_chel_id", return_value=None),
        ):
            handler = FakeHandler("")
            ConsiliumHandler._consume_messenger_login(handler, "used-token")
            self.assertEqual(handler.status, 303)
            self.assertEqual(
                handler.response_headers["Location"],
                "/?auth=messenger_required",
            )

    def test_one_user_can_link_telegram_and_max(self):
        chel_id = "chel_auth_test_two_messengers"
        try:
            db.ensure_user(chel_id)
            db.set_current_chel_id(chel_id)
            telegram_intent = db.create_auth_intent("telegram")
            telegram = db.create_messenger_login(
                "telegram", "tg-user-2001", intent_token=telegram_intent["token"],
            )
            max_intent = db.create_auth_intent("max")
            max_login = db.create_messenger_login(
                "max", "max-user-2001", intent_token=max_intent["token"],
            )
            self.assertEqual(telegram["chel_id"], chel_id)
            self.assertEqual(max_login["chel_id"], chel_id)
            self.assertEqual(
                {item["provider"] for item in db.current_external_identities()},
                {"telegram", "max"},
            )
        finally:
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_auth_intent_is_provider_specific_and_one_time(self):
        chel_id = "chel_auth_test_intent"
        try:
            db.ensure_user(chel_id)
            db.set_current_chel_id(chel_id)
            intent = db.create_auth_intent("telegram")
            with self.assertRaisesRegex(ValueError, "недействителен"):
                db.create_messenger_login("max", "max-user-3001", intent_token=intent["token"])
            db.create_messenger_login("telegram", "tg-user-3001", intent_token=intent["token"])
            with self.assertRaisesRegex(ValueError, "недействителен"):
                db.create_messenger_login("telegram", "tg-user-3002", intent_token=intent["token"])
        finally:
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_auth_screen_explains_messenger_and_anonymous_access(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="authGate"', index)
        self.assertIn('id="telegramAuthButton"', index)
        self.assertIn('id="maxAuthButton"', index)
        self.assertIn('id="anonymousAuthButton"', index)
        self.assertIn('id="anonymousWarning"', index)
        self.assertIn("consilium_anonymous_access", script)
        self.assertIn("=== identity.chel_id", script)
        self.assertIn("/api/auth/messenger/start", script)
        self.assertIn("identity.authenticated", script)

    def test_returning_messenger_user_with_questionnaire_opens_chat_directly(self):
        project_root = Path(__file__).resolve().parents[1]
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        main_source = (project_root / "backend" / "main.py").read_text(encoding="utf-8")
        self.assertIn('entryParams.get(\'auth\') === \'messenger_login\'', script)
        self.assertIn("function hasCompletedQuestionnaire(onboarding)", script)
        self.assertIn("onboarding?.profile?.updated_at", script)
        self.assertIn("['exams','payment','complete'].includes(onboarding?.status)", script)
        self.assertIn("async function enterKnownUser()", script)
        self.assertIn("const onboarding = await api('/api/onboarding');", script)
        self.assertIn("openCompletedMessengerAccount:true", script)
        self.assertIn("return openMainApp({ skipIntro:true });", script)
        self.assertIn('self.send_header("Location", "/?auth=messenger_login")', main_source)

    def test_messenger_can_be_linked_from_menu_and_after_examinations(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="menuMessengerLinkButton"', index)
        self.assertIn('id="messengerLinkModal"', index)
        self.assertIn('data-onboarding-action="link-messenger-after-exams"', script)
        self.assertIn('data-onboarding-action="link-messenger-after-skip"', script)
        self.assertIn("state.identity?.authenticated ? ''", script)
        self.assertIn("consilium_messenger_link_pending", script)
        self.assertIn("/api/auth/messenger/start", script)
        self.assertIn("messenger-link-option", styles)
        self.assertIn("exam-messenger-offer", styles)

    def test_auth_token_is_redacted_from_application_log(self):
        fake_handler = type("FakeHandler", (), {"address_string": lambda self: "127.0.0.1"})()
        with patch("builtins.print") as output:
            ConsiliumHandler.log_message(
                fake_handler, '%s', "GET /auth/max?t=super-secret-token HTTP/1.1"
            )
        logged = output.call_args.args[0]
        self.assertNotIn("super-secret-token", logged)
        self.assertIn("[REDACTED]", logged)
        with patch("builtins.print") as output:
            ConsiliumHandler.log_message(
                fake_handler, '%s', "GET /auth/messenger?t=another-secret HTTP/1.1"
            )
        self.assertNotIn("another-secret", output.call_args.args[0])

    def test_onboarding_state_and_lifestyle_profile_are_persisted(self):
        appearance = db.save_onboarding(status="questionnaire", font_size="large")
        self.assertEqual(appearance["font_size"], "large")
        profile = db.save_profile({
            "age": 41, "sex": "male", "height_cm": 178, "weight_kg": 92,
            "smoking": "former", "alcohol": "rarely", "activity": "moderate",
            "blood_pressure": "high", "blood_sugar": "unknown", "dark_in_eyes": "no",
            "joint_pain": "no", "fatigue": "yes",
        })
        state = db.save_onboarding(
            status="payment", selected_tests=["fatigue_basic", "lipids"], payment_status="pending"
        )
        self.assertEqual(profile["activity"], "moderate")
        self.assertEqual(state["selected_tests"], ["fatigue_basic", "lipids"])
        self.assertFalse(state["intro_seen"])
        state = db.save_onboarding(status="complete", intro_seen=True)
        self.assertTrue(state["intro_seen"])
        self.assertEqual(state["font_size"], "large")
        self.assertEqual(len(TEST_CATALOG), 22)
        self.assertIn("fatigue_basic", recommend_test_ids(profile))
        self.assertIn("fatigue_extended", recommend_test_ids(profile))
        self.assertIn("weight_basic", recommend_test_ids(profile))
        self.assertIn("weight_extended", recommend_test_ids(profile))

    def test_extended_examinations_replace_matching_basic_complexes(self):
        self.assertEqual(EXAMINATION_UPGRADE_PAIRS, {
            "fatigue_basic": "fatigue_extended",
            "weight_basic": "weight_extended",
            "liver_basic": "liver_extended",
        })
        self.assertEqual(
            normalize_examination_selection([
                "fatigue_basic", "lipids", "fatigue_extended", "iron",
            ]),
            ["lipids", "fatigue_extended", "iron"],
        )
        self.assertEqual(
            normalize_examination_selection(["weight_basic", "liver_basic"]),
            ["weight_basic", "liver_basic"],
        )

        payload = public_onboarding(
            {"selected_tests": ["liver_basic", "liver_extended"]}, {}, TEST_CATALOG,
        )
        self.assertEqual(payload["selected_tests"], ["liver_extended"])

        project_root = Path(__file__).resolve().parents[1]
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("function selectExamination(id)", script)
        self.assertIn("state.selectedTests.delete(basicId)", script)
        self.assertIn("disabled-by-upgrade", script)
        self.assertIn("Уже входит в", script)
        self.assertIn("function renderExamSelection(scrollPosition = null)", script)
        self.assertIn("examList:card.closest('.exam-list')?.scrollTop || 0", script)
        self.assertIn("onboarding:$('#onboarding').scrollTop || 0", script)
        self.assertIn("examList.scrollTop = scrollPosition.examList || 0", script)
        self.assertIn("onboarding.scrollTop = scrollPosition.onboarding || 0", script)
        self.assertIn(".exam-card.disabled-by-upgrade", styles)

    def test_yookassa_order_freezes_server_prices_and_completes_only_after_verified_success(self):
        chel_id = "chel_yookassa_payment_test"
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            db.save_onboarding(
                status="payment", selected_tests=["fatigue_basic", "lipids"],
                payment_status="pending",
            )
            order = db.create_payment_order()
            private = db.payment_order_private(order["id"])
            expected = sum(
                item["price"] for item in db.list_examinations()
                if item["id"] in {"fatigue_basic", "lipids"}
            )
            self.assertEqual(private["amount_kopecks"], expected * 100)
            self.assertEqual(private["status"], "creating")

            provider = {
                "id": "2f3e4567-89ab-4cde-8012-3456789abcde",
                "status": "pending", "paid": False, "test": True,
                "amount": {"value": f"{expected:.2f}", "currency": "RUB"},
                "metadata": {"order_id": order["id"]},
                "confirmation": {"confirmation_url": "https://yoomoney.ru/pay/test"},
            }
            attached = db.attach_yookassa_payment(order["id"], provider)
            self.assertEqual(attached["status"], "pending")
            self.assertNotIn("chel_id", attached)
            self.assertNotIn("provider_payment_id", attached)
            self.assertEqual(db.get_onboarding()["status"], "payment")

            succeeded = {**provider, "status": "succeeded", "paid": True}
            paid = db.apply_yookassa_status(order["id"], succeeded)
            self.assertTrue(paid["paid"])
            self.assertEqual(db.get_onboarding()["status"], "complete")
            self.assertEqual(db.get_onboarding()["payment_status"], "paid_online")
            repeated = db.apply_yookassa_status(order["id"], succeeded)
            self.assertTrue(repeated["paid"])
        finally:
            db.set_current_chel_id(chel_id)
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_yookassa_redirect_payload_uses_server_order_and_idempotence(self):
        order = {
            "id": "ord_1234567890abcdef",
            "idempotence_key": "1b80fa2e-9e58-4c2b-8726-59f42b36e645",
            "amount_kopecks": 123400,
            "items": [{"id": "test", "name": "Комплекс", "price": 1234}],
        }
        payment_settings = SimpleNamespace(
            yookassa_receipts_enabled=False,
            yookassa_vat_code=1,
            yookassa_payment_mode="full_prepayment",
        )
        with (
            patch.object(yookassa, "settings", payment_settings),
            patch.object(yookassa, "_request", return_value={"id": "payment-id"}) as request,
        ):
            yookassa.create_payment(order, "https://example.test/payment-return")
        method, path, payload = request.call_args.args
        self.assertEqual((method, path), ("POST", "payments"))
        self.assertEqual(payload["amount"], {"value": "1234.00", "currency": "RUB"})
        self.assertTrue(payload["capture"])
        self.assertEqual(payload["confirmation"]["type"], "redirect")
        self.assertEqual(payload["metadata"]["order_id"], order["id"])
        self.assertEqual(
            request.call_args.kwargs["idempotence_key"], order["idempotence_key"],
        )

        with patch.object(yookassa, "_request", return_value={"status": "canceled"}) as cancel:
            yookassa.cancel_payment("2f3e4567-89ab-4cde-8012-3456789abcde", "order-cancel")
        self.assertEqual(cancel.call_args.args[:2], ("POST", "payments/2f3e4567-89ab-4cde-8012-3456789abcde/cancel"))
        self.assertEqual(cancel.call_args.args[2], {})
        self.assertEqual(cancel.call_args.kwargs["idempotence_key"], "order-cancel")

    def test_yookassa_rejects_tampered_amount(self):
        chel_id = "chel_yookassa_tamper_test"
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            db.save_onboarding(
                status="payment", selected_tests=["lipids"], payment_status="pending",
            )
            order = db.create_payment_order()
            provider = {
                "id": "3f3e4567-89ab-4cde-8012-3456789abcde",
                "status": "pending", "paid": False, "test": True,
                "confirmation": {"confirmation_url": "https://yoomoney.ru/pay/test"},
            }
            db.attach_yookassa_payment(order["id"], provider)
            with self.assertRaisesRegex(ValueError, "Сумма"):
                db.apply_yookassa_status(order["id"], {
                    **provider, "status": "succeeded", "paid": True,
                    "amount": {"value": "1.00", "currency": "RUB"},
                    "metadata": {"order_id": order["id"]},
                })
            self.assertEqual(db.get_onboarding()["status"], "payment")
        finally:
            db.set_current_chel_id(chel_id)
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_yookassa_rejects_unsafe_redirect_and_fractional_kopecks(self):
        chel_id = "chel_yookassa_validation_test"
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            db.save_onboarding(
                status="payment", selected_tests=["lipids"], payment_status="pending",
            )
            order = db.create_payment_order()
            provider = {
                "id": "4f3e4567-89ab-4cde-8012-3456789abcde",
                "status": "pending", "paid": False, "test": True,
                "confirmation": {"confirmation_url": "http://example.test/pay"},
            }
            with self.assertRaisesRegex(ValueError, "безопасную ссылку"):
                db.attach_yookassa_payment(order["id"], provider)

            provider["confirmation"]["confirmation_url"] = "https://yoomoney.ru/pay/test"
            db.attach_yookassa_payment(order["id"], provider)
            with self.assertRaisesRegex(ValueError, "Сумма"):
                db.apply_yookassa_status(order["id"], {
                    **provider, "status": "succeeded", "paid": True,
                    "amount": {"value": "1500.001", "currency": "RUB"},
                    "metadata": {"order_id": order["id"]},
                })
        finally:
            db.set_current_chel_id(chel_id)
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_abandoned_payment_is_listed_and_does_not_block_new_attempt(self):
        chel_id = "chel_yookassa_abandoned_test"
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            db.save_onboarding(
                status="payment", selected_tests=["lipids"], payment_status="pending",
            )
            first = db.create_payment_order()
            provider = {
                "id": "5f3e4567-89ab-4cde-8012-3456789abcde",
                "status": "pending", "paid": False, "test": True,
                "confirmation": {"confirmation_url": "https://yoomoney.ru/pay/test"},
            }
            db.attach_yookassa_payment(first["id"], provider)
            abandoned = db.mark_payment_abandoned(first["id"])
            self.assertEqual(abandoned["status"], "abandoned")
            self.assertEqual(db.list_payment_orders()[0]["id"], first["id"])
            self.assertEqual(db.list_payment_orders()[0]["status"], "abandoned")

            hidden = db.hide_payment_order(first["id"])
            self.assertEqual(hidden["status"], "abandoned")
            self.assertEqual(db.list_payment_orders(), [])

            # Reopening the same order reuses the provider idempotency key and
            # cannot accidentally create a second charge while the first
            # provider payment may still be pending.
            second = db.create_payment_order()
            self.assertEqual(second["id"], first["id"])

            resumed = db.attach_yookassa_payment(second["id"], provider)
            self.assertEqual(resumed["status"], "pending")
            self.assertEqual(resumed["id"], first["id"])
            self.assertIn(first["id"], [item["id"] for item in db.list_payment_orders()])
            db.mark_payment_abandoned(first["id"])
            db.hide_payment_order(first["id"])

            # A late provider confirmation remains authoritative even after the
            # browser-side attempt was marked as unfinished.
            succeeded = {
                **provider, "status": "succeeded", "paid": True,
                "amount": {"value": first["amount"], "currency": "RUB"},
                "metadata": {"order_id": first["id"]},
            }
            paid = db.apply_yookassa_status(first["id"], succeeded)
            self.assertEqual(paid["status"], "succeeded")
            self.assertTrue(paid["paid"])
            self.assertIn(first["id"], [item["id"] for item in db.list_payment_orders()])
            with self.assertRaisesRegex(ValueError, "только неуспешную"):
                db.hide_payment_order(first["id"])

            failed = db.create_payment_order()
            self.assertNotEqual(failed["id"], first["id"])
            db.mark_payment_creation_failed(failed["id"], "provider unavailable")
            db.hide_payment_order(failed["id"])
            self.assertNotIn(failed["id"], [item["id"] for item in db.list_payment_orders()])
        finally:
            db.set_current_chel_id(chel_id)
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_new_users_start_with_extra_large_font_and_keep_the_choice(self):
        chel_id = "chel_new_font_default"
        db.ensure_user(chel_id)
        try:
            db.set_current_chel_id(chel_id)
            db.reset_current_user()
            db.ensure_user(chel_id)
            initial = db.get_onboarding()
            self.assertEqual(initial["status"], "appearance")
            self.assertEqual(initial["font_size"], "extra")

            selected = db.save_onboarding(status="questionnaire", font_size="large")
            self.assertEqual(selected["font_size"], "large")
            self.assertEqual(db.get_onboarding()["font_size"], "large")
        finally:
            db.set_current_chel_id(chel_id)
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

        project_root = Path(__file__).resolve().parents[1]
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("state.onboarding.font_size || 'extra'", script)
        self.assertIn("const fontSizeLabels = { standard:'Обычный', large:'Крупный', extra:'Очень крупный' }", script)

    def test_body_symptom_is_persisted_in_history_and_ai_context(self):
        symptom = db.add_body_symptom({
            "region": "Грудь", "view": "front", "symptom_type": "Боль",
            "intensity": 7, "started_at": "2026-07-23T10:30",
            "duration": "hours", "pattern": "movement",
            "notes": "Усиливается при быстрой ходьбе",
        })
        self.assertEqual(symptom["status"], "active")
        self.assertEqual(symptom["intensity"], 7)

        fake = FakeLLM()
        result = ConversationOrchestrator(fake).process(None, "При ходьбе болит в груди")
        routed_symptoms = fake.route_calls[-1]["conversation"]["_body_symptoms"]
        self.assertTrue(any(item["id"] == symptom["id"] for item in routed_symptoms))

        db.add_message(
            result.conversation_id, "user", "Добавляю результат обследования",
            metadata={"attachments": [{"name": "ЭКГ.pdf", "type": "application/pdf"}]},
        )
        history = db.list_health_history()
        self.assertTrue(any(item["id"] == f"symptom-{symptom['id']}" for item in history))
        self.assertTrue(any(item["type"] == "document" and item["title"] == "ЭКГ.pdf" for item in history))
        self.assertTrue(any(item["type"] == "consultation" and item["details"]["conversation_id"] == result.conversation_id for item in history))

        resolved = db.set_body_symptom_status(symptom["id"], "resolved")
        self.assertEqual(resolved["status"], "resolved")

    def test_custom_body_symptom_requires_description(self):
        payload = {
            "region": "Живот", "view": "front", "symptom_type": "Другое",
            "custom_symptom": "  чувство   распирания  ", "intensity": 4,
            "duration": "days", "pattern": "episodes",
        }
        validated = ConsiliumHandler._validate_body_symptom(payload)
        self.assertEqual(validated["symptom_type"], "чувство распирания")
        with self.assertRaisesRegex(ValueError, "Опишите симптом"):
            ConsiliumHandler._validate_body_symptom({**payload, "custom_symptom": ""})

    def test_result_entry_marks_user_without_questionnaire(self):
        chel_id = "chel_result_entry_test"
        db.ensure_user(chel_id, pending=True)
        try:
            db.set_current_chel_id(chel_id)
            self.assertFalse(db.current_user_has_result_entry())
            marked = db.mark_current_user_result_entry()
            self.assertEqual(marked["registration_method"], "result")
            self.assertTrue(marked["registered_at"])
            self.assertTrue(db.current_user_has_result_entry())
            table = db.admin_table("users", "result_entry_test", limit=10)
            self.assertEqual(table["rows"][0]["entry_flow"], "result")
            self.assertGreaterEqual(db.admin_dashboard()["summary"]["result_entry_users"], 1)
        finally:
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")

    def test_result_flow_has_real_screens_and_metric_definitions(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        app = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        dashboard = (project_root / "static" / "dashboard.js").read_text(encoding="utf-8")
        dashboard_styles = (project_root / "static" / "dashboard.css").read_text(encoding="utf-8")
        main = (project_root / "backend" / "main.py").read_text(encoding="utf-8")
        for screen_id in (
            "result_existing", "result_welcome", "result_tube", "result_messenger", "result_search",
            "result_found", "result_not_found", "result_notification",
        ):
            self.assertIn(screen_id, app)
            self.assertIn(screen_id, dashboard)
        definitions = {item["id"]: item for item in analytics._metric2_screen_definitions()}
        self.assertTrue(definitions["result_welcome"]["root"])
        self.assertTrue(definitions["result_existing"]["root"])
        self.assertEqual(definitions["result_welcome"]["flow"], "result")
        self.assertEqual(definitions["result_tube"]["parent_id"], "result_welcome")
        self.assertEqual(definitions["result_found"]["parent_id"], "result_search")
        self.assertIn('"/result"', main)
        self.assertIn("/api/result-entry/start", app + main)
        self.assertIn("/api/lab-results/notification", app + main)
        self.assertIn('id="requestLabResultNotificationButton"', index)
        self.assertIn("requestLabResultNotification", app)
        self.assertIn("linkedMessengerProviders().size", app)
        self.assertNotIn("['Уникальных переходов'", dashboard)
        self.assertIn("['result_existing','result_welcome']", dashboard)
        self.assertIn("['result_found','result_not_found']", dashboard)
        self.assertIn("['payment_success','payment_result','payment_unavailable']", dashboard)
        self.assertIn("['completion','completion_skipped']", dashboard)
        self.assertIn("@media (min-width:1200px)", dashboard_styles)
        self.assertIn("metric2-logical-level-3", dashboard_styles)

    def test_result_subscription_uses_existing_bot_delivery_stream(self):
        login = db.create_messenger_login("telegram", "result-notify-user")
        try:
            db.set_current_chel_id(login["chel_id"])
            db.save_profile({"tube_number": "TUBE-NOTIFY-1"})
            subscription = db.create_lab_result_subscription("TUBE-NOTIFY-1")
            due = db.claim_due_lab_result_subscriptions(10)
            claimed_subscription = next(item for item in due if item["id"] == subscription["id"])
            self.assertEqual(claimed_subscription["med_id"], "TUBE-NOTIFY-1")

            queued = db.complete_lab_result_subscription_check(
                subscription["id"], [{"url": "https://example.test/result.pdf"}],
                "https://consilium.example.test",
            )
            self.assertEqual(queued, 1)
            notifications = db.claim_user_result_notifications("telegram", 10)
            notification = next(item for item in notifications if item["payload"].get("med_id") == "TUBE-NOTIFY-1")
            self.assertLess(notification["id"], 0)
            self.assertIn("Результаты анализов готовы", notification["payload"]["title"])
            self.assertEqual(
                notification["payload"]["action_url"],
                "https://consilium.example.test/result",
            )
            self.assertEqual(notification["payload"]["action_label"], "Открыть результаты")
            self.assertTrue(db.acknowledge_user_result_notification(
                abs(notification["id"]), notification["lease_token"], True,
            ))
            with db.connection() as conn:
                status = conn.execute(
                    "SELECT status FROM lab_result_subscriptions WHERE id = ?",
                    (subscription["id"],),
                ).fetchone()[0]
            self.assertEqual(status, "notified")
        finally:
            db.reset_current_user()
            db.ensure_user("chel_test_default")
            db.set_current_chel_id("chel_test_default")


if __name__ == "__main__":
    unittest.main()
