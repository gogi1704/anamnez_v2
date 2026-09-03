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
                "id": "registration", "title": "Регистрация", "users": 20,
                "percent_of_start": 50, "percent_of_parent": 50,
                "comparison_users": 40, "actual_dropoff_users": 20,
                "stopped_users": 2, "incomplete_transition_users": 0,
                "data_quality": "complete",
            }],
        }
        previous_flow = {
            "flow_label": "Обычный путь",
            "summary": {"start_users": 40, "reached_completion": 25},
            "screens": [{"id": "registration", "percent_of_parent": 80, "comparison_users": 40}],
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
        serialized = json.dumps(report, ensure_ascii=False)
        for forbidden in ("chel_id", "tube_number", "messages", "answers"):
            self.assertNotIn(forbidden, serialized)

    def test_manual_analysis_enables_only_requested_section(self):
        base = {**db.FUNNEL_MONITOR_DEFAULTS, "dialog_id": "chat123"}
        payment = funnel_monitor.manual_analysis_config(base, "payments")
        self.assertEqual(payment["analysis_label"], "Оплаты")
        self.assertTrue(payment["include_payments"])
        self.assertFalse(payment["include_standard"])
        self.assertFalse(payment["include_result"])
        self.assertFalse(payment["include_errors"])
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


if __name__ == "__main__":
    unittest.main()
