import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


_temp_dir = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = str(Path(_temp_dir.name) / "test.db")

from backend import database as db  # noqa: E402
from backend.lab_results import (  # noqa: E402
    LabResult, extract_urls, lab_result_documents, normalize_med_id,
)
from backend.llm import LLMService  # noqa: E402
from backend.main import ConsiliumHandler, admin_token_valid  # noqa: E402
from backend.orchestrator import ConversationOrchestrator  # noqa: E402
from backend.onboarding import TEST_CATALOG, public_onboarding, recommend_test_ids  # noqa: E402
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

    def test_human_ticket_stays_pending_while_ai_continues(self):
        service = ConversationOrchestrator(FakeLLM())
        result = service.process(None, "Позовите живого оператора")
        ticket = result.human_ticket_id
        self.assertTrue(result.human_escalation)
        self.assertRegex(ticket, r"^H-[A-F0-9]{6}$")

        continued = service.process(result.conversation_id, "А пока помоги разобраться с головной болью")
        saved = db.get_conversation(result.conversation_id)
        self.assertEqual(continued.agent, "neurologist")
        self.assertFalse(continued.human_escalation)
        self.assertEqual(saved["status"], "active")
        self.assertEqual(saved["human_status"], "pending")
        self.assertEqual(saved["human_ticket_id"], ticket)

    def test_repeated_human_request_reuses_ticket(self):
        service = ConversationOrchestrator(FakeLLM())
        first = service.process(None, "Позови человека")
        repeated = service.process(first.conversation_id, "Подключи оператора")
        self.assertEqual(first.human_ticket_id, repeated.human_ticket_id)
        self.assertIn("уже подготовлено", repeated.assistant_message["content"])

    def test_closed_human_request_gets_new_ticket_when_requested_again(self):
        service = ConversationOrchestrator(FakeLLM())
        first = service.process(None, "Позови человека")
        db.set_human_channel(first.conversation_id, "chat")
        closed = db.manager_close_conversation(first.conversation_id, "Ольга")
        self.assertEqual(closed["human_status"], "closed")

        repeated = service.process(first.conversation_id, "Позови человека снова")
        self.assertNotEqual(first.human_ticket_id, repeated.human_ticket_id)
        self.assertIsNone(repeated.human_channel)

    def test_human_channel_is_persisted_without_closing_ai_dialogue(self):
        service = ConversationOrchestrator(FakeLLM())
        result = service.process(None, "Позови человека")
        saved = db.set_human_channel(result.conversation_id, "chat")

        self.assertEqual(saved["human_channel"], "chat")
        self.assertEqual(saved["human_status"], "pending")
        self.assertEqual(saved["status"], "waiting_human")

        continued = service.process(result.conversation_id, "А пока ответь на мой вопрос")
        after_continue = db.get_conversation(result.conversation_id)
        self.assertFalse(continued.human_escalation)
        self.assertEqual(after_continue["human_channel"], "chat")

    def test_russian_phone_is_normalized_and_saved_for_call(self):
        self.assertEqual(
            ConsiliumHandler._normalize_russian_phone("8 (999) 123-45-67"),
            "+79991234567",
        )
        self.assertEqual(
            ConsiliumHandler._normalize_russian_phone("495 123 45 67"),
            "+74951234567",
        )
        with self.assertRaisesRegex(ValueError, "российский номер"):
            ConsiliumHandler._normalize_russian_phone("+1 202 555 0100")

        result = ConversationOrchestrator(FakeLLM()).process(None, "Позови человека")
        saved = db.set_human_channel(result.conversation_id, "call", "+79991234567")
        self.assertEqual(saved["human_channel"], "call")
        self.assertEqual(saved["human_phone"], "+79991234567")

    def test_text_call_choice_opens_phone_prompt_for_pending_handoff(self):
        fake = FakeLLM()
        service = ConversationOrchestrator(fake)
        handoff = service.process(None, "Позови человека")

        call_choice = service.process(handoff.conversation_id, "созвон")
        saved = db.get_conversation(handoff.conversation_id)

        self.assertEqual(call_choice.action, "human_channel_prompt")
        self.assertEqual(call_choice.human_channel_prompt, "call")
        self.assertEqual(call_choice.human_ticket_id, handoff.human_ticket_id)
        self.assertIn("На какой номер вам позвонить?", call_choice.assistant_message["content"])
        self.assertEqual(saved["human_status"], "pending")
        self.assertIsNone(saved["human_channel"])
        self.assertEqual(len(fake.route_calls), 0)

    def test_negative_call_phrase_does_not_open_phone_prompt(self):
        fake = FakeLLM()
        service = ConversationOrchestrator(fake)
        handoff = service.process(None, "Позови человека")

        response = service.process(handoff.conversation_id, "Не хочу созвон, продолжим здесь")

        self.assertNotEqual(response.action, "human_channel_prompt")

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

    def test_medical_assessment_stops_after_five_questions_and_offers_human(self):
        service = ConversationOrchestrator(QuestioningMedicalLLM())
        first = service.process(None, "У меня кашель")
        second = service.process(first.conversation_id, "Начался вчера, температура 37,5")
        third = service.process(first.conversation_id, "Одышки нет")
        fourth = service.process(first.conversation_id, "Становится немного лучше")

        self.assertEqual(first.assistant_message["metadata"]["assessment_questions_asked"], 2)
        self.assertEqual(second.assistant_message["metadata"]["assessment_questions_asked"], 2)
        self.assertEqual(third.assistant_message["metadata"]["assessment_questions_asked"], 1)
        self.assertEqual(third.assistant_message["metadata"]["assessment_questions_total"], 5)
        self.assertTrue(fourth.human_escalation)
        self.assertEqual(fourth.agent, "manager")
        self.assertIn("в чате со специалистом или созвоном", fourth.assistant_message["content"])

    def test_medical_agent_can_offer_human_before_question_limit_when_context_is_sufficient(self):
        service = ConversationOrchestrator(EarlyHumanMedicalLLM())
        first = service.process(None, "У меня второй день болит горло")
        second = service.process(first.conversation_id, "Температуры нет, становится легче")

        self.assertEqual(first.assistant_message["metadata"]["assessment_questions_asked"], 2)
        self.assertTrue(second.human_escalation)
        self.assertIn("в чате со специалистом или созвоном", second.assistant_message["content"])

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
            "preferred_name": "Анна", "age": 34, "sex": "female", "height_cm": 168,
            "weight_kg": 62, "pregnancy": "no", "conditions": ["Астма"],
            "medications": ["Назначенный ингалятор"], "allergies": ["Пенициллин"],
            "smoking": "never", "tube_number": "LAB-2026-0042", "notes": "",
        })
        self.assertEqual(profile["age"], 34)
        self.assertEqual(profile["allergies"], ["Пенициллин"])
        self.assertEqual(profile["tube_number"], "LAB-2026-0042")

        fake = FakeLLM()
        ConversationOrchestrator(fake).process(None, "У меня болит голова")
        runtime = json.loads(LLMService.runtime_context([], normalize_context(None), fake.route_calls[-1]["conversation"]))
        self.assertEqual(runtime["user_profile"]["weight_kg"], 62.0)

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

    def test_user_language_and_supported_topics_are_clear(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        public_ui = index + script

        self.assertIn("Я Мария, ваш ИИ-менеджер", script)
        self.assertIn("Задавайте вопросы о здоровье, питании, спорте", script)
        self.assertNotIn("AI-оркестратор", public_ui)
        self.assertNotIn("Команда агентов", public_ui)
        self.assertNotIn("План проекта", public_ui)
        self.assertIn("Здоровье и образ жизни", public_ui)

        manager_prompt = PROFILES["manager"].prompt
        lifestyle_prompt = PROFILES["general"].prompt
        self.assertIn("медицине, симптомах, профилактике", manager_prompt)
        self.assertIn("спорте, физических нагрузках", manager_prompt)
        self.assertIn("работе самого сервиса", manager_prompt)
        self.assertIn("не решай постороннюю задачу", manager_prompt)
        self.assertIn("спорт, нагрузки, восстановление, сон, питание", ORCHESTRATOR_PROMPT)
        self.assertIn("respond + manager", ORCHESTRATOR_PROMPT)
        self.assertIn("Не выполняй задачи про программирование", lifestyle_prompt)

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
        self.assertNotIn("SECRET", json.dumps(content, ensure_ascii=False))

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

    def test_layout_prevents_desktop_shell_and_focus_from_scrolling_outside_frame(self):
        project_root = Path(__file__).resolve().parents[1]
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        app_script = (project_root / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("grid-template-rows:minmax(0,1fr)", styles)
        self.assertIn(".app-shell > * { min-width: 0; min-height:0; }", styles)
        self.assertIn(".agent-list { min-width:0; min-height:0; max-height:none; flex:1 1 0;", styles)
        self.assertIn("input.focus({ preventScroll: true })", app_script)

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
            with db.connection() as conn:
                conn.execute(
                    "UPDATE users SET created_at = ? WHERE chel_id = ?",
                    ("2099-01-15T10:00:00+00:00", january_id),
                )
                conn.execute(
                    "UPDATE users SET created_at = ? WHERE chel_id = ?",
                    ("2099-02-20T10:00:00+00:00", february_id),
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

    def test_dashboard_files_and_admin_menu_entry_exist(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        dashboard = (project_root / "dashboard.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "dashboard.js").read_text(encoding="utf-8")
        styles = (project_root / "static" / "dashboard.css").read_text(encoding="utf-8")
        self.assertIn('id="menuDashboardButton"', index)
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
        self.assertIn('id="examinationForm"', dashboard)
        self.assertIn('id="examinationName"', dashboard)
        self.assertIn('id="examinationDescription"', dashboard)
        self.assertIn('id="examinationPrice"', dashboard)
        self.assertIn('id="examinationList"', dashboard)
        self.assertIn('id="staffList"', dashboard)
        self.assertIn('id="staffPassword" type="text" minlength="6"', dashboard)
        self.assertIn("sessionStorage", script)
        self.assertIn("Authorization:`Bearer ${token || ''}`", script)
        self.assertIn("/api/admin/table", script)
        self.assertIn("/api/admin/managers", script)
        self.assertIn("/api/admin/examinations", script)
        self.assertIn("saveExamination", script)
        self.assertIn("manageExamination", script)
        self.assertIn('data-staff-action="delete"', script)
        self.assertIn("method:'DELETE'", script)
        self.assertIn("@media (max-width:700px)", styles)

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
            db.add_body_symptom({
                "region": "Грудь", "symptom_type": "Боль", "intensity": 4,
                "notes": "После нагрузки",
            })
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
            self.assertTrue(any(
                item["action"] == "ai_mode" for item in detail["manager_actions"]
            ))

            updates = db.list_messages_after(conversation["id"], queued["id"])
            self.assertEqual(updates[-1]["content"], "Здравствуйте, я подключилась к диалогу.")
            self.assertEqual(updates[-1]["metadata"]["sender_type"], "human_manager")

            queue = db.manager_list_conversations("Тестовый", "ai_off")
            self.assertEqual(queue[0]["id"], conversation["id"])
            self.assertGreaterEqual(queue[0]["unanswered_user_messages"], 0)

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
        self.assertIn("/close", manager_script)
        self.assertIn("playManagerSignal('request')", manager_script)
        self.assertIn("playManagerSignal('message')", manager_script)
        self.assertIn("previous && !item.ai_enabled", manager_script)
        self.assertIn("sender_type === 'human_manager'", manager_script)
        self.assertIn('id="chatModeBanner"', index)
        self.assertNotIn("Я правильно понял?", index)
        self.assertNotIn('id="insightDock"', index)
        self.assertIn("Проверьте обращение перед отправкой", index)
        self.assertIn('id="editHandoffContext"', app)
        self.assertIn("returnToHumanAfterContextEdit", app)
        self.assertIn("/updates?after_id=", app)
        self.assertIn("Сообщение ожидает ответа менеджера", app)
        self.assertIn("playUserMessageSound()", app)
        self.assertIn("index.html dashboard.html manager.html", dockerfile)

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

    def test_reset_button_is_in_menu_and_font_size_remains_last(self):
        project_root = Path(__file__).resolve().parents[1]
        index = (project_root / "index.html").read_text(encoding="utf-8")
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="resetUserButton"', index)
        self.assertLess(index.index('id="resetUserButton"'), index.index('id="menuFontSizeButton"'))
        self.assertIn("X-Consilium-Action':'reset-user'", script)
        self.assertIn("localStorage.removeItem('consilium_conversation_id')", script)

    def test_exam_offer_explains_choices_and_confirms_skipping(self):
        project_root = Path(__file__).resolve().parents[1]
        script = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("Нажимая «Посмотреть варианты»", script)
        self.assertIn("Позже анкету также можно отредактировать", script)
        self.assertIn('data-onboarding-action="review-exam-skip"', script)
        self.assertIn("Сдавая дополнительные обследования, вы получаете", script)
        self.assertIn("Всё равно отказаться", script)
        self.assertIn('data-onboarding-action="confirm-skip-exams"', script)
        self.assertNotIn("Мы можем показать наборы из проекта медосмотров", script)
        self.assertIn(".exam-skip { width:100%; min-height:44px;", styles)
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
        self.assertIn('id="menuInstallAppButton"', index)
        self.assertIn('id="installAppModal"', index)
        self.assertIn("beforeinstallprompt", script)
        self.assertIn("navigator.serviceWorker.register('/service-worker.js')", script)
        self.assertIn("registration.update()", script)
        self.assertIn("controllerchange", script)
        self.assertIn("url.pathname.startsWith('/api/')", worker)
        self.assertIn("url.pathname.startsWith('/auth/')", worker)
        self.assertIn("consilium-shell-v5", worker)
        self.assertIn("fetch(request)", worker)
        self.assertIn("/static/app.js?v=20260730-5", index)
        self.assertLess(
            index.index('id="menuInstallAppButton"'),
            index.index('id="menuFontSizeButton"'),
        )

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
        self.assertIn("examinations(?:/[A-Za-z0-9_-]+)?", nginx)
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
            self.assertEqual(handler.response_headers["Location"], "/")
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
        self.assertIn("weight_basic", recommend_test_ids(profile))

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


if __name__ == "__main__":
    unittest.main()
