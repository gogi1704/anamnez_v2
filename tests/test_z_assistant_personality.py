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
        self.assertEqual(item["provider"], "openai")
        self.assertEqual(item["openai_model"], "gpt-5.6-sol")
        self.assertFalse(item["apply_to_all"])
        self.assertEqual(item["test_branch_model"], "deepseek-v4.1-flash")
        self.assertEqual(item["test_branch_fallback_model"], "qwen3.6-35b-a3b")

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
            "provider": "yandex",
            "openai_model": "gpt-5.6-terra",
            "test_branch_model": "qwen3.6-35b-a3b",
            "test_branch_fallback_model": "gpt-oss-120b",
        })
        self.assertEqual(saved["preset"], "custom")
        self.assertTrue(saved["apply_to_all"])
        self.assertEqual(db.admin_assistant_personality_settings()["sociability"], 90)
        self.assertEqual(saved["test_branch_model"], "qwen3.6-35b-a3b")
        self.assertEqual(saved["test_branch_fallback_model"], "gpt-oss-120b")
        self.assertEqual(saved["provider"], "yandex")
        self.assertEqual(saved["openai_model"], "gpt-5.6-terra")

        prompt = db.assistant_personality_prompt()
        self.assertIn("Формальность 10/100", prompt)
        self.assertIn("Теплота 95/100", prompt)
        self.assertIn("будь общительной", prompt)
        self.assertIn("общайся с пользователем на «ты»", prompt)
        self.assertIn("Юмор 80/100", prompt)
        self.assertIn("Никогда не шути", prompt)
        self.assertIn("не отменяет медицинскую точность", prompt)

    def test_dialog_types_have_independent_personality_profiles(self):
        psychology = db.admin_assistant_personality_settings(
            "masterclass_psychology"
        )
        self.assertEqual(psychology["initiative"], 25)
        self.assertEqual(psychology["address_mode"], "contextual")

        saved = db.admin_update_assistant_personality_settings({
            "profile_key": "masterclass_psychology",
            "preset": "custom",
            "initiative": 10,
            "emoji": 0,
            "provider": "yandex",
            "openai_model": "gpt-5.6-luna",
        })
        self.assertEqual(saved["profile_key"], "masterclass_psychology")
        self.assertEqual(saved["initiative"], 10)
        self.assertEqual(saved["emoji"], 0)
        self.assertEqual(saved["provider"], "yandex")
        self.assertEqual(saved["openai_model"], "gpt-5.6-luna")
        self.assertEqual(db.admin_assistant_personality_settings()["provider"], "openai")
        self.assertEqual(db.admin_assistant_personality_settings()["initiative"], 60)
        self.assertEqual(
            db.admin_assistant_personality_settings("weight_control")["initiative"],
            60,
        )

        profiles = db.admin_assistant_personality_profiles()
        self.assertEqual(
            set(profiles), {"general", "weight_control", "masterclass_psychology"},
        )

    def test_llm_selects_personality_by_dialog_type(self):
        db.ensure_user("chel_profile_scoping", pending=True)
        db.set_current_chel_id("chel_profile_scoping")
        db.set_ai_branch("chel_profile_scoping", db.AI_BRANCH_TEST)
        db.admin_update_assistant_personality_settings({
            "profile_key": "general", "preset": "custom", "formality": 80,
        })
        db.admin_update_assistant_personality_settings({
            "profile_key": "masterclass_psychology", "preset": "custom",
            "formality": 10,
        })

        service = LLMService()
        general = service._assistant_personality_prompt({"dialog_type": "general"})
        psychology = service._assistant_personality_prompt({
            "dialog_type": "masterclass_psychology",
        })
        self.assertIn("Формальность 80/100", general)
        self.assertIn("Формальность 10/100", psychology)

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
            {"test_branch_fallback_model": "some-unknown-model"},
            {"provider": "unknown"},
            {"openai_model": "unknown"},
            {
                "test_branch_model": "qwen3.6-35b-a3b",
                "test_branch_fallback_model": "qwen3.6-35b-a3b",
            },
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
        self.assertEqual(item["provider"], "openai")
        self.assertEqual(item["openai_model"], "gpt-5.6-sol")
        self.assertFalse(item["apply_to_all"])
        self.assertEqual(item["test_branch_model"], "deepseek-v4.1-flash")
        self.assertEqual(item["test_branch_fallback_model"], "qwen3.6-35b-a3b")

    def test_existing_profile_table_gets_provider_columns_without_losing_style(self):
        with db.connection() as connection:
            connection.execute("DROP TABLE assistant_personality_profiles")
            connection.execute("""CREATE TABLE assistant_personality_profiles (
                profile_key TEXT PRIMARY KEY,
                preset TEXT NOT NULL,
                address_mode TEXT NOT NULL,
                formality INTEGER NOT NULL,
                warmth INTEGER NOT NULL,
                sociability INTEGER NOT NULL,
                supportiveness INTEGER NOT NULL,
                humor INTEGER NOT NULL,
                emoji INTEGER NOT NULL,
                initiative INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            connection.execute(
                """INSERT INTO assistant_personality_profiles VALUES
                   ('weight_control','custom','informal',11,92,83,94,22,17,73,'now')"""
            )
            connection.commit()

        db.init_db()
        item = db.admin_assistant_personality_settings("weight_control")
        self.assertEqual(item["preset"], "custom")
        self.assertEqual(item["formality"], 11)
        self.assertEqual(item["provider"], "openai")
        self.assertEqual(item["openai_model"], "gpt-5.6-sol")

    def test_yandex_model_update_does_not_change_dialog_profiles(self):
        db.admin_update_assistant_personality_settings({
            "profile_key": "masterclass_psychology",
            "preset": "custom",
            "provider": "openai",
            "openai_model": "gpt-5.6-terra",
            "warmth": 97,
        })
        before = db.admin_assistant_personality_settings("masterclass_psychology")
        db.admin_update_yandex_model_settings({
            "test_branch_model": "gpt-oss-120b",
            "test_branch_fallback_model": "qwen3.6-35b-a3b",
        })
        after = db.admin_assistant_personality_settings("masterclass_psychology")
        for key in ("preset", "provider", "openai_model", "warmth"):
            self.assertEqual(after[key], before[key])

    def test_personality_prompt_is_enabled_for_every_selected_chat_provider(self):
        service = LLMService()
        db.ensure_user("chel_personality_test", pending=True)
        db.set_current_chel_id("chel_personality_test")

        self.assertEqual(db.current_ai_branch(), db.AI_BRANCH_MAIN)
        self.assertIn("Настройка характера Ольги", service._assistant_personality_prompt())

        db.set_ai_branch("chel_personality_test", db.AI_BRANCH_TEST)
        self.assertIn("Настройка характера Ольги", service._assistant_personality_prompt())

    def test_each_chat_profile_routes_to_its_selected_provider_and_model(self):
        db.admin_update_yandex_model_settings({
            "test_branch_model": "qwen3.6-35b-a3b",
            "test_branch_fallback_model": "gpt-oss-120b",
        })
        db.admin_update_assistant_personality_settings({
            "profile_key": "masterclass_psychology",
            "provider": "yandex",
        })
        db.admin_update_assistant_personality_settings({
            "profile_key": "weight_control",
            "provider": "openai",
            "openai_model": "gpt-5.6-luna",
        })
        service = LLMService()
        response = {"output": [{"type": "message", "content": [{
            "type": "output_text", "text": "ok",
        }]}]}

        with mock.patch.object(service, "_yandex_request", return_value=response) as yandex:
            service._request({
                "_personality_profile_key": "masterclass_psychology",
                "model": "ignored",
            })
        self.assertEqual(yandex.call_args.kwargs["model"], "qwen3.6-35b-a3b")

        with mock.patch.object(service, "_openai_request", return_value=response) as openai:
            service._request({
                "_personality_profile_key": "weight_control",
                "model": "ignored",
            })
        self.assertEqual(openai.call_args.args[0]["model"], "gpt-5.6-luna")

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
        self.assertIn('id="personalityYandexForm"', html)
        self.assertIn('id="personalityTestBranchModel"', html)
        self.assertIn('id="personalityTestBranchFallbackModel"', html)
        self.assertIn('id="personalityProvider"', html)
        self.assertIn('id="personalityOpenAIModel"', html)
        self.assertIn('data-personality-profile="general"', html)
        self.assertIn('data-personality-profile="weight_control"', html)
        self.assertIn('data-personality-profile="masterclass_psychology"', html)
        self.assertIn("profile_key:activePersonalityProfile", javascript)
        self.assertIn("provider:$('#personalityProvider').value", javascript)
        self.assertIn("openai_model:$('#personalityOpenAIModel').value", javascript)
        self.assertIn("settings_scope:'yandex'", javascript)
        self.assertIn(".dashboard.show-personality", (project / "static" / "dashboard.css").read_text(encoding="utf-8"))
        self.assertIn("/api/admin/assistant-personality", javascript)
        self.assertIn("schedulePersonalitySave", javascript)
        self.assertGreaterEqual(llm.count("self._assistant_personality_prompt"), 6)
        self.assertGreaterEqual(llm.count("self._weight_control_personality_prompt(state)"), 2)
        self.assertLess(
            llm.index("self._assistant_personality_prompt(conversation)"),
            llm.index("def generate_health_passport"),
        )


if __name__ == "__main__":
    unittest.main()
