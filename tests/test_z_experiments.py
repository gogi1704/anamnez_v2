import tempfile
import unittest
from pathlib import Path

from backend import analytics
from backend import database as db
from backend.config import settings
from backend.main import resolve_metrika_counter_id


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = settings.database_path
        self.original_analytics = settings.analytics_database_path
        object.__setattr__(settings, "database_path", Path(self.temp_dir.name) / "main.db")
        object.__setattr__(settings, "analytics_database_path", Path(self.temp_dir.name) / "analytics.db")
        db.init_db()
        analytics.init_db()

    def tearDown(self):
        db.set_current_chel_id("chel_test_default")
        object.__setattr__(settings, "database_path", self.original_database)
        object.__setattr__(settings, "analytics_database_path", self.original_analytics)
        self.temp_dir.cleanup()

    def test_assignment_is_stable_when_percentage_changes(self):
        db.admin_update_experiment_settings({
            "enabled": True,
            "experiment_key": "marketer_test",
            "name": "Тест маркетолога",
            "marketer_percent": 100,
            "control_version": "main_v1",
            "marketer_version": "market_v1",
            "yandex_enabled": True,
            "yandex_goal_prefix": "market_test",
        })
        db.ensure_user("chel_experiment_first", pending=True)
        db.set_current_chel_id("chel_experiment_first")
        first = db.current_experiment_assignment()
        self.assertEqual(first["variant"], "marketer")

        db.admin_update_experiment_settings({"marketer_percent": 0})
        self.assertEqual(db.current_experiment_assignment()["variant"], "marketer")

        db.ensure_user("chel_experiment_second", pending=True)
        db.set_current_chel_id("chel_experiment_second")
        self.assertEqual(db.current_experiment_assignment()["variant"], "control")

    def test_preview_assignment_never_persists_or_skews_the_report(self):
        db.admin_update_experiment_settings({
            "enabled": True,
            "experiment_key": "preview_test",
            "name": "Превью-ссылка",
            "marketer_percent": 25,
            "control_version": "main_v1",
            "marketer_version": "market_v1",
            "yandex_enabled": True,
            "yandex_goal_prefix": "preview_test",
        })
        preview = db.experiment_preview_assignment("marketer")
        self.assertEqual(preview["variant"], "marketer")
        self.assertEqual(preview["version"], "market_v1")
        self.assertTrue(preview.get("preview"))
        # Opening the demo link repeatedly must not create real assignments.
        db.experiment_preview_assignment("marketer")
        db.experiment_preview_assignment("marketer")
        report = db.admin_experiment_report("all")
        self.assertEqual(report["total_users"], 0)

    def test_assignment_is_exact_round_robin_not_random_sample(self):
        db.admin_update_experiment_settings({
            "enabled": True,
            "experiment_key": "round_robin_test",
            "name": "Круговое распределение",
            "marketer_percent": 25,
            "control_version": "main_v1",
            "marketer_version": "market_v1",
            "yandex_enabled": True,
            "yandex_goal_prefix": "round_robin_test",
        })
        variants = []
        for index in range(12):
            chel_id = f"chel_round_robin_{index}"
            db.ensure_user(chel_id, pending=True)
            db.set_current_chel_id(chel_id)
            variants.append(db.current_experiment_assignment()["variant"])
        # Exactly every 4th user — no randomness in who lands where.
        self.assertEqual(
            variants,
            ["control", "control", "control", "marketer"] * 3,
        )

    def test_round_robin_matches_every_step_for_all_allowed_percentages(self):
        expected_patterns = {
            0: ["control"] * 8,
            25: (["control", "control", "control", "marketer"] * 2),
            50: (["control", "marketer"] * 4),
            75: (["control", "marketer", "marketer", "marketer"] * 2),
            100: ["marketer"] * 8,
        }
        for percent, expected in expected_patterns.items():
            db.admin_update_experiment_settings({
                "enabled": True,
                "experiment_key": f"step_test_{percent}",
                "name": f"Шаг {percent}",
                "marketer_percent": percent,
                "control_version": "main_v1",
                "marketer_version": "market_v1",
                "yandex_enabled": True,
                "yandex_goal_prefix": f"step_test_{percent}",
            })
            variants = []
            for index in range(8):
                chel_id = f"chel_step_{percent}_{index}"
                db.ensure_user(chel_id, pending=True)
                db.set_current_chel_id(chel_id)
                variants.append(db.current_experiment_assignment()["variant"])
            self.assertEqual(variants, expected, f"mismatch at {percent}%")

    def test_only_multiples_of_25_percent_are_accepted(self):
        db.admin_update_experiment_settings({
            "enabled": True,
            "experiment_key": "percent_validation_test",
            "name": "Проверка шага",
            "marketer_percent": 25,
            "control_version": "main_v1",
            "marketer_version": "market_v1",
            "yandex_enabled": True,
            "yandex_goal_prefix": "percent_validation_test",
        })
        for invalid_percent in (1, 10, 33, 60, 99):
            with self.assertRaisesRegex(ValueError, "0, 25, 50, 75 или 100"):
                db.admin_update_experiment_settings({"marketer_percent": invalid_percent})

    def test_disabling_experiment_stops_the_split_immediately(self):
        db.admin_update_experiment_settings({
            "enabled": True,
            "experiment_key": "disable_test",
            "name": "Проверка выключения",
            "marketer_percent": 100,
            "control_version": "main_v1",
            "marketer_version": "market_v1",
            "yandex_enabled": True,
            "yandex_goal_prefix": "disable_test",
        })
        db.ensure_user("chel_disable_before", pending=True)
        db.set_current_chel_id("chel_disable_before")
        self.assertEqual(db.current_experiment_assignment()["variant"], "marketer")

        db.admin_update_experiment_settings({"enabled": False})
        # Even an already-assigned user stops seeing a variant once disabled.
        off_for_existing = db.current_experiment_assignment()
        self.assertEqual(off_for_existing["variant"], "off")
        self.assertFalse(off_for_existing["enabled"])

        # New users are not split into any variant while disabled, and nothing
        # gets written to experiment_assignments for them.
        db.ensure_user("chel_disable_after", pending=True)
        db.set_current_chel_id("chel_disable_after")
        off_for_new = db.current_experiment_assignment()
        self.assertEqual(off_for_new["variant"], "off")
        report = db.admin_experiment_report("all")
        self.assertEqual(report["total_users"], 1)  # only chel_disable_before

    def test_report_compares_control_and_marketer_cohorts(self):
        db.admin_update_experiment_settings({
            "enabled": True,
            "experiment_key": "report_test",
            "name": "Отчёт эксперимента",
            "marketer_percent": 100,
            "control_version": "main_v1",
            "marketer_version": "market_v1",
            "yandex_enabled": True,
            "yandex_goal_prefix": "report_test",
        })
        chel_id = "chel_experiment_report"
        db.ensure_user(chel_id, pending=True)
        db.set_current_chel_id(chel_id)
        assignment = db.current_experiment_assignment()
        analytics.record_events(chel_id, [
            {"event_id": "experiment-assigned", "session_id": "experiment-session", "event_name": "experiment_assigned", "properties": {"experiment_key": "report_test", "experiment_variant": "marketer", "funnel_version": "market_v1"}},
            {"event_id": "experiment-questionnaire", "session_id": "experiment-session", "event_name": "questionnaire_completed", "properties": {"experiment_key": "report_test", "experiment_variant": "marketer", "funnel_version": "market_v1"}},
            {"event_id": "experiment-selection", "session_id": "experiment-session", "event_name": "examinations_selection_completed", "properties": {"selected_count": 1, "experiment_key": "report_test", "experiment_variant": "marketer", "funnel_version": "market_v1"}},
        ])
        self.assertEqual(assignment["variant"], "marketer")
        report = db.admin_experiment_report("all")
        marketer = next(item for item in report["variants"] if item["variant"] == "marketer")
        stages = {item["key"]: item for item in marketer["stages"]}
        self.assertEqual(marketer["users"], 1)
        self.assertEqual(stages["questionnaire"]["users"], 1)
        self.assertEqual(stages["application"]["users"], 1)
        self.assertEqual(report["yandex_goal"], "report_test_event")


class MetrikaCounterRoutingTests(unittest.TestCase):
    def test_marketer_variant_uses_dedicated_counter_when_configured(self):
        self.assertEqual(
            resolve_metrika_counter_id("marketer", "111111", "112754652"), "112754652",
        )

    def test_control_and_disabled_experiment_use_main_counter(self):
        self.assertEqual(
            resolve_metrika_counter_id("control", "111111", "222222"), "111111",
        )
        self.assertEqual(resolve_metrika_counter_id("off", "111111", "222222"), "111111")

    def test_marketer_never_falls_back_to_control_counter(self):
        self.assertEqual(resolve_metrika_counter_id("marketer", "111111", ""), "")
        self.assertEqual(
            resolve_metrika_counter_id("marketer", "111111", "not-a-number"), "",
        )

    def test_no_counter_configured_at_all_returns_empty(self):
        self.assertEqual(resolve_metrika_counter_id("marketer", "", ""), "")
        self.assertEqual(resolve_metrika_counter_id("control", "", ""), "")


if __name__ == "__main__":
    unittest.main()
