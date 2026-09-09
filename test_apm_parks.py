import os
import tempfile
import unittest
from unittest.mock import patch
import server


class ParkProjectionTest(unittest.TestCase):
    def test_nested_reports_and_later_frames_cannot_impersonate_a_park(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "queue-state.edn")
            with open(path, "w") as handle:
                handle.write('{:parked [{:frame/id "f200" :problem/id "m01J06" '
                             ':error/code :solver-session-mismatch '
                             ':fault/result {:text "' + 'x' * 5000 + '" '
                             ':frame/id "f205" :problem/id "m02A06" '
                             ':error/code :wrong-nested-code} '
                             ':decision/status :awaiting-decision} '
                             '{:frame/id "f202" :problem/id "m02A03" '
                             ':error/code :solver-remediation-required '
                             ':decision/status :awaiting-decision} '
                             '{:frame/id "old" :decision/status :decided}]}')
            expected = {"rows": [
                {"frame": "f200", "problem": "m01J06", "code": "solver-session-mismatch"},
                {"frame": "f202", "problem": "m02A03", "code": "solver-remediation-required"}],
                "error": None}
            self.assertEqual(expected, server._apm_parked_decisions(directory))
            with patch.object(server.subprocess, "run", side_effect=AssertionError("cache missed")):
                self.assertEqual(expected, server._apm_parked_decisions(directory))
            with open(path, "w") as handle:
                handle.write('{:parked []}')
            self.assertEqual([], server._apm_parked_decisions(directory)["rows"])

    def test_parse_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "queue-state.edn"), "w") as handle:
                handle.write('{:parked [')
            result = server._apm_parked_decisions(directory)
            self.assertIsNotNone(result["error"])


if __name__ == "__main__":
    unittest.main()
