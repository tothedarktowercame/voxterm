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
                {"frame": "f200", "problem": "m01J06", "phase": None,
                 "code": "solver-session-mismatch"},
                {"frame": "f202", "problem": "m02A03", "phase": None,
                 "code": "solver-remediation-required"}],
                "repair": None, "error": None}
            self.assertEqual(expected, server._apm_parked_decisions(directory))
            with patch.object(server.subprocess, "run", side_effect=AssertionError("cache missed")):
                self.assertEqual(expected, server._apm_parked_decisions(directory))
            with open(path, "w") as handle:
                handle.write('{:parked []}')
            self.assertEqual([], server._apm_parked_decisions(directory)["rows"])

    def test_phase_distinguishes_a_parked_measurement_from_a_parked_problem(self):
        # Pinned verbatim from jit-all-open-v3/queue-state.edn, 2026-09-09:
        # f206's proof was solved, verified and landed on apm-lean master in
        # 33c076fd, and the frame parked in the learning arm two phases later.
        # Without the phase the strip said "m02A06", which reads as unsolved.
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "queue-state.edn"), "w") as handle:
                handle.write('{:parked [{:frame/id "f206" :problem/id "m02A06" '
                             ':phase :student-attempt-2 '
                             ':error/code :live-job-terminal-repair-exhausted '
                             ':decision/status :awaiting-decision}]}')
            rows = server._apm_parked_decisions(directory)["rows"]
            self.assertEqual([{"frame": "f206", "problem": "m02A06",
                               "phase": "student-attempt-2",
                               "code": "live-job-terminal-repair-exhausted"}], rows)
            self.assertEqual("m02A06@student-attempt-2",
                             server._apm_park_label(rows[0]))

    def test_a_fault_park_without_a_phase_still_names_itself(self):
        self.assertEqual("f169", server._apm_park_label(
            {"frame": "f169", "problem": None, "phase": None}))

    def test_parse_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "queue-state.edn"), "w") as handle:
                handle.write('{:parked [')
            result = server._apm_parked_decisions(directory)
            self.assertIsNotNone(result["error"])


if __name__ == "__main__":
    unittest.main()
