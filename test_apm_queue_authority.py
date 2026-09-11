import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import server


class QueueAuthorityTest(unittest.TestCase):
    def test_recovery_uses_active_frame_and_explicit_retry_not_newest_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns = root / 'apm-campaigns'
            campaign = 'jit-all-open-v3'
            cdir = campaigns / campaign
            cdir.mkdir(parents=True)
            registry = root / 'apm-coordinators'
            registry.mkdir()
            coord = cdir / 'coordinator.edn'
            coord.write_text(
                '{:regulator/status :running :regulator/last-result {:status :awaiting-substrate} '
                ':regulator/quiescence-witness {:witnessed-at "2026-09-11T16:54:00Z"} '
                ':coordinator/delayed-retry {:kind :transport :not-before-ms 2000000 '
                ':scheduled-at-ms 1900000 :attempt 0 :max-attempts 3 '
                ':history [{:error/code :memory-snapshot-visibility-not-obtained}]}}')
            (registry / 'registry.edn').write_text(
                '{:entries {"jit-queue:jit-all-open-v3" {:coordinator/enabled? true '
                ':coordinator/state-path ' + json.dumps(str(coord)) + '}}}')
            (cdir / 'queue-state.edn').write_text(
                '{:active {:frame {:frame/id "f224"}} '
                ':resumption-queue [{:frame {:frame/id "f225"}} {:frame {:frame/id "f226"}}] '
                ':historical {:status :failed-systematic-frame-failure}}')
            for n in (224, 225, 226):
                frame = cdir / f'{campaign}-f{n}'
                (frame / 'live').mkdir(parents=True)
                (frame / 'ledger.edn').write_text(
                    '{:event/type :frame/advanced :event/at "1970-01-01T00:30:00Z" '
                    ':event/body {:problem-id "p%d" :from :student-attempt-3, :to :scribe-reduce}}' % n)
                (frame / 'live' / 'scribe-reduce.edn').write_text(
                    '{:stage :old :error/code :live-proof-terminal-invalid}')
            with patch.object(server, 'APM_ROOT', str(campaigns)), \
                 patch.object(server.time, 'time', return_value=1950), \
                 patch.object(server, '_apm_running_jobs', return_value=[]), \
                 patch.object(server, '_jvm_health', return_value=None), \
                 patch.object(server, '_substrate_permits', return_value=None), \
                 patch.object(server, '_apm_parked_decisions', return_value={'rows': [], 'error': None}):
                result = server.apm_status()
            self.assertEqual('f224', result['frame'])
            self.assertEqual('scribe-reduce', result['phase'])
            self.assertEqual('waiting', result['state'])
            self.assertIn('00:33:20Z', result['alert'])
            self.assertEqual(50, result['retry_wait']['retry_in_s'])
            self.assertEqual('memory-snapshot-visibility-not-obtained',
                             result['phase_detail']['error_code'])
            self.assertIsNone(result['lifecycle']['stopped_at'])
            self.assertEqual(['f226', 'f225'], [r['frame'] for r in result['recent']])
            self.assertTrue(all(r['end'] == 'queued to resume; not terminal'
                                for r in result['recent']))

    def test_missing_active_frame_is_an_error_not_a_fallback(self):
        with patch.object(server, '_apm_active_campaign', return_value='c'), \
             patch.object(server, '_apm_lifecycle', return_value={'active_frame': 'f224'}), \
             patch.object(server, '_apm_frame_dirs', return_value=[(226, '/f226')]):
            result = server.apm_status()
        self.assertFalse(result['ok'])
        self.assertIn('f224', result['error'])

    def test_phase_error_is_not_taken_from_retained_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / 'live'
            live.mkdir()
            (live / 'solve.edn').write_text(
                '{:stage :running :history [{:stage :failed '
                ':error/code :live-proof-terminal-invalid}]}')
            result = server._apm_phase_detail(tmp, 'solve')
            self.assertEqual('running', result['stage'])
            self.assertIsNone(result['error_code'])
            (live / 'solve.edn').write_text('{:broken')
            self.assertEqual('status-unreadable',
                             server._apm_phase_detail(tmp, 'solve')['stage'])


if __name__ == '__main__':
    unittest.main()
