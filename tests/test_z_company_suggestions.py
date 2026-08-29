import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import company_suggestions


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


def _settings(api_key="secret"):
    return SimpleNamespace(
        dadata_api_key=api_key,
        dadata_suggestions_url="https://suggestions.dadata.ru/test",
        dadata_timeout_seconds=5,
        dadata_suggestions_cache_seconds=600,
    )


class CompanySuggestionsTests(unittest.TestCase):
    def setUp(self):
        company_suggestions._cache.clear()
        company_suggestions._client_requests.clear()

    def test_disabled_provider_keeps_manual_input_available(self):
        with patch.object(company_suggestions, "settings", _settings("")), patch.object(
            company_suggestions, "urlopen"
        ) as mocked:
            self.assertEqual(company_suggestions.suggest_companies("7707"), [])
            mocked.assert_not_called()

    def test_short_query_does_not_call_provider(self):
        with patch.object(company_suggestions, "settings", _settings()), patch.object(
            company_suggestions, "urlopen"
        ) as mocked:
            self.assertEqual(company_suggestions.suggest_companies("770"), [])
            mocked.assert_not_called()

    def test_results_are_minimal_deduplicated_and_prefix_filtered(self):
        response = _Response({"suggestions": [
            {"value": "ПАО СБЕРБАНК", "data": {"inn": "7707083893"}},
            {"value": "ПАО СБЕРБАНК", "data": {"inn": "7707083893"}},
            {"value": "Другая организация", "data": {"inn": "7812014560"}},
        ]})
        with patch.object(company_suggestions, "settings", _settings()), patch.object(
            company_suggestions, "urlopen", return_value=response
        ) as mocked:
            result = company_suggestions.suggest_companies("7707", "127.0.0.1")
        self.assertEqual(result, [{"inn": "7707083893", "name": "ПАО СБЕРБАНК"}])
        request = mocked.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Token secret")
        self.assertEqual(json.loads(request.data), {
            "query": "7707", "count": 7, "status": ["ACTIVE"],
        })

    def test_cached_result_avoids_second_provider_request(self):
        response = _Response({"suggestions": [
            {"value": "Организация", "data": {"inn": "7707000000"}},
        ]})
        with patch.object(company_suggestions, "settings", _settings()), patch.object(
            company_suggestions, "urlopen", return_value=response
        ) as mocked:
            company_suggestions.suggest_companies("7707", "first")
            result = company_suggestions.suggest_companies("7707", "second")
        self.assertEqual(result[0]["inn"], "7707000000")
        self.assertEqual(mocked.call_count, 1)


if __name__ == "__main__":
    unittest.main()
