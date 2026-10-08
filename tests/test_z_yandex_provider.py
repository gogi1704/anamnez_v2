import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import database as db
from backend.config import settings
from backend.llm import LLMNotConfigured, LLMProviderError, LLMService


class FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def canned(text: str) -> dict:
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}], "usage": {}}


def isolated_database(test: unittest.TestCase) -> None:
    temp_dir = tempfile.TemporaryDirectory()
    original_path = settings.database_path
    object.__setattr__(settings, "database_path", Path(temp_dir.name) / "main.db")
    db.init_db()
    db.ensure_user("chel_llm_test", pending=True)
    db.set_current_chel_id("chel_llm_test")

    def restore():
        object.__setattr__(settings, "database_path", original_path)
        db.set_current_chel_id("chel_test_default")
        temp_dir.cleanup()

    test.addCleanup(restore)


class YandexProviderTests(unittest.TestCase):
    def setUp(self):
        isolated_database(self)
        self.original = {
            "llm_provider": settings.llm_provider,
            "yandex_folder_id": settings.yandex_folder_id,
            "yandex_api_key": settings.yandex_api_key,
            "yandex_model": settings.yandex_model,
        }
        object.__setattr__(settings, "llm_provider", "yandex")
        object.__setattr__(settings, "yandex_folder_id", "b1testfolder")
        object.__setattr__(settings, "yandex_api_key", "test-key")
        object.__setattr__(settings, "yandex_model", "yandexgpt-5.1")

    def tearDown(self):
        for name, value in self.original.items():
            object.__setattr__(settings, name, value)

    def send(self, payload: dict, reply: str) -> tuple[dict, dict]:
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(canned(reply))

        with mock.patch("backend.llm.urllib.request.urlopen", side_effect=fake_urlopen):
            result = LLMService()._request(payload)
        return captured, result

    def test_request_targets_yandex_with_api_key_and_folder_model(self):
        captured, _ = self.send({"instructions": "Роль.", "input": "Вопрос"}, "ok")
        self.assertEqual(captured["url"], "https://ai.api.cloud.yandex.net/v1/responses")
        self.assertEqual(captured["headers"]["Authorization"], "Api-Key test-key")
        self.assertEqual(captured["body"]["model"], "gpt://b1testfolder/yandexgpt-5.1")
        self.assertIn("Роль.", captured["body"]["input"])
        self.assertIn("Вопрос", captured["body"]["input"])
        self.assertNotIn("reasoning", captured["body"])
        self.assertNotIn("store", captured["body"])

    def test_json_schema_is_described_in_instructions_and_fences_are_removed(self):
        schema = {"type": "object", "properties": {"message": {"type": "string"}}}
        payload = {
            "instructions": "Роль.",
            "input": "Вопрос",
            "text": {"format": {"type": "json_schema", "name": "x", "schema": schema}},
        }
        captured, result = self.send(payload, '```json\n{"message": "привет"}\n```')
        self.assertIn('"message"', captured["body"]["input"])
        text = LLMService._output_text(result)
        self.assertEqual(json.loads(text), {"message": "привет"})

    def test_deepseek_gets_reasoning_effort_others_do_not(self):
        original = settings.yandex_reasoning_effort
        try:
            object.__setattr__(settings, "yandex_reasoning_effort", "none")
            object.__setattr__(settings, "yandex_model", "deepseek-v4-flash")
            captured, _ = self.send({"instructions": "x", "input": "y"}, "ok")
            self.assertEqual(captured["body"]["reasoning"], {"effort": "none"})
            object.__setattr__(settings, "yandex_model", "yandexgpt-5.1")
            captured, _ = self.send({"instructions": "x", "input": "y"}, "ok")
            self.assertNotIn("reasoning", captured["body"])
        finally:
            object.__setattr__(settings, "yandex_reasoning_effort", original)

    def test_health_passport_uses_the_passport_model_without_reasoning(self):
        original_passport = settings.yandex_passport_model
        try:
            object.__setattr__(settings, "yandex_model", "deepseek-v4-flash")
            object.__setattr__(settings, "yandex_passport_model", "yandexgpt-5.1")
            payload = {
                "instructions": "x",
                "input": "y",
                "text": {"format": {"type": "json_schema", "name": "health_passport", "schema": {}}},
            }
            captured, _ = self.send(payload, '{"status": "ok"}')
            self.assertEqual(captured["body"]["model"], "gpt://b1testfolder/yandexgpt-5.1")
            self.assertNotIn("reasoning", captured["body"])
        finally:
            object.__setattr__(settings, "yandex_passport_model", original_passport)

    def test_images_are_replaced_by_a_note_and_text_is_kept(self):
        payload = {
            "instructions": "Роль.",
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "Контекст пользователя"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"},
            ]}],
        }
        captured, _ = self.send(payload, "ok")
        self.assertIn("Контекст пользователя", captured["body"]["input"])
        self.assertIn("Изображение не передано", captured["body"]["input"])
        self.assertNotIn("data:image", captured["body"]["input"])

    def test_non_pdf_file_is_reported_not_sent(self):
        payload = {
            "instructions": "Роль.",
            "input": [{"role": "user", "content": [
                {"type": "input_file", "filename": "a.docx", "file_data": "data:application/msword;base64,AAAA"},
            ]}],
        }
        captured, _ = self.send(payload, "ok")
        self.assertIn("Файл не передан", captured["body"]["input"])

    def test_http_error_becomes_provider_error(self):
        import urllib.error

        def failing(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, _Body(b'{"error":"bad key"}'),
            )

        with mock.patch("backend.llm.urllib.request.urlopen", side_effect=failing):
            with self.assertRaisesRegex(LLMProviderError, "Yandex AI Studio"):
                LLMService()._request({"instructions": "x", "input": "y"})

    def test_main_yandex_branch_does_not_use_the_secret_branch_fallback(self):
        object.__setattr__(settings, "yandex_model", "deepseek-v4.1-flash")
        failed = {
            "status": "failed",
            "error": {"message": "503: Service temporarily unavailable"},
            "output": [],
        }
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(failed)

        with mock.patch("backend.llm.urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaisesRegex(LLMProviderError, "503"):
                LLMService()._request({"instructions": "x", "input": "y"})

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "gpt://b1testfolder/deepseek-v4.1-flash")

    def test_missing_credentials_are_reported_clearly(self):
        object.__setattr__(settings, "yandex_api_key", "")
        with self.assertRaisesRegex(LLMNotConfigured, "YANDEX_FOLDER_ID"):
            LLMService()._request({"instructions": "x", "input": "y"})

    def test_truncated_answer_is_reported_even_when_partial_text_exists(self):
        truncated = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"message": "обрыв'}]}],
        }
        with mock.patch("backend.llm.urllib.request.urlopen", return_value=FakeResponse(truncated)):
            with self.assertRaisesRegex(LLMProviderError, "оборвала ответ.*max_output_tokens"):
                LLMService()._request({"instructions": "x", "input": "y"})

    def test_reasoning_that_exhausts_the_limit_gives_a_clear_error(self):
        incomplete = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "reasoning", "content": []}],
        }
        with mock.patch("backend.llm.urllib.request.urlopen", return_value=FakeResponse(incomplete)):
            with self.assertRaisesRegex(LLMProviderError, "оборвала ответ"):
                LLMService()._request({"instructions": "x", "input": "y"})

    def test_empty_answer_is_rejected(self):
        with mock.patch("backend.llm.urllib.request.urlopen", return_value=FakeResponse({"output": []})):
            with self.assertRaisesRegex(LLMProviderError, "не вернула текстовый ответ"):
                LLMService()._request({"instructions": "x", "input": "y"})


