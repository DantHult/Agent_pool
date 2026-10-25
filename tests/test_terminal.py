"""Typed Git, file-run plans, bounded history and nonblocking readiness."""
import threading
import time
import unittest
from unittest.mock import Mock,patch

from relay.terminal import parse
import test_local_native_dashboard as fixture

CLIENT='terminal-browser-0123456789'


class CommandParserTests(unittest.TestCase):
    def test_quotes_and_supported_git_commands(self):
        self.assertEqual(parse('git commit -m "slight improvements"')['arguments'],['commit','-m','slight improvements'])
        self.assertEqual(parse('git restore --staged "hello world.py"')['arguments'],['unstage','hello world.py'])
        self.assertEqual(parse('git add .','src')['arguments'],['add','src'])
        self.assertEqual(parse('git add nested/file.py','src')['arguments'],['add','src/nested/file.py'])
        for command in ('git status','git diff','git diff --staged','git log','git branch','git branch feature/test','git switch feature/test','git switch -c feature/test'):
            self.assertEqual(parse(command)['kind'],'git')
        self.assertEqual(parse('python3 -c "print(\'hello world\')"')['kind'],'sandbox')

    def test_shell_remote_flags_and_escape_rejected(self):
        for command in ('git push','git push origin main','git -c core.hooksPath=/tmp status','git reset --hard','git clean -fd','git add ../secret','git add /etc/passwd','git add . && git push','sh -c whoami','/usr/bin/python3 hello.py','git commit -m "unclosed'):
            with self.subTest(command=command),self.assertRaises((ValueError,PermissionError)):parse(command)
        with self.assertRaises(ValueError):parse('git status','../')


class TerminalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.LocalNativeDashboardTests();self.fixture.setUp()
        self.client=self.fixture.client;self.relay=self.fixture.server.relay
        self.ctx={'project_id':self.fixture.project,'client_id':CLIENT,'conversation_id':self.fixture.conversation}
    def tearDown(self):self.fixture.tearDown()
    def save(self,path,content):
        self.client.post('ide/prepare',self.ctx)
        return self.client.post('ide/save',{**self.ctx,'path':path,'content':content,'version':None})
    def command(self,text,cwd=''):return self.client.post('ide/terminal',{**self.ctx,'command':text,'cwd':cwd})

    def test_actual_git_add_quoted_commit_history_unstage_and_branch(self):
        self.save('hello.py','print("hello")\n')
        with patch('relay.console.AppServerClient',side_effect=AssertionError('Git must not start a command sandbox')):
            self.assertEqual(self.command('git add .')['exit_code'],0)
            self.assertIn('hello.py',self.command('git diff --staged')['output'])
            self.assertEqual(self.command('git restore --staged hello.py')['exit_code'],0)
            self.assertIn('?? hello.py',self.command('git status')['output'])
            self.command('git add hello.py')
            committed=self.command('git commit -m "slight improvements"')
            self.assertEqual(committed['exit_code'],0)
            self.assertEqual(self.relay.ws.git(self.relay.ws.path(self.fixture.project),'log','-1','--format=%s'),'slight improvements')
            self.assertIn('slight improvements',self.command('git log')['output'])
            self.assertEqual(self.command('git branch terminal-branch')['exit_code'],0)
            self.assertEqual(self.command('git switch terminal-branch')['exit_code'],0)

    def test_directory_staging_excludes_credentials_and_cwd_is_scoped(self):
        self.save('src/code.py','value=42\n')
        workspace=self.relay.ws.path(self.fixture.project);(workspace/'src/.env').write_text('nonsecret-protected-fixture')
        self.command('git add .','src')
        names=self.relay.ws.git(workspace,'diff','--cached','--name-only')
        self.assertIn('src/code.py',names);self.assertNotIn('.env',names)
        outside=self.fixture.root/'outside';outside.mkdir();(workspace/'linked').symlink_to(outside,target_is_directory=True)
        self.assertIn(self.client.request('/api/ide/terminal',{**self.ctx,'command':'git status','cwd':'linked'})[0],(400,403))

    def test_file_plans_validate_runtime_paths_and_quotes(self):
        for path in ('hello.py','hello world.js','src/example.mjs','src/example.cjs'):
            self.save(path,'print("hello")\n' if path.endswith('.py') else 'console.log("hello")\n')
            plan=self.client.post('ide/run-file',{**self.ctx,'path':path})
            self.assertEqual(plan['kind'],'sandbox')
            self.assertEqual(plan['cwd'],'src' if path.startswith('src/') else '')
        self.assertIn("'hello world.js'",self.client.post('ide/run-file',{**self.ctx,'path':'hello world.js'})['command'])
        self.save('page.html','hello')
        (self.relay.ws.path(self.fixture.project)/'linked.py').symlink_to(self.fixture.root/'outside.py')
        for path in ('../escape.py','.git/config.py','missing.py','page.html','linked.py'):
            self.assertIn(self.client.request('/api/ide/run-file',{**self.ctx,'path':path})[0],(400,403))

    def test_auth_csrf_push_errors_and_agent_writer_exclusion(self):
        self.assertEqual(self.client.request('/api/ide/terminal',{**self.ctx,'command':'git status'},{'X-Relay-CSRF':''})[0],403)
        self.assertEqual(self.client.request('/api/ide/terminal',{**self.ctx,'command':'git status'},{'Forwarded':'for=127.0.0.1'})[0],403)
        rejected=self.client.request('/api/ide/terminal',{**self.ctx,'command':'git push'})
        self.assertEqual(rejected[0],400);self.assertIn('unavailable',rejected[1]['error'])
        from relay.native import close_native_sessions,profile_paths
        close_native_sessions();home,_=profile_paths('a');(home/'scenario.json').write_text('{"delay":3}')
        worker=self.fixture.enroll('a');task=self.fixture.submit(worker)
        self.assertEqual(self.client.request('/api/ide/terminal',{**self.ctx,'command':'git add .'})[0],409)
        self.assertEqual(self.command('git status')['exit_code'],0)
        self.client.post('cancel',{'task_id':task['id']});self.fixture.wait(task,'cancelled')

    def test_light_startup_never_probes_providers_or_reads_full_workspace(self):
        self.fixture.enroll('a')
        with patch('relay.native.NativeCodexAdapter.health',side_effect=AssertionError('No provider probe on editor startup')),patch.object(self.relay.ws,'inspect',side_effect=AssertionError('No workspace diff on editor startup')):
            self.assertTrue(self.client.get('overview?light=1')['features']['recent_history'])
            self.assertTrue(self.client.get('projects/'+self.fixture.project+'?light=1')['light'])

    def test_streamed_handoff_events_include_current_generation(self):
        a=self.fixture.enroll('a');b=self.fixture.enroll('b')
        task=self.fixture.submit(a);paused=self.fixture.wait(task,'handoff_pending')
        self.client.post('switch',{'task_id':task['id'],'generation':paused['generation'],'target_worker_id':b})
        done=self.fixture.wait(task,'completed')
        events=self.relay.rows("SELECT metadata FROM events WHERE task_id=? AND worker_id=? AND event_type='assignment'",(task['id'],b))
        import json
        self.assertEqual(json.loads(events[-1]['metadata'])['generation'],done['generation'])

    def test_recent_history_paginates_all_events_without_mutating_records(self):
        with self.relay.lock:
            task={'id':None,'conversation_id':self.fixture.conversation,'execution_mode':'native'}
            for index in range(350):self.relay.event(task,'agent_delta',str(index))
            before=self.relay.rows('SELECT event_id,sequence,content FROM events ORDER BY sequence')
        page=self.client.get('conversations/'+self.fixture.conversation+'/events?recent=1&limit=100')
        self.assertEqual(len(page['events']),100);self.assertTrue(page['has_older'])
        all_events=list(page['events'])
        while page['has_older']:
            page=self.client.get('conversations/'+self.fixture.conversation+'/events?before='+str(page['before'])+'&limit=100')
            all_events=page['events']+all_events
        self.assertEqual([event['event_id'] for event in all_events],[event['event_id'] for event in before])
        self.assertEqual(before,self.relay.rows('SELECT event_id,sequence,content FROM events ORDER BY sequence'))
        plan=self.relay.db.execute('EXPLAIN QUERY PLAN SELECT * FROM events WHERE conversation_id=? AND sequence<? ORDER BY sequence DESC LIMIT 100',(self.fixture.conversation,100)).fetchall()
        self.assertIn('INDEX',str([tuple(row) for row in plan]))

    def test_cached_readiness_does_not_block_and_dispatch_revalidates(self):
        worker=self.fixture.enroll('a');row=self.relay.one('SELECT * FROM workers WHERE id=?',(worker,))
        gate=threading.Event();adapter=Mock()
        def health():gate.wait(2);return {'available':False}
        adapter.health.side_effect=health
        self.relay.health_cache.clear()
        try:
            with patch.object(self.relay,'adapter',return_value=adapter):
                started=time.monotonic();health=self.relay.worker_health(row,cached=True)
                self.assertLess(time.monotonic()-started,.2);self.assertEqual(health['status'],'checking')
                gate.set()
        finally:gate.set()
        with patch.object(self.relay,'adapter') as factory:
            factory.return_value.health.return_value={'available':False,'reason':'Authentication revoked'}
            self.assertFalse(self.relay.eligibility(row,self.fixture.project,self.fixture.owner)[0])
