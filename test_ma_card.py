"""The /xiang/card match: which 馬 card belongs to a turn (pure function)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def entry(turn, at, work="skip"):
    fresh = {} if work == "skip" else {"work": work}
    return {"evidence/body": {"event": "agent-card", "turn-evidence-id": turn,
                              "at": at, "card": {"fresh": fresh}}}


class MaCardWorkTest(unittest.TestCase):
    def test_picks_the_matching_turn_latest_of_several(self):
        entries = [entry("t1", "2026-10-19T03:38:20Z", {"none": "first"}),
                   entry("t1", "2026-10-19T03:41:45Z", {"item": "i", "quote": "q"}),
                   entry("t2", "2026-10-19T03:50:00Z", {"none": "other turn"})]
        self.assertEqual(server.ma_card_work(entries, "t1")["at"], "2026-10-19T03:41:45Z")
        self.assertEqual(server.ma_card_work(entries, "t1")["card"]["fresh"]["work"],
                         {"item": "i", "quote": "q"})

    def test_none_when_unread_or_malformed(self):
        self.assertIsNone(server.ma_card_work([], "t1"))
        self.assertIsNone(server.ma_card_work([entry("t2", "2026-10-19T03:50:00Z")], "t1"))
        self.assertIsNone(server.ma_card_work([{"evidence/body": None}], "t1"))
        # a card written before the work record: matched, but no work key
        b = server.ma_card_work([entry("t1", "2026-10-19T01:00:00Z")], "t1")
        self.assertEqual(b["at"], "2026-10-19T01:00:00Z")
        self.assertNotIn("work", b["card"]["fresh"])


if __name__ == "__main__":
    unittest.main()
