import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import database as db
from backend.config import settings
from backend.llm import LLMService
from backend.schemas import RouteDecision


class AssistantPersonalityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = settings.database_path
        object.__setattr__(settings, "database_path", Path(self.temp_dir.name) / "main.db")
        db.init_db()

    def tearDown(self):
        db.set_current_chel_id("chel_test_default")
        object.__setattr__(settings, "database_path", self.original_database)
        self.temp_dir.cleanup()

    def test_balanced_personality_is_available_by_default(self):
        item = db.admin_assistant_personality_settings()
        self.assertEqual(item["preset"], "balanced")
        self.assertEqual(item["formality"], 35)
        self.assertEqual(item["warmth"], 80)
        self.assertEqual(item["sociability"], 70)
        self.assertEqual(item["supportiveness"], 85)
        self.assertEqual(item["address_mode"], "formal")
        self.assertEqual(item["humor"], 15)
        self.assertEqual(item["emoji"], 5)
        self.assertEqual(item["initiative"], 60)
        self.assertFalse(item["apply_to_all"])
        self.assertEqual(item["test_branch_model"], "deepseek-v4.1-flash")

    def test_custom_personality_is_persisted_and_used_in_prompt(self):
        saved = db.admin_update_assistant_personality_settings({
            "preset": "custom",
            "apply_to_all": True,
            "address_mode": "informal",
            "formality": 10,
            "warmth": 95,
            "sociability": 90,
            "supportiveness": 100,
            "humor": 80,
            "emoji": 30,
            "initiative": 90,
            "test_branch_model": "qwen3.6-35b-a3b",
        })
        self.assertEqual(saved["preset"], "custom")
        self.assertTrue(saved["apply_to_all"])
        self.assertEqual(db.admin_assistant_personality_settings()["sociability"], 90)
        self.assertEqual(saved["test_branch_model"], "qwen3.6-35b-a3b")

        prompt = db.assistant_personality_prompt()
        self.assertIn("Формальность 10/100", prompt)
        self.assertIn("Теплота 95/100", prompt)
        self.assertIn("будь общительной", prompt)
        self.assertIn("общайся с пользователем на «ты»", prompt)
        self.assertIn("Юмор 80/100", prompt)
        self.assertIn("Никогда не шути", prompt)
        self.assertIn("не отменяет медицинскую точность", prompt)

    def test_invalid_personality_values_are_rejected(self):
        invalid_payloads = (
            {"preset": "unknown"},
            {"formality": -1},
            {"warmth": 101},
            {"sociability": "много"},
            {"address_mode": "как получится"},
            {"humor": 101},
            {"apply_to_all": "везде"},
            {"test_branch_model": "some-unknown-model"},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                db.admin_update_assistant_personality_settings(payload)

    def test_existing_personality_table_is_migrated(self):
        with db.connection() as connection:
            connection.execute("DROP TABLE assistant_personality_settings")
            connection.execute("""CREATE TABLE assistant_personality_settings (
                id INTEGER PRIMARY KEY, preset TEXT NOT NULL,
                formality INTEGER NOT NULL, warmth INTEGER NOT NULL,
                sociability INTEGER NOT NULL, supportiveness INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            connection.execute(
                "INSERT INTO assistant_personality_settings VALUES (1,'balanced',35,80,70,85,'now')"
            )
            connection.commit()

        db.init_db()
        item = db.admin_assistant_personality_settings()
        self.assertEqual(item["address_mode"], "formal")
        self.assertEqual(item["humor"], 15)
        self.assertEqual(item["emoji"], 5)
        self.assertEqual(item["initiative"], 60)
        self.assertFalse(item["apply_to_all"])
        self.assertEqual(item["test_branch_model"], "deepseek-v4.1-flash")

    def test_personality_prompt_is_enabled_only_for_yandex_test_branch(self):
        service = LLMService()
        db.ensure_user("chel_personality_test", pending=True)
        db.set_current_chel_id("chel_personality_test")

        self.assertEqual(db.current_ai_branch(), db.AI_BRANCH_MAIN)
        self.assertEqual(service._assistant_personality_prompt(), "")

        db.set_ai_branch("chel_personality_test", db.AI_BRANCH_TEST)
        self.assertIn("Настройка характера Ольги", service._assistant_personality_prompt())

        db.set_ai_branch("chel_personality_test", db.AI_BRANCH_MAIN)
        db.admin_update_assistant_personality_settings({"apply_to_all": True})
        self.assertIn("Настройка характера Ольги", service._assistant_personality_prompt())

    def test_informal_address_and_emoji_reach_actual_answer_request(self):
        db.ensure_user("chel_personality_request", pending=True)
        db.set_current_chel_id("chel_personality_request")
        db.admin_update_assistant_personality_settings({
            "preset": "custom",
            "apply_to_all": True,
            "address_mode": "informal",
            "formality": 10,
            "warmth": 90,
            "sociability": 80,
            "supportiveness": 85,
            "humor": 70,
            "emoji": 25,
            "initiative": 75,
        })
        captured = {}
        response = {
            "output": [{"type": "message", "content": [{
                "type": "output_text",
                "text": '{"message":"Хорошо 🙂","next_action":"respond","target_agent":null,"handoff_reason":"","urgency":"routine","missing_information":[]}',
            }]}],
        }

        def fake_request(payload):
            captured.update(payload)
            return response

        service = LLMService()
        with mock.patch.object(service, "_request", side_effect=fake_request):
            service.answer(
                "general", [{"role": "user", "content": "Как улучшить сон?"}], {},
                RouteDecision("respond", "general", "test", {}),
                {"active_agent": "general"},
            )

        self.assertIn("обязательно общайся с пользователем на «ты»", captured["instructions"])
        self.assertIn("добавляй ровно один уместный эмодзи", captured["instructions"])
        for expected in (
            "Выбранный профиль: custom", "Формальность 10/100",
            "Теплота 90/100", "Общительность 80/100", "Поддержка 85/100",
            "Юмор 70/100", "Эмодзи 25/100", "Инициативность 75/100",
            "Одновременно соблюдай каждый параметр выше",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, captured["instructions"])

    def test_admin_ui_and_user_facing_llm_modes_are_connected(self):
        project = Path(__file__).resolve().parents[1]
        html = (project / "dashboard.html").read_text(encoding="utf-8")
        javascript = (project / "static" / "dashboard.js").read_text(encoding="utf-8")
        llm = (project / "backend" / "llm.py").read_text(encoding="utf-8")

        self.assertIn('id="personalityTab"', html)
        self.assertIn('id="personalityForm"', html)
        self.assertIn('id="personalityYandexOnly"', html)
        self.assertIn('id="personalityTestBranchModel"', html)
        self.assertIn("apply_to_all:!$('#personalityYandexOnly').checked", javascript)
        self.assertIn("test_branch_model:$('#personalityTestBranchModel').value", javascript)
        self.assertIn("/api/admin/assistant-personality", javascript)
        self.assertIn("schedulePersonalitySave", javascript)
        self.assertGreaterEqual(llm.count("self._assistant_personality_prompt()"), 6)
        self.assertLess(
            llm.index("self._assistant_personality_prompt()"),
            llm.index("def generate_health_passport"),
        )


if __name__ == "__main__":
    unittest.main()
