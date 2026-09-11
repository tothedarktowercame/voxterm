import json
import tempfile
import unittest
from pathlib import Path
from wm_work_status import read_work_status

class WorkTests(unittest.TestCase):
    def test_preparation_separate_from_job_completion(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'work.json'
            p.write_text(json.dumps(dict(schema='voxterm/wm-work-v1',summary='Staffing',next_step='Review',updated_at='date',issues=[],job_id='invoke-123')))
            for state in ['running','done','failed']:
                r=read_work_status(p,lambda _:30,lambda _: {'job-id':'invoke-123','state':state,'agent-id':'zai-2','events':[{'at':'date','type':'done'}]})
                self.assertEqual(r['kind'],'preparation-only')
                self.assertEqual(r['job']['state'],state)
                self.assertNotIn('run_evidence',r)
                self.assertNotIn('result',r)
            r=read_work_status(p,lambda _:30,lambda _: {'job-id':'foreign'})
            self.assertEqual(r['job']['state'],'unknown')
            self.assertIn('observation_error',r['job'])
    def test_incomplete_readback_is_not_task_acceptance(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)/'work.json'
            doc = dict(schema='voxterm/wm-work-v1', summary='Failure', next_step='Fix',
                       updated_at='date', issues=[], execution_observation=dict(
                           click_id='wm-click-123', status='incomplete', error='Pin refused',
                           observed_at='date', evidence='/retained/readback.md'))
            p.write_text(json.dumps(doc))
            r = read_work_status(p, lambda _: 0)
            self.assertEqual(r['execution_observation']['authority'], 'coordinator-readback')
            self.assertNotIn('result', r)
            doc['execution_observation']['status'] = 'passed'
            p.write_text(json.dumps(doc))
            self.assertEqual(read_work_status(p, lambda _: 0)['state'], 'invalid')

    def test_invalid_and_absent(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'work.json'
            self.assertEqual(read_work_status(p,lambda _:0)['state'],'unconfigured')
            for value in [None,[],{}, {'schema':'voxterm/wm-work-v1'}]:
                p.write_text(json.dumps(value))
                self.assertEqual(read_work_status(p,lambda _:0)['state'],'invalid')

if __name__=='__main__': unittest.main()
