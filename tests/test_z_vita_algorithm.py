import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import database as db
from backend.config import settings
from backend.main import (
    ConsiliumHandler,
    _psychology_crisis_detected,
    _psychology_immediate_crisis_detected,
)
from backend.orchestrator import ConversationOrchestrator
from backend.prompts import MASTERCLASS_PSYCHOLOGY_CHAT
from backend.schemas import (
    AGENT_RESULT_JSON_SCHEMA,
    AgentResult,
    RouteDecision,
    normalize_context,
    normalize_psychology_dialog_state,
)


class VitaAlgorithmTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = settings.database_path
        object.__setattr__(settings, "database_path", Path(self.temp_dir.name) / "vita.db")
        db.set_current_chel_id("chel_vita_algorithm")
        db.init_db()

    def tearDown(self):
        db.set_current_chel_id("chel_test_default")
        object.__setattr__(settings, "database_path", self.original_database)
        self.temp_dir.cleanup()

    def create_chat(self):
        return db.create_conversation(
            "Психологическая поддержка",
            dialog_type="masterclass_psychology",
            pinned=True,
        )

    def test_canonical_welcome_identifies_vita_and_explains_safety(self):
        greeting = db.MASTERCLASS_PSYCHOLOGY_WELCOME
        self.assertIn("Я Вита — ИИ-помощник команды психологов", greeting)
        self.assertIn("Я не человек и не ставлю диагнозов", greeting)
        self.assertIn("первая встреча бесплатна", greeting)
        self.assertIn("мысли причинить себе вред", greeting)

    def test_structured_state_is_normalized_and_persisted(self):
        chat = self.create_chat()
        saved = db.save_psychology_dialog_state(chat["id"], {
            "stage": "technique",
            "category_primary": "anxiety",
            "categories_secondary": ["sleep"],
            "manifestations": {
                "thoughts": "не справлюсь", "body": "сжимает грудь",
                "behavior": "избегаю встреч",
            },
            "impact_score": 8,
            "techniques_used": [{
                "name": "Квадратное дыхание", "before": 8,
                "after": 6, "feedback": "стало немного легче",
            }],
            "risk_flags": [],
            "priority": "attention",
            "consent_summary": True,
        })
        loaded = db.get_psychology_dialog_state(chat["id"])
        self.assertEqual(saved["stage"], "technique")
        self.assertEqual(loaded["category_primary"], "anxiety")
        self.assertEqual(loaded["manifestations"]["body"], "сжимает грудь")
        self.assertEqual(loaded["techniques_used"][0]["before"], 8)
        self.assertEqual(loaded["message_index"], 1)
        self.assertTrue(loaded["free_session_available"])

    def test_state_normalizer_bounds_scores_and_keeps_required_shape(self):
        state = normalize_psychology_dialog_state({
            "stage": "unknown", "impact_score": 99, "age": 500,
            "priority": "unknown", "manifestations": {"thoughts": "тревога"},
        })
        self.assertEqual(state["stage"], "request")
        self.assertEqual(state["impact_score"], 10)
        self.assertEqual(state["age"], 120)
        self.assertEqual(state["priority"], "routine")
        self.assertEqual(state["category_primary"], "unclear")
        self.assertEqual(state["manifestations"]["body"], "")

    def test_client_card_respects_summary_consent(self):
        chat = self.create_chat()
        base = {
            "name": "Анна", "preferred_time": "после 18:00",
            "category_primary": "anxiety", "client_quote": "Мне тревожно",
            "duration": "три недели", "impact_score": 7,
            "context": "нагрузка на работе", "consent_summary": False,
        }
        db.save_psychology_dialog_state(chat["id"], base)
        private = db.psychology_client_card(chat["id"], "12345")
        self.assertIn("согласия на выжимку нет", private)
        self.assertNotIn("нагрузка на работе", private)
        base["consent_summary"] = True
        db.save_psychology_dialog_state(chat["id"], base, increment_message=False)
        full = db.psychology_client_card(chat["id"], "12345")
        self.assertIn("нагрузка на работе", full)
        self.assertIn("Гипотеза-вопрос", full)
        self.assertLessEqual(len(full), 1300)

    def test_psychology_thresholds_are_admin_configurable(self):
        current = db.admin_assistant_personality_settings("masterclass_psychology")
        saved = db.admin_update_assistant_personality_settings({
            **current,
            "profile_key": "masterclass_psychology",
            "duration_trigger_days": 21,
            "impact_trigger_score": 7,
            "technique_shift_trigger": 3,
            "max_messages_before_offer": 9,
            "offer_cooldown_messages": 4,
        })
        self.assertEqual(saved["duration_trigger_days"], 21)
        self.assertEqual(saved["impact_trigger_score"], 7)
        self.assertEqual(saved["offer_cooldown_messages"], 4)

    def test_minor_is_not_offered_a_paid_session(self):
        chat = self.create_chat()
        db.save_psychology_dialog_state(chat["id"], {})
        db.save_profile({"preferred_name": "Ира", "age": 17})
        with self.assertRaisesRegex(ValueError, "несовершеннолетним"):
            db.create_psychology_session_payment_order(chat["id"])

    def test_prompt_contains_required_safety_and_technique_rules(self):
        self.assertIn("Спасибо, что сказали. Мне важно уточнить", MASTERCLASS_PSYCHOLOGY_CHAT)
        self.assertIn("8-800-2000-122", MASTERCLASS_PSYCHOLOGY_CHAT)
        self.assertIn("5-4-3-2-1", MASTERCLASS_PSYCHOLOGY_CHAT)
        self.assertIn("максимум две", MASTERCLASS_PSYCHOLOGY_CHAT)
        self.assertIn("согласия на выжимку нет", MASTERCLASS_PSYCHOLOGY_CHAT)
        self.assertIn("dialog_state", AGENT_RESULT_JSON_SCHEMA["required"])

    def test_risk_filter_distinguishes_clarification_from_immediate_danger(self):
        self.assertTrue(_psychology_crisis_detected("Я не хочу жить"))
        self.assertFalse(_psychology_immediate_crisis_detected("Я не хочу жить"))
        self.assertFalse(_psychology_immediate_crisis_detected("У меня есть план пойти в кино"))
        self.assertTrue(_psychology_immediate_crisis_detected(
            "У меня есть план покончить с собой сегодня"
        ))

    def test_orchestrator_persists_dialog_state_returned_by_vita(self):
        chat = self.create_chat()

        class FakeVita:
            def route(self, history, context, conversation, attachments=None):
                return RouteDecision(
                    "continue", "psychologist", "Психологический чат",
                    normalize_context(context),
                )

            def answer(self, agent_id, history, context, route_decision, conversation, attachments=None):
                state = dict(conversation["_psychology_dialog_state"])
                state.update({
                    "stage": "details", "category_primary": "stress",
                    "client_quote": "Я очень устал",
                })
                return AgentResult(
                    message="Похоже, напряжение накопилось. Как это ощущается в теле?",
                    next_action="ask", target_agent=None, handoff_reason="",
                    urgency="routine", missing_information=["Проявления в теле"],
                    dialog_state=state,
                )

        response = ConversationOrchestrator(FakeVita()).process(
            chat["id"], "Я очень устал",
        )
        state = db.get_psychology_dialog_state(chat["id"])
        self.assertEqual(response.agent, "psychologist")
        self.assertEqual(state["stage"], "details")
        self.assertEqual(state["category_primary"], "stress")

    def test_session_notification_contains_private_client_card(self):
        chat = self.create_chat()
        db.save_psychology_dialog_state(chat["id"], {
            "name": "Анна", "category_primary": "anxiety",
            "client_quote": "Мне тревожно", "duration": "три недели",
            "impact_score": 7, "consent_summary": True,
        })
        session = db.request_psychology_session(chat["id"])
        now = db.utc_now()
        with db.connection() as conn:
            conn.execute(
                """INSERT INTO staff_users
                   (display_name,login,password_hash,role,is_active,max_chat_id,
                    notify_new_requests,created_at,updated_at)
                   VALUES ('Психолог','vita_psychologist','x','psychologist',1,
                           'max-test',1,?,?)""",
                (now, now),
            )
            conn.commit()
        queued = db.enqueue_manager_notifications(
            "psychology_session_requested", chat["id"],
            recipient_role="psychologist",
            request_number=session["request_number"],
        )
        self.assertEqual(queued, 1)
        items = db.claim_manager_notifications("max")
        self.assertEqual(len(items), 1)
        payload = items[0]["payload"]
        self.assertIn("Карточка", "Карточка")
        self.assertIn("Фраза клиента", payload["client_card"])
        self.assertIn("Мне тревожно", payload["body"])
        detail = db.manager_conversation_detail(chat["id"])
        self.assertIn("Гипотеза-вопрос", detail["psychology_client_card"])

    def test_confirmed_or_immediate_danger_stops_ai_and_notifies_psychologist(self):
        class FakeHandler:
            path = "/api/chat"
            response = None

            def _ensure_user_context(self):
                return None

            def _read_json(self, **_kwargs):
                return {
                    "conversation_id": "psychology-danger",
                    "message": "У меня есть план покончить с собой сегодня",
                    "attachments": [],
                }

            def _validate_attachments(self, attachments):
                return attachments

            def _track_analytics(self, *_args, **_kwargs):
                return None

            def _json(self, status, payload):
                self.response = (status, payload)
                return self.response

        conversation = {
            "id": "psychology-danger", "dialog_type": "masterclass_psychology",
            "ai_enabled": True, "human_status": "none", "context_summary": "{}",
        }
        confirmed = {
            **conversation, "ai_enabled": False, "human_status": "pending",
            "human_ticket_id": "URG-123",
        }
        with (
            patch("backend.main._chat_access_allowed", return_value=False),
            patch("backend.main.db.get_conversation", return_value=conversation),
            patch("backend.main.db.list_messages", return_value=[{"id": 1}]),
            patch("backend.main.db.add_message", side_effect=[
                {"id": 91, "role": "user"}, {"id": 92, "role": "assistant"},
            ]),
            patch("backend.main.db.get_psychology_dialog_state", return_value={
                "risk_flags": [], "priority": "routine",
            }),
            patch("backend.main.db.save_psychology_dialog_state"),
            patch("backend.main.db.get_profile", return_value={"age": 30}),
            patch("backend.main.db.confirm_human_chat", return_value=(confirmed, True)),
            patch("backend.main.db.enqueue_manager_notifications") as notify,
        ):
            handler = FakeHandler()
            ConsiliumHandler.do_POST(handler)
        self.assertEqual(handler.response[0], 202)
        self.assertFalse(handler.response[1]["ai_enabled"])
        self.assertTrue(handler.response[1]["emergency"])
        self.assertIsNotNone(handler.response[1]["assistant_message"])
        notify.assert_called_once_with(
            "psychology_crisis", "psychology-danger",
            message_id=91, recipient_role="psychologist",
        )


if __name__ == "__main__":
    unittest.main()