def canned_with_usage(text: str, input_tokens: int = 1000, output_tokens: int = 500) -> dict:
    payload = canned(text)
    payload["usage"] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    return payload


class AiBranchTests(unittest.TestCase):
    TOKEN = "branch-test-secret"

    def setUp(self):
        isolated_database(self)
        names = ("llm_provider", "yandex_folder_id", "yandex_api_key", "yandex_model",
                 "test_branch_token")
        self.original = {name: getattr(settings, name) for name in names}
        for name, value in {
            "llm_provider": "openai",
            "yandex_folder_id": "b1testfolder",
            "yandex_api_key": "test-key",
            "yandex_model": "yandexgpt-5.1",
            "test_branch_token": self.TOKEN,
        }.items():
            object.__setattr__(settings, name, value)

    def tearDown(self):
        for name, value in self.original.items():
            object.__setattr__(settings, name, value)

    def capture(self, payloads: list[dict]):
        sent = []

        def fake_urlopen(request, timeout=None):
            sent.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(canned_with_usage('{"message": "ok"}'))

        return mock.patch("backend.llm.urllib.request.urlopen", side_effect=fake_urlopen), sent

    def test_only_the_secret_link_switches_the_branch(self):
        self.assertEqual(db.current_ai_branch(), db.AI_BRANCH_MAIN)
        db.apply_ai_branch_link("wrong-token")
        self.assertEqual(db.current_ai_branch(), db.AI_BRANCH_MAIN)
        db.apply_ai_branch_link(self.TOKEN)
        self.assertEqual(db.current_ai_branch(), db.AI_BRANCH_TEST)
        db.apply_ai_branch_link("main")
        self.assertEqual(db.current_ai_branch(), db.AI_BRANCH_MAIN)

    def test_secret_link_is_ignored_when_no_token_is_configured(self):
        object.__setattr__(settings, "test_branch_token", "")
        db.apply_ai_branch_link("")
        self.assertEqual(db.current_ai_branch(), db.AI_BRANCH_MAIN)

    def test_test_branch_sends_every_call_to_deepseek_including_passport(self):
        db.apply_ai_branch_link(self.TOKEN)
        patcher, sent = self.capture([])
        passport_format = {"format": {"type": "json_schema", "name": "health_passport", "schema": {}}}
        with patcher:
            LLMService()._request({"instructions": "x", "input": "y"})
            LLMService()._request({"instructions": "x", "input": "y", "text": passport_format})
        self.assertEqual([body["model"] for body in sent], ["gpt://b1testfolder/deepseek-v4.1-flash"] * 2)

    def test_deepseek_failure_falls_back_to_qwen_only_in_test_branch(self):
        db.apply_ai_branch_link(self.TOKEN)
        sent = []
        responses = [
            {
                "status": "failed",
                "error": {
                    "code": "model_call_error",
                    "message": "Error while calling model: 503: Service temporarily unavailable",
                },
                "output": [],
                "usage": {},
            },
            canned_with_usage('{"message": "ответ Qwen"}'),
        ]

        def fake_urlopen(request, timeout=None):
            sent.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(responses.pop(0))

        with mock.patch("backend.llm.urllib.request.urlopen", side_effect=fake_urlopen):
            result = LLMService()._request({"instructions": "x", "input": "y"})

        self.assertEqual(
            [body["model"] for body in sent],
            [
                "gpt://b1testfolder/deepseek-v4.1-flash",
                "gpt://b1testfolder/qwen3.6-35b-a3b",
            ],
        )
        self.assertEqual(result["model"], "qwen3.6-35b-a3b")

    def test_admin_can_select_qwen_or_openai_oss_for_test_branch(self):
        db.apply_ai_branch_link(self.TOKEN)
        for model in ("qwen3.6-35b-a3b", "gpt-oss-120b"):
            with self.subTest(model=model):
                db.admin_update_assistant_personality_settings({"test_branch_model": model})
                patcher, sent = self.capture([])
                with patcher:
                    result = LLMService()._request({"instructions": "x", "input": "y"})
                self.assertEqual(sent[0]["model"], f"gpt://b1testfolder/{model}")
                self.assertEqual(result["model"], model)
                self.assertEqual(len(sent), 1)

    def test_failed_status_includes_the_provider_reason(self):
        db.apply_ai_branch_link(self.TOKEN)
        db.admin_update_assistant_personality_settings({"test_branch_model": "qwen3.6-35b-a3b"})
        failed = {
            "status": "failed",
            "error": {"code": "model_call_error", "message": "503: Service temporarily unavailable"},
            "output": [],
        }
        with mock.patch(
            "backend.llm.urllib.request.urlopen", return_value=FakeResponse(failed),
        ):
            with self.assertRaisesRegex(LLMProviderError, "503: Service temporarily unavailable"):
                LLMService()._request({"instructions": "x", "input": "y"})

    def test_main_branch_keeps_the_configured_yandex_model(self):
        patcher, sent = self.capture([])
        passport_format = {"format": {"type": "json_schema", "name": "health_passport", "schema": {}}}
        with patcher:
            object.__setattr__(settings, "llm_provider", "yandex")
            LLMService()._request({"instructions": "x", "input": "y", "text": passport_format})
        self.assertEqual(sent[0]["model"], "gpt://b1testfolder/yandexgpt-5.1")

    def test_test_branch_costs_appear_as_a_deepseek_row(self):
        db.apply_ai_branch_link(self.TOKEN)
        patcher, _ = self.capture([])
        with patcher:
            LLMService()._request({"instructions": "x", "input": "y"})
        rows = [row for row in db.admin_ai_costs("all")["by_model"] if row["model"] == "deepseek-v4.1-flash"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["pricing_known"])
        expected = (1000 * 2.459016 + 500 * 4.09836) / 1_000_000
        self.assertAlmostEqual(rows[0]["total_cost_usd"], expected, places=6)


class ReadinessTests(unittest.TestCase):
    def test_yandex_mode_does_not_need_the_openai_key(self):
        from backend.main import ai_configured

        original = {name: getattr(settings, name) for name in ("llm_provider", "openai_api_key", "yandex_folder_id", "yandex_api_key")}
        try:
            object.__setattr__(settings, "llm_provider", "yandex")
            object.__setattr__(settings, "openai_api_key", "")
            object.__setattr__(settings, "yandex_folder_id", "b1testfolder")
            object.__setattr__(settings, "yandex_api_key", "test-key")
            self.assertTrue(ai_configured())
            object.__setattr__(settings, "yandex_api_key", "")
            self.assertFalse(ai_configured())
            object.__setattr__(settings, "llm_provider", "openai")
            object.__setattr__(settings, "openai_api_key", "sk-test")
            self.assertTrue(ai_configured())
        finally:
            for name, value in original.items():
                object.__setattr__(settings, name, value)


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def close(self):
        return None


if __name__ == "__main__":
    unittest.main()
