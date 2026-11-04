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
        db.set_current_chel_id("chel_reoffer_primary")
        db.ensure_user("chel_reoffer_primary", pending=False)
        db.save_profile({"company_inn": "1234567890"})

    def schedule(self, offset=1):
        examination_day = date.today() + timedelta(days=offset)
        db.replace_enterprise_examination_schedule([{
            "source_sheet_id": "1", "source_row_id": str(offset),
            "inn": "1234567890", "organization_name": "Завод",
            "examination_date": examination_day.isoformat(), "brigade": "Бригада 1",
        }], ["1"])
        return examination_day

    def latest_message(self):
        with db.connection() as conn:
            row = conn.execute(
                "SELECT content,metadata FROM messages ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return str(row["content"]), json.loads(row["metadata"])

    def tearDown(self):
        object.__setattr__(settings, "database_path", self.original_database)
        object.__setattr__(settings, "analytics_database_path", self.original_analytics)
        self.temp_dir.cleanup()

    def test_dwell_bucket_and_due_delivery_are_idempotent(self):
        candidate = db.save_checkup_reoffer_dwell("journey-12345678", 67)
        self.assertEqual(candidate["duration_bucket"], "30_to_120")
        tomorrow = self.schedule()
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
        self.assertEqual(notifications[0]["event_type"], "pre_exam_reminder")
        self.assertEqual(notifications[0]["payload"]["audience_group"], "unselected")
        self.assertIn("checkup_reoffer=", notifications[0]["payload"]["action_url"])

    def test_selected_for_payment_at_exam_gets_discount_message(self):
        tomorrow = self.schedule()
        db.save_onboarding(
            status="complete", selected_tests=["protein"], payment_status="pay_at_exam",
        )
        queued = db.queue_due_checkup_reoffers(tomorrow, "https://example.test")
        self.assertEqual(queued[0]["audience_group"], "selected")
        content, metadata = self.latest_message()
        self.assertIn("Ранее вы выбрали", content)
        self.assertIn("скидка 10%", content)
        self.assertIn("Баланс белка", content)
        self.assertEqual(metadata["action_label"], "💳 Оплатить онлайн со скидкой")
        opened = db.open_checkup_reoffer(metadata["access_token"])
        self.assertEqual(opened["audience_group"], "selected")

    def test_short_or_missing_visit_does_not_exclude_user(self):
        candidate = db.save_checkup_reoffer_dwell("journey-12345678", 12)
        self.assertEqual(candidate["duration_bucket"], "under_30")
        tomorrow = self.schedule()
        queued = db.queue_due_checkup_reoffers(tomorrow, "https://example.test")
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["audience_group"], "unselected")

    def test_paid_online_gets_receipt_without_action_button(self):
        tomorrow = self.schedule()
        db.save_onboarding(
            status="payment", selected_tests=["lipids"], payment_status="pending",
        )
        order = db.create_payment_order()
        amount = db.payment_order_private(order["id"])["amount_kopecks"]
        payment = {
            "id": "provider-real-payment", "status": "succeeded", "paid": True,
            "test": False, "amount": {"value": f"{amount / 100:.2f}", "currency": "RUB"},
            "metadata": {"order_id": order["id"]},
        }
        db.attach_yookassa_payment(order["id"], {
            **payment, "status": "pending", "paid": False,
            "confirmation": {"confirmation_url": "https://yoomoney.ru/pay/real"},
        })
        db.apply_yookassa_status(order["id"], payment)
        queued = db.queue_due_checkup_reoffers(tomorrow, "https://example.test")
        self.assertEqual(queued[0]["audience_group"], "paid_online")
        content, metadata = self.latest_message()
        self.assertIn("уже оплачены онлайн", content)
        self.assertIn("Здоровье сердца и сосудов", content)
        self.assertIn("Статус: оплачено полностью", content)
        self.assertEqual(metadata["action_url"], "")
        self.assertEqual(metadata["action_label"], "")

    def test_test_payment_is_not_treated_as_real_paid_order(self):
        tomorrow = self.schedule()
        db.save_onboarding(
            status="payment", selected_tests=["lipids"], payment_status="pending",
        )
        order = db.create_payment_order()
        amount = db.payment_order_private(order["id"])["amount_kopecks"]
        pending = {
            "id": "provider-test-payment", "status": "pending", "paid": False,
            "test": True, "amount": {"value": f"{amount / 100:.2f}", "currency": "RUB"},
            "metadata": {"order_id": order["id"]},
            "confirmation": {"confirmation_url": "https://yoomoney.ru/pay/test"},
        }
        db.attach_yookassa_payment(order["id"], pending)
        db.apply_yookassa_status(order["id"], {**pending, "status": "succeeded", "paid": True})
        queued = db.queue_due_checkup_reoffers(tomorrow, "https://example.test")
        self.assertEqual(queued[0]["audience_group"], "selected")

    def test_unselected_gets_up_to_two_questionnaire_recommendations(self):
        tomorrow = self.schedule()
        db.save_profile({"company_inn": "1234567890", "fatigue": "yes", "hair_loss": "yes"})
        queued = db.queue_due_checkup_reoffers(tomorrow, "https://example.test")
        self.assertEqual(queued[0]["audience_group"], "unselected")
        self.assertLessEqual(queued[0]["recommended_count"], 2)
        content, metadata = self.latest_message()
        self.assertIn("Завтра у вас медицинский осмотр", content)
        self.assertIn("По вашим ответам", content)
        self.assertEqual(metadata["action_label"], "🩺 Выбрать чек-апы")

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
        base = {"context": "reoffer", "source": "pre_exam_reminder"}
        recorded = analytics.record_events(db.current_chel_id(), [
            {"event_id": "pre-exam-sent-0001", "session_id": "reoffer-session",
             "event_name": "pre_exam_reminder_sent", "properties": {
                 **base, "screen": "pre_exam_unselected_message", "audience_group": "unselected",
             }},
            {"event_id": "pre-exam-root-0001", "session_id": "reoffer-session",
             "event_name": "onboarding_screen_viewed", "properties": {
                 **base, "screen": "pre_exam_unselected_message",
                 "audience_group": "unselected",
             }},
            {"event_id": "pre-exam-click-0001", "session_id": "reoffer-session",
             "event_name": "pre_exam_reminder_clicked", "properties": {
                 **base, "screen": "pre_exam_unselected_message", "action": "choose_checkups",
                 "audience_group": "unselected",
             }},
            {"event_id": "pre-exam-screen-0001", "session_id": "reoffer-session",
             "event_name": "onboarding_screen_viewed", "properties": {
                 **base, "screen": "exam_selection",
                 "previous_screen": "pre_exam_unselected_message",
             }},
        ], is_server=True)
        self.assertEqual(recorded["accepted"], 4)
        analytics.refresh_reports()
        report = analytics.metric2_report("all", flow="reoffer")
        self.assertEqual(report["flow"], "reoffer")
        self.assertEqual(report["summary"]["start_users"], 1)
        screens = {item["id"]: item for item in report["screens"]}
        self.assertEqual(screens["pre_exam_unselected_message"]["users"], 1)
        self.assertEqual(screens["exam_selection"]["users"], 1)
        action_ids = {item["id"] for item in screens["pre_exam_unselected_message"]["actions"]}
        self.assertEqual(action_ids, {"choose_checkups"})
        groups = {item["id"]: item for item in report["summary"]["reoffer"]["audience_groups"]}
        self.assertEqual(groups["unselected"]["sent_users"], 1)
        self.assertEqual(groups["unselected"]["clicked_users"], 1)

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

    def test_diagnostics_show_all_three_planned_groups(self):
        examination_day = self.schedule(offset=2)
        diagnostics = db.admin_checkup_reoffer_diagnostics()
        self.assertEqual(diagnostics["day_after_tomorrow"], examination_day.isoformat())
        self.assertEqual(diagnostics["scheduled_tomorrow"], 1)
        self.assertEqual(diagnostics["scheduled_audience_groups"]["unselected"], 1)
        db.save_onboarding(
            status="complete", selected_tests=["protein"], payment_status="pay_at_exam",
        )
        diagnostics = db.admin_checkup_reoffer_diagnostics()
        self.assertEqual(diagnostics["scheduled_tomorrow"], 1)
        self.assertEqual(diagnostics["scheduled_audience_groups"]["selected"], 1)


if __name__ == "__main__":
    unittest.main()
