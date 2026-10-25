"""Pinned local-owner HTTP flow using the existing native stdio fixture.

No Codex login or inference occurs here. The separate opt-in browser smoke uses
actual native profiles and records its own genuine-execution evidence.
"""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from relay.accounts import InviteIdentityProvider,digest
from relay.native import initialize_profiles,profile_paths,close_native_sessions
from relay.server import make_server
from relay.store import Relay
try:
    from test_native import MockNativeClient
    from test_shared_server import Client,PASSWORD
except ImportError:
    from tests.test_native import MockNativeClient
    from tests.test_shared_server import Client,PASSWORD

OBJECTIVE='Implement normalize(text) with lowercase, collapsed whitespace and Python unittest coverage.'

class LocalNativeDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.server=None
        self.env=patch.dict(os.environ,{'RELAY_ENABLE_NATIVE':'1','RELAY_NATIVE_ROOT':str(self.root/'profiles-root'),'RELAY_PUBLIC_URL':'','RELAY_ORG_OPENAI_API_KEY':'TEST-ONLY-ORG-SECRET-MUST-NOT-LEAK'})
        self.env.start();self.platform=patch('relay.native.platform.system',return_value='Darwin');self.platform.start()
        self.cli=patch('relay.native.shutil.which',return_value='/mock/codex');self.cli.start()
        self.transport=patch('relay.native.AppServerClient',MockNativeClient);self.transport.start()
        initialize_profiles();home,_=profile_paths('b');(home/'scenario.json').write_text('{"complete":true}')
        relay=Relay(self.root/'data',demo=False)
        self.owner=InviteIdentityProvider(relay).bootstrap('owner@relay.invalid','Pinned Owner',PASSWORD,local_owner=True)['id'];relay.close()
        self.start();self.client=Client(self.server);self.client.login('owner@relay.invalid')
        self.project=self.client.post('projects',{'name':'Disposable native sample'})['id']
        self.conversation=self.client.get('projects/'+self.project)['conversation']['id']
    def start(self):
        self.server=make_server(self.root/'data',0,'local-owner')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def stop(self):
        if self.server:self.server.shutdown();self.server.server_close();self.thread.join(2);self.server=None
    def tearDown(self):
        self.stop();close_native_sessions();self.transport.stop();self.cli.stop();self.platform.stop();self.env.stop();self.tmp.cleanup()
    def enroll(self,profile):return self.client.post('native/profiles/enroll',{'profile':profile,'project_id':self.project})['id']
    def submit(self,worker):return self.client.post('prompt',{'conversation_id':self.conversation,'worker_id':worker,'execution_mode':'native','prompt':OBJECTIVE,'pause_after_turn':True})
    def wait(self,task,state):
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            with self.server.relay.lock:
                current=self.server.relay.task(self.owner,task['id'])
                if current['state']==state and task['id'] not in self.server.relay.jobs:return current
            time.sleep(.02)
        self.fail('Native task did not reach '+state+': '+current['state'])
    def test_owner_http_native_handoff_persists_actual_fixture_state_and_isolated_inputs(self):
        profiles=self.client.get('native/profiles?project_id='+self.project)['profiles']
        self.assertEqual({p['status'] for p in profiles},{'available'})
        a=self.enroll('a');b=self.enroll('b');task=self.submit(a);first=self.wait(task,'handoff_pending')
        self.client.post('switch',{'task_id':task['id'],'generation':first['generation'],'target_worker_id':b})
        done=self.wait(task,'completed');self.assertEqual(done['conversation_id'],self.conversation)
        detail=self.client.get('projects/'+self.project);events=self.client.get('conversations/'+self.conversation+'/events?limit=100')['events']
        self.assertEqual(len(detail['execution_runs']),2);self.assertEqual(len(detail['handoffs']),1)
        self.assertEqual(len({r['native_thread_id'] for r in detail['execution_runs']}),2)
        self.assertIn('native_tools.py',detail['workspace']['files']);self.assertIn('Ran 2 tests',str(events))
        for profile in ('a','b'):
            home,_=profile_paths(profile);received=json.loads((home/'received.json').read_text())
            self.assertIn(OBJECTIVE,received['text']);self.assertEqual(received['keys_present'],[])
            self.assertIn('/workspaces/'+profile+'/',received['cwd'].replace('\\','/'));self.assertEqual(received['permissions'],'relay-native')
        old_cookie=self.client.cookie;self.stop();self.start();self.client.server=self.server
        self.assertEqual(self.client.cookie,old_cookie)
        restored=self.client.get('projects/'+self.project)
        self.assertEqual(restored['execution_runs'],detail['execution_runs']);self.assertEqual(restored['checkpoints'],detail['checkpoints'])
        self.assertEqual(self.client.get('conversations/'+self.conversation+'/events?limit=100')['events'],events)
    def test_nonowner_identity_session_csrf_and_forwarded_requests_are_rejected(self):
        a=self.enroll('a');b=self.enroll('b');task=self.submit(a);pending=self.wait(task,'handoff_pending')
        anonymous=Client(self.server)
        self.assertEqual(anonymous.request('/api/native/profiles')[0],401)
        self.assertEqual(self.client.request('/api/native/profiles',headers={'X-Forwarded-For':'127.0.0.1'})[0],403)
        self.assertEqual(self.client.request('/api/native/profiles',headers={'Forwarded':'for=127.0.0.1'})[0],403)
        self.assertEqual(self.client.request('/api/native/profiles',headers={'Host':'public.example.com'})[0],403)
        self.assertEqual(self.client.request('/api/native/profiles/enroll',{'profile':'a','project_id':self.project},{'X-Relay-CSRF':''})[0],403)
        self.assertEqual(anonymous.request('/api/login',{'user_id':self.owner})[0],400)
        # Even a valid separately issued account/session cannot become the owner.
        provider=InviteIdentityProvider(self.server.relay)
        with self.server.relay.lock:
            other=provider.create_account('other@relay.invalid','Other',provider._dummy,'admin')
            self.server.relay.db.execute('INSERT INTO memberships VALUES(?,?,?)',(other,self.project,'admin'))
            now=time.time();self.server.relay.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(digest('forged-other-session'),other,now,now+600,now))
        anonymous.cookie=self.server.cookie_name+'=forged-other-session'
        self.assertEqual(anonymous.request('/api/native/profiles')[0],401)
        self.assertEqual(anonymous.request('/api/native/profiles/enroll',{'profile':'b','project_id':self.project})[0],401)
        self.assertEqual(anonymous.request('/api/switch',{'task_id':task['id'],'generation':pending['generation'],'target_worker_id':b})[0],401)
        self.assertEqual(len(self.client.get('projects/'+self.project)['execution_runs']),1)
        with self.assertRaises(PermissionError):self.server.relay.enroll_native_profile(other,'b',self.project)
    def test_no_registration_invitation_or_native_profile_disclosure(self):
        for path,body in (('auth/register',{'email':'other@relay.invalid','name':'Other','password':PASSWORD,'invitation_token':'none'}),('invitations',{'email':'other@relay.invalid'}),('membership',{'project_id':self.project,'user_id':self.owner,'role':'member'})):
            self.assertEqual(self.client.request('/api/'+path,body)[0],403)
        a=self.enroll('a');task=self.submit(a);self.wait(task,'handoff_pending')
        detail=self.client.get('projects/'+self.project);history=self.client.get('conversations/'+self.conversation+'/events?limit=100')
        for value in (self.client.get('native/profiles'),detail,history,self.client.get('tasks/'+task['id']+'/result')):
            serialized=json.dumps(value)
            for sensitive in ('TEST-ONLY-ORG-SECRET-MUST-NOT-LEAK',PASSWORD,'private-reasoning','fixture.invalid','password_hash',str(profile_paths('a')[0])):self.assertNotIn(sensitive,serialized)
        download=self.client.request('/api/projects/'+self.project+'/download?file=native_tools.py')
        self.assertEqual(download[0],200);self.assertNotIn('SECRET',download[1])
        canonical=self.server.relay.ws.path(self.project)
        tracked=self.server.relay.ws.git(canonical,'ls-files')
        self.assertNotIn('auth.json',tracked);self.assertNotIn('profiles/',tracked)
    def test_pause_waits_for_native_interruption_then_explicit_successor(self):
        close_native_sessions();home,_=profile_paths('a');(home/'scenario.json').write_text('{"delay":5}')
        a=self.enroll('a');b=self.enroll('b');task=self.submit(a)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline and not (home/'received.json').exists():time.sleep(.02)
        paused=self.client.post('pause',{'task_id':task['id'],'generation':task['generation']})
        self.assertIn(paused['state'],('pausing','handoff_pending'))
        pending=self.wait(task,'handoff_pending')
        self.assertEqual(len(self.client.get('projects/'+self.project)['execution_runs']),1)
        self.assertFalse(self.server.relay.rows('SELECT * FROM workspace_leases'))
        self.client.post('switch',{'task_id':task['id'],'generation':pending['generation'],'target_worker_id':b})
        self.wait(task,'completed')
        runs=self.client.get('projects/'+self.project)['execution_runs'];self.assertEqual(runs[0]['state'],'cancelled')
    def test_quota_never_rotates_and_failure_preserves_pending_work(self):
        close_native_sessions();home,_=profile_paths('a');(home/'scenario.json').write_text('{"quota":true}')
        a=self.enroll('a');b=self.enroll('b');task=self.submit(a);pending=self.wait(task,'handoff_pending')
        states={p['profile']:p['status'] for p in self.client.get('native/profiles')['profiles']}
        self.assertEqual(states['a'],'quota_blocked')
        self.assertEqual(self.client.request('/api/switch',{'task_id':task['id'],'generation':pending['generation'],'target_worker_id':b})[0],400)
        self.assertEqual(len(self.client.get('projects/'+self.project)['execution_runs']),1)
    def test_cli_unavailable_is_actionable_without_simulated_fallback(self):
        with patch('relay.native.shutil.which',return_value=None):
            statuses=self.client.get('native/profiles')['profiles']
            self.assertEqual({p['status'] for p in statuses},{'unavailable'})
            self.assertIn('CLI',statuses[0]['reason'])
            self.assertEqual(self.client.request('/api/native/profiles/enroll',{'profile':'a','project_id':self.project})[0],400)
    def test_missing_authentication_and_restart_recovery_remain_owner_controlled(self):
        close_native_sessions();home,_=profile_paths('a');(home/'scenario.json').write_text('{"auth":"apiKey"}')
        statuses={p['profile']:p for p in self.client.get('native/profiles')['profiles']}
        self.assertEqual(statuses['a']['status'],'unauthenticated')
        self.assertIn('ChatGPT login',statuses['a']['reason'])
    def test_cli_crash_preserves_private_changes_without_unverified_publish(self):
        close_native_sessions();home,workspaces=profile_paths('a');(home/'scenario.json').write_text('{"crash":true}')
        a=self.enroll('a');task=self.submit(a);pending=self.wait(task,'handoff_pending')
        self.assertTrue(pending['last_error'])
        detail=self.client.get('projects/'+self.project)
        self.assertEqual(detail['execution_runs'][0]['state'],'failed')
        self.assertNotIn('native_tools.py',detail['workspace']['files'])
        self.assertTrue(any(path.name=='native_tools.py' for path in workspaces.rglob('native_tools.py')))
        self.assertFalse(self.server.relay.rows('SELECT * FROM workspace_leases'))
    def test_restart_recovers_native_running_record_without_replaying_tools(self):
        a=self.enroll('a');task=self.submit(a);self.wait(task,'handoff_pending')
        home,_=profile_paths('a');before=(home/'received.json').stat().st_mtime_ns
        with self.server.relay.lock:
            self.server.relay.db.execute("UPDATE tasks SET state='running' WHERE id=?",(task['id'],))
            self.server.relay.db.execute("UPDATE execution_runs SET state='running' WHERE task_id=?",(task['id'],))
            self.server.relay.db.execute('INSERT INTO workspace_leases VALUES(?,?,?,?,?)',(self.project,task['id'],a,'test-lease',str(time.time())))
        self.stop();self.start();self.client.server=self.server
        detail=self.client.get('projects/'+self.project)
        self.assertEqual(detail['tasks'][0]['state'],'handoff_pending')
        self.assertEqual(detail['execution_runs'][0]['state'],'interrupted')
        self.assertEqual((home/'received.json').stat().st_mtime_ns,before)
        self.assertIn('native_tools.py',detail['workspace']['files'])
        self.assertFalse(self.server.relay.rows('SELECT * FROM workspace_leases'))
    def test_busy_profile_and_conversation_reject_parallel_writers(self):
        close_native_sessions();home,_=profile_paths('a');(home/'scenario.json').write_text('{"delay":5}')
        a=self.enroll('a');task=self.submit(a);deadline=time.monotonic()+30
        while time.monotonic()<deadline and not (home/'received.json').exists():time.sleep(.02)
        states={p['profile']:p['status'] for p in self.client.get('native/profiles')['profiles']}
        self.assertEqual(states['a'],'busy')
        self.assertEqual(self.client.request('/api/prompt',{'conversation_id':self.conversation,'worker_id':a,'prompt':OBJECTIVE})[0],400)
        self.assertEqual(len(self.server.relay.rows('SELECT * FROM workspace_leases')),1)
        self.client.post('cancel',{'task_id':task['id'],'generation':task['generation']});self.wait(task,'cancelled')
        self.assertFalse(self.server.relay.rows('SELECT * FROM workspace_leases'))
    def test_demo_shared_public_and_os_owner_mismatch_fail_closed(self):
        for origin in ('https://relay.example.com','http://192.168.1.10:8000'):
            with self.assertRaises(ValueError):make_server(self.root/'bad-origin',8000,'local-owner',origin)
        self.stop()
        with patch('relay.server.process_uid',return_value=(os.getuid()+1 if hasattr(os,'getuid') else 1)):
            with self.assertRaises(ValueError):make_server(self.root/'data',0,'local-owner')
        self.start()
        for mode in ('demo','invite'):
            server=make_server(self.root/mode,0,mode,'http://127.0.0.1:0' if mode=='invite' else None,True)
            try:
                self.assertFalse(server.relay.native_enabled)
                user='alice' if mode=='demo' else InviteIdentityProvider(server.relay).bootstrap(mode+'@relay.invalid',mode,PASSWORD)['id']
                project='demo' if mode=='demo' else server.relay.create_project(user,'Shared')['id']
                with self.assertRaises(ValueError):server.relay.mutate(user,'workers/enroll',{'display_name':'Forbidden','owner_type':'user','execution_mode':'native','native_profile':'a','project_ids':[project]})
                # Older data can contain native IDs. Direct HTTP must reject
                # those as well as new enrollment, regardless of the enable env.
                for worker,profile in (('existingNativeA','a'),('existingNativeB','b')):
                    server.relay._insert_worker(worker,worker,'user',user,'native',True,1,None)
                    server.relay.db.execute('UPDATE workers SET native_profile=? WHERE id=?',(profile,worker))
                    server.relay._grant(user,worker,project)
                http_thread=threading.Thread(target=server.serve_forever,daemon=True);http_thread.start()
                client=Client(server)
                if mode=='demo':client.post('login',{'user_id':user})
                else:client.login(mode+'@relay.invalid')
                conversation=server.relay.one('SELECT id FROM conversations WHERE project_id=?',(project,))['id']
                self.assertEqual(client.request('/api/native/profiles')[0],403)
                self.assertEqual(client.request('/api/prompt',{'conversation_id':conversation,'worker_id':'existingNativeA','execution_mode':'native','prompt':OBJECTIVE})[0],403)
                with server.relay.lock:
                    server.relay.db.execute('INSERT INTO tasks(id,conversation_id,state,worker_id,objective,generation,requested_by,execution_mode) VALUES(?,?,?,?,?,0,?,?)',('old-native-task',conversation,'handoff_pending','existingNativeA',OBJECTIVE,user,'native'))
                self.assertEqual(client.request('/api/switch',{'task_id':'old-native-task','generation':0,'target_worker_id':'existingNativeB'})[0],403)
                self.assertFalse(server.relay.rows('SELECT * FROM execution_runs'))
                server.shutdown();http_thread.join(2)
            finally:server.server_close()

if __name__=='__main__':unittest.main()
