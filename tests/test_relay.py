import concurrent.futures
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from relay.store import Relay
from relay.server import make_server

PROMPT='Add a password-strength validator and tests to this sample project.'
class RelayAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.r=Relay(self.tmp.name)
        self.cid=self.r.detail('alice','demo')['conversation']['id']
    def tearDown(self): self.r.db.close(); self.tmp.cleanup()
    def start(self): return self.r.mutate('alice','prompt',{'conversation_id':self.cid,'prompt':PROMPT})
    def switch(self,t,**kwargs): return self.r.mutate('alice','limit',{'task_id':t['id'],'generation':t['generation'],**kwargs})
    def test_01_prompt_survives_reload_and_real_process_restart(self):
        self.start(); before=self.r.events('alice',self.cid,limit=100)['events']; self.r.db.close()
        script="from relay.store import Relay; import sys,json; r=Relay(sys.argv[1]); print(json.dumps(r.events('alice',sys.argv[2],limit=100))); r.db.close()"
        result=subprocess.run([sys.executable,'-c',script,self.tmp.name,self.cid],capture_output=True,text=True,check=True)
        events=json.loads(result.stdout)['events']; self.assertEqual(events[:len(before)],before); self.assertEqual(events[0]['content'],PROMPT)
        self.r=Relay(self.tmp.name); self.assertEqual(self.r.detail('alice','demo')['conversation']['id'],self.cid)
    def test_02_worker_a_records_real_file_and_event(self):
        t=self.start(); self.assertEqual(t['worker_id'],'codexA'); self.assertTrue((Path(self.tmp.name)/'workspaces/demo/validator.py').exists())
        self.assertTrue(any(e['event_type']=='tool_output' and e['worker_id']=='codexA' for e in self.r.events('alice',self.cid)['events']))
    def test_03_04_05_handoff_checkpoint_context_and_same_conversation(self):
        t=self.start(); result=self.switch(t); self.assertEqual(result['worker_id'],'codexB'); self.assertEqual(result['state'],'completed')
        d=self.r.detail('alice','demo'); self.assertEqual(d['conversation']['id'],self.cid)
        handoff=d['handoffs'][0]; self.assertEqual(handoff['reason'],'Simulated usage limit'); self.assertEqual(len(handoff['checkpoint']),40)
        md=d['workspace']['files']['.relay/HANDOFF.md']; self.assertIn('source_range',md)
        events=self.r.events('alice',self.cid,limit=100)['events']; b=next(e for e in events if e['worker_id']=='codexB' and e['event_type']=='response')
        context=json.loads(b['metadata'])['context_received']; self.assertEqual(context['objective'],PROMPT); self.assertEqual(context['conversation_id'],self.cid); self.assertTrue(context['todos']); self.assertIn('tests',context); self.assertIn('validator.py',context['files_changed'])
        output=next(e for e in events if e['worker_id']=='codexB' and e['event_type']=='tool_output'); self.assertIn('Ran 5 tests',output['content']); self.assertIn('OK',output['content'])
        commit=self.r.ws.git(self.r.ws.path('demo'),'show',handoff['checkpoint']+':validator.py'); self.assertIn('password_strength',commit)
    def test_06_append_only_history_versioned_context_and_pagination(self):
        t=self.start(); before=self.r.events('alice',self.cid,limit=100)['events']; self.switch(t)
        after=self.r.events('alice',self.cid,limit=100)['events']; self.assertEqual(after[:len(before)],before)
        d=self.r.detail('alice','demo'); self.assertGreaterEqual(len(d['contexts']),3); self.assertLess(len(d['contexts'][0]['recent_turns']),len(after))
        a=self.r.events('alice',self.cid,0,3); b=self.r.events('alice',self.cid,a['next_after'],3); self.assertEqual(b['events'][0]['sequence'],4)
        with self.assertRaises(Exception): self.r.db.execute('DELETE FROM events')
    def test_07_competing_switches_single_assignment_and_idempotency(self):
        t=self.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _: self.switch(t),range(2)))
        self.assertEqual(results[0]['id'],results[1]['id'])
        self.assertEqual(len(self.r.rows('SELECT * FROM handoffs')),1); self.assertEqual(len(self.r.rows('SELECT * FROM assignments')),2)
        self.assertEqual(len(self.r.rows("SELECT * FROM events WHERE event_type='tool_invocation' AND worker_id='codexB'")),1)
    def test_08_project_authorization_all_paths(self):
        private=self.r.detail('bob','private')['conversation']['id']
        for callback in [lambda:self.r.detail('alice','private'),lambda:self.r.events('alice',private),lambda:self.r.mutate('alice','prompt',{'conversation_id':private,'prompt':PROMPT})]:
            with self.assertRaises(PermissionError):callback()
        t=self.r.mutate('bob','prompt',{'conversation_id':private,'prompt':PROMPT})
        with self.assertRaises(PermissionError):self.r.mutate('alice','switch',{'task_id':t['id'],'generation':0})
    def test_09_unavailable_worker_preserves_pending_and_can_resume(self):
        t=self.start(); self.r.mutate('bob','availability',{'worker_id':'codexB','state':'offline'}); result=self.switch(t)
        self.assertEqual(result['state'],'handoff_pending'); self.assertEqual(len(self.r.rows('SELECT * FROM assignments')),1)
        self.r.mutate('bob','availability',{'worker_id':'codexB','state':'available'}); result=self.switch(result); self.assertEqual(result['state'],'completed')
    def test_10_no_credentials_and_explicit_simulation(self):
        self.assertFalse(self.r.overview('alice')['execution']['connected']); self.assertEqual(len(self.r.overview('alice')['workers']),2)
    def test_permissions_availability_cancel_and_request_replay(self):
        with self.assertRaises(PermissionError):self.r.mutate('alice','availability',{'worker_id':'codexB','state':'offline'})
        t=self.start()
        with self.assertRaises(ValueError):self.start()
        args={'task_id':t['id'],'generation':0,'request_id':'unique'}; self.r.mutate('alice','limit',args); self.r.mutate('alice','limit',args)
        self.assertEqual(len(self.r.rows('SELECT * FROM handoffs')),1)
    def test_secret_redaction_and_reject_arbitrary_tasks(self):
        t=self.r.mutate('alice','prompt',{'conversation_id':self.cid,'prompt':PROMPT+' api_key=sk-abcdefghijklmnop'})
        self.assertNotIn('sk-abcdefghijklmnop',str(self.r.events('alice',self.cid)))
        self.r.mutate('alice','cancel',{'task_id':t['id']})
        with self.assertRaises(ValueError):self.r.mutate('alice','prompt',{'conversation_id':self.cid,'prompt':'run rm -rf /'})

class HTTPAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.server=make_server(self.tmp.name,0); self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start(); self.cookie=None
    def tearDown(self): self.server.shutdown(); self.server.server_close(); self.server.relay.db.close(); self.tmp.cleanup()
    def request(self,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port); hs={'Content-Type':'application/json'}
        if self.cookie: hs['Cookie']=self.cookie
        hs.update(headers or {}); c.request('POST' if body else 'GET',path,json.dumps(body) if body else None,hs); r=c.getresponse(); content=r.read(); cookie=r.getheader('Set-Cookie'); status=r.status;c.close(); return status,content,cookie
    def test_http_auth_routes_and_cross_origin(self):
        self.assertEqual(self.request('/api/overview')[0],403)
        status,_,cookie=self.request('/api/login',{'user_id':'alice'});self.assertEqual(status,200);self.cookie=cookie.split(';')[0]
        status,content,_=self.request('/api/overview'); self.assertEqual(status,200); self.assertIn('Password toolkit',content.decode())
        self.assertEqual(self.request('/api/projects/private')[0],403)
        self.assertEqual(self.request('/api/projects',{'name':'unsafe'},{'Origin':'https://evil.test'})[0],403)
        self.assertEqual(self.request('/')[0],200)
        cid=self.server.relay.detail('alice','demo')['conversation']['id']; status,content,_=self.request('/api/prompt',{'conversation_id':cid,'prompt':PROMPT}); self.assertEqual(status,200)
        t=json.loads(content);status,content,_=self.request('/api/limit',{'task_id':t['id'],'generation':0});self.assertEqual(status,200);self.assertEqual(json.loads(content)['state'],'completed')
        self.assertEqual(self.request('/api/conversations/'+cid+'/events?after=0')[0],200)
if __name__=='__main__':unittest.main()
