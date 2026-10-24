"""Real HTTP, independent cookie jars and persistence; no provider requests."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from urllib.parse import urlparse
from unittest.mock import patch

from relay.server import make_server,public_origin
from relay.accounts import hash_password,verify_password,csrf_token
from relay.workers import ExecutionResult

PASSWORD='Test-only passphrase 2026!'
PROMPT='Add a password-strength validator and tests to this sample project.'

class Client:
    def __init__(self,server):self.server=server;self.cookie='';self.csrf=''
    def request(self,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        h={'Content-Type':'application/json','Origin':self.server.public_url}
        if self.cookie:h['Cookie']=self.cookie
        if self.csrf:h['X-Relay-CSRF']=self.csrf
        h.update(headers or {})
        connection.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,h)
        response=connection.getresponse();content=response.read();status=response.status
        cookie=response.getheader('Set-Cookie')
        if cookie:self.cookie=cookie.split(';')[0]
        kind=response.getheader('Content-Type','');connection.close()
        result=json.loads(content) if 'json' in kind else content.decode()
        if isinstance(result,dict) and result.get('csrf_token'):self.csrf=result['csrf_token']
        return status,result,cookie
    def post(self,path,data):
        status,result,_=self.request('/api/'+path,data)
        if status not in (200,201):raise AssertionError(str(status)+' '+str(result))
        return result
    def get(self,path):
        status,result,_=self.request('/api/'+path)
        if status!=200:raise AssertionError(str(status)+' '+str(result))
        return result
    def login(self,email):self.post('login',{'email':email,'password':PASSWORD})
    def stream(self,conversation):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        connection.request('GET','/api/conversations/'+conversation+'/stream',headers={'Cookie':self.cookie})
        response=connection.getresponse()
        return connection,response

class SharedServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.start()
        self.admin_id=self.server.identity_provider.bootstrap('admin@relay.invalid','Organization Admin',PASSWORD)['id']
        self.admin=Client(self.server);self.admin.login('admin@relay.invalid')
        self.project=self.admin.post('projects',{'name':'Shared sample'})['id']
        self.conversation=self.admin.get('projects/'+self.project)['conversation']['id']
    def start(self):
        self.server=make_server(self.tmp.name,0,'invite','http://127.0.0.1:0',True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def stop(self):self.server.shutdown();self.server.server_close();self.thread.join(2)
    def tearDown(self):self.stop();self.tmp.cleanup()
    def member(self,name,project=True,role='member',org_role='member'):
        email=name+'@relay.invalid';data={'email':email,'project_role':role,'org_role':org_role}
        if project:data['project_id']=self.project
        invite=self.admin.post('invitations',data);token=urlparse(invite['invitation_url']).fragment.removeprefix('invite=')
        client=Client(self.server);result=client.post('auth/register',{'email':email,'name':name,'password':PASSWORD,'invitation_token':token});client.login(email)
        return client,result['id'],token
    def worker(self,label='A',mode='simulation'):
        return self.admin.post('workers/enroll',{'display_name':label,'owner_type':'org','execution_mode':mode,'project_ids':[self.project]})['id']
    def submit(self,client,worker,**extra):return client.post('prompt',{'conversation_id':self.conversation,'worker_id':worker,'prompt':PROMPT,**extra})
    def test_invite_login_logout_expiration_and_session_restart(self):
        alex,user,_=self.member('alex');cookie=alex.cookie;csrf=alex.csrf
        self.assertNotIn('password_hash',str(alex.get('overview')));self.assertNotIn(PASSWORD,str(alex.get('overview')))
        self.stop();self.start();alex.server=self.server
        self.assertEqual(alex.get('auth/session')['user']['id'],user);self.assertEqual(alex.csrf,csrf)
        self.assertEqual(alex.cookie,cookie)
        alex.post('logout',{});self.assertEqual(alex.request('/api/overview')[0],401)
        alex.login('alex@relay.invalid')
        with self.server.relay.lock:self.server.relay.db.execute('UPDATE sessions SET expires_at=0 WHERE user_id=?',(user,))
        self.assertEqual(alex.request('/api/overview')[0],401)
    def test_invitation_email_binding_expiration_reuse_and_revocation(self):
        invite=self.admin.post('invitations',{'email':'new@relay.invalid','project_id':self.project})
        token=urlparse(invite['invitation_url']).fragment.removeprefix('invite=');client=Client(self.server)
        data={'email':'wrong@relay.invalid','name':'New','password':PASSWORD,'invitation_token':token}
        self.assertEqual(client.request('/api/auth/register',data)[0],400)
        data['email']='new@relay.invalid';client.post('auth/register',data)
        self.assertEqual(client.request('/api/auth/register',data)[0],400)
        second=self.admin.post('invitations',{'email':'expired@relay.invalid'})
        with self.server.relay.lock:self.server.relay.db.execute('UPDATE invitations SET expires_at=0 WHERE id=?',(second['id'],))
        data.update(email='expired@relay.invalid',invitation_token=urlparse(second['invitation_url']).fragment.removeprefix('invite='))
        self.assertEqual(client.request('/api/auth/register',data)[0],400)
        third=self.admin.post('invitations',{'email':'revoked@relay.invalid'});self.admin.post('invitations/revoke',{'id':third['id']})
        data.update(email='revoked@relay.invalid',invitation_token=urlparse(third['invitation_url']).fragment.removeprefix('invite='))
        self.assertEqual(client.request('/api/auth/register',data)[0],400)
    def test_csrf_origin_login_spoofing_and_rate_limit(self):
        self.assertEqual(self.admin.request('/api/projects',{'name':'CSRF'},{'X-Relay-CSRF':''})[0],403)
        self.assertEqual(self.admin.request('/api/projects',{'name':'CSRF'},{'Origin':'https://evil.invalid'})[0],403)
        self.assertEqual(self.admin.request('/api/login',{'user_id':'alice'})[0],400)
        anonymous=Client(self.server)
        with patch('relay.accounts.verify_password',return_value=False):
            statuses=[anonymous.request('/api/login',{'email':'nobody@relay.invalid','password':PASSWORD})[0] for _ in range(11)]
        self.assertEqual(statuses[:10],[401]*10);self.assertEqual(statuses[-1],429)
    def test_viewer_cannot_mutate_and_fourth_account_cannot_read_any_project_surface(self):
        viewer,_,_=self.member('viewer',role='viewer');outsider,_,_=self.member('outsider',project=False)
        worker=self.worker();task=self.submit(self.admin,worker)
        self.server.relay.event(task,'tool_output','secret-project-evidence '*2000)
        artifact=self.server.relay.rows("SELECT json_extract(metadata,'$.artifact_id') AS id FROM events WHERE json_extract(metadata,'$.artifact_id') IS NOT NULL")[0]['id']
        self.assertEqual(viewer.request('/api/prompt',{'conversation_id':self.conversation,'prompt':PROMPT,'worker_id':worker})[0],403)
        self.assertEqual(viewer.request('/api/pause',{'task_id':task['id'],'generation':task['generation']})[0],403)
        self.assertEqual(viewer.request('/api/workers/enroll',{'display_name':'Read-only project grant','owner_type':'user','execution_mode':'simulation','project_ids':[self.project]})[0],403)
        for path in ('projects/'+self.project,'projects/'+self.project+'/diff','projects/'+self.project+'/download?file=validator.py','conversations/'+self.conversation+'/events','conversations/'+self.conversation+'/stream','artifacts/'+artifact,'tasks/'+task['id']+'/result'):
            self.assertEqual(outsider.request('/api/'+path)[0],403,path)
        self.assertEqual(viewer.request('/api/projects/'+self.project+'/download?file=validator.py')[0],200)
        for path in ('/.relay-native/profiles/a/auth.json','/workspaces/'+self.project+'/validator.py','/api/projects/'+self.project+'/download?file=.git/config','/api/logs','/api/database/export'):
            self.assertNotEqual(outsider.request(path)[0],200,path)
    def test_three_cookie_jars_pause_handoff_c_and_reconnect_restart(self):
        alex,_,_=self.member('alex');sam,_,_=self.member('sam');jordan,_,_=self.member('jordan')
        workers=[self.worker(label) for label in ('A','B','C')]
        task=self.submit(alex,workers[0],pause_after_turn=True,native_phases=3)
        self.assertIn('validator.py',sam.get('projects/'+self.project)['workspace']['files'])
        paused=sam.post('pause',{'task_id':task['id'],'generation':task['generation']})
        self.assertEqual(paused['state'],'handoff_pending');self.assertFalse(self.server.relay.rows('SELECT * FROM workspace_leases'))
        second=sam.post('switch',{'task_id':task['id'],'generation':paused['generation'],'target_worker_id':workers[1]})
        self.assertEqual(second['state'],'running');self.assertIn('test_validator.py',jordan.get('projects/'+self.project)['workspace']['files'])
        paused=jordan.post('pause',{'task_id':task['id'],'generation':second['generation']})
        final=jordan.post('switch',{'task_id':task['id'],'generation':paused['generation'],'target_worker_id':workers[2]})
        self.assertEqual(final['state'],'completed');before=jordan.get('conversations/'+self.conversation+'/events?limit=100')['events']
        self.assertTrue(any('Ran 5 tests' in e['content'] for e in before))
        detail=sam.get('projects/'+self.project)
        self.assertEqual(len(detail['execution_runs']),3);self.assertEqual(len({r['worker_id'] for r in detail['execution_runs']}),3)
        self.stop();self.start()
        for client in (alex,sam,jordan):
            client.server=self.server
            self.assertEqual(client.get('conversations/'+self.conversation+'/events?limit=100')['events'],before)
            self.assertEqual(client.get('projects/'+self.project)['checkpoints'],detail['checkpoints'])
    def test_resume_same_worker_and_idempotency_after_role_revocation(self):
        alex,user,_=self.member('alex');worker=self.worker();task=self.submit(alex,worker)
        paused=alex.post('pause',{'task_id':task['id'],'generation':task['generation']})
        data={'task_id':task['id'],'generation':paused['generation'],'request_id':'resume-once'}
        done=alex.post('resume',data);self.assertEqual(done['state'],'completed')
        self.assertEqual(alex.post('resume',data)['id'],task['id'])
        self.admin.post('membership',{'project_id':self.project,'user_id':user,'role':'viewer'})
        self.assertEqual(alex.request('/api/resume',data)[0],403)
    def test_authenticated_sse_and_open_stream_revocation(self):
        alex,user,_=self.member('alex');task=self.submit(self.admin,self.worker())
        connection,response=alex.stream(self.conversation)
        self.assertEqual(response.status,200)
        received=[]
        while True:
            line=response.readline();received.append(line)
            if line.startswith(b'data:'):break
        self.admin.post('membership',{'project_id':self.project,'user_id':user,'role':'remove'})
        with self.server.relay.lock:self.server.relay.event(task,'response','AFTER-REVOKE-DO-NOT-LEAK')
        received.append(response.read());connection.close()
        self.assertNotIn(b'AFTER-REVOKE-DO-NOT-LEAK',b''.join(received))
        self.assertEqual(alex.request('/api/conversations/'+self.conversation+'/events')[0],403)
    def test_admin_session_revocation_and_no_auth_values_in_exports(self):
        alex,user,invitation=self.member('alex');session=alex.cookie.split('=',1)[1]
        self.admin.post('auth/revoke-sessions',{'user_id':user})
        self.assertEqual(alex.request('/api/overview')[0],401)
        for result in (self.admin.get('overview'),self.admin.get('projects/'+self.project)):
            serialized=json.dumps(result)
            for value in (session,invitation,PASSWORD):self.assertNotIn(value,serialized)
            self.assertNotIn('password_hash',serialized)
        dump='\n'.join(self.server.relay.db.iterdump())
        self.assertNotIn(session,dump);self.assertNotIn(invitation,dump);self.assertNotIn(PASSWORD,dump)
    def test_arbitrary_objective_reaches_org_adapter_with_authorized_human(self):
        alex,_,_=self.member('alex');objective='Implement a CSV parser with quoted Unicode fields and tests.'
        seen=[]
        class Adapter:
            def health(self):return {'available':True,'connected':False}
            def start(self,task,workspace,context,emit,cancel):
                seen.append(task['objective']);emit('tool_output','MOCK adapter only; no real provider call',{})
                return ExecutionResult('completed','Mocked execution complete',native_thread_id='mock-org-thread',exit_code=0)
            resume=start
        with patch('relay.store.get_adapter',return_value=Adapter()):
            worker=self.worker(mode='organization_api')
            task=alex.post('prompt',{'conversation_id':self.conversation,'worker_id':worker,'prompt':objective})
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and task['id'] in self.server.relay.jobs:time.sleep(.02)
        self.assertEqual(seen,[objective]);self.assertEqual(alex.get('tasks/'+task['id']+'/result')['task']['state'],'completed')
    def test_shared_native_enrollment_disabled_and_no_demo_users_seeded(self):
        self.assertFalse(self.server.relay.rows("SELECT * FROM users WHERE id IN ('alice','bob','admin')"))
        self.assertEqual(self.admin.request('/api/workers/enroll',{'display_name':'Native','owner_type':'user','execution_mode':'native','native_profile':'a'})[0],400)
        self.assertEqual(Client(self.server).request('/api/overview')[0],401)
    def test_idle_expiry_logout_all_and_bootstrap_is_sealed(self):
        alex,user,_=self.member('alex');other=Client(self.server);other.login('alex@relay.invalid')
        alex.post('auth/logout-all',{})
        self.assertEqual(other.request('/api/overview')[0],401)
        alex.login('alex@relay.invalid')
        with self.server.relay.lock:self.server.relay.db.execute('UPDATE sessions SET last_seen=0 WHERE user_id=?',(user,))
        self.assertEqual(alex.request('/api/overview')[0],401)
        with self.assertRaises(ValueError):self.server.identity_provider.bootstrap('second@relay.invalid','Second',PASSWORD)
    def test_secure_cookie_https_proxy_headers_and_missing_origin(self):
        self.server.secure=True;self.server.public_url='https://relay.example.com';self.server.cookie_name='__Host-relay_session'
        client=Client(self.server);headers={'Host':'relay.example.com','X-Forwarded-Proto':'https'}
        status,_,cookie=client.request('/api/login',{'email':'admin@relay.invalid','password':PASSWORD},headers)
        self.assertEqual(status,200)
        for attribute in ('__Host-relay_session=','HttpOnly','SameSite=Strict','Path=/','Secure'):self.assertIn(attribute,cookie)
        self.assertEqual(client.request('/api/overview',headers={'Host':'relay.example.com'})[0],403)
        self.assertEqual(client.request('/api/projects',{'name':'Missing origin'},{**headers,'Origin':''})[0],403)
    def test_websocket_upgrade_does_not_bypass_project_access(self):
        outsider,_,_=self.member('outside',project=False)
        path='/api/conversations/'+self.conversation+'/stream'
        self.assertEqual(outsider.request(path,headers={'Upgrade':'websocket','Connection':'Upgrade'})[0],403)
        self.assertEqual(self.admin.request(path,headers={'Upgrade':'websocket','Connection':'Upgrade'})[0],400)
    def test_existing_account_can_accept_project_invite_without_role_escalation(self):
        member,user,_=self.member('existing',project=False)
        invite=self.admin.post('invitations',{'email':'existing@relay.invalid','project_id':self.project,'org_role':'admin'})
        token=urlparse(invite['invitation_url']).fragment.removeprefix('invite=')
        member.post('invitations/accept',{'email':'existing@relay.invalid','invitation_token':token})
        self.assertEqual(member.get('projects/'+self.project)['conversation']['id'],self.conversation)
        self.assertFalse(self.server.relay.is_admin(user))
    def test_password_hash_and_origin_guards(self):
        encoded=hash_password(PASSWORD);self.assertTrue(verify_password(PASSWORD,encoded));self.assertFalse(verify_password('incorrect password value',encoded));self.assertNotIn(PASSWORD,encoded)
        for value in ('http://192.168.1.10:8000','https://user:password@example.com','https://example.com/path'):
            with self.assertRaises(ValueError):public_origin(value,True)
        self.assertEqual(public_origin('https://relay.example.com'),'https://relay.example.com')
        self.assertNotEqual(csrf_token('one-session'),csrf_token('another-session'))

if __name__=='__main__':unittest.main()
