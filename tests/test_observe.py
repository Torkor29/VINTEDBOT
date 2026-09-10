import json
import unittest

from app.observe_catalog import analyse


class ObserveTests(unittest.TestCase):
    def test_structural_analysis_finds_dates_without_titles_or_accounts(self):
        raw = json.dumps({"items": [
            {"id": 20, "title": "secret title", "created_at": "2026-09-10T10:00:00Z",
             "user": {"login": "private"}},
            {"id": 19, "title": "other", "created_at": "2026-09-10T09:59:00Z"},
        ], "pagination": {"current_page": 1}})
        result = analyse(raw)
        self.assertEqual(result["ids"], ["20", "19"])
        self.assertEqual(result["date_fields"]["created_at"]["type"], "str")
        self.assertNotIn("secret title", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))
