import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend import database as db, notification_health
from backend.config import settings


class NotificationHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.original = {
            name: getattr(settings, name)
            for name in (
                "database_path", "notification_health_enabled",
                "notification_health_max_chat_id", "notification_health_send_time",
                "notification_health_timezone", "notification_health_expiry_hours",
            )
        }
        object.__setattr__(settings, "database_path", Path(self.temp_dir.name) / "health.db")
        object.__setattr__(settings, "notification_health_enabled", True)
        object.__setattr__(settings, "notification_health_max_chat_id", "429690835")
        object.__setattr__(settings, "notification_health_send_time", "09:00")
        object.__setattr__(settings, "notification_health_timezone", "Europe/Moscow")
        object.__setattr__(settings, "notification_health_expiry_hours", 3)
        db.init_db()

    def tearDown(self):
        for name, value in self.original.items():
            object.__setattr__(settings, name, value)
        self.temp_dir.cleanup()

    def test_denied_dialog_is_failed_immediately(self):
        self.assertTrue(db.queue_system_notification(
            "max", "111", "test", {"title": "Тест"}, "denied-test",
        ))
        item = db.claim_system_notifications("max", 1)[0]
        raw_id = -int(item["id"]) - db.SYSTEM_NOTIFICATION_ID_OFFSET
        self.assertTrue(db.acknowledge_system_notification(
            raw_id, item["lease_token"], False,
            "403 chat.denied: error.dialog.suspended",
        ))
        with db.connection() as conn:
            row = conn.execute(
                "SELECT status,next_attempt_at FROM system_notification_outbox WHERE id=?",
                (raw_id,),
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["next_attempt_at"])

    def test_expired_control_message_is_not_delivered(self):
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        db.queue_system_notification(
            "max", "111", "notification_health", {"title": "Старое"},
            "expired-test", expires_at=expired,
        )
        self.assertEqual(db.claim_system_notifications("max", 10), [])
        with db.connection() as conn:
            status = conn.execute(
                "SELECT status FROM system_notification_outbox WHERE dedupe_key='expired-test'",
            ).fetchone()["status"]
        self.assertEqual(status, "cancelled")

    def test_daily_check_is_deduplicated_and_contains_queue_state(self):
        now = datetime.now(timezone(timedelta(hours=3)))
        self.assertTrue(notification_health.queue_daily_check(now=now))
        self.assertFalse(notification_health.queue_daily_check(now=now))
        item = db.claim_system_notifications("max", 5)[0]
        self.assertEqual(item["event_type"], "notification_health")
        self.assertEqual(item["recipient_id"], "429690835")
        self.assertIn("Уведомления Консилиума работают", item["payload"]["title"])
        self.assertIn("Ожидают отправки", item["payload"]["body"])

    def test_init_reconciles_old_poison_rows(self):
        db.queue_system_notification(
            "max", "111", "test", {"title": "Тест"}, "legacy-poison",
        )
        with db.connection() as conn:
            conn.execute(
                """UPDATE system_notification_outbox
                   SET attempts=34,last_error='403 error.dialog.suspended'
                   WHERE dedupe_key='legacy-poison'""",
            )
            conn.commit()
        db.init_db()
        with db.connection() as conn:
            row = conn.execute(
                "SELECT status FROM system_notification_outbox WHERE dedupe_key='legacy-poison'",
            ).fetchone()
        self.assertEqual(row["status"], "failed")


if __name__ == "__main__":
    unittest.main()
