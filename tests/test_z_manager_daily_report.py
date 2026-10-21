import json
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from backend import database as db, manager_daily_report
from backend.config import settings


class ManagerDailyReportTests(unittest.TestCase):
    def setUp(self):
        self.original = {
            "bitrix_connector_url": settings.bitrix_connector_url,
            "bitrix_metrics_secret": settings.bitrix_metrics_secret,
            "manager_daily_report_dialog_id": settings.manager_daily_report_dialog_id,
            "manager_daily_report_timezone": settings.manager_daily_report_timezone,
        }
        object.__setattr__(settings, "bitrix_connector_url", "http://connector")
        object.__setattr__(settings, "bitrix_metrics_secret", "metrics-secret")
        object.__setattr__(settings, "manager_daily_report_dialog_id", "chat86723")
        object.__setattr__(settings, "manager_daily_report_timezone", "Europe/Moscow")

    def tearDown(self):
        for key, value in self.original.items():
            object.__setattr__(settings, key, value)

    def test_report_lists_every_known_manager_including_zero(self):
        source = {
            "total_users": 4,
            "attributed_users": 3,
            "managers": [{
                "from_manager": "manager_2",
                "manager_name": "Аминева",
                "users": 3,
                "users_with_examinations": 2,
                "examination_conversion": 66.7,
            }],
        }
        with patch.object(db, "admin_manager_first_arrivals", return_value=source) as report:
            payload = manager_daily_report.build_report(
                report_date=date(2026, 10, 20),
            )
        report.assert_called_once_with("2026-10-20", "2026-10-20")
        self.assertEqual(payload["total_users"], 4)
        self.assertEqual(payload["unattributed_users"], 1)
        self.assertEqual(len(payload["managers"]), len(db.FROM_MANAGER_NAMES))
        managers = {item["from_manager"]: item for item in payload["managers"]}
        self.assertEqual(managers["manager_2"]["users"], 3)
        self.assertEqual(managers["manager_1"]["users"], 0)
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("chel_id", "phone", "messages", "answers"):
            self.assertNotIn(forbidden, serialized)

    def test_send_uses_deduplicated_daily_report_endpoint(self):
        response = MagicMock()
        response.read.return_value = b'{"status":"queued"}'
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with (
            patch.object(db, "admin_manager_first_arrivals", return_value={
                "total_users": 0, "attributed_users": 0, "managers": [],
            }),
            patch("backend.manager_daily_report.urlopen", return_value=response) as opened,
        ):
            result = manager_daily_report.send_report(
                report_date=date(2026, 10, 20),
            )
        self.assertEqual(result["connector"]["status"], "queued")
        request = opened.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/bitrix/reports/manager-users"))
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["report_id"], "consilium-manager-users-2026-10-20")


if __name__ == "__main__":
    unittest.main()
