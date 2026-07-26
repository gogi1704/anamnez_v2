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
from backend.llm import LLMService  # noqa: E402
from backend.main import ConsiliumHandler  # noqa: E402
from backend.orchestrator import ConversationOrchestrator  # noqa: E402
from backend.onboarding import TEST_CATALOG, recommend_test_ids  # noqa: E402
from backend.prompts import ORCHESTRATOR_PROMPT, PROFILES  # noqa: E402
from backend.schemas import AgentResult, RouteDecision, normalize_context  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.route_calls = []
        self.answer_calls = []

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

    def test_manager_explains_tube_number_results_flow(self):
        manager_prompt = PROFILES["manager"].prompt
        self.assertIn("«Результаты анализов» находятся в меню функций", manager_prompt)
        self.assertIn("не просит номер повторно", manager_prompt)
        self.assertIn("получение результатов пока является заглушкой", manager_prompt)
        self.assertIn("относятся к manager и интерфейсу", ORCHESTRATOR_PROMPT)

    def test_layout_prevents_desktop_shell_and_focus_from_scrolling_outside_frame(self):
        project_root = Path(__file__).resolve().parents[1]
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        app_script = (project_root / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("grid-template-rows:minmax(0,1fr)", styles)
        self.assertIn(".app-shell > * { min-width: 0; min-height:0; }", styles)
        self.assertIn(".agent-list { min-width:0; min-height:0; max-height:none; flex:1 1 0;", styles)
        self.assertIn("input.focus({ preventScroll: true })", app_script)

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
                for table in ("memories", "body_symptoms", "user_profile", "onboarding_state"):
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
        self.assertNotIn("0.0.0.0:${CONSILIUM_HOST_PORT", compose)
        self.assertEqual(nginx.count("proxy_pass http://127.0.0.1:8002;"), 3)
        self.assertNotIn("proxy_pass http://127.0.0.1:8000;", nginx)
        self.assertIn("anketa_bot_max", server_guide)
        self.assertIn("bitrix_connector", server_guide)
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

    def test_auth_token_is_redacted_from_application_log(self):
        fake_handler = type("FakeHandler", (), {"address_string": lambda self: "127.0.0.1"})()
        with patch("builtins.print") as output:
            ConsiliumHandler.log_message(
                fake_handler, '%s', "GET /auth/max?t=super-secret-token HTTP/1.1"
            )
        logged = output.call_args.args[0]
        self.assertNotIn("super-secret-token", logged)
        self.assertIn("[REDACTED]", logged)

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
