import tempfile
import unittest
from pathlib import Path

from backend import database as db
from backend.config import settings


class BroadcastTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = settings.database_path
        self.original_public_url = settings.public_base_url
        object.__setattr__(settings, "database_path", Path(self.temp_dir.name) / "broadcasts.db")
        object.__setattr__(settings, "public_base_url", "https://consilium.test")
        db.init_db()
        now = db.utc_now()
        for chel_id in ("chel_broadcast_one", "chel_broadcast_two"):
            db.ensure_user(chel_id, pending=True)
        with db.connection() as conn:
            conn.executemany(
                """INSERT INTO external_identities
                   (provider,provider_user_id,chat_id,chel_id,access_status,created_at,last_login_at)
                   VALUES (?,?,?,?, 'active',?,?)""",
                [
                    ("telegram", "101", "", "chel_broadcast_one", now, now),
                    ("max", "201", "301", "chel_broadcast_one", now, now),
                    ("telegram", "102", "", "chel_broadcast_two", now, now),
                ],
            )
            conn.commit()

    def tearDown(self):
        object.__setattr__(settings, "database_path", self.original_path)
        object.__setattr__(settings, "public_base_url", self.original_public_url)
        self.temp_dir.cleanup()

    def test_message_delivery_and_unique_click_statistics(self):
        campaign = db.create_broadcast_campaign({
            "kind": "message", "title": "Новости", "body": "Полезный материал",
            "button_label": "Открыть Консилиум", "provider_scope": "all",
        })
        self.assertEqual(campaign["users"], 2)
        self.assertEqual(campaign["recipients"], 3)
        telegram = db.claim_broadcast_notifications("telegram", 5)
        max_items = db.claim_broadcast_notifications("max", 5)
        self.assertEqual(len(telegram), 2)
        self.assertEqual(len(max_items), 1)
        self.assertEqual(telegram[0]["payload"]["kind"], "broadcast_message")
        self.assertIn("/broadcast/open?t=", telegram[0]["payload"]["action_url"])
        for item in telegram + max_items:
            raw_id = abs(item["id"]) - db.BROADCAST_NOTIFICATION_ID_OFFSET
            self.assertTrue(db.acknowledge_broadcast_notification(
                raw_id, item["lease_token"], True,
            ))
        with db.connection() as conn:
            click_token = conn.execute(
                """SELECT click_token FROM broadcast_recipients
                   WHERE campaign_id=? AND chel_id='chel_broadcast_one' LIMIT 1""",
                (campaign["id"],),
            ).fetchone()[0]
        self.assertEqual(db.record_broadcast_click(click_token), "https://consilium.test")
        self.assertEqual(db.record_broadcast_click(click_token), "https://consilium.test")
        report = db.admin_broadcast_report()["campaigns"][0]
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["sent"], 3)
        self.assertEqual(report["unique_clicks"], 1)
        self.assertEqual(report["total_clicks"], 2)

    def test_poll_counts_one_user_and_locks_first_answer(self):
        campaign = db.create_broadcast_campaign({
            "kind": "poll", "title": "Опрос", "body": "Как вы себя чувствуете?",
            "provider_scope": "telegram", "options": ["Хорошо", "Нормально", "Плохо"],
        })
        notification = db.claim_broadcast_notifications("telegram", 1)[0]
        options = notification["payload"]["poll_options"]
        first = db.record_broadcast_vote("telegram", "101", options[0]["callback_data"])
        self.assertFalse(first["changed"])
        self.assertFalse(first["already_recorded"])
        repeated = db.record_broadcast_vote("telegram", "101", options[1]["callback_data"])
        self.assertFalse(repeated["changed"])
        self.assertTrue(repeated["already_recorded"])
        self.assertEqual(repeated["label"], "Хорошо")
        db.record_broadcast_vote("telegram", "102", options[1]["callback_data"])
        report = db.admin_broadcast_report()["campaigns"][0]
        self.assertEqual(report["voters"], 2)
        votes = {item["label"]: item["votes"] for item in report["options"]}
        self.assertEqual(votes, {"Хорошо": 1, "Нормально": 1, "Плохо": 0})
        self.assertEqual(campaign["users"], 2)

    def test_test_send_targets_only_requested_chel_id(self):
        result = db.create_broadcast_campaign({
            "kind": "message", "body": "Проверка", "provider_scope": "all",
        }, test_chel_id="chel_broadcast_one")
        self.assertTrue(result["is_test"])
        self.assertEqual(result["users"], 1)
        self.assertEqual(result["recipients"], 2)
        self.assertEqual(db.admin_broadcast_report()["campaigns"], [])
        with self.assertRaisesRegex(ValueError, "нет активной привязки"):
            db.create_broadcast_campaign({
                "kind": "message", "body": "Проверка", "provider_scope": "max",
            }, test_chel_id="chel_broadcast_two")

    def test_invalid_poll_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "от 2 до 10"):
            db.create_broadcast_campaign({
                "kind": "poll", "body": "Один ответ", "options": ["Да"],
            })

    def test_transient_delivery_errors_stop_after_bounded_retries(self):
        campaign = db.create_broadcast_campaign({
            "kind": "message", "body": "Проверка повторов",
            "provider_scope": "max",
        })
        for _ in range(db.BROADCAST_MAX_ATTEMPTS):
            with db.connection() as conn:
                conn.execute(
                    "UPDATE broadcast_recipients SET next_attempt_at=NULL WHERE campaign_id=?",
                    (campaign["id"],),
                )
                conn.commit()
            notification = db.claim_broadcast_notifications("max", 1)[0]
            raw_id = abs(notification["id"]) - db.BROADCAST_NOTIFICATION_ID_OFFSET
            self.assertTrue(db.acknowledge_broadcast_notification(
                raw_id, notification["lease_token"], False, "temporary timeout",
            ))
        self.assertEqual(db.claim_broadcast_notifications("max", 1), [])
        report = db.admin_broadcast_report()["campaigns"][0]
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["failed"], 1)

    def test_broadcast_admin_view_hides_main_dashboard_and_labels_analytics(self):
        project = Path(__file__).resolve().parents[1]
        css = (project / "static" / "dashboard.css").read_text(encoding="utf-8")
        html = (project / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn(
            ".dashboard.show-broadcasts > :not(.dashboard-header):not(.admin-tabs):not(.broadcasts-admin)",
            css,
        )
        self.assertIn("Аналитика и история рассылок", html)
        script = (project / "static" / "dashboard.js").read_text(encoding="utf-8")
        main = (project / "backend" / "main.py").read_text(encoding="utf-8")
        self.assertIn("Для подтверждения введите слово ОТПРАВИТЬ", script)
        self.assertIn("payload.confirmation = 'ОТПРАВИТЬ'", script)
        self.assertIn("Подтвердите массовую рассылку словом ОТПРАВИТЬ", main)


if __name__ == "__main__":
    unittest.main()
