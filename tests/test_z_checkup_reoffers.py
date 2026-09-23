import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from backend import analytics, checkup_reoffers, database as db
from backend.config import settings


class CheckupReofferTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = settings.database_path
        self.original_analytics = settings.analytics_database_path
        object.__setattr__(settings, "database_path", Path(self.temp_dir.name) / "main.db")
        object.__setattr__(settings, "analytics_database_path", Path(self.temp_dir.name) / "analytics.db")
        db.init_db()
        analytics.init_db()
        db.set_current_chel_id("chel_reoffer_test")
        db.ensure_user("chel_reoffer_test", pending=False)
        db.save_profile({"company_inn": "1234567890"})

    def tearDown(self):
        object.__setattr__(settings, "database_path", self.original_database)
        object.__setattr__(settings, "analytics_database_path", self.original_analytics)
        self.temp_dir.cleanup()

    def test_dwell_bucket_and_due_delivery_are_idempotent(self):
        candidate = db.save_checkup_reoffer_dwell("journey-12345678", 67)
        self.assertEqual(candidate["duration_bucket"], "30_to_120")
        tomorrow = date.today() + timedelta(days=1)
        db.replace_enterprise_examination_schedule([{
            "source_sheet_id": "1", "source_row_id": "1", "inn": "1234567890",
            "organization_name": "Завод", "examination_date": tomorrow.isoformat(),
            "brigade": "Бригада 1",
        }], ["1"])
        with db.connection() as conn:
            conn.execute(
                """INSERT INTO external_identities
                (provider,provider_user_id,chel_id,access_status,created_at,last_login_at)
                VALUES ('telegram','777',?,'active',?,?)""",
                (db.current_chel_id(), db.utc_now(), db.utc_now()),
            )
            conn.commit()
        queued = db.queue_due_checkup_reoffers(tomorrow, "https://example.test")
        self.assertEqual(len(queued), 1)
        self.assertEqual(db.queue_due_checkup_reoffers(tomorrow, "https://example.test"), [])
        notifications = db.claim_user_notifications("telegram")
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["event_type"], "checkup_reoffer")
        self.assertIn("checkup_reoffer=", notifications[0]["payload"]["action_url"])

    def test_completed_selection_is_excluded(self):
        db.save_checkup_reoffer_dwell("journey-12345678", 130)
        tomorrow = date.today() + timedelta(days=1)
        db.replace_enterprise_examination_schedule([{
            "source_sheet_id": "1", "source_row_id": "1", "inn": "1234567890",
            "organization_name": "Завод", "examination_date": tomorrow.isoformat(),
            "brigade": "Бригада 1",
        }], ["1"])
        db.save_onboarding(
            status="complete", selected_tests=["protein"], payment_status="pay_at_exam",
        )
        self.assertEqual(db.queue_due_checkup_reoffers(tomorrow, "https://example.test"), [])

    def test_short_visit_is_recorded_but_not_sent(self):
        candidate = db.save_checkup_reoffer_dwell("journey-12345678", 12)
        self.assertEqual(candidate["duration_bucket"], "under_30")
        tomorrow = date.today() + timedelta(days=1)
        db.replace_enterprise_examination_schedule([{
            "source_sheet_id": "1", "source_row_id": "1", "inn": "1234567890",
            "organization_name": "Завод", "examination_date": tomorrow.isoformat(),
            "brigade": "Бригада 1",
        }], ["1"])
        self.assertEqual(db.queue_due_checkup_reoffers(tomorrow, "https://example.test"), [])

    def test_marketer_variant_is_not_recorded_for_reoffer(self):
        settings_payload = db.admin_experiment_settings()
        settings_payload.update({"enabled": True, "marketer_percent": 100})
        db.admin_update_experiment_settings(settings_payload)
        result = db.save_checkup_reoffer_dwell("journey-12345678", 90)
        self.assertEqual(result, {"status": "excluded", "reason": "marketer"})
        with db.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM checkup_reoffer_candidates").fetchone()[0]
        self.assertEqual(count, 0)

    def test_metric2_reoffer_flow_starts_at_message(self):
        analytics.record_server_event(
            db.current_chel_id(), "onboarding_screen_viewed",
            {"screen": "reoffer_message", "context": "reoffer", "source": "checkup_reoffer"},
            session_id="reoffer-test-session",
        )
        analytics.record_server_event(
            db.current_chel_id(), "onboarding_screen_action",
            {
                "screen": "reoffer_message", "action": "open_checkups",
                "context": "reoffer", "source": "checkup_reoffer",
            },
            session_id="reoffer-test-session",
        )
        analytics.record_server_event(
            db.current_chel_id(), "onboarding_screen_viewed",
            {
                "screen": "exam_selection", "previous_screen": "reoffer_message",
                "context": "reoffer", "source": "checkup_reoffer",
            },
            session_id="reoffer-test-session",
        )
        report = analytics.metric2_report("all", flow="reoffer")
        self.assertEqual(report["flow"], "reoffer")
        self.assertEqual(report["summary"]["start_users"], 1)
        screens = {item["id"]: item for item in report["screens"]}
        self.assertEqual(screens["reoffer_message"]["users"], 1)
        self.assertEqual(screens["exam_selection"]["users"], 1)
        action_ids = {item["id"] for item in screens["reoffer_message"]["actions"]}
        self.assertEqual(action_ids, {"open_checkups"})

    def test_admin_can_disable_scheduler_without_restart(self):
        updated = db.admin_update_checkup_reoffer_settings(False)
        self.assertFalse(updated["enabled"])
        result = checkup_reoffers.send_due()
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["queued"], 0)
        self.assertFalse(db.admin_checkup_reoffer_settings()["enabled"])

    def test_targeted_test_send_uses_real_outbox_but_is_marked_test(self):
        with db.connection() as conn:
            conn.execute(
                """INSERT INTO external_identities
                (provider,provider_user_id,chel_id,access_status,created_at,last_login_at)
                VALUES ('telegram','777',?,'active',?,?)""",
                (db.current_chel_id(), db.utc_now(), db.utc_now()),
            )
            conn.commit()
        result = db.queue_test_checkup_reoffer(
            db.current_chel_id(), "https://example.test",
        )
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["messenger_providers"], ["telegram"])
        notifications = db.claim_user_notifications("telegram")
        self.assertEqual(notifications[0]["event_type"], "checkup_reoffer_test")
        opened = db.open_checkup_reoffer(
            notifications[0]["payload"]["action_url"].split("checkup_reoffer=", 1)[1]
        )
        self.assertEqual(opened["is_test"], 1)
        diagnostics = db.admin_checkup_reoffer_diagnostics()
        self.assertEqual(diagnostics["sent_total"], 0)
        self.assertEqual(diagnostics["candidates"]["eligible"], 0)

    def test_max_notification_uses_chat_id_and_skips_identity_without_it(self):
        now = db.utc_now()
        with db.connection() as conn:
            conn.execute(
                """INSERT INTO external_identities
                (provider,provider_user_id,chat_id,chel_id,access_status,created_at,last_login_at)
                VALUES ('max','max-user','700003',?,'active',?,?)""",
                (db.current_chel_id(), now, now),
            )
            conn.execute(
                """INSERT INTO external_identities
                (provider,provider_user_id,chel_id,access_status,created_at,last_login_at)
                VALUES ('max','max-user-without-chat',?,'active',?,?)""",
                (db.current_chel_id(), now, now),
            )
            conn.commit()
        result = db.queue_test_checkup_reoffer(
            db.current_chel_id(), "https://example.test",
        )
        self.assertEqual(result["messenger_providers"], ["max"])
        notifications = db.claim_user_notifications("max")
        self.assertEqual(notifications[0]["recipient_id"], "700003")

    def test_permanent_missing_chat_error_is_not_retried(self):
        now = db.utc_now()
        with db.connection() as conn:
            conn.execute(
                """INSERT INTO external_identities
                (provider,provider_user_id,chel_id,access_status,created_at,last_login_at)
                VALUES ('telegram','777',?,'active',?,?)""",
                (db.current_chel_id(), now, now),
            )
            conn.commit()
        db.queue_test_checkup_reoffer(db.current_chel_id(), "https://example.test")
        notification = db.claim_user_notifications("telegram")[0]
        notification_id = abs(notification["id"]) - db.USER_NOTIFICATION_ID_OFFSET
        self.assertTrue(db.acknowledge_user_notification(
            notification_id, notification["lease_token"], False,
            "API 404: chat.not.found",
        ))
        self.assertEqual(db.claim_user_notifications("telegram"), [])
        with db.connection() as conn:
            status = conn.execute(
                "SELECT status FROM user_notification_outbox WHERE id=?",
                (notification_id,),
            ).fetchone()[0]
        self.assertEqual(status, "failed")

    def test_diagnostics_show_tomorrows_planned_sends_after_exclusions(self):
        db.save_checkup_reoffer_dwell("journey-12345678", 75)
        examination_day = date.today() + timedelta(days=2)
        db.replace_enterprise_examination_schedule([{
            "source_sheet_id": "1", "source_row_id": "planned", "inn": "1234567890",
            "organization_name": "Завод", "examination_date": examination_day.isoformat(),
            "brigade": "Бригада 1",
        }], ["1"])
        diagnostics = db.admin_checkup_reoffer_diagnostics()
        self.assertEqual(diagnostics["day_after_tomorrow"], examination_day.isoformat())
        self.assertEqual(diagnostics["scheduled_tomorrow"], 1)
        db.save_onboarding(
            status="complete", selected_tests=["protein"], payment_status="pay_at_exam",
        )
        diagnostics = db.admin_checkup_reoffer_diagnostics()
        self.assertEqual(diagnostics["scheduled_tomorrow"], 0)


if __name__ == "__main__":
    unittest.main()
