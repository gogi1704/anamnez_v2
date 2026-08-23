import json
import tempfile
import unittest
from pathlib import Path

from backend import analytics
from backend.config import settings


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = settings.analytics_database_path
        self.original_enabled = settings.analytics_enabled
        object.__setattr__(settings, "analytics_database_path", Path(self.temp_dir.name) / "analytics.db")
        object.__setattr__(settings, "analytics_enabled", True)
        analytics.init_db()

    def tearDown(self):
        object.__setattr__(settings, "analytics_database_path", self.original_path)
        object.__setattr__(settings, "analytics_enabled", self.original_enabled)
        self.temp_dir.cleanup()

    def test_events_are_deduplicated_and_medical_values_are_dropped(self):
        events = [{
            "event_id": "event-00000001", "session_id": "session-00000001",
            "event_name": "registration_completed",
            "properties": {"method": "anonymous", "answer": "secret", "phone": "+79990000000"},
        }]
        first = analytics.record_events("CHEL-TEST", events, user_agent="Mozilla/5.0 (iPhone) Safari")
        second = analytics.record_events("CHEL-TEST", events, user_agent="Mozilla/5.0")
        self.assertEqual(first, {"accepted": 1, "duplicates": 0})
        self.assertEqual(second, {"accepted": 0, "duplicates": 1})
        with analytics.connection() as conn:
            properties = json.loads(conn.execute("SELECT properties FROM analytics_events").fetchone()[0])
        self.assertEqual(properties, {"method": "anonymous"})

    def test_delete_user_data_removes_only_target_events_and_sessions(self):
        analytics.record_events("CHEL-DELETE", [{
            "event_id": "delete-event-0001", "session_id": "delete-session-0001",
            "event_name": "registration_completed", "properties": {"method": "anonymous"},
        }])
        analytics.record_events("CHEL-KEEP", [{
            "event_id": "keep-event-000001", "session_id": "keep-session-000001",
            "event_name": "registration_completed", "properties": {"method": "max"},
        }])
        result = analytics.delete_user_data("CHEL-DELETE")
        self.assertEqual(result, {"events": 1, "sessions": 1})
        with analytics.connection() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM analytics_events WHERE chel_id = 'CHEL-DELETE'"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM analytics_sessions WHERE chel_id = 'CHEL-DELETE'"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM analytics_events WHERE chel_id = 'CHEL-KEEP'"
            ).fetchone()[0], 1)

    def test_report_calculates_funnel_and_question_conversion(self):
        events = [
            {"event_id": "event-00000001", "session_id": "session-00000001", "event_name": "registration_completed", "properties": {"method": "max"}},
            {"event_id": "event-00000002", "session_id": "session-00000001", "event_name": "questionnaire_started", "properties": {}},
            {"event_id": "event-00000003", "session_id": "session-00000001", "event_name": "question_viewed", "properties": {"question_key": "age"}},
            {"event_id": "event-00000004", "session_id": "session-00000001", "event_name": "question_answered", "properties": {"question_key": "age", "duration_ms": 2200}},
        ]
        analytics.record_events("CHEL-TEST", events, user_agent="Mozilla/5.0 (Linux; Android 15) Chrome/130")
        report = analytics.admin_report("30")
        self.assertEqual(report["summary"], {"users": 1, "visitors": 1, "sessions": 1, "events": 4})
        self.assertEqual(report["funnel"][0]["users"], 1)
        funnel_labels = [item["label"] for item in report["funnel"]]
        self.assertIn("Вход в анкету", funnel_labels)
        self.assertIn("Выход из анкеты", funnel_labels)
        self.assertNotIn("Возраст", funnel_labels)
        self.assertEqual(report["questions"][0]["conversion"], 100.0)
        self.assertEqual(report["questions"][0]["avg_duration_ms"], 2200)
        self.assertEqual(report["devices"][0]["label"], "android")
        self.assertEqual(report["recent_pagination"], {"page": 1, "limit": 25, "total": 4, "pages": 1})

    def test_payment_method_does_not_replace_registration_method(self):
        analytics.record_events("CHEL-METHOD", [
            {
                "event_id": "method-register-01", "session_id": "method-session-01",
                "event_name": "registration_completed", "properties": {"method": "max"},
            },
            {
                "event_id": "method-payment-01", "session_id": "method-session-01",
                "event_name": "payment_method_selected", "properties": {"method": "at_exam"},
            },
        ])
        report = analytics.admin_report("30")
        self.assertEqual(report["filter_options"]["methods"], ["max"])
        self.assertEqual(report["registrations"], [{"label": "max", "users": 1, "events": 1}])
        self.assertEqual(analytics.admin_report("30", method="max")["summary"]["events"], 2)
        self.assertEqual(analytics.admin_report("30", method="at_exam")["summary"]["events"], 0)
        with analytics.connection() as conn:
            self.assertEqual(conn.execute(
                "SELECT registration_method FROM analytics_sessions WHERE session_id='method-session-01'"
            ).fetchone()[0], "max")

    def test_recent_events_are_paginated_and_page_is_clamped(self):
        events = [
            {"event_id": f"event-page-{index:04d}", "session_id": "session-page-0001", "event_name": "chat_opened", "properties": {}}
            for index in range(31)
        ]
        analytics.record_events("CHEL-PAGE", events, user_agent="Mozilla/5.0")
        first = analytics.admin_report("30", recent_page=1, recent_limit=25)
        second = analytics.admin_report("30", recent_page=2, recent_limit=25)
        beyond = analytics.admin_report("30", recent_page=99, recent_limit=25)
        self.assertEqual(len(first["recent"]), 25)
        self.assertEqual(len(second["recent"]), 6)
        self.assertEqual(second["recent_pagination"], {"page": 2, "limit": 25, "total": 31, "pages": 2})
        self.assertEqual(beyond["recent_pagination"]["page"], 2)

    def test_examination_popularity_uses_each_users_latest_confirmed_selection(self):
        analytics.record_events("CHEL-EXAM-1", [
            {
                "event_id": "exam-one-complete-old", "session_id": "exam-session-01",
                "event_name": "examinations_selection_completed",
                "properties": {"selection_id": "selection-old", "selected_count": 2},
            },
            {
                "event_id": "exam-one-a-old", "session_id": "exam-session-01",
                "event_name": "examination_selection_confirmed",
                "properties": {"selection_id": "selection-old", "exam_id": "exam-a", "exam_name": "Обследование А"},
            },
            {
                "event_id": "exam-one-b-old", "session_id": "exam-session-01",
                "event_name": "examination_selection_confirmed",
                "properties": {"selection_id": "selection-old", "exam_id": "exam-b", "exam_name": "Обследование Б"},
            },
            {
                "event_id": "exam-one-complete-new", "session_id": "exam-session-01",
                "event_name": "examinations_selection_completed",
                "properties": {"selection_id": "selection-new", "selected_count": 1},
            },
            {
                "event_id": "exam-one-b-new", "session_id": "exam-session-01",
                "event_name": "examination_selection_confirmed",
                "properties": {"selection_id": "selection-new", "exam_id": "exam-b", "exam_name": "Обследование Б"},
            },
        ])
        analytics.record_events("CHEL-EXAM-2", [
            {
                "event_id": "exam-two-complete", "session_id": "exam-session-02",
                "event_name": "examinations_selection_completed",
                "properties": {"selection_id": "selection-two", "selected_count": 1},
            },
            {
                "event_id": "exam-two-a", "session_id": "exam-session-02",
                "event_name": "examination_selection_confirmed",
                "properties": {"selection_id": "selection-two", "exam_id": "exam-a", "exam_name": "Обследование А"},
            },
        ])
        analytics.record_events("CHEL-EXAM-3", [{
            "event_id": "exam-three-skipped", "session_id": "exam-session-03",
            "event_name": "examinations_selection_completed",
            "properties": {"selection_id": "selection-three", "selected_count": 0},
        }])

        report = analytics.admin_report("30")
        self.assertEqual(report["examination_summary"], {
            "completed_users": 3, "users_with_selection": 2, "selected_items": 2,
        })
        examinations = {item["exam_id"]: item for item in report["examinations"]}
        self.assertEqual(examinations["exam-a"]["users"], 1)
        self.assertEqual(examinations["exam-b"]["users"], 1)
        self.assertEqual(examinations["exam-a"]["percent_of_selectors"], 50.0)
        self.assertEqual(examinations["exam-a"]["percent_of_completed"], 33.3)

    def test_funnel_stages_include_expandable_action_breakdowns(self):
        first_events = [
            {"event_id": "detail-max-register", "session_id": "detail-session-01", "event_name": "registration_completed", "properties": {"method": "max"}},
            {"event_id": "detail-font-extra", "session_id": "detail-session-01", "event_name": "appearance_completed", "properties": {"font_size": "extra"}},
            {"event_id": "detail-offer-view", "session_id": "detail-session-01", "event_name": "examinations_offer_viewed", "properties": {}},
            {"event_id": "detail-edit-form", "session_id": "detail-session-01", "event_name": "funnel_action", "properties": {"stage": "examinations_offer", "action": "edit_questionnaire"}},
            {"event_id": "detail-catalog-info", "session_id": "detail-session-01", "event_name": "funnel_action", "properties": {"stage": "examinations_offer", "action": "catalog_info"}},
            {"event_id": "detail-options-view", "session_id": "detail-session-01", "event_name": "examinations_opened", "properties": {}},
            {"event_id": "detail-online-pay", "session_id": "detail-session-01", "event_name": "funnel_action", "properties": {"stage": "examinations_options", "action": "pay_online"}},
        ]
        second_events = [
            {"event_id": "detail-anon-register", "session_id": "detail-session-02", "event_name": "registration_completed", "properties": {"method": "anonymous"}},
            {"event_id": "detail-anon-button", "session_id": "detail-session-02", "event_name": "anonymous_warning_viewed", "properties": {}},
            {"event_id": "detail-anon-back", "session_id": "detail-session-02", "event_name": "anonymous_warning_cancelled", "properties": {}},
            {"event_id": "detail-font-large", "session_id": "detail-session-02", "event_name": "appearance_completed", "properties": {"font_size": "large"}},
            {"event_id": "detail-offer-view-2", "session_id": "detail-session-02", "event_name": "examinations_offer_viewed", "properties": {}},
            {"event_id": "detail-offer-skip", "session_id": "detail-session-02", "event_name": "funnel_action", "properties": {"stage": "examinations_offer", "action": "skip"}},
            {"event_id": "detail-options-view-2", "session_id": "detail-session-02", "event_name": "examinations_opened", "properties": {}},
            {"event_id": "detail-exam-payment", "session_id": "detail-session-02", "event_name": "funnel_action", "properties": {"stage": "examinations_options", "action": "pay_at_exam"}},
        ]
        analytics.record_events("CHEL-DETAIL-1", first_events, user_agent="Mozilla/5.0")
        analytics.record_events("CHEL-DETAIL-2", second_events, user_agent="Mozilla/5.0")
        funnel = {item["event_name"]: item for item in analytics.admin_report("30")["funnel"]}
        registration = {item["key"]: item for item in funnel["registration_completed"]["details"]}
        appearance = {item["key"]: item for item in funnel["appearance_completed"]["details"]}
        offer = {item["key"]: item for item in funnel["examinations_offer_viewed"]["details"]}
        options = {item["key"]: item for item in funnel["examinations_opened"]["details"]}
        self.assertEqual(registration["max"]["users"], 1)
        self.assertEqual(registration["anonymous_button"]["events"], 1)
        self.assertEqual(appearance["extra"]["users"], 1)
        self.assertEqual(offer["catalog_info"]["users"], 1)
        self.assertEqual(offer["skip"]["users"], 1)
        self.assertEqual(options["pay_online"]["users"], 1)
        self.assertEqual(options["pay_at_exam"]["users"], 1)

    def test_metric2_reports_screen_reach_actions_and_branches(self):
        analytics.record_events("CHEL-METRIC-ONE", [
            {"event_id": "metric-one-welcome", "session_id": "metric-session-one", "event_name": "onboarding_screen_viewed", "properties": {"screen": "welcome", "context": "onboarding"}},
            {"event_id": "metric-one-continue", "session_id": "metric-session-one", "event_name": "onboarding_screen_action", "properties": {"screen": "welcome", "action": "continue", "context": "onboarding"}},
            {"event_id": "metric-one-register", "session_id": "metric-session-one", "event_name": "onboarding_screen_viewed", "properties": {"screen": "registration", "previous_screen": "welcome", "context": "onboarding"}},
            {"event_id": "metric-one-anonymous", "session_id": "metric-session-one", "event_name": "onboarding_screen_action", "properties": {"screen": "registration", "action": "anonymous", "context": "onboarding"}},
            {"event_id": "metric-one-warning", "session_id": "metric-session-one", "event_name": "onboarding_screen_viewed", "properties": {"screen": "anonymous_warning", "previous_screen": "registration", "context": "onboarding"}},
        ])
        analytics.record_events("CHEL-METRIC-TWO", [{
            "event_id": "metric-two-welcome", "session_id": "metric-session-two",
            "event_name": "onboarding_screen_viewed",
            "properties": {"screen": "welcome", "context": "onboarding"},
        }])
        # A chat-context visit must never enter the onboarding funnel.
        analytics.record_events("CHEL-METRIC-CHAT", [{
            "event_id": "metric-chat-exams", "session_id": "metric-session-chat",
            "event_name": "onboarding_screen_viewed",
            "properties": {"screen": "exam_selection", "context": "chat"},
        }])

        report = analytics.metric2_report("30")
        screens = {item["id"]: item for item in report["screens"]}
        self.assertEqual(report["summary"]["start_users"], 2)
        self.assertEqual(screens["welcome"]["percent_of_start"], 100.0)
        self.assertEqual(screens["registration"]["users"], 1)
        self.assertEqual(screens["registration"]["percent_of_start"], 50.0)
        self.assertTrue(screens["anonymous_warning"]["branch"])
        self.assertEqual(screens["anonymous_warning"]["percent_of_parent"], 100.0)
        welcome_actions = {item["id"]: item for item in screens["welcome"]["actions"]}
        self.assertEqual(welcome_actions["continue"]["users"], 1)
        self.assertEqual(welcome_actions["continue"]["percent_of_screen"], 50.0)
        self.assertEqual(screens["exam_selection"]["users"], 0)
        self.assertNotIn("chat", [item["id"] for item in report["screens"]])

    def test_metric2_reports_skipped_examinations_completion_screen(self):
        events = []
        for index, definition in enumerate(analytics._metric2_screen_definitions()):
            if definition.get("branch"):
                continue
            events.append({
                "event_id": f"metric-skip-main-{index:04d}",
                "session_id": "metric-session-skip",
                "event_name": "onboarding_screen_viewed",
                "properties": {"screen": definition["id"], "context": "onboarding"},
            })
            if definition["id"] == "exam_offer":
                break
        events.extend([
            {"event_id": "metric-skip-objection", "session_id": "metric-session-skip", "event_name": "onboarding_screen_viewed", "properties": {"screen": "exam_objection", "context": "onboarding"}},
            {"event_id": "metric-skip-refuse", "session_id": "metric-session-skip", "event_name": "onboarding_screen_action", "properties": {"screen": "exam_objection", "action": "refuse", "context": "onboarding"}},
            {"event_id": "metric-skip-completion", "session_id": "metric-session-skip", "event_name": "onboarding_screen_viewed", "properties": {"screen": "completion_skipped", "previous_screen": "exam_objection", "context": "onboarding"}},
            {"event_id": "metric-skip-link", "session_id": "metric-session-skip", "event_name": "onboarding_screen_action", "properties": {"screen": "completion_skipped", "action": "link_messenger", "context": "onboarding"}},
        ])
        analytics.record_events("CHEL-METRIC-SKIP", events)

        report = analytics.metric2_report("30")
        screens = {item["id"]: item for item in report["screens"]}
        completion = screens["completion_skipped"]
        self.assertEqual(completion["users"], 1)
        self.assertEqual(completion["percent_of_start"], 100.0)
        self.assertEqual(completion["percent_of_parent"], 100.0)
        self.assertTrue(completion["branch"])
        actions = {item["id"]: item for item in completion["actions"]}
        self.assertEqual(actions["link_messenger"]["users"], 1)
        self.assertEqual(actions["link_messenger"]["percent_of_screen"], 100.0)

    def test_metric2_legacy_events_cannot_exceed_the_start_cohort(self):
        for index in range(10):
            events = [{
                "event_id": f"legacy-auth-{index:04d}",
                "session_id": f"legacy-session-{index:04d}",
                "event_name": "auth_gate_viewed", "properties": {},
            }]
            if index < 6:
                events.insert(0, {
                    "event_id": f"legacy-welcome-{index:04d}",
                    "session_id": f"legacy-session-{index:04d}",
                    "event_name": "welcome_viewed", "properties": {},
                })
            analytics.record_events(f"CHEL-LEGACY-{index}", events)

        report = analytics.metric2_report("30")
        screens = {item["id"]: item for item in report["screens"]}
        self.assertEqual(screens["welcome"]["users"], 6)
        self.assertEqual(screens["registration"]["users"], 6)
        self.assertEqual(screens["registration"]["percent_of_start"], 100.0)
        self.assertTrue(all(item["percent_of_start"] <= 100 for item in report["screens"]))

    def test_reports_support_inclusive_custom_date_range(self):
        for index in range(2):
            analytics.record_events(f"CHEL-DATE-{index}", [{
                "event_id": f"date-welcome-{index:04d}",
                "session_id": f"date-session-{index:04d}",
                "event_name": "welcome_viewed", "properties": {},
            }])
        with analytics.connection() as conn:
            conn.execute(
                "UPDATE analytics_events SET received_at='2026-08-10T12:00:00+00:00' "
                "WHERE chel_id='CHEL-DATE-0'"
            )
            conn.execute(
                "UPDATE analytics_events SET received_at='2026-08-11T12:00:00+00:00' "
                "WHERE chel_id='CHEL-DATE-1'"
            )
            conn.commit()

        report = analytics.admin_report(
            "all", date_from="2026-08-10", date_to="2026-08-10",
        )
        metric2 = analytics.metric2_report(
            "all", date_from="2026-08-10", date_to="2026-08-10",
        )
        self.assertEqual(report["summary"]["visitors"], 1)
        self.assertEqual(metric2["summary"]["start_users"], 1)
        self.assertEqual(report["date_from"], "2026-08-10")
        self.assertEqual(report["date_to"], "2026-08-10")
        with self.assertRaisesRegex(ValueError, "начала периода"):
            analytics.metric2_report(
                "all", date_from="2026-08-12", date_to="2026-08-10",
            )


if __name__ == "__main__":
    unittest.main()
