import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend import database as db, funnel_monitor
from backend.config import settings


class FunnelMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.original_database_path = settings.database_path
        object.__setattr__(settings, "database_path", Path(self.directory.name) / "main.db")
        db.init_db()

    def tearDown(self):
        object.__setattr__(settings, "database_path", self.original_database_path)
        self.directory.cleanup()

    def test_monitor_settings_are_validated_and_persisted(self):
        saved = db.admin_update_funnel_monitor_settings({
            "enabled": True, "dialog_id": "sg123", "send_time": "08:45",
            "timezone": "Europe/Moscow", "period_days": 7,
            "include_standard": True, "include_result": True,
            "include_payments": True, "include_errors": False,
            "minimum_users": 30, "alert_threshold_pp": 8.5,
        })
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["dialog_id"], "sg123")
        self.assertEqual(saved["period_days"], 7)
        self.assertEqual(saved["alert_threshold_pp"], 8.5)
        with self.assertRaises(ValueError):
            db.admin_update_funnel_monitor_settings({"dialog_id": "bad chat"})

    def test_report_contains_only_aggregates_and_detects_drop(self):
        config = {
            **db.FUNNEL_MONITOR_DEFAULTS,
            "dialog_id": "chat123", "include_result": False,
            "minimum_users": 10, "alert_threshold_pp": 10,
        }
        admin_current = {
            "summary": {"users": 20, "visitors": 40, "sessions": 42, "events": 200},
            "funnel": [], "devices": [], "registrations": [], "sources": [],
            "errors": [], "payments": {"summary": {}},
        }
        admin_previous = {
            "summary": {"users": 30, "visitors": 40, "sessions": 41, "events": 210},
            "funnel": [], "devices": [], "registrations": [], "sources": [],
            "errors": [], "payments": {"summary": {}},
        }
        current_flow = {
            "flow_label": "Обычный путь",
            "summary": {"start_users": 40, "reached_completion": 12},
            "screens": [{
                "id": "registration", "title": "Ошибочное внешнее название", "users": 20,
                "percent_of_start": 50, "percent_of_parent": 50,
                "comparison_users": 40, "actual_dropoff_users": 20,
                "stopped_users": 2, "incomplete_transition_users": 0,
                "data_quality": "complete",
            }],
        }
        previous_flow = {
            "flow_label": "Обычный путь",
            "summary": {"start_users": 40, "reached_completion": 25},
            "screens": [{
                "id": "registration", "users": 32,
                "percent_of_start": 80, "percent_of_parent": 80,
                "comparison_users": 40, "actual_dropoff_users": 8,
                "stopped_users": 5, "incomplete_transition_users": 1,
                "data_quality": "complete",
            }],
        }
        with (
            patch("backend.funnel_monitor.analytics.admin_report", side_effect=[admin_current, admin_previous]),
            patch("backend.funnel_monitor.analytics.metric2_report", side_effect=[current_flow, previous_flow]),
        ):
            report = funnel_monitor.build_report(
                config, test=True,
                now=datetime(2026, 9, 3, 10, 0, tzinfo=timezone(timedelta(hours=3))),
            )
        self.assertEqual(report["current_period"], {"date_from": "2026-09-02", "date_to": "2026-09-02"})
        self.assertEqual(report["flows"][0]["alerts"][0]["change_pp"], -30.0)
        self.assertEqual(
            report["flows"][0]["screens"][0]["title"],
            "Выбор способа входа",
        )
        flow = report["flows"][0]
        self.assertEqual(flow["summary"]["previous_start_users"], 40)
        self.assertEqual(flow["summary"]["previous_reached_completion"], 25)
        self.assertEqual(flow["screens"][0]["previous_users"], 32)
        self.assertEqual(flow["screens"][0]["previous_percent_of_start"], 80)
        self.assertEqual(flow["screens"][0]["previous_stopped_users"], 5)
        instruction = report["ai_instruction"]
        self.assertIn("Описание чек-апов", instruction)
        self.assertIn("не основная цель воронки", instruction)
        self.assertIn("Сообщите ИНН вашего предприятия", instruction)
        self.assertIn("Оплатить на медосмотре", instruction)
        self.assertIn("целевым успешным действием", instruction)
        self.assertIn("flows[].screens[].title", instruction)
        self.assertIn("не анализируй текущий период изолированно", instruction)
        serialized = json.dumps(report, ensure_ascii=False)
        for forbidden in ("chel_id", "tube_number", "messages", "answers"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(report["new_users_daily"]["date"], "2026-09-02")

    def test_daily_new_users_summary_counts_routes_and_engagement(self):
        start = "2026-09-01T21:00:00+00:00"
        end = "2026-09-02T21:00:00+00:00"
        with db.connection() as conn:
            conn.executemany(
                """INSERT INTO users
                   (chel_id,created_at,last_seen_at,registered_at,registration_method)
                   VALUES (?,?,?,?,?)""",
                [
                    ("new-one", "2026-09-02T08:00:00+00:00", "2026-09-02T09:00:00+00:00", "2026-09-02T08:05:00+00:00", "telegram"),
                    ("new-two", "2026-09-02T10:00:00+00:00", "2026-09-02T10:00:00+00:00", None, None),
                    ("old-new-login", "2026-08-20T10:00:00+00:00", "2026-09-02T12:00:00+00:00", "2026-09-02T12:00:00+00:00", "max"),
                ],
            )
            conn.executemany(
                """INSERT INTO onboarding_state
                   (chel_id,status,entry_route,selected_tests,payment_status,
                    questionnaire_skipped,marketer_result_seen,intro_seen,font_size,updated_at)
                   VALUES (?,'appearance',?,'[]','none',0,0,0,'extra',?)""",
                [
                    ("new-one", "other", "2026-09-02T08:05:00+00:00"),
                    ("new-two", "results", "2026-09-02T10:00:00+00:00"),
                ],
            )
            conn.execute(
                """INSERT INTO external_identities
                   (provider,provider_user_id,chat_id,chel_id,access_status,created_at,last_login_at)
                   VALUES ('telegram','tg-1','chat-1','new-one','active',?,?)""",
                ("2026-09-02T08:05:00+00:00", "2026-09-02T08:05:00+00:00"),
            )
            conn.execute(
                """INSERT INTO conversations
                   (id,chel_id,title,dialog_type,created_at,updated_at)
                   VALUES ('conv-1','new-one','Поддержка','psychology',?,?)""",
                ("2026-09-02T08:10:00+00:00", "2026-09-02T08:10:00+00:00"),
            )
            conn.execute(
                """INSERT INTO messages
                   (conversation_id,role,content,created_at)
                   VALUES ('conv-1','user','Здравствуйте',?)""",
                ("2026-09-02T08:11:00+00:00",),
            )
            conn.commit()

        result = db.daily_new_users_summary(start, end)
        self.assertEqual(result["first_visitors"], 2)
        self.assertEqual(result["registered_users"], 2)
        self.assertEqual(result["registrations_from_new_visitors"], 1)
        self.assertEqual(result["registration_conversion"], 50.0)
        self.assertEqual(result["messenger_linked_users"], 1)
        self.assertEqual(result["chat_users"], 1)
        self.assertEqual(result["psychology_chat_users"], 1)
        self.assertEqual(result["message_users"], 1)
        self.assertEqual(
            {item["key"]: item["users"] for item in result["routes"]},
            {"other": 1, "results": 1},
        )
        self.assertEqual(
            {item["key"]: item["users"] for item in result["registration_methods"]},
            {"telegram": 1, "max": 1},
        )

    def test_manual_analysis_enables_only_requested_section(self):
        base = {**db.FUNNEL_MONITOR_DEFAULTS, "dialog_id": "chat123"}
        payment = funnel_monitor.manual_analysis_config(base, "payments")
        self.assertEqual(payment["analysis_label"], "Оплаты")
        self.assertTrue(payment["include_payments"])
        self.assertFalse(payment["include_standard"])
        self.assertFalse(payment["include_result"])
        self.assertFalse(payment["include_errors"])
        new_users = funnel_monitor.manual_analysis_config(base, "new_users")
        self.assertEqual(new_users["analysis_label"], "Новые пользователи")
        self.assertFalse(new_users["include_standard"])
        self.assertFalse(new_users["include_result"])
        self.assertFalse(new_users["include_payments"])
        self.assertFalse(new_users["include_errors"])
        with self.assertRaises(ValueError):
            funnel_monitor.manual_analysis_config(base, "unknown")

    def test_small_previous_sample_and_incomplete_data_do_not_raise_alarm(self):
        config = {**db.FUNNEL_MONITOR_DEFAULTS, "minimum_users": 20}
        current = {"summary": {"start_users": 100}, "screens": [{
            "id": "step", "percent_of_parent": 50, "comparison_users": 100,
            "terminal": True,
        }]}
        previous = {"summary": {"start_users": 2}, "screens": [{
            "id": "step", "percent_of_parent": 100, "comparison_users": 2,
        }]}
        result = funnel_monitor._compact_flow(current, previous, config)
        self.assertEqual(result["alerts"], [])
        self.assertFalse(result["sample_sufficient"])
        self.assertTrue(result["screens"][0]["terminal"])
        previous["screens"][0].update(comparison_users=100, data_quality="incomplete")
        self.assertEqual(funnel_monitor._compact_flow(current, previous, config)["alerts"], [])

    def test_inn_and_checkup_description_remain_visible_but_do_not_raise_alarm(self):
        config = {
            **db.FUNNEL_MONITOR_DEFAULTS,
            "minimum_users": 20,
            "alert_threshold_pp": 10,
        }
        current = {
            "flow_label": "Обычный путь",
            "summary": {"start_users": 100},
            "screens": [
                {
                    "id": "question_company_inn",
                    "title": "Сообщите ИНН вашего предприятия",
                    "users": 40,
                    "percent_of_parent": 40,
                    "comparison_users": 100,
                    "data_quality": "complete",
                },
                {
                    "id": "exam_catalog",
                    "title": "Описание чек-апов",
                    "users": 20,
                    "percent_of_parent": 20,
                    "comparison_users": 100,
                    "data_quality": "complete",
                },
            ],
        }
        previous = {
            "summary": {"start_users": 100},
            "screens": [
                {
                    "id": "question_company_inn",
                    "percent_of_parent": 90,
                    "comparison_users": 100,
                    "data_quality": "complete",
                },
                {
                    "id": "exam_catalog",
                    "percent_of_parent": 80,
                    "comparison_users": 100,
                    "data_quality": "complete",
                },
            ],
        }
        result = funnel_monitor._compact_flow(current, previous, config)
        self.assertEqual(result["alerts"], [])
        self.assertEqual(
            [screen["title"] for screen in result["screens"]],
            ["Сообщите ИНН вашего предприятия", "Описание чек-апов"],
        )


if __name__ == "__main__":
    unittest.main()
