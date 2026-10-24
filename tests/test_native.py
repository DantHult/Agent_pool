"""Native adapter integration through a test-only JSON-RPC subprocess.

Authentic CLI initialization/isolation is tested separately; this fixture does
not claim ChatGPT authentication, model inference, or real account handoffs.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from relay.app_server import AppServerClient
from relay.native import initialize_profiles,close_native_sessions,profile_paths,native_readiness
from relay.store import Relay

PROMPT='Implement a normalize(text) Python function with lowercase collapsed whitespace and unittest coverage.'
FIXTURE=Path(__file__).parent/'fixtures/native_app_server.py'

class MockNativeClient(AppServerClient):
    def __init__(self,command,env,cwd):super().__init__([sys.executable,'-u',str(FIXTURE.resolve())],env,cwd)

class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'RELAY_ENABLE_NATIVE':'1','RELAY_NATIVE_ROOT':str(self.root/'native'),'RELAY_NATIVE_OWNER':'alice','RELAY_ORG_OPENAI_API_KEY':'private-org-must-not-forward','OPENAI_API_KEY':'private-api-must-not-forward','CODEX_API_KEY':'private-codex-must-not-forward'})
        self.env.start();self.platform=patch('relay.native.platform.system',return_value='Darwin');self.platform.start()
        self.cli=patch('relay.native.shutil.which',return_value='/mock/codex');self.cli.start()
        self.transport=patch('relay.native.AppServerClient',MockNativeClient);self.transport.start()
        initialize_profiles();self.relay=Relay(self.root/'data');self.conversation=self.relay.detail('alice','demo')['conversation']['id'];self.workers=[]
        for profile in ('a','b','c'):
            w=self.relay.mutate('alice','workers/enroll',{'display_name':'Native '+profile,'owner_type':'user','execution_mode':'native','native_profile':profile,'project_ids':['demo']});self.workers.append(w['id'])
    def tearDown(self):
        self.relay.close();close_native_sessions();self.transport.stop();self.cli.stop();self.platform.stop();self.env.stop();self.tmp.cleanup()
    def wait(self,task,state):
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            with self.relay.lock:
                current=self.relay.task('alice',task['id'])
                if current['state']==state and task['id'] not in self.relay.jobs:return current
            time.sleep(.02)
        self.fail(str(current))
    def start(self):return self.relay.mutate('alice','prompt',{'conversation_id':self.conversation,'worker_id':self.workers[0],'execution_mode':'native','prompt':PROMPT,'pause_after_turn':True,'native_phases':3})
    def test_native_three_process_phase_handoff_same_conversation_and_restart(self):
        task=self.start();first=self.wait(task,'handoff_pending')
        phases=[first]
        for index in (1,2):
            previous=phases[-1]
            self.relay.mutate('alice','switch',{'task_id':task['id'],'generation':previous['generation'],'target_worker_id':self.workers[index]})
            phases.append(self.wait(task,'handoff_pending' if index==1 else 'completed'))
        detail=self.relay.detail('alice','demo')
        self.assertEqual({t['conversation_id'] for t in phases},{self.conversation})
        self.assertEqual(len(detail['handoffs']),2);self.assertEqual(len(detail['execution_runs']),3)
        self.assertEqual(len({r['native_thread_id'] for r in detail['execution_runs']}),3)
        self.assertIn('native_tools.py',detail['workspace']['files'])
        full=self.relay.events('alice',self.conversation,limit=100)['events']
        self.assertTrue(any('Ran 2 tests' in e['content'] and e['worker_id']==self.workers[2] for e in full))
        self.assertNotIn('private-reasoning',str(full));self.assertNotIn('fixture.invalid',str(full))
        workspaces=[]
        for profile in ('a','b','c'):
            home,_=profile_paths(profile);record=json.loads((home/'received.json').read_text())
            self.assertEqual(record['keys_present'],[]);self.assertEqual(record['permissions'],'relay-native');self.assertIn(PROMPT,record['text']);workspaces.append(record['cwd'])
        self.assertEqual(len(set(workspaces)),3);self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases'))
        self.relay.close();self.relay=Relay(self.root/'data');restored=self.relay.detail('alice','demo')
        self.assertEqual(detail['execution_runs'],restored['execution_runs']);self.assertEqual(full,self.relay.events('alice',self.conversation,limit=100)['events'])
    def test_other_human_cannot_spend_native_subscription_or_trigger_handoff(self):
        with self.assertRaises(PermissionError):self.relay.mutate('bob','prompt',{'conversation_id':self.conversation,'worker_id':self.workers[0],'execution_mode':'native','prompt':PROMPT})
        task=self.start();first=self.wait(task,'handoff_pending')
        with self.assertRaises(PermissionError):self.relay.mutate('bob','switch',{'task_id':task['id'],'generation':first['generation'],'target_worker_id':self.workers[1]})
    def test_native_usage_limit_does_not_rotate_to_other_account(self):
        close_native_sessions();home,_=profile_paths('a');(home/'scenario.json').write_text('{"quota":true}')
        task=self.start();pending=self.wait(task,'handoff_pending')
        self.assertEqual(len(self.relay.detail('alice','demo')['assignments']),1)
        with self.assertRaisesRegex(ValueError,'quota'):self.relay.mutate('alice','switch',{'task_id':task['id'],'generation':pending['generation'],'target_worker_id':self.workers[1]})
    def test_interrupt_preserves_partial_files_before_new_profile_runs(self):
        close_native_sessions();home,_=profile_paths('a');(home/'scenario.json').write_text('{"delay":5}')
        task=self.start();deadline=time.monotonic()+30
        while time.monotonic()<deadline and not (home/'received.json').exists():time.sleep(.02)
        self.assertTrue((home/'received.json').exists())
        current=self.relay.task('alice',task['id'])
        self.relay.mutate('alice','switch',{'task_id':task['id'],'generation':current['generation'],'target_worker_id':self.workers[1]})
        self.wait(task,'handoff_pending');detail=self.relay.detail('alice','demo')
        self.assertEqual(len(detail['execution_runs']),2)
        first=next(run for run in detail['execution_runs'] if run['worker_id']==self.workers[0])
        self.assertEqual(first['state'],'cancelled')
        self.assertIn('native_tools.py',detail['workspace']['files'])
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases'))
    def test_conflicting_canonical_checkpoint_refuses_native_publish(self):
        from relay.native import NativeCodexAdapter
        worker=self.relay.one('SELECT * FROM workers WHERE id=?',(self.workers[0],))
        adapter=NativeCodexAdapter(worker);canonical=self.relay.ws.path('demo')
        private,expected=adapter._prepare(canonical)
        (private/'private_edit.py').write_text('value = 1\n')
        self.relay.ws.checkpoint('demo','Concurrent change outside native adapter')
        with self.assertRaisesRegex(ValueError,'Conflicting canonical'):adapter._publish(canonical,private,expected)
        self.assertFalse((canonical/'private_edit.py').exists())
    def test_duplicate_native_profile_cannot_be_enrolled(self):
        with self.assertRaisesRegex(ValueError,'already enrolled'):
            self.relay.mutate('alice','workers/enroll',{'display_name':'Duplicate A','execution_mode':'native','native_profile':'a'})
    def test_mocked_native_smoke_orchestrates_three_rpc_peers_and_checks_proof(self):
        from scripts.live_smoke import run_native
        output=io.StringIO();data=self.root/'mocked-smoke-only'
        with contextlib.redirect_stdout(output):run_native(data,PROMPT)
        report=json.loads((data/'native-smoke-report.json').read_text())
        self.assertTrue(report['live_verified'])  # Test-only transport substitution above.
        self.assertEqual(report['backend'],'native');self.assertEqual(len(report['handoffs']),2)
        self.assertEqual(len(report['execution_runs']),3)
    def test_native_transport_refuses_escalation_and_external_token_requests(self):
        close_native_sessions();home,_=profile_paths('a');(home/'scenario.json').write_text('{"approval":true}')
        task=self.start();self.wait(task,'handoff_pending')
        replies=[json.loads(line) for line in (home/'approval-replies.jsonl').read_text().splitlines()]
        by_id={reply['id']:reply for reply in replies}
        self.assertEqual(by_id[900]['result']['decision'],'decline')
        self.assertEqual(by_id[901]['error']['code'],-32601)
    def test_native_cli_symlink_is_canonicalized_for_sandbox_helpers(self):
        from relay.app_server import server_command
        binary=self.root/'codex-real';binary.write_text('test binary path only')
        alias=self.root/'codex-alias'
        try:alias.symlink_to(binary)
        except OSError as exc:self.skipTest(f'Symlink creation requires privileges or Developer Mode on Windows: {exc}')
        command=server_command(alias)
        self.assertEqual(command[0],str(binary.resolve()))
        profile=next(value for value in command if value.startswith('permissions.relay-native.filesystem='))
        self.assertIn(json.dumps(str(binary.resolve()))+'="read"',profile)
    def test_native_preflight_does_not_require_api_docker_or_model(self):
        with patch.dict(os.environ,{'RELAY_ORG_OPENAI_API_KEY':'','RELAY_CODEX_MODEL':'','RELAY_ENABLE_ORG_API':'0'}):
            checks,details=native_readiness()
        self.assertTrue(all(ok for _,ok in checks),details)
        self.assertFalse(any('Docker' in label or 'API key' in label for label,_ in checks))
    def test_windows_host_is_eligible_for_native_preflight(self):
        with patch('relay.native.platform.system', return_value='Windows'):
            checks, details = native_readiness(('a',))
        self.assertEqual(checks[0], ('Native macOS or Windows execution host', True))
        self.assertTrue(all(ok for _, ok in checks), details)
    def test_other_hosts_cannot_start_native_profiles(self):
        with patch('relay.native.platform.system', return_value='Linux'):
            checks, details = native_readiness()
        self.assertEqual(checks[0][1], False)
        self.assertFalse(any(label.startswith('Profile ') for label, _ in checks))
        self.assertEqual(details, [])
    def test_native_rejects_api_authenticated_profile(self):
        close_native_sessions();home,_=profile_paths('a');(home/'scenario.json').write_text('{"auth":"apiKey"}')
        checks,details=native_readiness();self.assertFalse(all(ok for _,ok in checks));self.assertIn('ChatGPT login',' '.join(details))

if __name__=='__main__':unittest.main()
