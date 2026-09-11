import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import server


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.frame = Path(self.tmp.name)
        (self.frame / 'ledger.edn').write_text(
            '{:event/at "2026-09-11T00:00:00Z", :from :verify, :to :promote-solver}')

    def tearDown(self):
        self.tmp.cleanup()

    def timeline(self, lifecycle, jobs=None, now=9999999999):
        with patch.object(server, '_apm_running_jobs', return_value=jobs or []), \
             patch.object(server.time, 'time', return_value=now):
            return server._apm_frame_timeline(str(self.frame), 'c', 'f218', lifecycle)[-1]

    def test_stopped_age_does_not_keep_growing(self):
        life = {'enabled': False, 'tick_claim': False, 'stopped_at': '2026-09-11T00:02:00Z'}
        before = self.timeline(life)
        after = self.timeline(life, now=99999999999)
        self.assertEqual('stopped', before['actor'])
        self.assertEqual(120, before['duration_s'])
        self.assertEqual(before['duration_s'], after['duration_s'])
        self.assertEqual('phase age at stop', after['duration_kind'])

    def test_positive_tick_evidence_and_draining_role(self):
        self.assertEqual('in-process', self.timeline({'enabled': True, 'tick_claim': True})['actor'])
        self.assertEqual('waiting', self.timeline({'enabled': True, 'tick_claim': False})['actor'])
        self.assertEqual('unknown', self.timeline(None)['actor'])
        row = self.timeline({'enabled': False}, [{'agent': 'f218-guide', 'for_s': 10}])
        self.assertEqual('agent', row['actor'])
        self.assertTrue(row['draining'])

    def test_operator_stop_precedes_stale_watchdog(self):
        with patch.object(server, '_apm_lifecycle', return_value={'enabled': False}), \
             patch.object(server, '_apm_running_jobs', return_value=[]), \
             patch.object(server, '_jvm_health', return_value=None), \
             patch.object(server, '_substrate_permits', return_value=None):
            # Actual paused campaign: no runtime call or restart involved.
            result = server.apm_status()
        self.assertEqual('stopped', result['state'])
        self.assertIn('automatic restart disabled', result['alert'])
        self.assertNotIn('supervisor gone', result['alert'])

    def test_lifecycle_reader_ignores_nested_historical_state(self):
        data = self.frame
        (data / 'apm-coordinators').mkdir()
        (data / 'apm-campaigns').mkdir()
        state = data / 'state.edn'
        state.write_text('{:regulator/status :stopped, :historical {:regulator/status :running}}')
        (data / 'apm-coordinators' / 'registry.edn').write_text(
            '{:entries {"jit-queue:c" {:coordinator/enabled? false, '
            ':coordinator/state-path ' + json.dumps(str(state)) + '}}}')
        with patch.object(server, 'APM_ROOT', str(data / 'apm-campaigns')):
            self.assertEqual(False, server._apm_lifecycle('c')['enabled'])
            self.assertEqual('stopped', server._apm_lifecycle('c')['status'])

    def test_store_worker_queue_is_distinct_from_read_permits(self):
        body = (b'{:permits/total 4 :permits/available 2 :permits/waiters 1 '
                b':request-workers [{:workers/total 4 :workers/active 4 :requests/queued 7}] '
                b':node-open? true}')
        with patch.object(server, 'urlopen', return_value=io.BytesIO(body)) as request:
            status = server._substrate_permits()
        self.assertEqual(2, status['available'])
        self.assertEqual(1, status['permit_waiters'])
        self.assertEqual(7, status['requests_queued'])
        self.assertEqual(4, status['workers_active'])
        self.assertIn('/health', request.call_args.args[0])
        with patch.object(server, 'urlopen', return_value=io.BytesIO(b'{:node-open? true}')):
            self.assertIsNone(server._substrate_permits()['requests_queued'])
        with patch.object(server, 'urlopen', side_effect=TimeoutError):
            self.assertIsNone(server._substrate_permits())


if __name__ == '__main__':
    unittest.main()
