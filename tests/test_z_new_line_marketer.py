import tempfile
import unittest
from pathlib import Path

from backend import database as db
from backend.config import settings
from backend.onboarding import (
    marketer_offer_context,
    marketer_result_context,
    online_examination_price,
    public_onboarding,
)


class NewLineMarketerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = settings.database_path
        object.__setattr__(settings, "database_path", Path(self.temp_dir.name) / "main.db")
        db.init_db()

    def tearDown(self):
        db.set_current_chel_id("chel_test_default")
        object.__setattr__(settings, "database_path", self.original_database)
        self.temp_dir.cleanup()

    def test_editable_offer_texts_default_to_fourteen_days_and_persist(self):
        texts = db.admin_marketer_examination_texts()
        self.assertEqual(texts["result_days"], 14)
        self.assertEqual(texts["price_caption_template"], "При оплате онлайн")
        self.assertIn("retention_body_3", texts)
        self.assertIn("retention_benefit_4_body", texts)
        self.assertEqual(
            texts["result_bridge"],
            "Анкета — не формальность: по ответам видно, на что врач обратил бы внимание в первую очередь.",
        )
        self.assertEqual(texts["result_explanation_title"], "С чем это часто связано")
        self.assertEqual(
            texts["result_decision_body"],
            "Из той же пробы, что возьмут на осмотре — без записи и отдельной поездки.",
        )
        self.assertEqual(
            texts["result_disclaimer"],
            "Это не диагноз, а то, на что стоит обратить внимание. Добавлять анализы не обязательно.",
        )
        texts["result_days"] = 21
        texts["headline"] = "Новый проверочный заголовок"
        saved = db.admin_update_marketer_examination_texts(texts)
        self.assertEqual(saved["result_days"], 21)
        self.assertEqual(saved["headline"], "Новый проверочный заголовок")

    def test_online_discount_is_ten_percent_for_every_real_price(self):
        self.assertEqual(online_examination_price({"price": 1000}), 900)
        self.assertEqual(online_examination_price({"price": 3500}), 3150)
        self.assertEqual(online_examination_price({"price": 999}), 899)

    def test_variant_c_rule_copy_and_packages_match_specification(self):
        texts = db.admin_marketer_examination_texts()
        self.assertEqual(texts["result_rule_1_chip"], "усталость, которая не проходит после отдыха")
        self.assertEqual(texts["result_rule_3_chip"], "давление бывает повышенным")
        self.assertEqual(texts["result_rule_7_chip"], "{возраст} лет")
        self.assertEqual(texts["result_rule_10_chip"], "жалоб нет")
        self.assertEqual(texts["result_rule_10_chip_pressure"], "давление в норме")
        self.assertEqual(texts["result_rule_10_chip_no_smoking"], "не курите")
        available = {item["id"] for item in db.list_examinations()}
        male_fatigue = marketer_result_context({
            "fatigue": "yes", "sex": "male", "age": 30,
        }, available)
        self.assertEqual(
            male_fatigue["rule_test_ids"]["2"],
            ["fatigue_basic", "vitamin_d"],
        )

    def test_offer_context_selects_one_primary_and_three_visible_packages(self):
        available = {item["id"] for item in db.list_examinations()}
        context = marketer_offer_context({"fatigue": "yes", "sex": "female"}, available)
        self.assertEqual(context["rule_id"], 5)
        self.assertTrue(context["primary_test_id"])
        self.assertLessEqual(len(context["visible_recommended_test_ids"]), 3)
        self.assertEqual(
            context["visible_recommended_test_ids"][0], context["primary_test_id"],
        )

    def test_complaints_alone_do_not_select_a_personal_rule(self):
        available = {item["id"] for item in db.list_examinations()}
        context = marketer_offer_context(
            {"notes": "Болит голова", "sex": "female", "age": 30}, available,
        )
        self.assertEqual(context["rule_id"], 10)

    def test_personal_copy_rules_use_first_matching_condition_in_priority_order(self):
        available = {item["id"] for item in db.list_examinations()}
        cases = [
            ({"blood_pressure": "high", "height_cm": 170, "weight_kg": 100, "alcohol": "often"}, 2),
            ({"blood_pressure": "unstable", "fatigue": "yes", "sex": "female"}, 2),
            ({"height_cm": 170, "weight_kg": 90, "alcohol": "often", "joint_pain": "yes"}, 3),
            ({"alcohol": "often", "fatigue": "yes", "sex": "female"}, 4),
            ({"fatigue": "yes", "sex": "female", "joint_pain": "yes", "age": 50}, 5),
            ({"fatigue": "yes", "sex": "male", "joint_pain": "yes", "age": 45}, 6),
            ({"joint_pain": "yes", "sex": "female", "age": 50}, 7),
            ({"sex": "female", "age": 45}, 8),
            ({"sex": "male", "age": 40}, 9),
            ({"sex": "female", "age": 30}, 10),
        ]
        for profile, expected_rule in cases:
            with self.subTest(profile=profile):
                self.assertEqual(
                    marketer_offer_context(profile, available)["rule_id"],
                    expected_rule,
                )

    def test_each_personal_rule_recommends_the_matching_method_packages(self):
        available = {item["id"] for item in db.list_examinations()}
        cases = [
            ({"blood_pressure": "high"}, ["kidneys", "thyroid", "lipids"]),
            ({"height_cm": 170, "weight_kg": 90}, ["weight_basic", "lipids", "liver_basic", "kidneys", "thyroid"]),
            ({"alcohol": "often"}, ["liver_basic", "kidneys"]),
            ({"fatigue": "yes", "sex": "female"}, ["iron", "fatigue_basic", "vitamin_d"]),
            ({"fatigue": "yes", "sex": "male"}, ["vitamin_d", "fatigue_basic"]),
            ({"joint_pain": "yes"}, ["joints", "inflammation"]),
            ({"sex": "female", "age": 45}, ["female_hormones"]),
            ({"sex": "male", "age": 40}, ["male_health"]),
            ({"sex": "female", "age": 30}, ["liver_basic", "kidneys", "vitamin_d"]),
        ]
        for profile, expected_ids in cases:
            with self.subTest(profile=profile):
                context = marketer_offer_context(profile, available)
                self.assertEqual(context["recommended_test_ids"], expected_ids)
                self.assertEqual(context["primary_test_id"], expected_ids[0])
                self.assertEqual(context["visible_recommended_test_ids"], expected_ids[:3])

    def test_rule_text_is_not_reset_when_primary_package_differs(self):
        context = marketer_offer_context(
            {"blood_pressure": "high"}, {"fatigue_basic"},
        )
        self.assertEqual(context["rule_id"], 2)

    def test_marketer_context_never_recommends_gender_incompatible_tests(self):
        onboarding = public_onboarding(
            {"selected_tests": []},
            {"sex": "female", "age": 48},
            db.list_examinations(),
        )
        incompatible = set(onboarding["gender_incompatible_test_ids"])
        offered = {
            onboarding["marketer_offer"]["primary_test_id"],
            *onboarding["marketer_offer"]["visible_recommended_test_ids"],
        }
        self.assertIn("male_health", incompatible)
        self.assertTrue(offered.isdisjoint(incompatible))

    def test_new_admin_and_client_markup_contains_required_controls(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "dashboard.html").read_text(encoding="utf-8")
        dashboard_css = (root / "static" / "dashboard.css").read_text(encoding="utf-8")
        dashboard_script = (root / "static" / "dashboard.js").read_text(encoding="utf-8")
        app = (root / "static" / "app.js").read_text(encoding="utf-8")
        main = (root / "backend" / "main.py").read_text(encoding="utf-8")
        self.assertIn('data-content-text-tab="retention"', dashboard)
        self.assertIn('id="contentTextPreviewPhone"', dashboard)
        self.assertIn('id="contentTextPreviewRule"', dashboard)
        self.assertIn('id="metric2ExperimentLinkOpen"', dashboard)
        self.assertIn('Тестовая ссылка ветки маркетолога', dashboard)
        self.assertIn(".dashboard.show-content-texts", dashboard_css)
        self.assertIn(".content-text-preview", dashboard_css)
        self.assertIn("function renderContentTextPreview", dashboard_script)
        self.assertIn("preview_marketer_variant=${marketerCFlow ? 'c' : 'b'}", dashboard_script)
        self.assertIn("Открыть тестовую ветку C", dashboard_script)
        self.assertIn("suppliedData = null", dashboard_script)
        self.assertIn('"examinations": db.list_examinations()', main)
        self.assertIn('class="marketer-offer-actions"', app)
        self.assertIn('class="marketer-retention-benefits"', app)
        self.assertIn("setOnboardingMeta('Ваши ответы',100)", app)
        self.assertIn("result_rule_10_chip_no_smoking", app)
        self.assertIn("phrase2_part:phrases[1] ? `, а ${phrases[1]}` : ''", app)
        self.assertIn("width:min(560px,calc(100vw - 32px))", (root / "static" / "styles.css").read_text(encoding="utf-8"))
        self.assertNotIn("Ещё может быть полезно", app)
        self.assertNotIn('data-text-key="personal_rule_1"', dashboard)


if __name__ == "__main__":
    unittest.main()
