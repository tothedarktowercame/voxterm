"""Coordinator work notes and Agency job observations, never execution evidence."""
import json
import re
from urllib.request import urlopen


def fetch_job(job_id):
    with urlopen('http://127.0.0.1:7070/api/alpha/invoke/jobs/' + job_id, timeout=2) as response:
        return json.load(response)['job']


def read_work_status(path, age, fetch=fetch_job):
    try:
        with open(path, encoding='utf-8') as handle:
            doc = json.load(handle)
        if (not isinstance(doc, dict) or doc.get('schema') != 'voxterm/wm-work-v1'
                or not all(isinstance(doc.get(k), str) and doc[k].strip()
                           for k in ('summary', 'next_step', 'updated_at'))
                or not isinstance(doc.get('issues'), list)
                or len(doc['issues']) > 20
                or age(doc['updated_at']) is None):
            raise ValueError('invalid work note')
        issues = []
        for item in doc['issues']:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k]
                    for k in ('title', 'status', 'detail', 'evidence')):
                raise ValueError('invalid work issue')
            issues.append({k: item[k] for k in ('title', 'status', 'detail', 'evidence')})
        result = {k: doc[k] for k in ('summary', 'next_step', 'updated_at')}
        result.update(state='recorded', source=path, issues=issues,
                      note_age_s=age(doc['updated_at']), kind='preparation-only')
        job_id = doc.get('job_id')
        if job_id is not None:
            if not isinstance(job_id, str) or not re.fullmatch(r'invoke-[A-Za-z0-9-]+', job_id):
                raise ValueError('invalid job ID')
            result['job'] = {'id': job_id, 'state': 'unknown'}
            try:
                job = fetch(job_id)
                if job.get('job-id') != job_id:
                    raise ValueError('job identity mismatch')
                events = job.get('events', [])
                last = next((e for e in reversed(events) if e.get('at')), {})
                result['job'].update(state=job.get('state', 'unknown'), owner=job.get('agent-id'),
                                     event_age_s=age(last.get('at')), last_event_type=last.get('type'))
            except Exception:
                result['job']['observation_error'] = 'Agency job unavailable; activity unknown'
        return result
    except FileNotFoundError:
        return {'state': 'unconfigured', 'kind': 'preparation-only'}
    except (OSError, ValueError, TypeError):
        return {'state': 'invalid', 'kind': 'preparation-only', 'source': path}
