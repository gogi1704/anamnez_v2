import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import splitter_tracking


ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
VISITOR_ID = "22222222-2222-4222-8222-222222222222"


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class SplitterTrackingTests(unittest.TestCase):
    def test_tracking_requires_two_valid_uuids(self):
        self.assertEqual(splitter_tracking.tracking_from_payload({
            "attemptId": ATTEMPT_ID, "visitorId": VISITOR_ID,
        }), {"attemptId": ATTEMPT_ID, "visitorId": VISITOR_ID})
        self.assertIsNone(splitter_tracking.tracking_from_payload({
            "attemptId": "bad", "visitorId": VISITOR_ID,
        }))

    def test_notify_uses_server_secret_and_expected_payload(self):
        fake_settings = SimpleNamespace(
            splitter_event_url="http://ab_splitter:8000/api/delivery/event",
            splitter_event_secret="shared-secret",
            splitter_event_timeout_seconds=2,
        )
        tracking = {"attemptId": ATTEMPT_ID, "visitorId": VISITOR_ID}
        with patch.object(splitter_tracking, "settings", fake_settings), patch.object(
            splitter_tracking, "urlopen", return_value=_Response(),
        ) as mocked:
            self.assertEqual(splitter_tracking.notify(tracking, "welcome_shown"), "sent")
        request = mocked.call_args.args[0]
        self.assertEqual(request.get_header("X-splitter-secret"), "shared-secret")
        self.assertEqual(json.loads(request.data), {
            **tracking, "event": "welcome_shown",
        })

    def test_disabled_tracking_does_not_make_network_request(self):
        fake_settings = SimpleNamespace(
            splitter_event_url="", splitter_event_secret="",
            splitter_event_timeout_seconds=2,
        )
        tracking = {"attemptId": ATTEMPT_ID, "visitorId": VISITOR_ID}
        with patch.object(splitter_tracking, "settings", fake_settings), patch.object(
            splitter_tracking, "urlopen",
        ) as mocked:
            self.assertEqual(splitter_tracking.notify(tracking, "javascript_started"), "disabled")
            mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
