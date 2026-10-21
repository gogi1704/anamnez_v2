import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend import data_retention, database as db
from backend.config import settings


class DataRetentionDryRunTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = settings.database_path
        self.original_values = {
            name: getattr(settings, name)
            for name in (
                "data_retention_enabled",
                "data_retention_dry_run",
                "data_retention_inactive_days",
                "data_retention_grace_days",
                "data_retention_interval_days",
                "data_retention_report_telegram_chat_id",
                "data_retention_report_max_chat_id",
                "data_retention_report_dir",
            )
        }
        object.__setattr__(
            settings, "database_path", Path(self.temp_dir.name) / "retention.db",
        )
        object.__setattr__(settings, "data_retention_enabled", True)
        object.__setattr__(settings, "data_retention_dry_run", True)
        object.__setattr__(settings, "data_retention_inactive_days", 30)
        object.__setattr__(settings, "data_retention_grace_days", 2)
        object.__setattr__(settings, "data_retention_interval_days", 14)
        object.__setattr__(
            settings, "data_retention_report_telegram_chat_id", "report-chat-777",
        )
        object.__setattr__(settings, "data_retention_report_max_chat_id", "")
        object.__setattr__(
            settings, "data_retention_report_dir", Path(self.temp_dir.name) / "reports",
        )
        db.init_db()

    def tearDown(self):
        object.__setattr__(settings, "database_path", self.original_database)
        for name, value in self.original_values.items():
            object.__setattr__(settings, name, value)
        db.set_current_chel_id("chel_test_default")
        self.temp_dir.cleanup()

    @staticmethod
    def _create_old_user(chel_id, old_at, *, provider="", chat_id=""):
        db.ensure_user(chel_id, pending=False)
        with db.connection() as conn:
            conn.execute(
                "UPDATE users SET created_at=?,last_seen_at=? WHERE chel_id=?",
                (old_at, old_at, chel_id),
            )
            if provider:
                conn.execute(
                    """INSERT INTO external_identities
                    (provider,provider_user_id,chat_id,chel_id,access_status,created_at,last_login_at)
                    VALUES (?,?,?,?, 'active',?,?)""",
                    (provider, f"provider-{chel_id}", chat_id, chel_id, old_at, old_at),
                )
            conn.commit()

    def test_dry_run_reports_candidates_without_warning_or_deletion(self):
        now = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
        old_at = (now - timedelta(days=45)).isoformat()
        self._create_old_user(
            "chel_retention_linked", old_at,
            provider="telegram", chat_id="user-chat-100",
        )
        self._create_old_user("chel_retention_unlinked", old_at)
        self._create_old_user("chel_retention_protected", old_at)
        with db.connection() as conn:
            conn.execute(
                """INSERT INTO lab_result_subscriptions
                (chel_id,med_id,providers,status,created_at,updated_at)
                VALUES (?,?,'[]','pending',?,?)""",
                ("chel_retention_protected", "med-retention", old_at, old_at),
            )
            conn.commit()

        result = data_retention.run_dry_run(now=now, force=True)

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["messenger_count"], 1)
        self.assertEqual(result["no_messenger_count"], 1)
        self.assertEqual(result["excluded_count"], 1)
        self.assertEqual(result["report_notifications"], 1)
        report_path = Path(result["report_path"])
        self.assertTrue(report_path.is_file())
        report_text = report_path.read_text(encoding="utf-8-sig")
        self.assertIn("chel_retention_linked", report_text)
        self.assertIn("chel_retention_unlinked", report_text)
        self.assertIn("chel_retention_protected", report_text)
        with db.connection() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM users WHERE chel_id LIKE 'chel_retention_%'"
            ).fetchone()[0]
            user_warnings = conn.execute(
                "SELECT COUNT(*) FROM user_notification_outbox"
            ).fetchone()[0]
        self.assertEqual(remaining, 3)
        self.assertEqual(user_warnings, 0)

        reports = db.claim_system_notifications("telegram")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["recipient_id"], "report-chat-777")
        self.assertEqual(reports[0]["event_type"], "data_retention_dry_run")
        body = reports[0]["payload"]["body"]
        self.assertIn("DRY-RUN", body)
        self.assertIn("chel_retention_linked", body)
        self.assertIn("chel_retention_unlinked", body)
        self.assertIn("Исключено из очистки: 1", body)
        notification_id = abs(reports[0]["id"]) - db.SYSTEM_NOTIFICATION_ID_OFFSET
        self.assertTrue(db.acknowledge_system_notification(
            notification_id, reports[0]["lease_token"], True,
        ))
        self.assertTrue(db.queue_system_notification(
            "telegram", "report-chat-777", "delivery_diagnostic",
            {"title": "Проверка", "body": "Проверка необратимой ошибки"},
            "delivery-diagnostic-permanent-error",
        ))
        failed_report = db.claim_system_notifications("telegram")[0]
        failed_report_id = abs(failed_report["id"]) - db.SYSTEM_NOTIFICATION_ID_OFFSET
        self.assertTrue(db.acknowledge_system_notification(
            failed_report_id, failed_report["lease_token"], False,
            "Bad Request: chat not found",
        ))
        with db.connection() as conn:
            failed_status = conn.execute(
                "SELECT status FROM system_notification_outbox WHERE id=?",
                (failed_report_id,),
            ).fetchone()[0]
        self.assertEqual(failed_status, "failed")
        self.assertEqual(db.claim_system_notifications("telegram"), [])

    def test_recent_messenger_login_prevents_selection(self):
        now = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
        old_at = (now - timedelta(days=45)).isoformat()
        recent_at = (now - timedelta(days=1)).isoformat()
        self._create_old_user(
            "chel_recent_login", old_at,
            provider="max", chat_id="user-chat-200",
        )
        with db.connection() as conn:
            conn.execute(
                "UPDATE external_identities SET last_login_at=? WHERE chel_id=?",
                (recent_at, "chel_recent_login"),
            )
            conn.commit()

        preview = db.data_retention_preview(
            (now - timedelta(days=30)).isoformat(),
        )

        selected = {
            item["chel_id"] for item in preview["candidates"] + preview["excluded"]
        }
        self.assertNotIn("chel_recent_login", selected)

    def test_audit_waits_fourteen_days_and_live_mode_is_blocked(self):
        now = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
        first = data_retention.run_dry_run(now=now, force=True)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(
            data_retention.run_dry_run(now=now + timedelta(days=13))["status"],
            "not_due",
        )
        object.__setattr__(settings, "data_retention_dry_run", False)
        blocked = data_retention.run_dry_run(
            now=now + timedelta(days=14), force=True,
        )
        self.assertEqual(blocked["status"], "blocked")

    def test_missing_report_recipient_does_not_consume_audit_interval(self):
        object.__setattr__(settings, "data_retention_report_telegram_chat_id", "")
        result = data_retention.run_dry_run(force=True)
        self.assertEqual(result["status"], "misconfigured")
        self.assertTrue(data_retention.audit_due())
        with db.connection() as conn:
            runs = conn.execute(
                "SELECT COUNT(*) FROM data_retention_audit_runs"
            ).fetchone()[0]
        self.assertEqual(runs, 0)

    def test_large_audit_queues_one_summary_instead_of_message_flood(self):
        preview = {
            "cutoff_at": "2026-09-20T00:00:00+00:00",
            "candidate_count": 13_271,
            "messenger_count": 0,
            "no_messenger_count": 13_271,
            "excluded_count": 11,
            "candidates": [
                {
                    "chel_id": f"chel_large_{index:05d}",
                    "preferred_name": f"Пользователь {index}",
                    "last_activity_at": "2026-01-01T00:00:00+00:00",
                    "messenger_providers": [],
                    "messenger_linked": False,
                }
                for index in range(13_271)
            ],
            "excluded": [],
        }

        queued = data_retention.queue_report(
            "retention-large-test", preview, "/app/backups/retention-large-test.csv",
        )

        self.assertEqual(queued, 1)
        reports = db.claim_system_notifications("telegram")
        self.assertEqual(len(reports), 1)
        body = reports[0]["payload"]["body"]
        self.assertIn("Ещё 13266", body)
        self.assertLessEqual(len(body), data_retention.REPORT_BODY_LIMIT)

    def test_activity_touch_is_throttled_but_refreshes_stale_user(self):
        chel_id = "chel_activity_touch"
        old_at = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        self._create_old_user(chel_id, old_at)

        db.touch_user_activity(chel_id)
        with db.connection() as conn:
            first = conn.execute(
                "SELECT last_seen_at FROM users WHERE chel_id=?", (chel_id,),
            ).fetchone()[0]
        db.touch_user_activity(chel_id)
        with db.connection() as conn:
            second = conn.execute(
                "SELECT last_seen_at FROM users WHERE chel_id=?", (chel_id,),
            ).fetchone()[0]

        self.assertGreater(first, old_at)
        self.assertEqual(second, first)


if __name__ == "__main__":
    unittest.main()
