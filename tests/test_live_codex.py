"""Full scheduler → real adapter → faithful mock Codex JSONL subprocess.

Only container launch/health/cleanup are replaced. No inference is claimed: the
transport writes known fixture code and runs actual tests in a temporary project.
Production has no way to select this transport and never falls back to it.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from relay.store import Relay
from relay.workers import OrganizationApiCodexAdapter, get_adapter
from relay.codex_events import provider_error

PROMPT='Create normalize_tags(text): split on semicolons, trim spaces, lowercase, discard empties, deduplicate in order; add Python unittest coverage.'
PRELUDE='''
import json,os,sys,subprocess,time
from pathlib import Path
payload=sys.stdin.read()
Path(capture).write_text(payload)
os.chdir(workspace)
def event(value,ending='\\n'):
    print(json.dumps(value),end=ending,flush=True)
def command(name,body):
    event({'type':'item.started','item':{'id':name,'type':'command_execution','command':body[0]}})
    result=subprocess.run(body,capture_output=True,text=True)
    event({'type':'item.completed','item':{'id':name,'type':'command_execution','aggregated_output':result.stdout+result.stderr,'exit_code':result.returncode}})
    return result
'''
PHASE_A='''
event({'type':'thread.started','thread_id':'mock-native-A'})
event({'type':'item.completed','item':{'type':'reasoning','text':'hidden-reasoning-token=never-persist'}})
command('inspect',[sys.executable,'-c',"from pathlib import Path; print(Path('README.md').read_text())"])
event({'type':'item.started','item':{'id':'write-A','type':'command_execution','command':'write initial normalization function'}})
Path('tags.py').write_text("def normalize_tags(text):\\n    return [part.strip() for part in text.split(';') if part.strip()]\\n")
Path('writer-A.pid').write_text(str(os.getpid()))
event({'type':'item.completed','item':{'id':'write-A','type':'command_execution','aggregated_output':'phase1 file created','exit_code':0}})
print('fixture diagnostic fixture-proxy-capability',file=sys.stderr,flush=True)
if block: time.sleep(20)
progress={'summary':'Mock Codex transport completed phase one; Bearer fixture-proxy-capability','completed_work':['Created initial tags.py parser'], 'pending_steps':['Lowercase, deduplicate and add unittest coverage'], 'decisions':['Preserve first occurrence order'],'test_results':['Tests not yet added'],'errors':[],'limitations':[]}
event({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(progress)}})
event({'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':4}},ending='')
'''
PHASE_B='''
assert Path('.relay/HANDOFF.md').is_file()
assert Path('.relay/history/index.json').is_file()
assert 'normalize_tags' in Path('.relay/HANDOFF.md').read_text()
try: os.kill(int(Path('writer-A.pid').read_text()),0)
except ProcessLookupError: pass
else: raise RuntimeError('Writer A still running; concurrent writers forbidden')
event({'type':'thread.started','thread_id':'mock-native-B'})
event({'type':'item.started','item':{'id':'write-B','type':'command_execution','command':'finish normalization and add tests'}})
Path('tags.py').write_text("def normalize_tags(text):\\n    return list(dict.fromkeys(part.strip().lower() for part in text.split(';') if part.strip()))\\n")
Path('test_tags.py').write_text("import unittest\\nfrom tags import normalize_tags\\nclass TagsTests(unittest.TestCase):\\n    def test_tags(self): self.assertEqual(normalize_tags(' A ;b;A;; B '), ['a','b'])\\n    def test_empty(self): self.assertEqual(normalize_tags(';;'), [])\\n")
event({'type':'item.completed','item':{'id':'write-B','type':'command_execution','aggregated_output':'tags.py and test_tags.py written','exit_code':0}})
result=command('tests',[sys.executable,'-I','-c',"import sys,unittest;sys.path.insert(0,'.');r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('.',pattern='test_tags.py'));sys.exit(not r.wasSuccessful())"])
progress={'summary':'Mock Codex transport completed the remaining objective','completed_work':['Normalization and two tests implemented'], 'pending_steps':[], 'decisions':['Keep first occurrence order'],'test_results':[result.stdout+result.stderr],'errors':[],'limitations':[]}
event({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(progress)}})
event({'type':'turn.completed','usage':{'input_tokens':5,'output_tokens':6}})
'''

class FixtureAdapter(OrganizationApiCodexAdapter):
    def __init__(self,worker,root,script,block=False):
        super().__init__(worker); self.fixture_root=Path(root); self.script=script; self.block=block
    def health(self): return {'available':True,'connected':False,'reason':'MOCK TRANSPORT ONLY'}
    def recover(self,task_id): return True
    @staticmethod
    def _remove_container(name): return True
    def _command(self,workspace,config,name,task_id=None):
        prefix=f'workspace={str(workspace)!r}\ncapture={str(self.fixture_root/(self.worker_id+".prompt"))!r}\nblock={self.block!r}\n'
        return [sys.executable,'-u','-c',prefix+PRELUDE+self.script]

class LiveTransportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.adapters={}; self.b=None
        self.env=patch.dict(os.environ,{'RELAY_CODEX_PROXY_TOKEN':'fixture-proxy-capability'}); self.env.start()
        def factory(worker):
            if worker['execution_mode']!='organization_api': return get_adapter(worker)
            if worker['id'] not in self.adapters:
                self.adapters[worker['id']]=FixtureAdapter(worker,self.root,PHASE_B if worker['id']==self.b else PHASE_A)
            return self.adapters[worker['id']]
        self.factory_patch=patch('relay.store.get_adapter',side_effect=factory);self.factory_patch.start()
        self.relay=Relay(self.root/'data')
        self.conversation=self.relay.detail('alice','demo')['conversation']['id']
        self.a=self.enroll('Live A'); self.b=self.enroll('Live B')
        self.adapters[self.b].script=PHASE_B
    def tearDown(self):
        self.relay.close(); self.factory_patch.stop(); self.env.stop(); self.tmp.cleanup()
    def enroll(self,name):
        return self.relay.mutate('admin','workers/enroll',{'display_name':name,'owner_type':'org','execution_mode':'organization_api','project_ids':['demo']})['id']
    def start(self,pause=True):
        return self.relay.mutate('alice','prompt',{'conversation_id':self.conversation,'worker_id':self.a,'prompt':PROMPT,'pause_after_turn':pause})
    def wait(self,task,state):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            with self.relay.lock:
                current=self.relay.task('alice',task['id'])
                if current['state']==state and task['id'] not in self.relay.jobs:return current
            time.sleep(.02)
        self.fail(f'Task never reached {state}: {current}')
    def handoff(self,task):
        current=self.relay.task('alice',task['id'])
        self.relay.mutate('alice','switch',{'task_id':task['id'],'generation':current['generation'],'target_worker_id':self.b})
        return self.wait(task,'completed')
    def test_arbitrary_prompt_phase_boundary_real_edits_tests_and_restart(self):
        task=self.start(); pending=self.wait(task,'handoff_pending')
        self.assertEqual(pending['execution_phase'],1)
        first=self.relay.detail('alice','demo')
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases'))
        transmitted=(self.root/(self.a+'.prompt')).read_text()
        self.assertEqual(transmitted.split('Original user request (verbatim except secret redaction):\n')[1].split('\n\nCurated Relay context:')[0],PROMPT)
        self.assertIn('PHASE 1 / HANDOFF BOUNDARY',(self.root/(self.a+'.prompt')).read_text())
        self.assertIn('Lowercase',str(first['contexts'][0]['todos']))
        self.assertIn('tags.py',first['workspace']['task_diff'])
        completed=self.handoff(task); self.assertEqual(completed['conversation_id'],self.conversation)
        detail=self.relay.detail('alice','demo')
        self.assertIn('dict.fromkeys',detail['workspace']['files']['tags.py'])
        full=self.relay.events('alice',self.conversation,limit=100)['events']
        self.assertTrue(any('Ran 2 tests' in e['content'] and e['worker_id']==self.b for e in full))
        self.assertNotIn('hidden-reasoning',str(full)); self.assertNotIn('fixture-proxy-capability',str(full))
        self.assertTrue(any(e['event_type']=='runtime_output' and json.loads(e['metadata'])['channel']=='stderr' for e in full))
        for run in detail['execution_runs']:
            self.assertEqual(run['exit_code'],0);self.assertGreater(run['duration_ms'],0);self.assertEqual(run['execution_mode'],'organization_api')
        self.assertEqual({a['native_thread_id'] for a in detail['assignments']},{'mock-native-A','mock-native-B'})
        self.assertIn('Lowercase',(self.root/(self.b+'.prompt')).read_text())
        self.relay.close(); self.relay=Relay(self.root/'data')
        restored=self.relay.detail('alice','demo');self.assertEqual(restored['execution_runs'],detail['execution_runs'])
        self.assertEqual(self.relay.events('alice',self.conversation,limit=100)['events'],full)
        self.assertEqual(restored['workspace']['files']['tags.py'],detail['workspace']['files']['tags.py'])
    def test_switch_during_running_transport_waits_for_process_stop(self):
        self.adapters[self.a].block=True
        task=self.start(False); deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with self.relay.lock:
                if any(e['content']=='phase1 file created' for e in self.relay.events('alice',self.conversation,limit=100)['events']):break
            time.sleep(.02)
        else:self.fail('Worker A never wrote its actual phase one file')
        completed=self.handoff(task)
        self.assertEqual(completed['worker_id'],self.b)
        runs=self.relay.detail('alice','demo')['execution_runs']
        self.assertEqual(runs[0]['state'],'cancelled');self.assertEqual(runs[1]['state'],'completed')
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases'))
    def test_shared_429_overrides_pending_switch_and_never_executes_peer(self):
        self.adapters[self.a].script="event({'type':'thread.started','thread_id':'quota-thread'});event({'type':'turn.failed','error':{'code':'rate_limit_exceeded','status_code':429,'retry_after':30,'message':'HTTP 429 Too Many Requests'}});time.sleep(20)"
        task=self.start(False)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with self.relay.lock:
                if any(e['event_type']=='provider_error' for e in self.relay.events('alice',self.conversation,limit=100)['events']):break
            time.sleep(.02)
        else:self.fail('Expected the actual quota event before the switch')
        self.relay.mutate('alice','switch',{'task_id':task['id'],'generation':0,'target_worker_id':self.b})
        self.wait(task,'handoff_pending')
        self.assertFalse((self.root/(self.b+'.prompt')).exists())
        for worker in (self.a,self.b):
            cap=self.relay.one('SELECT * FROM capacity_sources WHERE worker_id=?',(worker,));self.assertEqual(cap['availability'],'quota_blocked');self.assertEqual(cap['limit_kind'],'rate_limit_exceeded')
        run=self.relay.detail('alice','demo')['execution_runs'][0]
        self.assertEqual(run['error_category'],'rate_limit_exceeded');self.assertEqual(run['retry_after_seconds'],30)
    def test_live_handoff_cannot_select_simulated_worker(self):
        task=self.start();self.wait(task,'handoff_pending')
        with self.assertRaises(ValueError):self.relay.mutate('alice','switch',{'task_id':task['id'],'generation':0,'target_worker_id':'codexB'})
    def test_live_submission_and_handoff_enforce_project_and_worker_approval(self):
        private=self.relay.detail('bob','private')['conversation']['id']
        with self.assertRaises(PermissionError):self.relay.mutate('alice','prompt',{'conversation_id':private,'worker_id':self.a,'prompt':PROMPT})
        with self.assertRaises(PermissionError):self.relay.mutate('bob','prompt',{'conversation_id':private,'worker_id':self.a,'prompt':PROMPT})
        self.assertFalse((self.root/(self.a+'.prompt')).exists())
        task=self.start();self.wait(task,'handoff_pending')
        self.relay.mutate('admin','workers/revoke',{'worker_id':self.b,'project_id':'demo'})
        with self.assertRaises(PermissionError):self.handoff(task)
        self.assertFalse((self.root/(self.b+'.prompt')).exists())
    def test_provider_failure_and_exit_status_cannot_report_success(self):
        self.adapters[self.a].script="event({'type':'turn.completed'});sys.exit(7)"
        task=self.start(False);self.wait(task,'failed')
        self.assertEqual(self.relay.detail('alice','demo')['execution_runs'][0]['exit_code'],7)
    def test_timeout_preserves_partial_work_and_records_retry_guidance(self):
        self.adapters[self.a].script="Path('partial.txt').write_text('keep');time.sleep(20)"
        with patch.object(self.adapters[self.a],'_configuration',return_value={'timeout':.15}):
            task=self.start(False);self.wait(task,'handoff_pending')
        detail=self.relay.detail('alice','demo')
        self.assertEqual(detail['workspace']['files']['partial.txt'],'keep')
        self.assertEqual(detail['execution_runs'][0]['error_category'],'timeout')
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases'))
    def test_timeout_also_bounds_stdin_when_runtime_never_reads_prompt(self):
        adapter=self.adapters[self.a];emitted=[]
        with patch.object(adapter,'_configuration',return_value={'timeout':.15}),patch.object(adapter,'_command',return_value=[sys.executable,'-c','import time;time.sleep(20)']):
            task={'id':'blocked-stdin','objective':'a'*8000,'conversation_id':self.conversation}
            result=adapter.start(task,self.relay.ws.path('demo'),{'large':'b'*30000},lambda *e:emitted.append(e),__import__('threading').Event())
        self.assertEqual(result.error_category,'timeout');self.assertLess(result.duration_ms,3000)
        self.assertTrue(result.safe_to_release)

class ProviderErrorTests(unittest.TestCase):
    def test_quoted_auth_diagnostics_are_redacted_without_breaking_json(self):
        from relay.security import clean
        value=clean('{"access_token":"not-a-real-private-token","id_token":"private-id","message":"connection failed"}')
        self.assertNotIn('not-a-real-private-token',value)
        self.assertEqual(json.loads(value)['id_token'],'[REDACTED]')
        self.assertEqual(json.loads(value)['message'],'connection failed')
    def test_explicit_status_and_categories(self):
        for message,category in [('unexpected status 429 Too Many Requests','rate_limit_exceeded'),('HTTP 503 unavailable','provider_unavailable'),('network disconnected','transport_error')]:
            self.assertEqual(provider_error({'message':message})['category'],category)
        self.assertEqual(provider_error({'error':{'code':'insufficient_quota'}})['category'],'insufficient_quota')
        self.assertIsNone(provider_error({'message':'counter 429 items'})['http_status'])

if __name__=='__main__':unittest.main()
