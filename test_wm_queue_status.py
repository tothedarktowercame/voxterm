import json
import tempfile
import unittest
from pathlib import Path
from wm_queue_status import read_queue_status


class QueueStatusTests(unittest.TestCase):
    def test_real_producer_shape_preserves_hold_not_worker_activity(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'queue.json'
            doc = {"schema":"wm/run4-series-queue-visibility-v1", "queue_id":"q",
                   "controller_state":"held", "updated_at":"date", "hold_reason":"terminal-evidence-incomplete",
                   "target":{"entry_id":"e", "series_id":"s", "manifest_sha256":"a"*64},
                   "assigned_roles":{"author":"zai-2", "reviewer":"codex-12", "repair_reviewer":"codex-12"},
                   "active_actors":[], "in_flight":{"entry-id":"e", "click-id":"click"}}
            path.write_text(json.dumps(doc))
            result=read_queue_status(str(path),lambda _:3,60)
            self.assertEqual('held',result['state'])
            self.assertEqual([],result['active_actors'])
            self.assertEqual('click',result['in_flight']['click-id'])
            self.assertEqual('stale',read_queue_status(str(path),lambda _:61,60)['state'])
            for field, value in [('active_actors',['zai-2']),('target',[]),('controller_state',{}),('hold_reason',None)]:
                path.write_text(json.dumps({**doc,field:value}))
                self.assertEqual('invalid',read_queue_status(str(path),lambda _:3,60)['state'])
    def test_absent_invalid_and_unconfigured_are_distinct(self):
        self.assertEqual('unconfigured',read_queue_status(None,lambda _:0,60)['state'])
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'queue.json'
            self.assertEqual('absent',read_queue_status(str(path),lambda _:0,60)['state'])
            path.write_text('false')
            self.assertEqual('invalid',read_queue_status(str(path),lambda _:0,60)['state'])

if __name__=='__main__': unittest.main()
