import base64
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

class ChatAttachmentTests(unittest.TestCase):
    def setUp(self):
        from backend import chat_attachments
        self.attachments = chat_attachments
        project_root = Path(__file__).resolve().parents[1]
        self.storage = project_root / ".codex-test-tmp" / f"attachments-{uuid.uuid4().hex}"
        self.storage.mkdir(parents=True)
        self.storage_patch = patch(
            "backend.chat_attachments.storage_dir", return_value=self.storage,
        )
        self.storage_patch.start()

    def tearDown(self):
        self.storage_patch.stop()
        shutil.rmtree(self.storage, ignore_errors=True)

    def test_original_file_is_persisted_and_public_metadata_has_actions(self):
        content = b"test pdf body"
        stored = self.attachments.store({
            "name": "results.pdf",
            "type": "application/pdf",
            "data_url": "data:application/pdf;base64," + base64.b64encode(content).decode(),
        }, "chel_attachment_owner")

        self.assertEqual(stored["name"], "results.pdf")
        self.assertEqual(stored["size"], len(content))
        self.assertTrue(stored["url"].startswith("/api/chat-attachments/"))
        self.assertEqual(stored["download_url"], stored["url"] + "?download=1")
        token = stored["url"].rsplit("/", 1)[-1]
        record, loaded = self.attachments.load(token)
        self.assertEqual(record["owner_chel_id"], "chel_attachment_owner")
        self.assertEqual(loaded, content)
        self.assertNotIn("data_url", self.attachments.public_metadata({
            **stored, "data_url": "secret", "text": "parsed text",
        }))

    def test_invalid_token_and_invalid_base64_are_rejected(self):
        self.assertIsNone(self.attachments.load("../secret"))
        with self.assertRaisesRegex(ValueError, "Некорректные данные"):
            self.attachments.store({
                "name": "bad.pdf", "type": "application/pdf",
                "data_url": "data:application/pdf;base64,not-base64!",
            }, "chel_attachment_owner")

    def test_both_chat_interfaces_render_open_and_download_controls(self):
        project_root = Path(__file__).resolve().parents[1]
        app = (project_root / "static" / "app.js").read_text(encoding="utf-8")
        manager = (project_root / "static" / "manager.js").read_text(encoding="utf-8")
        styles = (project_root / "static" / "styles.css").read_text(encoding="utf-8")
        manager_styles = (project_root / "static" / "manager.css").read_text(encoding="utf-8")

        self.assertIn("function chatAttachmentMarkup", app)
        self.assertIn("item.download_url", app)
        self.assertIn("Открыть", app)
        self.assertIn("Скачать", app)
        self.assertIn("item.download_url", manager)
        self.assertIn("manager-message-document", manager)
        self.assertIn(".message-document-icon", styles)
        self.assertIn(".manager-message-photo", manager_styles)


if __name__ == "__main__":
    unittest.main()
