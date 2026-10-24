"""HTTP regressions for explicit worker ownership, eligibility and demo execution."""
import http.client
import json
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from relay.server import make_server

PROMPT = 'Add a password-strength validator and tests to this sample project.'


class WorkerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(self.tmp.name, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = None
        self.login('alice')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.relay.close()
        self.tmp.cleanup()

    def request(self, path, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        headers = {'Content-Type': 'application/json'}
        if self.cookie:
            headers['Cookie'] = self.cookie
        connection.request('POST' if body is not None else 'GET', path,
                           json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        content = response.read()
        cookie = response.getheader('Set-Cookie')
        status = response.status
        connection.close()
        return status, json.loads(content), cookie

    def login(self, user):
        status, _, cookie = self.request('/api/login', {'user_id': user})
        self.assertEqual(status, 200)
        self.cookie = cookie.split(';')[0]

    def post(self, method, body, expected=200):
        status, data, _ = self.request('/api/' + method, body)
        self.assertEqual(status, expected, data)
        return data

    def get(self, path, expected=200):
        status, data, _ = self.request('/api/' + path)
        self.assertEqual(status, expected, data)
        return data

    def test_worker_directory_applies_owner_filter_and_project_permissions(self):
        directory = self.get('workers?' + urlencode({'project_id': 'demo'}))
        self.assertEqual({worker['id'] for worker in directory['workers']}, {'codexA', 'codexB'})
        self.assertTrue(all(worker['eligible'] for worker in directory['workers']))
        self.assertFalse(directory['personal_subscription']['supported'])
        mine = self.get('workers?mine=1')
        self.assertEqual([worker['id'] for worker in mine['workers']], ['codexA'])
        self.assertNotIn('credential_ref', json.dumps(directory))
        self.get('workers?project_id=private', expected=403)
        self.get('projects/private', expected=403)
        private_conversation = self.server.relay.detail('bob', 'private')['conversation']['id']
        self.get('conversations/' + private_conversation + '/events', expected=403)
    def test_execution_result_route_requires_project_membership(self):
        private=self.server.relay.detail('bob','private')['conversation']['id']
        task=self.server.relay.mutate('bob','prompt',{'conversation_id':private,'prompt':PROMPT,'worker_id':'codexB'})
        self.get('tasks/'+task['id']+'/result',expected=403)
        self.login('bob')
        result=self.get('tasks/'+task['id']+'/result')
        self.assertEqual(result['task']['id'],task['id'])
        self.assertEqual(result['runs'][0]['execution_mode'],'simulation')
        self.assertNotIn('credential_ref',json.dumps(result))

    def test_enrollment_and_settings_require_owner_authorization_and_explicit_grants(self):
        worker = self.post('workers/enroll', {
            'display_name': 'Alex new worker', 'owner_type': 'user',
            'execution_mode': 'simulation', 'project_ids': [], 'opt_in': True,
        })
        self.assertEqual(worker['owner_id'], 'alice')
        self.assertFalse(worker['eligible'])
        detail = self.get('projects/demo')
        self.post('prompt', {'conversation_id': detail['conversation']['id'],
                             'prompt': PROMPT, 'worker_id': worker['id']}, expected=403)
        self.post('workers/configure', {'worker_id': worker['id'], 'project_ids': ['demo']})
        directory = self.get('workers?project_id=demo')
        enrolled = next(item for item in directory['workers'] if item['id'] == worker['id'])
        self.assertTrue(enrolled['eligible'])
        self.login('bob')
        self.post('workers/configure', {'worker_id': worker['id'], 'opt_in': False}, expected=403)
        self.post('workers/revoke', {'worker_id': worker['id']}, expected=403)
        self.login('alice')
        self.post('workers/revoke', {'worker_id': worker['id']})
        directory = self.get('workers?project_id=demo')
        enrolled = next(item for item in directory['workers'] if item['id'] == worker['id'])
        self.assertFalse(enrolled['opt_in'])
        self.assertFalse(enrolled['eligible'])

    def test_same_organization_does_not_reveal_unshared_workers(self):
        self.login('bob')
        worker = self.post('workers/enroll', {
            'display_name': 'Sam unshared worker', 'owner_type': 'user',
            'execution_mode': 'simulation', 'project_ids': [],
        })
        self.login('alice')
        directory = self.get('workers')
        self.assertNotIn(worker['id'], [item['id'] for item in directory['workers']])
        self.post('workers/configure', {'worker_id': worker['id'], 'project_ids': ['demo']}, expected=403)
        self.get('workers/' + worker['id'] + '/credentials', expected=404)

    def test_credentials_are_rejected_without_echo_or_persistence(self):
        for field in ('credential_ref', 'api_key', 'access_token', 'cookies', 'auth_dir'):
            payload = {
                'display_name': 'Rejected worker', 'owner_type': 'user',
                'execution_mode': 'simulation', field: 'do-not-echo-this-secret',
            }
            data = self.post('workers/enroll', payload, expected=400)
            self.assertNotIn('do-not-echo-this-secret', json.dumps(data))
        self.post('workers/enroll', {
            'display_name': 'Rejected nested credential', 'execution_mode': 'simulation',
            'config': {'refresh_token': 'do-not-store-this-secret'},
        }, expected=400)
        self.assertEqual(len(self.get('workers?mine=1')['workers']), 1)
        self.assertNotIn('credential_ref', json.dumps(self.get('overview')))
        self.assertNotIn('do-not-store-this-secret', str(self.server.relay.rows('SELECT * FROM audit_events')))

    def test_personal_subscription_enrolls_disabled_and_execution_fails_closed(self):
        worker = self.post('workers/enroll', {
            'display_name': 'Unsupported personal worker', 'owner_type': 'user',
            'execution_mode': 'personal_supported', 'project_ids': ['demo'],
        })
        self.assertEqual(worker['state'], 'unsupported')
        self.assertFalse(worker['eligible'])
        self.assertFalse(worker['health']['available'])
        self.assertNotIn('credential_ref', json.dumps(worker))
        conversation = self.get('projects/demo')['conversation']['id']
        failure = self.post('prompt', {'conversation_id': conversation, 'prompt': PROMPT,
                                      'worker_id': worker['id']}, expected=400)
        self.assertEqual(failure['code'], 'unsupported_execution_mode')
        self.assertIn('unsupported', failure['error'].lower())
        self.assertEqual(self.get('projects/demo')['tasks'], [])

    def test_organization_api_enrollment_is_admin_only_and_labels_shared_scope(self):
        self.post('workers/enroll', {
            'display_name': 'Unauthorized org worker', 'owner_type': 'org',
            'execution_mode': 'organization_api', 'project_ids': ['demo'],
        }, expected=403)
        self.login('admin')
        workers = [self.post('workers/enroll', {
            'display_name': 'Organization API ' + str(index), 'owner_type': 'org',
            'execution_mode': 'organization_api', 'project_ids': ['demo'],
        }) for index in range(2)]
        self.assertEqual(workers[0]['capacity']['quota_scope'], workers[1]['capacity']['quota_scope'])
        self.assertTrue(all(worker['execution_mode'] == 'organization_api' for worker in workers))
        self.assertTrue(all(worker['capacity']['source'] == 'manual' for worker in workers))
        self.assertTrue(all(worker['capacity']['value'] is None for worker in workers))
        self.assertNotIn('credential_ref', json.dumps(workers))

    def test_demo_button_preserves_conversation_history_checkpoint_and_files(self):
        before = self.get('projects/demo')
        conversation = before['conversation']['id']
        task = self.post('demo', {'project_id': 'demo', 'request_id': 'http-demo-once'})
        self.assertEqual(task['state'], 'completed')
        self.assertEqual(task['conversation_id'], conversation)
        after = self.get('projects/demo')
        self.assertEqual(after['conversation']['id'], conversation)
        self.assertIn('validator.py', after['workspace']['files'])
        self.assertIn('test_validator.py', after['workspace']['files'])
        self.assertIn('source_range', after['workspace']['files']['.relay/HANDOFF.md'])
        self.assertGreaterEqual(len(after['checkpoints']), 3)
        self.assertEqual([assignment['worker_id'] for assignment in after['assignments']], ['codexA', 'codexB'])
        self.assertEqual(after['handoffs'][0]['authorized_by'], 'alice')
        events = self.get('conversations/' + conversation + '/events?after=0&limit=100')['events']
        self.assertTrue(any(event['worker_id'] == 'codexA' and event['event_type'] == 'tool_output' for event in events))
        self.assertTrue(any(event['worker_id'] == 'codexB' and 'Ran 5 tests' in event['content'] for event in events))
        self.assertEqual(self.post('demo', {'project_id': 'demo', 'request_id': 'http-demo-once'})['id'], task['id'])
        self.assertEqual(len(self.get('projects/demo')['assignments']), 2)


if __name__ == '__main__':
    unittest.main()
