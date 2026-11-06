import json
import os
import tempfile
import time
import unittest

import server


class MonthlyUsageTest(unittest.TestCase):
    def test_platform_month_to_date_uses_enforcement_ledger(self):
        old_path = server.CLAUDE_PLATFORM_LEDGER
        old_limit = server.CLAUDE_PLATFORM_MONTHLY_USD
        month = "%04d-%02d" % (time.gmtime().tm_year, time.gmtime().tm_mon)
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "platform.jsonl")
            with open(path, "w", encoding="utf-8") as h:
                h.write(json.dumps({"at": month + "-02T00:00:00Z", "cost-usd": 12.5}) + "\n")
                h.write("not json\n")
                h.write(json.dumps({"at": "2020-01-02T00:00:00Z", "cost-usd": 99}) + "\n")
            try:
                server.CLAUDE_PLATFORM_LEDGER = path
                server.CLAUDE_PLATFORM_MONTHLY_USD = 200.0
                got = server._usage_claude_platform()
            finally:
                server.CLAUDE_PLATFORM_LEDGER = old_path
                server.CLAUDE_PLATFORM_MONTHLY_USD = old_limit
        self.assertEqual(got["monthly_spend_usd"], 12.5)
        self.assertEqual(got["monthly_used_pct"], 6.25)
        self.assertEqual(got["monthly_receipts"], 1)

    def test_missing_ledger_is_measured_zero(self):
        old_path = server.CLAUDE_PLATFORM_LEDGER
        try:
            server.CLAUDE_PLATFORM_LEDGER = "/definitely/missing/platform.jsonl"
            got = server._usage_claude_platform()
        finally:
            server.CLAUDE_PLATFORM_LEDGER = old_path
        self.assertEqual(got["monthly_used_pct"], 0.0)
        self.assertEqual(got["monthly_receipts"], 0)

    def test_browser_has_separate_monthly_row(self):
        with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as h:
            page = h.read()
        self.assertIn('id="monthlyUsage"', page)
        self.assertIn("['claude_platform', 'platform'], ['kimi', 'kimi']", page)
        self.assertNotIn("['zai', 'zai'],\n                         ['kimi', 'kimi']", page)


if __name__ == "__main__":
    unittest.main()
